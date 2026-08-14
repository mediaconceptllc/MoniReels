"""FFmpeg/FFprobe binary discovery and subprocess execution with progress + cancel.

Discovery order: FFMPEG_PATH env -> backend/bin/ -> system PATH.
Never raises on "not found" at import/startup time — callers check `FfmpegBinaries.available`.
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.timecode import ffmpeg_out_time_to_seconds

logger = get_logger(__name__)


def _local_bin_dir() -> Path:
    """Same sys.frozen split as app.utils.env_file.env_file_path: once
    PyInstaller freezes this module, __file__ resolves under the bundle's
    _internal/ tree, not next to the exe - parents[2] from there lands on
    _internal/, not the packaged backend/bin/ the installer actually places
    ffmpeg.exe/ffprobe.exe in. Use the exe's own directory when frozen.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "bin"
    # backend/app/video/ffmpeg.py -> backend/bin
    return Path(__file__).resolve().parents[2] / "bin"


LOCAL_BIN_DIR = _local_bin_dir()


@dataclass(frozen=True)
class FfmpegBinaries:
    ffmpeg: Path | None
    ffprobe: Path | None

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None


def _find_one(exe_name: str) -> Path | None:
    settings = get_settings()

    # 1. FFMPEG_PATH env: may point at a directory or directly at the exe.
    if settings.ffmpeg_path:
        configured = Path(settings.ffmpeg_path)
        candidates = [configured] if configured.suffix.lower() == ".exe" else [configured / exe_name]
        for c in candidates:
            if c.is_file():
                return c

    # 2. backend/bin/
    local = LOCAL_BIN_DIR / exe_name
    if local.is_file():
        return local

    # 3. system PATH
    found = shutil.which(exe_name)
    if found:
        return Path(found)

    return None


def discover_ffmpeg() -> FfmpegBinaries:
    ffmpeg = _find_one("ffmpeg.exe")
    ffprobe = _find_one("ffprobe.exe")
    if not ffmpeg or not ffprobe:
        logger.warning("FFmpeg binaries not fully found: ffmpeg=%s ffprobe=%s", ffmpeg, ffprobe)
    return FfmpegBinaries(ffmpeg=ffmpeg, ffprobe=ffprobe)


async def get_ffmpeg_version(ffmpeg_path: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        str(ffmpeg_path), "-version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    first_line = out.decode("utf-8", errors="replace").splitlines()[0] if out else ""
    # e.g. "ffmpeg version 6.1.1-full_build-www.gyan.dev Copyright ..."
    parts = first_line.split()
    return parts[2] if len(parts) >= 3 else first_line


class FfmpegCancelled(Exception):
    pass


class FfmpegFailed(Exception):
    def __init__(self, returncode: int, stderr_tail: str):
        super().__init__(f"ffmpeg exited with code {returncode}: {stderr_tail}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class FfmpegRun:
    """A cancelable FFmpeg invocation that reports 0..1 progress via callback."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._cancel_requested = False

    async def run(
        self,
        ffmpeg_path: Path,
        args: list[str],
        *,
        expected_duration_sec: float,
        cwd: Path | None = None,
        on_progress: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Runs `ffmpeg {args}` with machine-readable progress on stdout.

        `args` must NOT include -progress/-nostdin/-loglevel/-stats_period;
        those are added here so every call gets consistent, parseable output.
        """
        full_args = [
            "-nostdin", "-hide_banner", "-loglevel", "error",
            "-y",
            *args,
            "-progress", "pipe:1", "-stats_period", "0.5",
        ]
        logger.debug("ffmpeg command: %s %s", ffmpeg_path, " ".join(full_args))

        self._process = await asyncio.create_subprocess_exec(
            str(ffmpeg_path), *full_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )

        stderr_tail: list[str] = []

        async def _read_stderr() -> None:
            assert self._process is not None and self._process.stderr is not None
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    stderr_tail.append(text)
                    if len(stderr_tail) > 50:
                        stderr_tail.pop(0)

        stderr_task = asyncio.create_task(_read_stderr())

        assert self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("out_time="):
                    if on_progress and expected_duration_sec > 0:
                        elapsed = ffmpeg_out_time_to_seconds(text.split("=", 1)[1])
                        progress = min(1.0, max(0.0, elapsed / expected_duration_sec))
                        await on_progress(progress)
                elif text == "progress=end":
                    if on_progress:
                        await on_progress(1.0)
        finally:
            await stderr_task
            returncode = await self._process.wait()

        if self._cancel_requested:
            raise FfmpegCancelled()
        if returncode != 0:
            raise FfmpegFailed(returncode, "\n".join(stderr_tail[-20:]))

    async def cancel(self) -> None:
        self._cancel_requested = True
        if self._process is None or self._process.returncode is not None:
            return
        try:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=3)
        except (ProcessLookupError, TimeoutError):
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
