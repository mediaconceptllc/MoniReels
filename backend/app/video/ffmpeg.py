"""FFmpeg/FFprobe discovery and subprocess execution with progress + cancel.

On a container ffmpeg is installed by the image and lives on PATH, so the
desktop build's three-tier search (FFMPEG_PATH -> backend/bin/ -> PATH) and
its PyInstaller `sys.frozen` branch are gone: both existed to find a binary
the installer had placed next to a Windows .exe.

FFMPEG_PATH is still honoured, for a local checkout with a custom build.

Discovery never raises — callers check `FfmpegBinaries.available` — so an
image built without ffmpeg produces a clear 503 on the routes that need it
rather than a crash at import time.
"""
from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.timecode import ffmpeg_out_time_to_seconds

logger = get_logger(__name__)


@dataclass(frozen=True)
class FfmpegBinaries:
    ffmpeg: Path | None
    ffprobe: Path | None

    @property
    def available(self) -> bool:
        return self.ffmpeg is not None and self.ffprobe is not None


def _find_one(exe_name: str) -> Path | None:
    configured = getattr(get_settings(), "ffmpeg_path", "")
    if configured:
        path = Path(configured)
        candidate = path if path.is_file() else path / exe_name
        if candidate.is_file():
            return candidate

    found = shutil.which(exe_name)
    return Path(found) if found else None


def discover_ffmpeg() -> FfmpegBinaries:
    ffmpeg = _find_one("ffmpeg")
    ffprobe = _find_one("ffprobe")
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
    # e.g. "ffmpeg version 6.1.1-static ..."
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
