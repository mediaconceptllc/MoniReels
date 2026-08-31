"""Request/response schemas and, in one place, every input limit.

Limits are matched deliberately to the database column widths. Without them
Postgres rejects an oversized value at the driver level and FastAPI turns
that into a 500, when the honest answer is a 422 naming the field.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

# Column-width-matched ceilings.
ID_MAX = 32
NAME_MAX = 200
KEY_MAX = 512
TITLE_MAX = 300
PASSWORD_MIN = 8
PASSWORD_MAX = 200
# Long enough for any provider key in use; short enough that the field
# cannot be used to park arbitrary data in the settings table.
SECRET_MAX = 400
MODEL_MAX = 120

# A username reaches R2 object keys. A slash forges a path, a space breaks
# the signature, so the character set is constrained at creation time.
# Deliberately NOT enforced on login: an account that predates the rule must
# still be able to sign in (and be renamed), not be locked out by it.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")

Username = Annotated[str, StringConstraints(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
Password = Annotated[str, StringConstraints(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)]

# Anything a browser can realistically upload as a source video.
UPLOAD_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"})
UPLOAD_MAX_BYTES = 20 * 1024**3


def valid_username(name: str) -> bool:
    return bool(_USERNAME_RE.match(name))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class LoginIn(BaseModel):
    # No pattern here on purpose — see the note on _USERNAME_RE.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)


class TokenOut(BaseModel):
    token: str
    username: str
    role: str
    expires_in_s: int


class MeOut(BaseModel):
    id: str
    username: str
    role: str


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX)
    new_password: Password


class CreateUserIn(BaseModel):
    username: Username
    password: Password
    role: Literal["admin", "editor"] = "editor"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class CreateProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    filename: str = Field(min_length=1, max_length=NAME_MAX)
    size_bytes: int = Field(ge=1, le=UPLOAD_MAX_BYTES)

    @field_validator("filename")
    @classmethod
    def _known_extension(cls, v: str) -> str:
        suffix = ("." + v.rsplit(".", 1)[-1]).lower() if "." in v else ""
        if suffix not in UPLOAD_EXTENSIONS:
            allowed = ", ".join(sorted(UPLOAD_EXTENSIONS))
            raise ValueError(f"Unsupported video format {suffix!r}. Use one of: {allowed}")
        return v


class CreateProjectOut(BaseModel):
    project_id: str
    # The browser PUTs the file here directly. Bytes never pass through the
    # API — see app.r2.
    upload_url: str
    upload_key: str
    upload_expires_in_s: int


class RenameProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)


class UploadCompleteOut(BaseModel):
    project_id: str
    job_id: str


class ExportSettingsIn(BaseModel):
    orientation: Literal["portrait", "landscape"] | None = None
    portrait_fill: Literal["blur", "crop", "pad"] | None = None
    crf: int | None = Field(default=None, ge=0, le=51)
    preset: (
        Literal[
            "ultrafast", "superfast", "veryfast", "faster",
            "fast", "medium", "slow", "slower", "veryslow",
        ]
        | None
    ) = None
    burn_subtitles: bool | None = None
    write_srt: bool | None = None


class SubtitleStyleIn(BaseModel):
    enabled: bool | None = None
    font_family: str | None = Field(default=None, max_length=80)

    @field_validator("font_family")
    @classmethod
    def _installed(cls, value: str | None) -> str | None:
        """Refuse a family this image cannot render.

        Checked on the way IN, never on the way out: a project stored before
        this existed carries "Arial", and failing to LOAD it over a font
        would be far worse than rendering it in something legible. The render
        path substitutes with a warning (app.subtitle.fonts.resolve); this
        stops the operator from choosing a substitution in the first place.
        """
        from app.subtitle import fonts

        if value is not None and value not in fonts.available():
            raise ValueError(
                f"'{value}' фонт энэ сервер дээр суулгагдаагүй байна. "
                f"Боломжтой: {', '.join(fonts.available())}"
            )
        return value
    font_size: int | None = Field(default=None, ge=8, le=200)
    primary_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_width: float | None = Field(default=None, ge=0, le=20)
    shadow: float | None = Field(default=None, ge=0, le=20)
    position: Literal["bottom", "top", "center"] | None = None
    margin_v: int | None = Field(default=None, ge=0, le=500)


class SubtitleStyleFull(BaseModel):
    """A complete style, for saving as a template.

    Not SubtitleStyleIn: that one is a PATCH where every field is optional,
    and a template with half its fields missing is not a house style. The
    font is validated the same way, so a saved template can never carry a
    family this image lacks.
    """

    enabled: bool = True
    font_family: str = Field(max_length=80)
    font_size: int = Field(ge=8, le=200)
    primary_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_width: float = Field(ge=0, le=20)
    shadow: float = Field(ge=0, le=20)
    position: Literal["bottom", "top", "center"]
    margin_v: int = Field(ge=0, le=500)

    @field_validator("font_family")
    @classmethod
    def _installed(cls, value: str) -> str:
        from app.subtitle import fonts

        if value not in fonts.available():
            raise ValueError(
                f"'{value}' фонт энэ сервер дээр суулгагдаагүй байна. "
                f"Боломжтой: {', '.join(fonts.available())}"
            )
        return value


class SubtitleTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    style: SubtitleStyleFull


class TransitionIn(BaseModel):
    type: str | None = Field(default=None, max_length=60)
    duration: float | None = Field(default=None, ge=0.0, le=2.0)


class UpdateProjectIn(BaseModel):
    """Partial update. Only the fields a client actually sends are touched —
    a whole-document PUT lets one stale tab overwrite work it never saw."""

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    export: ExportSettingsIn | None = None
    subtitle_style: SubtitleStyleIn | None = None
    transition: TransitionIn | None = None


class SegmentEditIn(BaseModel):
    """One transcript line the user corrected before asking for suggestions.

    Only `text` is editable: timings come from our own cut boundaries and are
    exact, so letting a client rewrite them can only make them wrong.
    """

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(max_length=5000)


class UpdateTranscriptIn(BaseModel):
    segments: list[SegmentEditIn] = Field(max_length=5000)


class SelectRangesIn(BaseModel):
    """Build a timeline from explicit ranges — the "cut it myself" path."""

    ranges: list[tuple[float, float]] = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class OutputOut(BaseModel):
    id: str
    kind: str
    title: str
    duration_sec: float
    size_bytes: int
    created_at: float
    # Two separate URLs on purpose: `attachment` makes a browser download
    # instead of play, so one link cannot serve both the player and the
    # download button.
    play_url: str
    download_url: str
    srt_url: str | None = None


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


class BrandUploadIn(BaseModel):
    """Asks for a presigned PUT. The type is checked here rather than at
    render time, where a format ffmpeg cannot read would fail an export these
    assets are only decorating."""

    content_type: str = Field(max_length=64)


class BrandSaveIn(BaseModel):
    """`key` null clears the slot. Bounded because it becomes an R2 key."""

    key: str | None = Field(default=None, max_length=KEY_MAX)


class ProviderSettingsIn(BaseModel):
    """Every field is optional and means three different things.

    Absent — leave it alone. A value — store it. An empty string — drop the
    stored value and fall back to the environment. Collapsing the last two
    would make a mistyped key permanent.
    """

    openrouter_api_key: str | None = Field(default=None, max_length=SECRET_MAX)
    duudlaga_api_key: str | None = Field(default=None, max_length=SECRET_MAX)
    # Accepted and stored before anything reads it — see config.Settings.
    elevenlabs_api_key: str | None = Field(default=None, max_length=SECRET_MAX)
    openrouter_model: str | None = Field(default=None, max_length=MODEL_MAX)
    #: Which recogniser runs. A closed set, checked here rather than at the
    #: first transcribe: a typo would otherwise be stored, look saved, and
    #: fail a job an hour later.
    stt_provider: Literal["duudlaga", "elevenlabs"] | None = None
