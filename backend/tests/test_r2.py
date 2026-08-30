"""Object storage rules.

Signing is local, so these run without a network and without a real bucket.
What they pin is the two-layer distinction the whole storage design rests on:
a KEY is an address that never changes, a FILENAME is presentation chosen per
request.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("R2_ACCESS_KEY_ID", "testkey")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "testsecret")
os.environ.setdefault("R2_BUCKET", "testbucket")

from app import r2  # noqa: E402
from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _configured():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_keys_are_rooted_at_the_project_id():
    """Every key hangs off an id that is assigned once and never changes, so
    renaming a project cannot orphan the objects it already owns."""
    assert r2.source_key("abc123", ".mp4") == "sources/abc123/source.mp4"
    assert r2.thumbnail_key("abc123") == "thumbnails/abc123/thumb.jpg"
    assert r2.output_key("abc123", "reel", 2, "mp4") == "outputs/abc123/reel_2.mp4"


@pytest.mark.parametrize(
    "suffix",
    ["../../etc/passwd", "/nested/x.mp4", ".mp4", "..", "", ".MP4", "mp4?x=1", "a" * 200],
)
def test_key_suffix_cannot_smuggle_a_path(suffix):
    """A suffix comes from a filename the client chose.

    Asserted as a rule rather than a table of expected strings: what matters
    is that no input can produce a key outside the project's own prefix, and
    that the result is always a single lowercase extension.
    """
    key = r2.source_key("p", suffix)
    assert key.startswith("sources/p/source.")
    ext = key[len("sources/p/source.") :]
    assert ext.isalnum() and ext.islower() and 0 < len(ext) <= 7


def test_download_name_is_ascii_and_names_both_project_and_item():
    """Downloading six outputs otherwise lands reel(1).mp4, reel(2).mp4 in the
    browser's Downloads folder — and two different projects collide outright.
    Cyrillic needs RFC 5987 encoding older browsers mangle, and a quote breaks
    the header itself."""
    name = r2.download_name("Тэнгэрийн хайр", "reel", 2, "mp4")
    assert name.isascii()
    assert name.endswith("-Reel-2.mp4")
    assert '"' not in name and "/" not in name


def test_safe_filename_strips_header_breaking_characters():
    assert r2.safe_filename('a"b/c\\d.mp4') == "a_b_c_d.mp4"


def test_presigned_put_is_signed_and_scoped_to_one_key():
    url = r2.presign_put("sources/p/source.mp4", "video/mp4")
    assert url.startswith("https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/")
    assert "sources/p/source.mp4" in url
    assert "X-Amz-Signature" in url


def test_play_and_download_urls_differ_only_in_disposition():
    """`attachment` makes a browser save instead of play, so one object with
    two purposes needs two links — never one."""
    play = r2.presign_get("outputs/p/reel_1.mp4")
    download = r2.presign_get("outputs/p/reel_1.mp4", filename="Project-Reel-1.mp4")
    assert "response-content-disposition" not in play.lower()
    assert "response-content-disposition" in download.lower()


def test_delete_prefix_refuses_an_unmanaged_prefix():
    """A bad caller must not be able to sweep the whole bucket."""
    with pytest.raises(r2.R2Error, match="unmanaged"):
        r2.delete_prefix("")
    with pytest.raises(r2.R2Error, match="unmanaged"):
        r2.delete_prefix("someone-elses-data/")


def test_content_type_is_inferred_for_upload():
    assert r2.content_type_for("a.mp4") == "video/mp4"
    assert r2.content_type_for("a.srt").startswith("text/plain")
    assert r2.content_type_for("a.unknown") == "application/octet-stream"


def test_unconfigured_storage_raises_a_named_error(monkeypatch):
    """A confusing boto3 credential error is not an answer to "R2 was never
    set up on this deployment"."""
    monkeypatch.delenv("R2_BUCKET", raising=False)
    get_settings.cache_clear()
    assert r2.enabled() is False
    with pytest.raises(r2.R2Disabled):
        r2.presign_put("sources/p/source.mp4")


def _settings(**overrides):
    from app.config import Settings

    base = {
        "r2_account_id": "0123456789abcdef0123456789abcdef",
        "r2_access_key_id": "k",
        "r2_secret_access_key": "s",
        "r2_bucket": "b",
    }
    return Settings(_env_file=None, **{**base, **overrides})


@pytest.mark.parametrize(
    "pasted",
    [
        "0123456789abcdef0123456789abcdef",
        "0123456789ABCDEF0123456789ABCDEF",
        "  0123456789abcdef0123456789abcdef  ",
        "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/",
        "0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
    ],
)
def test_account_id_is_accepted_however_the_dashboard_showed_it(pasted):
    """Cloudflare only ever shows the id inside the S3 endpoint, so the whole
    URL is the natural thing to copy. Every form has to build one endpoint."""
    settings = _settings(r2_account_id=pasted)
    assert settings.r2_config_error is None
    assert (
        settings.resolved_r2_endpoint
        == "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
    )


def test_a_token_pasted_as_the_account_id_is_refused_without_echoing_it():
    """The failure this replaces was `ValueError: Invalid endpoint:
    https://<value>...` raised from inside botocore — a 500 with no hint of
    the cause that printed the credential into the logs when the value pasted
    was a token, as it was in production."""
    token = "cfat_" + "x" * 43
    settings = _settings(r2_account_id=token)

    problem = settings.r2_config_error
    assert problem is not None
    assert "R2_ACCOUNT_ID" in problem
    assert token not in problem
    assert settings.r2_enabled is False


def test_an_explicit_endpoint_overrides_the_account_id_entirely():
    """A private or S3-compatible endpoint is a deliberate override; the
    account id is then irrelevant and must not veto the configuration."""
    settings = _settings(r2_account_id="", r2_endpoint="https://minio.internal:9000/")
    assert settings.r2_config_error is None
    assert settings.resolved_r2_endpoint == "https://minio.internal:9000"


def test_each_missing_credential_is_named():
    assert "R2_BUCKET is not set" == _settings(r2_bucket="").r2_config_error
    assert "R2_ACCOUNT_ID is not set" == _settings(r2_account_id="").r2_config_error


# --------------------------------------------------------------------------
# abort_stale_uploads walks a paginated listing. It runs at worker startup,
# so a walk that does not end is a queue that never starts.
# --------------------------------------------------------------------------


class _StuckListing:
    """A server that says "more to come" and hands back the same page."""

    def __init__(self):
        self.calls = 0

    def list_multipart_uploads(self, **_kw):
        self.calls += 1
        return {"Uploads": [], "IsTruncated": True, "NextKeyMarker": "same", "NextUploadIdMarker": "same"}


class _EndlessListing:
    """A server whose marker advances forever."""

    def __init__(self):
        self.calls = 0

    def list_multipart_uploads(self, **_kw):
        self.calls += 1
        return {
            "Uploads": [],
            "IsTruncated": True,
            "NextKeyMarker": f"k{self.calls}",
            "NextUploadIdMarker": f"u{self.calls}",
        }


def test_abort_stale_uploads_stops_when_the_marker_stops_moving(monkeypatch):
    client = _StuckListing()
    monkeypatch.setattr(r2, "_client_or_raise", lambda: client)

    assert r2.abort_stale_uploads() == 0
    assert client.calls == 2  # the repeat is recognised, not requested forever


def test_abort_stale_uploads_has_a_page_ceiling(monkeypatch):
    client = _EndlessListing()
    monkeypatch.setattr(r2, "_client_or_raise", lambda: client)

    assert r2.abort_stale_uploads() == 0
    assert client.calls == r2.MAX_MPU_PAGES


def test_abort_stale_uploads_aborts_only_what_is_older_than_the_cutoff(monkeypatch):
    from datetime import UTC, datetime, timedelta

    fresh = datetime.now(UTC) - timedelta(hours=1)
    stale = datetime.now(UTC) - timedelta(days=7)
    aborted: list[str] = []

    class _Client:
        def list_multipart_uploads(self, **_kw):
            return {
                "Uploads": [
                    {"Key": "sources/new", "UploadId": "1", "Initiated": fresh},
                    {"Key": "sources/old", "UploadId": "2", "Initiated": stale},
                ],
                "IsTruncated": False,
            }

        def abort_multipart_upload(self, *, Bucket, Key, UploadId):  # noqa: N803 - boto3's own names
            aborted.append(Key)

    monkeypatch.setattr(r2, "_client_or_raise", lambda: _Client())

    assert r2.abort_stale_uploads() == 1
    assert aborted == ["sources/old"]
