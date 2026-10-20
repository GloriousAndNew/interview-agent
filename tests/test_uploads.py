import hashlib
import json
from types import SimpleNamespace

import oss2
import pytest

from interview_agent.providers import OSSStorage, ProviderError
from interview_agent.uploads import METADATA_HASH, PART_SIZE, multipart_upload, upload_summary
from interview_agent.upload_test import test_upload as run_upload_test


def missing(kind):
    return kind(404, {}, b"", {})


class Bucket:
    def __init__(self):
        self.sessions = {}
        self.objects = {}
        self.sent = []
        self.inits = 0
        self.fail_part = None
        self.fail_complete = False
        self.delete_fails = False
        self.listed = 0

    def init_multipart_upload(self, key, headers):
        self.inits += 1
        session = str(self.inits)
        self.sessions[session] = {"headers": headers, "parts": {}}
        return SimpleNamespace(upload_id=session)

    def upload_part(self, key, upload_id, number, data, progress_callback):
        self.sent.append(number)
        progress_callback(len(data) // 2, len(data))
        if self.fail_part == number:
            self.fail_part = None
            raise oss2.exceptions.RequestError(Exception("SECRET https://signed-url.invalid"))
        self.sessions[upload_id]["parts"][number] = data
        progress_callback(len(data), len(data))
        return SimpleNamespace(etag=hashlib.md5(data).hexdigest())

    def head_object(self, key):
        if key not in self.objects:
            raise missing(oss2.exceptions.NoSuchKey)
        data, headers = self.objects[key]
        return SimpleNamespace(content_length=len(data), headers=headers)

    def list_parts(self, key, upload_id, **kwargs):
        self.listed += 1
        if upload_id not in self.sessions:
            raise missing(oss2.exceptions.NoSuchUpload)
        return SimpleNamespace(parts=[oss2.models.PartInfo(n, hashlib.md5(data).hexdigest(), size=len(data)) for n, data in self.sessions[upload_id]["parts"].items()], is_truncated=False, next_marker="")

    def complete_multipart_upload(self, key, upload_id, parts):
        session = self.sessions.pop(upload_id)
        self.objects[key] = (b"".join(session["parts"][p.part_number] for p in parts), session["headers"])
        if self.fail_complete:
            self.fail_complete = False
            raise oss2.exceptions.RequestError(Exception("response lost"))

    def abort_multipart_upload(self, key, upload_id):
        if upload_id not in self.sessions:
            raise missing(oss2.exceptions.NoSuchUpload)
        del self.sessions[upload_id]

    def delete_object(self, key):
        if self.delete_fails:
            raise RuntimeError("denied")
        self.objects.pop(key, None)

    def sign_url(self, *args, **kwargs):
        return "https://example.invalid/SECRET"


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"a" * PART_SIZE + b"b" * PART_SIZE + b"last")
    return path


def storage(bucket):
    provider = OSSStorage.__new__(OSSStorage)
    provider.bucket = bucket
    return provider


def test_resume_only_missing_parts_after_interruption(source):
    bucket, state, saved = Bucket(), {}, []
    bucket.fail_part = 2
    with pytest.raises(ProviderError, match="续传") as error:
        storage(bucket).upload("key", source, state=state, save=lambda s: saved.append(json.loads(json.dumps(s))))
    assert "SECRET" not in str(error.value)
    assert state["confirmed_bytes"] == PART_SIZE
    assert saved[1].get("upload_id")  # session durable before first part
    restored = json.loads(json.dumps(state))
    storage(bucket).upload("key", source, state=restored, save=lambda s: saved.append(json.loads(json.dumps(s))))
    assert bucket.sent == [1, 2, 2, 3]
    assert bucket.inits == 1
    assert restored["resume_count"] == 1
    assert restored["attempts"][1]["resumed_bytes"] == PART_SIZE
    assert restored["confirmed_bytes"] == source.stat().st_size
    assert restored["status"] == "completed"
    assert bucket.objects["key"][0] == source.read_bytes()
    exported = json.dumps(upload_summary({"upload_state": restored}))
    assert "upload_id" not in exported and "sha256" not in exported and "SECRET" not in exported


def test_lost_completion_response_not_reuploaded(source):
    bucket, state = Bucket(), {}
    bucket.fail_complete = True
    with pytest.raises(ProviderError):
        storage(bucket).upload("key", source, state=state)
    storage(bucket).upload("key", source, state=state)
    assert bucket.sent == [1, 2, 3]
    assert bucket.inits == 1 and state["status"] == "completed"


def test_server_parts_survive_missing_local_confirmation(source):
    bucket, state = Bucket(), {}
    bucket.fail_part = 2
    with pytest.raises(ProviderError):
        storage(bucket).upload("key", source, state=state)
    state["confirmed_bytes"] = 0
    storage(bucket).upload("key", source, state=state)
    assert bucket.sent.count(1) == 1


def test_expired_session_starts_cleanly(source):
    bucket, state = Bucket(), {}
    bucket.fail_part = 2
    with pytest.raises(ProviderError):
        storage(bucket).upload("key", source, state=state)
    bucket.sessions.clear()
    storage(bucket).upload("key", source, state=state)
    assert bucket.inits == 2
    assert state["session_restarts"] == 1
    assert bucket.objects["key"][0] == source.read_bytes()


def test_changed_source_cannot_mix_parts(source):
    bucket, state = Bucket(), {}
    bucket.fail_part = 2
    with pytest.raises(ProviderError):
        storage(bucket).upload("key", source, state=state)
    source.write_bytes(b"x" * source.stat().st_size)
    with pytest.raises(ValueError, match="发生变化"):
        storage(bucket).upload("key", source, state=state)
    assert bucket.inits == 1


def test_cancel_and_lease_errors_propagate_and_preserve_session(source):
    bucket, state = Bucket(), {}
    def check():
        if bucket.sent:
            raise InterruptedError("cancel")
    with pytest.raises(InterruptedError):
        storage(bucket).upload("key", source, state=state, check=check)
    assert state["upload_id"] and state["status"] == "cancelled"
    from interview_agent.db import LostLease
    with pytest.raises(LostLease):
        storage(bucket).upload("key", source, state={}, save=lambda _: (_ for _ in ()).throw(LostLease()))


def test_progress_persists_during_part_and_finishes(source, monkeypatch):
    import interview_agent.uploads as module
    ticks = iter(range(0, 1000, 2))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    saved, state = [], {}
    storage(Bucket()).upload("key", source, state=state, save=lambda s: saved.append(json.loads(json.dumps(s))))
    assert any(0 < s.get("part_inflight", 0) < PART_SIZE and s["current_bps"] > 0 for s in saved)
    assert state["attempts"][0]["sent_bytes"] == source.stat().st_size
    assert state["attempts"][0]["average_bps"] > 0


@pytest.mark.parametrize("delete_fails", [False, True])
def test_explicit_upload_test_and_cleanup(settings, store, delete_fails):
    bucket = Bucket()
    bucket.delete_fails = delete_fails
    result = run_upload_test(settings, store, storage_factory=lambda _: storage(bucket))
    assert result["status"] == "completed"
    assert result["cleanup_pending"] == delete_fails
    row = store.one("SELECT * FROM cleanup")
    assert row["job_id"] is None
    assert row["status"] == ("pending" if delete_fails else "done")
    saved = (settings.data_dir / "upload-test.json").read_text()
    assert "SECRET" not in saved and "upload_id" not in saved
    assert not bucket.sessions
    assert len(bucket.objects) == (1 if delete_fails else 0)
