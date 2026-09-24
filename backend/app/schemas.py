"""Request/response schemas and, in one place, every input limit.

Limits are matched deliberately to the database column widths. Without them
Postgres rejects an oversized value at the driver level and FastAPI turns
that into a 500, when the honest answer is a 422 naming the field.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.ai.schema import MAX_SHORT_COUNT, MAX_YOUTUBE_COUNT

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


#: The languages a video can be declared as. Mirrors
#: app.languages.SOURCE_LANGUAGES; a Literal because a request schema has to
#: be a type, and a test holds the two to each other.
SourceLanguage = Literal["mn", "en"]


class CreateProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    filename: str = Field(min_length=1, max_length=NAME_MAX)
    size_bytes: int = Field(ge=1, le=UPLOAD_MAX_BYTES)
    #: What is SPOKEN in the video. Omitted means Mongolian, which is what
    #: every client before this field sent without saying.
    language: SourceLanguage = "mn"

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


class LogoIn(BaseModel):
    """The per-project half of the brand logo. Bounds match models.LogoSettings."""

    enabled: bool | None = None
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right"] | None = None
    width_pct: float | None = Field(default=None, gt=0.0, le=100.0)
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    margin_pct: float | None = Field(default=None, ge=0.0, lt=50.0)


class ExportSettingsIn(BaseModel):
    """Every field of models.ExportSettings, each optional.

    EVERY field, and a test holds the two lists together: `logo`,
    `use_intro` and `use_outro` were missing from here from the day they were
    added, and pydantic drops a field it does not know without a word — so
    the page saved them, answered "saved", and the next read had them off.
    No export ever carried a logo or an intro chosen on the page.
    """

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
    logo: LogoIn | None = None
    use_intro: bool | None = None
    use_outro: bool | None = None
    subtitle_language: Literal["mn", "source"] | None = None
    voice_over: bool | None = None
    original_volume: float | None = Field(default=None, ge=0.0, le=1.0)


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
    #: Correctable, because it is chosen at upload and a wrong one decodes the
    #: whole video as the wrong language. Changing it does not re-transcribe:
    #: the page says to, since that is a paid run.
    language: SourceLanguage | None = None
    export: ExportSettingsIn | None = None
    subtitle_style: SubtitleStyleIn | None = None
    transition: TransitionIn | None = None


class SegmentEditIn(BaseModel):
    """One transcript line the user corrected.

    Only the words are editable — what was said (`text`) and its Mongolian
    subtitle (`translation`). Timings come from our own cut boundaries and are
    exact, so letting a client rewrite them can only make them wrong.

    Either field may be omitted, not both: a client from before translation
    existed sends `text` alone and keeps working. An empty `translation`
    clears it, which queues the line for the next translation run.
    """

    id: str = Field(min_length=1, max_length=64)
    text: str | None = Field(default=None, max_length=5000)
    translation: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _says_something(self) -> SegmentEditIn:
        if self.text is None and self.translation is None:
            raise ValueError("An edit needs `text`, `translation`, or both")
        return self


class UpdateTranscriptIn(BaseModel):
    segments: list[SegmentEditIn] = Field(max_length=5000)


class SelectRangesIn(BaseModel):
    """Build a timeline from explicit ranges — the "cut it myself" path."""

    ranges: list[tuple[float, float]] = Field(min_length=1, max_length=200)


class TranslateIn(BaseModel):
    """Omitted, or `force: false`, translates only the lines that have no
    translation — which is also how a run that failed part-way is finished
    without paying for the lines that already came back. `force` sends every
    line again, replacing the Mongolian text, hand edits included: the page
    says so before the click."""

    force: bool = False


class SuggestIn(BaseModel):
    """How many ideas to ask for. Either field omitted keeps what the button
    always did — three of each where the video can hold them — so a client
    that predates choosing is unaffected.

    The field bounds are the absolute ceilings; the per-video limit
    (app.ai.schema.count_limits) is enforced by the route, which knows the
    video. Both are refusals, never silent clamps: the producer is standing at
    the button and "you asked for 8, you got 4" is theirs to know before the
    bill, not after.
    """

    shorts: int | None = Field(default=None, ge=1, le=MAX_SHORT_COUNT)
    youtube: int | None = Field(default=None, ge=0, le=MAX_YOUTUBE_COUNT)


class ExportSelectionIn(BaseModel):
    """Which of the model's ideas to render. Omitting both renders every one,
    which is what the button did before there was any way to choose.

    Shorts carry their own id; YouTube plans do not, so they are named by
    position. The position is pinned to the plan's title in the job payload —
    regenerating the suggestions between queueing and rendering would
    otherwise move index 2 onto a different plan and render it silently.
    """

    shorts: list[str] | None = Field(default=None, max_length=50)
    youtube: list[int] | None = Field(default=None, max_length=50)


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
    elevenlabs_api_key: str | None = Field(default=None, max_length=SECRET_MAX)
    openrouter_model: str | None = Field(default=None, max_length=MODEL_MAX)
    #: The voice-over's model and voice. Both end up in a request to
    #: ElevenLabs — the voice id in its URL PATH — so both are held to the
    #: characters an id can have: a slash would address another endpoint
    #: with the account's key. Empty clears, as for every field here.
    elevenlabs_tts_model: str | None = Field(
        default=None, max_length=MODEL_MAX, pattern=r"^[A-Za-z0-9_.-]*$"
    )
    elevenlabs_tts_voice_id: str | None = Field(
        default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$"
    )
    #: Which recogniser runs. A closed set, checked here rather than at the
    #: first transcribe: a typo would otherwise be stored, look saved, and
    #: fail a job an hour later.
    stt_provider: Literal["duudlaga", "elevenlabs"] | None = None
