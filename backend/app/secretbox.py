"""Provider keys at rest, sealed with a key that lives only in the environment.

The admin page stores the OpenRouter, duudlaga and ElevenLabs keys in the
`settings` table (app.provider_settings) so they can change without a
redeploy. In plain text there, anyone holding a database dump or a backup
held every paid account with it — the same set of people as anyone who can
read Postgres, which is a much larger set than anyone who can read Railway's
variables.

So a stored key is encrypted (Fernet: AES-128-CBC + HMAC-SHA256) with a key
that is never written to the database: `SETTINGS_ENCRYPTION_KEY`, set on both
services. A dump without the environment is ciphertext.

Three rules this module holds:

- **Tagged, not guessed.** A sealed value starts with `enc:v1:`. Plain rows
  from before keep working, and a value is never "decrypted" on the hope
  that it was encrypted.
- **A sealed value that cannot be opened is an error, never a key.** No key
  configured, or the wrong one: `SecretKeyError`, naming the variable. The
  ciphertext must never reach a provider as if it were the credential —
  that turns a configuration mistake into a stream of 401s that look like a
  revoked key.
- **Rotation without downtime.** The variable takes a comma-separated list:
  the FIRST key seals, every key opens. Put the new key first, keep the old
  one behind it until the API has started once (it re-seals every stored key
  with the new one), then drop the old.

Without the variable nothing changes — values are stored as before — and the
admin page says so (`provider_settings.storage_status`).
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings

PREFIX = "enc:v1:"
VARIABLE = "SETTINGS_ENCRYPTION_KEY"


class SecretKeyError(RuntimeError):
    """A sealed value this process cannot open, or a key it cannot use.

    The message names the variable and never a value: it reaches job errors,
    logs and the admin page.
    """


def _keys() -> list[str]:
    raw = get_settings().settings_encryption_key or ""
    return [k.strip() for k in raw.split(",") if k.strip()]


def enabled() -> bool:
    return bool(_keys())


def _box() -> MultiFernet | None:
    keys = _keys()
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except (ValueError, TypeError):
        # `from None`: the traceback must not carry the variable's contents
        # into a log line.
        raise SecretKeyError(
            f"{VARIABLE} нь Fernet түлхүүр биш байна (32 байтын url-safe base64). "
            "DEPLOY.md-д үүсгэх тушаал бий."
        ) from None


def check() -> None:
    """Raise SecretKeyError when the variable is set but unusable."""
    _box()


def is_sealed(value: str) -> bool:
    return value.startswith(PREFIX)


def seal(value: str) -> str:
    """The value as it should be stored: sealed when a key is configured,
    unchanged when not."""
    box = _box()
    if box is None:
        return value
    return PREFIX + box.encrypt(value.encode()).decode()


def unseal(value: str) -> str:
    """The stored value as the provider needs it. Plain values pass through."""
    if not is_sealed(value):
        return value
    box = _box()
    if box is None:
        raise SecretKeyError(
            f"Хадгалсан API түлхүүр шифрлэгдсэн боловч {VARIABLE} тавигдаагүй байна. "
            "Хувьсагчийг API ба worker хоёуланд нь тавина уу, эсвэл түлхүүрүүдийг дахин оруулна уу."
        )
    try:
        return box.decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise SecretKeyError(
            f"Хадгалсан API түлхүүрийг {VARIABLE}-ээр тайлж чадсангүй — түлхүүр солигдсон эсвэл "
            "сервисүүдэд өөр өөр утгатай байна. Хуучин түлхүүрийг жагсаалтад нэмэх эсвэл "
            "API түлхүүрүүдийг дахин оруулна уу."
        ) from None


def needs_reseal(value: str) -> bool:
    """Whether a stored value should be rewritten under the current first key:
    plain, or sealed with a key that is no longer first in the list."""
    if not enabled():
        return False
    if not is_sealed(value):
        return True
    primary = Fernet(_keys()[0])
    try:
        primary.decrypt(value[len(PREFIX):].encode())
    except InvalidToken:
        return True
    return False


def reseal(value: str) -> str:
    """Open with any configured key, seal with the first."""
    return seal(unseal(value))
