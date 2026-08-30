"""Loudness normalization, run before VAD so quiet speech is not missed as
silence. A segment VAD never detects is never transcribed at all — too-quiet
audio is a SILENT failure, not merely a quality one, because the words simply
go missing from the transcript with nothing to indicate they were there.

Uses ffmpeg-normalize (https://github.com/slhck/ffmpeg-normalize), which
wraps ffmpeg's own two-pass EBU R128 `loudnorm` filter - not a separate
audio engine, just the same normalization ffmpeg already does, with the
gain-measurement bookkeeping handled for us.

Only actually reprocesses audio that's meaningfully quieter than the
target; already-loud-enough audio is left completely untouched rather than
being reprocessed for no reason (measured once via ffmpeg's own loudnorm
analysis pass, using the app's own discovered ffmpeg binary - see
app.video.ffmpeg - not a second one ffmpeg-normalize would otherwise find
on its own via $FFMPEG_PATH/$PATH).
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)

# -16 LUFS integrated: a common target for single-speaker/podcast-style
# voice content - louder and more consistent than the -23 LUFS broadcast
# default most loudness tools (including ffmpeg-normalize itself) ship
# with, which is tuned for mixed TV audio, not a lone speaker.
TARGET_LUFS = -16.0

# Only normalize when measured at least this far below target - a small
# margin avoids reprocessing audio that's already close enough, and avoids
# the filter's own measurement noise on very short/quiet stretches from
# triggering a correction that isn't really needed.
TOO_QUIET_MARGIN_DB = 3.0

_LOUDNORM_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)


class NormalizeError(Exception):
    pass


async def _measure_integrated_loudness(ffmpeg_path: Path, wav_path: Path) -> float:
    """Runs ffmpeg's loudnorm filter in analysis-only mode (no output file)
    and returns the measured integrated loudness in LUFS, parsed from the
    JSON block loudnorm prints to stderr.
    """
    args = [
        str(ffmpeg_path), "-hide_banner", "-nostats",
        "-i", str(wav_path),
        "-af", f"loudnorm=I={TARGET_LUFS}:TP=-2:LRA=7:print_format=json",
        "-f", "null", "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    text = stderr.decode(errors="replace")
    match = _LOUDNORM_JSON_RE.search(text)
    if not match:
        raise NormalizeError(f"Could not parse loudnorm measurement output: {text[-500:]}")
    try:
        return float(json.loads(match.group(0))["input_i"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise NormalizeError(f"Unexpected loudnorm measurement JSON: {match.group(0)[:300]}") from e


def _normalize_sync(ffmpeg_path: Path, in_path: Path, out_path: Path, target_level: float) -> None:
    import os

    from ffmpeg_normalize import FFmpegNormalize

    # ffmpeg-normalize finds its own ffmpeg via $FFMPEG_PATH/$PATH, not
    # through this app's own discovery (app.video.ffmpeg) - pointing it at
    # the exact binary we already resolved keeps both in sync instead of
    # risking a second, possibly-missing ffmpeg on PATH.
    os.environ["FFMPEG_PATH"] = str(ffmpeg_path)

    normalizer = FFmpegNormalize(
        normalization_type="ebu",
        target_level=target_level,
        audio_codec="pcm_s16le",
        dynamic=False,  # linear gain - one consistent speaker, not mixed program audio
        progress=False,  # no tqdm progress bar (see app/audio/separation.py's headless-console note)
    )
    normalizer.add_media_file(str(in_path), str(out_path))
    normalizer.run_normalization()


async def normalize_if_too_quiet(
    ffmpeg_path: Path,
    wav_path: Path,
    out_path: Path,
    target_level: float = TARGET_LUFS,
    margin_db: float = TOO_QUIET_MARGIN_DB,
) -> Path:
    """Boosts `wav_path` up to `target_level` LUFS and writes it to
    `out_path`, but only when measured at least `margin_db` below that
    target. Returns `wav_path` unchanged (never touching it) whenever
    normalization wasn't needed or failed - this is a quality enhancement,
    not something worth failing the whole transcription pipeline over.
    """
    try:
        measured = await _measure_integrated_loudness(ffmpeg_path, wav_path)
    except Exception:
        logger.exception("Loudness measurement failed for %s; skipping normalization", wav_path)
        return wav_path

    if measured >= target_level - margin_db:
        logger.info(
            "%s measured %.1f LUFS, already at/above target %.1f - not normalizing",
            wav_path, measured, target_level,
        )
        return wav_path

    logger.info(
        "%s measured %.1f LUFS, more than %.0fdB below target %.1f - normalizing",
        wav_path, measured, margin_db, target_level,
    )
    try:
        await asyncio.to_thread(_normalize_sync, ffmpeg_path, wav_path, out_path, target_level)
    except Exception:
        logger.exception("Loudness normalization failed for %s; using unnormalized audio", wav_path)
        return wav_path
    return out_path
