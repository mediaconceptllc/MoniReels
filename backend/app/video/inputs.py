"""How ffmpeg and ffprobe open a file that came from outside (audit SEC-5).

Left to itself, ffmpeg picks a demuxer by sniffing the CONTENT, not the
name, and a demuxer may open further files or URLs of its own. An upload
called `source.mp4` that is really a text file reading

    ffconcat version 1.0
    file secret.ts

is opened by the concat demuxer, which reads `secret.ts` from the same
directory — MEASURED on this code: ffprobe reported the sibling file's
streams for an "mp4" that held none. HLS playlists (which fetch segment
URLs) and the other playlist-style formats work the same way; ffmpeg's
history of format-confusion bugs is mostly reads like these, local and
over the network.

So every input that a person supplied is opened with two whitelists: the
`file` protocol only (no http, no data:, no subfile, no concat: protocol),
and only the containers that input is allowed to be. A playlist that
names nothing outside is refused just the same — no upload needs one.

Files the pipeline made itself (normalized clips, concat lists, WAVs, the
dub bed) are opened without these: they are ours, and some of them need
exactly the formats refused here (the join is a concat list).
"""
from __future__ import annotations

from pathlib import Path

# The containers an upload may be (schemas.UPLOAD_EXTENSIONS: mp4, mov, m4v,
# mkv, webm, avi) — and a brand intro or outro (brand.CLIP_CONTENT_TYPES).
# Matched against each demuxer's own names: "mov,mp4,m4a,3gp,3g2,mj2" and
# "matroska,webm" are one demuxer each.
VIDEO_FORMATS = "mov,mp4,m4a,3gp,matroska,webm,avi"
# A brand logo (brand.LOGO_CONTENT_TYPES: png, webp, jpeg). `image2` is how
# ffmpeg opens a single image by its extension; the *_pipe demuxers are how
# it opens one by its content.
IMAGE_FORMATS = "image2,png_pipe,jpeg_pipe,webp_pipe"
# A speech clip from the TTS provider (mp3) or one cached from it.
SPEECH_FORMATS = "mp3,wav"


def guard(formats: str = VIDEO_FORMATS) -> list[str]:
    """The input options that confine the next input to local `formats`.

    For ffprobe, in front of the path; for ffmpeg, in front of its `-i`
    (input options apply to the input that follows them).
    """
    return ["-protocol_whitelist", "file", "-format_whitelist", formats]


def untrusted(path: Path | str, formats: str = VIDEO_FORMATS) -> list[str]:
    """`-i path` for ffmpeg, confined (see guard)."""
    return [*guard(formats), "-i", str(path)]
