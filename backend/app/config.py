"""Application configuration, loaded from the environment.

Cloud deployment (Railway) — every value arrives as an environment variable
set on the service, never from a file this process can write. The desktop
build's `.env`-writing settings endpoint is gone on purpose: an HTTP handler
that rewrites the process's own credentials is harmless on one Windows
machine and a full credential-takeover on a public URL.

`DATA_DIR` is deliberately NOT a persistent location any more. Railway's
container filesystem is ephemeral and per-instance; every artifact that has
to survive a restart goes to R2 (see app.r2), and everything under
`resolved_work_dir` is scratch space one job is free to delete.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_R2_ENDPOINT_SUFFIX = ".r2.cloudflarestorage.com"
_R2_ACCOUNT_RE = re.compile(r"[0-9a-f]{32}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- process -----------------------------------------------------
    # Railway injects PORT. Binding 0.0.0.0 (not 127.0.0.1 as the desktop
    # build did) is required or the platform's health check never connects.
    port: int = 8000
    host: str = "0.0.0.0"
    ffmpeg_path: str = ""  # optional override for a local checkout
    log_level: str = "INFO"
    environment: str = "production"

    # Comma-separated exact origins. "*" is refused in production by
    # main.py: credentials + wildcard is rejected by every browser anyway,
    # and the desktop build's blanket "*" was only ever safe because the
    # server was bound to loopback.
    cors_origins: str = ""

    # ---- data --------------------------------------------------------
    database_url: str = ""
    jwt_secret: str = ""
    jwt_hours: int = 12
    # Creates the first admin at startup ONLY when the users table is
    # empty. Without it a fresh deployment has no way in at all; with it
    # unconditional, rotating the variable would silently reset an
    # existing account.
    bootstrap_admin_username: str = ""
    bootstrap_admin_password: str = ""

    # ---- object storage (Cloudflare R2) ------------------------------
    # Media NEVER passes through this API: the browser PUTs to a presigned
    # URL and reads back through a presigned GET. A 13GB source video going
    # through a Railway dyno is both a timeout and a bandwidth bill.
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_endpoint: str = ""  # defaults to https://{account}.r2.cloudflarestorage.com
    r2_presign_ttl_s: int = 3600
    # Aborts multipart uploads older than this. A worker killed mid-upload
    # (OOM, redeploy) leaves parts that are billed but appear in no ordinary
    # listing, so they can only be found by age, never by prefix.
    r2_mpu_abort_h: int = 24

    # ---- LLM: OpenRouter is the only provider ------------------------
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # OpenRouter attributes usage to these two headers on its public
    # leaderboards; both are optional and neither affects billing.
    openrouter_app_url: str = ""
    openrouter_app_title: str = "MoniReels"

    # ---- STT: duudlaga.dev -------------------------------------------
    duudlaga_base_url: str = "https://api.duudlaga.dev/v1"
    duudlaga_api_key: str = ""
    duudlaga_model: str = ""
    # Ceiling for pause-based chunking. A chunk is only force-cut once a
    # pause-free stretch reaches this; real pauses split well below it.
    duudlaga_max_audio_sec: int = 60

    # ---- TTS: ElevenLabs ---------------------------------------------
    # Stored, not yet used. Nothing in the pipeline synthesises speech; the
    # key exists so it can be entered and rotated in the same place as the
    # others rather than being remembered somewhere else until TTS lands.
    elevenlabs_api_key: str = ""

    # ---- audio pipeline ----------------------------------------------
    # Demucs is OFF by default. On a shared container torch does not read
    # the cgroup CPU limit and spawns one thread per *host* core, which
    # throttles the whole container — including the uvicorn that answers
    # the platform health check, which then restarts the service mid-job.
    # Turn it on only on a worker service, never on the API service.
    enable_separation: bool = False
    demucs_model: str = "htdemucs"
    # Hard ceiling for any torch/ffmpeg thread pool. 0 => derive from the
    # container's own cgroup quota (see heavy_threads).
    torch_threads: int = 0
    cpu_reserve: int = 1

    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 250
    vad_min_silence_ms: int = 100
    vad_speech_pad_ms: int = 100

    # ---- worker ------------------------------------------------------
    # Jobs run concurrently, but an ffmpeg render or a Demucs pass saturates
    # the box on its own — see jobs.LANES for the per-resource caps that
    # actually bound this.
    worker_concurrency: int = 2
    # A job whose worker stopped heartbeating for this long is presumed
    # dead and returned to the queue. Must exceed the heartbeat interval by
    # a wide margin or a merely busy worker loses its own job.
    job_stale_sec: int = 300
    job_keep_days: int = 30
    work_dir: str = "/tmp/monireels"
    # Refuse to start a disk-heavy job below this much free space rather
    # than failing halfway through with [Errno 28].
    work_free_min_mb: int = 2048

    @property
    def resolved_work_dir(self) -> Path:
        return Path(self.work_dir)

    @property
    def r2_account(self) -> str:
        """The account id, however the dashboard happened to show it.

        Cloudflare presents the id only as the first label of the S3
        endpoint, so pasting the whole URL — or the bare hostname — is the
        obvious mistake, and both build a syntactically plausible endpoint
        that only fails deep inside botocore.
        """
        raw = self.r2_account_id.strip()
        if "//" in raw:
            raw = raw.split("//", 1)[1]
        raw = raw.split("/", 1)[0]
        if raw.lower().endswith(_R2_ENDPOINT_SUFFIX):
            raw = raw[: -len(_R2_ENDPOINT_SUFFIX)]
        return raw.lower()

    @property
    def resolved_r2_endpoint(self) -> str:
        if self.r2_endpoint:
            return self.r2_endpoint.rstrip("/")
        return f"https://{self.r2_account}{_R2_ENDPOINT_SUFFIX}"

    @property
    def r2_config_error(self) -> str | None:
        """Why R2 is unusable, in words an operator can act on.

        Checked here rather than at the first presign because an
        unrecognisable account id builds a client that raises `ValueError:
        Invalid endpoint: https://<value>...` — a 500 with no hint of the
        cause, and, when the value pasted was a token, the credential
        itself printed into the logs. Nothing below ever echoes a value.
        """
        missing = [
            name
            for name, value in (
                ("R2_ACCESS_KEY_ID", self.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", self.r2_secret_access_key),
                ("R2_BUCKET", self.r2_bucket),
            )
            if not value
        ]
        if missing:
            return f"{' / '.join(missing)} is not set"
        if self.r2_endpoint:
            return None
        if not self.r2_account:
            return "R2_ACCOUNT_ID is not set"
        if not _R2_ACCOUNT_RE.fullmatch(self.r2_account):
            return (
                "R2_ACCOUNT_ID is not a Cloudflare account id (32 hexadecimal "
                "characters). It is the first label of the S3 endpoint — the part "
                "between https:// and .r2.cloudflarestorage.com — not the API "
                "token value and not the access key id."
            )
        return None

    @property
    def r2_enabled(self) -> bool:
        return self.r2_config_error is None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def heavy_threads() -> int:
    """Thread ceiling for torch/ffmpeg that respects the CONTAINER's quota.

    `os.cpu_count()` reports the host's cores, not the share this container
    was actually granted. torch reads the former and happily spawns past the
    cgroup limit; the kernel's answer is to throttle every thread in the
    container, uvicorn included, so the platform health check times out and
    the service is restarted — killing whatever job caused it. Reading the
    real quota out of the cgroup is the only way to size the pool correctly.

    `cpu_reserve` keeps a core free for the web process when a worker shares
    a box with it.
    """
    settings = get_settings()
    if settings.torch_threads > 0:
        return settings.torch_threads

    quota = _cgroup_cpu_quota()
    if quota is None:
        quota = os.cpu_count() or 2
    return max(1, quota - settings.cpu_reserve)


def _cgroup_cpu_quota() -> int | None:
    """Whole cores this container may use, or None when not in a cgroup."""
    # cgroup v2: "max 100000" (unlimited) or "200000 100000" (2 cores)
    v2 = Path("/sys/fs/cgroup/cpu.max")
    if v2.is_file():
        try:
            quota_s, period_s = v2.read_text().split()
            if quota_s != "max":
                period = int(period_s)
                if period > 0:
                    return max(1, int(int(quota_s) / period))
        except (ValueError, OSError):
            pass

    # cgroup v1: two files, quota of -1 means unlimited
    v1_quota = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    v1_period = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if v1_quota.is_file() and v1_period.is_file():
        try:
            quota = int(v1_quota.read_text().strip())
            period = int(v1_period.read_text().strip())
            if quota > 0 and period > 0:
                return max(1, int(quota / period))
        except (ValueError, OSError):
            pass

    return None
