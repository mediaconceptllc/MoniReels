"""Uploading a source in parts, and starting an upload again (UP-1, UX-6, UX-7).

The audit found three things about the upload. One presigned PUT cannot
carry more than ~5 GiB while the limit is 20 GiB, so a 6 GB video failed
after an hour. A dropped connection restarted a 4 GB upload from zero. And
a project whose upload was cancelled, or whose import failed, was listed as
"Видео орсон" and shown as "Видео боловсруулагдаж байна…" for ever, with no
way to give it a video again.

R2 is replaced by a small in-memory S3 that keeps parts and objects the way
the real one does, so these tests exercise the whole route: what is signed,
what is checked against R2's own listing, and what is left behind.
"""
from __future__ import annotations

import itertools

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app import r2
from app.config import get_settings
from app.dbmodels import Job, User
from app.dbmodels import Project as ProjectRow
from app.security import hash_password
from app.store import get_row, to_domain
from tests.conftest import requires_db

pytestmark = requires_db

MIB = 1024**2
GIB = 1024**3


class FakeS3:
    """Just enough of S3 for multipart: uploads, parts, objects, listings."""

    def __init__(self):
        self.objects: dict[str, int] = {}
        self.uploads: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.list_calls = 0

    @staticmethod
    def _gone(op: str):
        return ClientError({"Error": {"Code": "NoSuchUpload", "Message": "gone"}}, op)

    def _upload(self, key: str, upload_id: str, op: str) -> dict:
        upload = self.uploads.get(upload_id)
        if upload is None or upload["key"] != key:
            raise self._gone(op)
        return upload

    # -- what the API calls ---------------------------------------------
    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803 - boto3's names
        extra = "".join(f"&{k}={Params[k]}" for k in ("UploadId", "PartNumber") if k in Params)
        return f"https://r2.test/{Params['Key']}?op={op}{extra}&expires={ExpiresIn}"

    def create_multipart_upload(self, Bucket, Key, ContentType):  # noqa: N803
        upload_id = f"u{next(self.ids)}"
        self.uploads[upload_id] = {"key": Key, "parts": {}, "type": ContentType}
        return {"UploadId": upload_id}

    def list_parts(self, Bucket, Key, UploadId, MaxParts=1000, PartNumberMarker=0):  # noqa: N803
        self.list_calls += 1
        upload = self._upload(Key, UploadId, "ListParts")
        numbers = sorted(n for n in upload["parts"] if n > PartNumberMarker)
        page = numbers[:MaxParts]
        return {
            "Parts": [
                {"PartNumber": n, "Size": upload["parts"][n], "ETag": f'"e{n}"'} for n in page
            ],
            "IsTruncated": len(numbers) > MaxParts,
            "NextPartNumberMarker": page[-1] if page else 0,
        }

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):  # noqa: N803
        upload = self._upload(Key, UploadId, "CompleteMultipartUpload")
        parts = MultipartUpload["Parts"]
        assert [p["PartNumber"] for p in parts] == sorted(upload["parts"])
        assert all(p["ETag"] == f'"e{p["PartNumber"]}"' for p in parts)
        self.objects[Key] = sum(upload["parts"].values())
        del self.uploads[UploadId]

    def abort_multipart_upload(self, Bucket, Key, UploadId):  # noqa: N803
        self._upload(Key, UploadId, "AbortMultipartUpload")
        del self.uploads[UploadId]

    def list_multipart_uploads(self, Bucket, Prefix="", **_marker):  # noqa: N803
        return {
            "Uploads": [
                {"Key": u["key"], "UploadId": uid} for uid, u in self.uploads.items()
                if u["key"].startswith(Prefix)
            ],
            "IsTruncated": False,
        }

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": self.objects[Key]}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, **_kw):  # noqa: N803
        keys = [k for k in self.objects if k.startswith(Prefix)]
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)

    # -- what the browser does ------------------------------------------
    def put_part(self, upload_id: str, number: int, size: int) -> None:
        self.uploads[upload_id]["parts"][number] = size

    def upload_all(self, upload_id: str, total: int) -> None:
        part_size, count = r2.part_plan(total)
        for n in range(1, count + 1):
            self.put_part(upload_id, n, r2.expected_part_size(total, n))


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setenv("R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "testkey")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "testsecret")
    monkeypatch.setenv("R2_BUCKET", "testbucket")
    get_settings.cache_clear()
    monkeypatch.setattr(r2, "_client_or_raise", lambda: fake)
    yield fake
    get_settings.cache_clear()


@pytest.fixture
def client(db, s3):
    from app.main import app
    from app.video.capabilities import Capabilities

    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    return TestClient(app)


def _user(db, username: str) -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(username=username, pw_salt=salt, pw_hash=digest, role="editor")
    db.add(user)
    db.commit()
    return user


def _auth(client: TestClient, username: str) -> dict:
    response = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create(client, auth, size: int, filename: str = "film.mp4") -> dict:
    made = client.post(
        "/projects",
        json={"name": "Кино", "filename": filename, "size_bytes": size},
        headers=auth,
    )
    assert made.status_code == 201, made.text
    return made.json()


def _fail(db, job_id: str) -> None:
    """An import that ended the way the queue ends one (queue.fail): failed,
    with its dedupe key released."""
    job = db.get(Job, job_id)
    job.state, job.error, job.dedupe_key = "failed", "Энэ файлаас видео уншиж чадсангүй.", None
    db.commit()


def _start(client, auth, project_id: str) -> dict:
    started = client.post(f"/projects/{project_id}/upload/multipart", headers=auth)
    assert started.status_code == 200, started.text
    return started.json()


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def test_the_part_plan_fits_r2_for_every_allowed_size():
    """R2: parts of one size (the last may be shorter), each ≥ 5 MiB, at
    most 10,000 — checked across the whole range the API accepts, and a
    20 GiB file is far past what one PUT can carry."""
    from app.schemas import UPLOAD_MAX_BYTES

    for size in (1, 5 * MIB, 64 * MIB, 5 * GIB + 1, 6 * GIB, 157 * GIB // 10, UPLOAD_MAX_BYTES):
        part_size, count = r2.part_plan(size)
        assert part_size >= 5 * MIB and part_size % MIB == 0
        assert count <= r2.MAX_PARTS
        last = r2.expected_part_size(size, count)
        assert 0 < last <= part_size
        assert part_size * (count - 1) + last == size
    assert r2.part_plan(UPLOAD_MAX_BYTES) == (16 * MIB, 1280)


# ---------------------------------------------------------------------------
# A whole multipart upload
# ---------------------------------------------------------------------------


def test_a_6_gb_upload_goes_in_parts_and_is_imported(client, db, s3):
    """Past a single PUT's ~5 GiB: started, signed in batches, completed from
    R2's own listing, then confirmed and queued exactly like a small file."""
    _user(db, "alice")
    auth = _auth(client, "alice")
    size = 6 * GIB + 123
    made = _create(client, auth, size)
    pid = made["project_id"]

    started = _start(client, auth, pid)
    assert started["mode"] == "multipart"
    assert (started["part_size"], started["part_count"]) == r2.part_plan(size)
    upload_id = started["upload_id"]
    assert s3.uploads[upload_id]["key"] == made["upload_key"]  # the server's key, not the browser's

    signed = client.post(
        f"/projects/{pid}/upload/multipart/sign",
        json={"upload_id": upload_id, "part_numbers": [3, 1, 2]},
        headers=auth,
    )
    assert signed.status_code == 200
    urls = signed.json()["urls"]
    assert sorted(urls) == ["1", "2", "3"]
    assert f"UploadId={upload_id}&PartNumber=2" in urls["2"]
    assert made["upload_key"] in urls["2"]

    s3.upload_all(upload_id, size)
    done = client.post(
        f"/projects/{pid}/upload/multipart/complete", json={"upload_id": upload_id}, headers=auth
    )
    assert done.status_code == 200, done.text
    assert s3.objects[made["upload_key"]] == size

    confirmed = client.post(f"/projects/{pid}/upload-complete", headers=auth)
    assert confirmed.status_code == 200 and confirmed.json()["job_id"]
    db.expire_all()
    row = db.get(ProjectRow, pid)
    assert row.video_key == made["upload_key"]
    assert to_domain(row).upload_key is None


def test_a_small_file_is_told_to_use_its_single_put(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, 10 * MIB)
    assert _start(client, auth, made["project_id"]) == {
        "mode": "single", "upload_id": None, "part_size": None, "part_count": None, "sign_batch": None,
    }
    assert s3.uploads == {}


def test_a_reloaded_page_learns_which_parts_r2_already_holds(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    size = 100 * MIB
    pid = _create(client, auth, size)["project_id"]
    upload_id = _start(client, auth, pid)["upload_id"]
    s3.put_part(upload_id, 1, 16 * MIB)
    s3.put_part(upload_id, 4, 16 * MIB)

    status = client.post(
        f"/projects/{pid}/upload/multipart/status", json={"upload_id": upload_id}, headers=auth
    )
    assert status.status_code == 200
    assert status.json() == {
        "part_size": 16 * MIB,
        "part_count": 7,
        "sign_batch": 50,
        "parts": [{"part_number": 1, "size": 16 * MIB}, {"part_number": 4, "size": 16 * MIB}],
    }


def test_the_listing_is_followed_across_pages(monkeypatch, s3):
    """1,280 parts is two pages of list_parts; completing from page one
    alone would refuse a finished upload as missing parts."""
    key = "sources/p/source.mp4"
    upload_id = s3.create_multipart_upload(Bucket="b", Key=key, ContentType="x")["UploadId"]
    for n in range(1, 1281):
        s3.put_part(upload_id, n, 16 * MIB)
    parts = r2.list_parts(key, upload_id)
    assert [p["part_number"] for p in parts] == list(range(1, 1281))
    assert s3.list_calls == 2


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


def test_complete_refuses_missing_and_wrongly_sized_parts(client, db, s3):
    """Assembled from R2's listing, checked against the declared size: a
    browser cannot stitch an object other than the one declared, and a
    half-finished upload is not passed off as a video."""
    _user(db, "alice")
    auth = _auth(client, "alice")
    size = 100 * MIB  # 7 parts: six of 16 MiB, one of 4 MiB
    made = _create(client, auth, size)
    pid = made["project_id"]
    upload_id = _start(client, auth, pid)["upload_id"]

    for n in range(1, 6):
        s3.put_part(upload_id, n, 16 * MIB)
    missing = client.post(
        f"/projects/{pid}/upload/multipart/complete", json={"upload_id": upload_id}, headers=auth
    )
    assert missing.status_code == 400
    assert "2 хэсэг ирээгүй" in missing.json()["detail"]

    s3.put_part(upload_id, 6, 16 * MIB)
    s3.put_part(upload_id, 7, 40 * MIB)  # more than the declared file has left
    wrong = client.post(
        f"/projects/{pid}/upload/multipart/complete", json={"upload_id": upload_id}, headers=auth
    )
    assert wrong.status_code == 400
    assert "1 хэсгийн хэмжээ буруу" in wrong.json()["detail"]
    assert made["upload_key"] not in s3.objects  # nothing was assembled

    s3.put_part(upload_id, 8, 16 * MIB)  # a part the plan does not have
    s3.put_part(upload_id, 7, 4 * MIB)
    extra = client.post(
        f"/projects/{pid}/upload/multipart/complete", json={"upload_id": upload_id}, headers=auth
    )
    assert extra.status_code == 400 and "хэмжээ буруу" in extra.json()["detail"]


def test_parts_beyond_the_plan_are_not_signed(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    pid = _create(client, auth, 100 * MIB)["project_id"]
    upload_id = _start(client, auth, pid)["upload_id"]
    refused = client.post(
        f"/projects/{pid}/upload/multipart/sign",
        json={"upload_id": upload_id, "part_numbers": [7, 8]},
        headers=auth,
    )
    assert refused.status_code == 400 and "7 хэсэгтэй" in refused.json()["detail"]
    too_many = client.post(
        f"/projects/{pid}/upload/multipart/sign",
        json={"upload_id": upload_id, "part_numbers": list(range(1, 52))},
        headers=auth,
    )
    assert too_many.status_code == 422


def test_an_upload_that_is_gone_says_so(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    pid = _create(client, auth, 100 * MIB)["project_id"]
    for path in ("status", "complete"):
        gone = client.post(
            f"/projects/{pid}/upload/multipart/{path}", json={"upload_id": "nope"}, headers=auth
        )
        assert gone.status_code == 404 and "дахин хуулна" in gone.json()["detail"]


def test_another_accounts_project_is_not_found(client, db, s3):
    """Every route answers 404 for a project that is not the caller's — the
    same answer as one that does not exist."""
    _user(db, "alice")
    _user(db, "mallory")
    alice, mallory = _auth(client, "alice"), _auth(client, "mallory")
    pid = _create(client, alice, 100 * MIB)["project_id"]
    upload_id = _start(client, alice, pid)["upload_id"]
    body = {"upload_id": upload_id}
    calls = [
        ("upload/multipart", None),
        ("upload/multipart/sign", {**body, "part_numbers": [1]}),
        ("upload/multipart/status", body),
        ("upload/multipart/complete", body),
        ("upload/multipart/abort", body),
        ("upload", {"filename": "x.mp4", "size_bytes": 10}),
    ]
    for path, payload in calls:
        response = client.post(f"/projects/{pid}/{path}", json=payload, headers=mallory)
        assert response.status_code == 404, path
    assert upload_id in s3.uploads  # mallory aborted nothing


def test_there_is_nothing_to_sign_once_the_video_is_in(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, 10 * MIB)
    s3.objects[made["upload_key"]] = 10 * MIB
    assert client.post(f"/projects/{made['project_id']}/upload-complete", headers=auth).status_code == 200
    late = client.post(f"/projects/{made['project_id']}/upload/multipart", headers=auth)
    assert late.status_code == 409


# ---------------------------------------------------------------------------
# Cancelling, and what is left behind
# ---------------------------------------------------------------------------


def test_abort_and_delete_leave_no_billed_parts(client, db, s3):
    """Unfinished parts are in no object listing, so deleting the prefix
    could never see them; a cancelled upload must abort them."""
    _user(db, "alice")
    auth = _auth(client, "alice")
    pid = _create(client, auth, 100 * MIB)["project_id"]
    upload_id = _start(client, auth, pid)["upload_id"]
    s3.put_part(upload_id, 1, 16 * MIB)

    aborted = client.post(
        f"/projects/{pid}/upload/multipart/abort", json={"upload_id": upload_id}, headers=auth
    )
    assert aborted.json() == {"aborted": True}
    assert s3.uploads == {}
    again = client.post(
        f"/projects/{pid}/upload/multipart/abort", json={"upload_id": upload_id}, headers=auth
    )
    assert again.json() == {"aborted": False}  # already gone is fine

    second = _start(client, auth, pid)["upload_id"]
    s3.put_part(second, 1, 16 * MIB)
    assert client.delete(f"/projects/{pid}", headers=auth).status_code == 200
    assert s3.uploads == {}


def test_starting_again_aborts_the_earlier_attempt(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    pid = _create(client, auth, 100 * MIB)["project_id"]
    first = _start(client, auth, pid)["upload_id"]
    second = _start(client, auth, pid)["upload_id"]
    assert list(s3.uploads) == [second] and first != second


# ---------------------------------------------------------------------------
# "Видео орсон" only when it is (UX-6)
# ---------------------------------------------------------------------------


def test_a_project_has_a_video_only_after_upload_complete(client, db, s3):
    """has_video read "a key exists", and the key was set at creation: a
    cancelled upload was listed as a video, and its page signed a URL to an
    object that was never there."""
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, 10 * MIB)
    pid = made["project_id"]

    def listed():
        return next(p for p in client.get("/projects", headers=auth).json() if p["id"] == pid)

    assert listed()["has_video"] is False
    assert client.get(f"/projects/{pid}", headers=auth).json()["media"]["source_url"] is None

    s3.objects[made["upload_key"]] = 10 * MIB
    assert client.post(f"/projects/{pid}/upload-complete", headers=auth).status_code == 200
    assert listed()["has_video"] is True
    assert client.get(f"/projects/{pid}", headers=auth).json()["media"]["source_url"]


def test_a_failed_import_is_listed_as_failed(client, db, s3):
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, 10 * MIB)
    pid = made["project_id"]
    s3.objects[made["upload_key"]] = 10 * MIB
    job_id = client.post(f"/projects/{pid}/upload-complete", headers=auth).json()["job_id"]

    def failed():
        return next(p for p in client.get("/projects", headers=auth).json() if p["id"] == pid)[
            "import_failed"
        ]

    assert failed() is False
    _fail(db, job_id)
    assert failed() is True


def test_a_failed_import_can_be_given_a_new_video(client, db, s3):
    """The way out of a failed import: a new source, under a new key if the
    file type changed, with the old object and any parts cleared away."""
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, 10 * MIB)
    pid = made["project_id"]
    s3.objects[made["upload_key"]] = 10 * MIB
    job_id = client.post(f"/projects/{pid}/upload-complete", headers=auth).json()["job_id"]

    busy = client.post(f"/projects/{pid}/upload", json={"filename": "b.mov", "size_bytes": 99}, headers=auth)
    assert busy.status_code == 409  # the import is still queued

    _fail(db, job_id)
    stray = s3.create_multipart_upload(Bucket="b", Key=f"sources/{pid}/source.mp4", ContentType="x")

    again = client.post(f"/projects/{pid}/upload", json={"filename": "b.mov", "size_bytes": 99}, headers=auth)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["upload_key"] == f"sources/{pid}/source.mov" and body["upload_url"]
    assert made["upload_key"] not in s3.objects  # the refused file is gone
    assert stray["UploadId"] not in s3.uploads
    db.expire_all()
    row = get_row(db, pid)
    project = to_domain(row)
    assert row.video_key is None
    assert (project.upload_key, project.upload_size_bytes) == (body["upload_key"], 99)

    s3.objects[body["upload_key"]] = 99
    assert client.post(f"/projects/{pid}/upload-complete", headers=auth).status_code == 200


def test_an_imported_project_keeps_its_video(client, db, s3):
    from app.models import VideoMeta

    _user(db, "alice")
    auth = _auth(client, "alice")
    pid = _create(client, auth, 10 * MIB)["project_id"]
    row = get_row(db, pid)
    project = to_domain(row)
    project.video = VideoMeta(
        source_key="sources/x/source.mp4", duration_sec=60.0, width=1920, height=1080, fps=30.0,
        has_audio=True, codec="h264", thumbnail_key="",
    )
    from app.store import save

    save(db, project)
    db.commit()
    refused = client.post(
        f"/projects/{pid}/upload", json={"filename": "b.mp4", "size_bytes": 9}, headers=auth
    )
    assert refused.status_code == 409 and "шинэ төсөл" in refused.json()["detail"]
