import io
import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from interview_agent import updates
from interview_agent.files import atomic_json, read_json
from interview_agent.update_bundle import MANIFEST, UpdateError, extract_bundle, sha256, version, verify_folder, write_manifest
from interview_agent.update_install import install, paths, recover, validate_plan


def bundle(folder, release="0.1.4"):
    folder.mkdir(parents=True)
    (folder / "_internal").mkdir()
    (folder / "interview-agent.exe").write_bytes(b"new-exe")
    (folder / "interview-agent-updater.exe").write_bytes(b"helper")
    (folder / "_internal/runtime.dll").write_bytes(b"runtime")
    write_manifest(folder, release)
    return folder


def archive_bytes(folder):
    stream = io.BytesIO()
    with ZipFile(stream, "w") as archive:
        for path in folder.rglob("*"):
            if path.is_file():
                archive.write(path, "interview-agent/" + path.relative_to(folder).as_posix())
    return stream.getvalue()


def release_payload(content):
    import hashlib
    return {"tag_name": "v0.1.4", "draft": False, "prerelease": False, "body": "New version", "assets": [{"name": "interview-agent-v0.1.4-windows-x64.zip", "state": "uploaded", "size": len(content), "digest": "sha256:" + hashlib.sha256(content).hexdigest(), "browser_download_url": "https://github.com/GloriousAndNew/interview-agent/releases/download/v0.1.4/interview-agent-v0.1.4-windows-x64.zip"}]}


def test_versions_and_old_remote_ignored():
    assert version("v0.1.10") > version("0.1.9")
    with pytest.raises(UpdateError):
        version("v1.0.0-beta")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"tag_name": "v0.1.0"})))
    assert not updates.latest(client)["available"]


def test_download_verified_bundle(tmp_path):
    content = archive_bytes(bundle(tmp_path / "bundle"))
    release = release_payload(content)
    def handler(request):
        assert "authorization" not in request.headers
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=release)
        return httpx.Response(200, content=content)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = tmp_path / "data"
    updates.download(data, client=client)
    state = updates.read_state(data)
    assert state["phase"] == "ready", state
    verify_folder(data / "updates/v0.1.4/staged/interview-agent", "0.1.4", state["manifest_hash"])


def test_wrong_checksum_and_redirect_rejected(tmp_path):
    content = archive_bytes(bundle(tmp_path / "bundle"))
    release = release_payload(content)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=release) if request.url.host == "api.github.com" else httpx.Response(200, content=b"x" * len(content))))
    updates.download(tmp_path / "data", client=client)
    assert updates.read_state(tmp_path / "data")["phase"] == "failed"
    with pytest.raises(UpdateError):
        updates.response(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "https://evil.example/file"}))), updates.API_URL)


@pytest.mark.parametrize("name", ["../evil", "interview-agent/../../evil", "interview-agent/C:/evil", "interview-agent/CON.txt", "interview-agent/_internal/evil.", "interview-agent/evil\\file"])
def test_malicious_archive_names(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with ZipFile(archive, "w") as zipped:
        zipped.writestr(name, b"malicious")
    with pytest.raises(UpdateError):
        extract_bundle(archive, tmp_path / "stage", "0.1.4")
    assert not (tmp_path / "stage").exists()


def test_manifest_hash_extra_file_and_wrong_version(tmp_path):
    target = bundle(tmp_path / "bundle")
    with pytest.raises(UpdateError):
        verify_folder(target, "0.1.5")
    (target / "surprise.exe").write_bytes(b"x")
    with pytest.raises(UpdateError):
        verify_folder(target, "0.1.4")
    (target / "surprise.exe").unlink()
    (target / "interview-agent.exe").write_bytes(b"tampered")
    with pytest.raises(UpdateError):
        verify_folder(target, "0.1.4")


@pytest.fixture
def plan(tmp_path):
    data = tmp_path / "data"
    (data / "updates").mkdir(parents=True)
    install_dir = tmp_path / "application"
    bundle(install_dir, "0.1.3")
    (install_dir / "interview-agent.exe").write_bytes(b"old-exe")
    staged = bundle(data / "updates/v0.1.4/staged/interview-agent")
    value = {"token": "a" * 32, "data_dir": str(data), "install_dir": str(install_dir), "staged_dir": str(staged), "version": "0.1.4", "old_version": "0.1.3", "port": 8547, "parent_pid": 0, "manifest_hash": sha256(staged / MANIFEST)}
    path = data / "updates/install-plan.json"
    atomic_json(path, value)
    atomic_json(data / "updates/installing.json", {"token": value["token"]})
    with contextlib.closing(sqlite3.connect(data / "app.db")) as con:
        con.execute("CREATE TABLE sample(value)")
        con.execute("INSERT INTO sample VALUES ('before')")
        con.commit()
    return path, value


class Process:
    def wait(self, timeout):
        return 0


def test_install_preserves_database_and_retains_old_program(plan):
    path, value = plan
    calls = []
    assert install(path, launch_fn=lambda p, **kw: calls.append(kw) or Process(), health_fn=lambda *args: True)
    data, app, incoming, backup, failed = paths(value)
    assert (app / "interview-agent.exe").read_bytes() == b"new-exe"
    assert (backup / "interview-agent.exe").read_bytes() == b"old-exe"
    assert not (data / "updates/installing.json").exists()
    assert (data / "updates/database-before-update.db").exists()
    assert updates.read_state(data)["phase"] == "completed"


def test_failed_start_rolls_back_database_migration_and_files(plan):
    path, value = plan
    data = Path(value["data_dir"])
    calls = []
    def unhealthy(*args):
        with contextlib.closing(sqlite3.connect(data / "app.db")) as con:
            con.execute("UPDATE sample SET value='migrated'")
            con.execute("PRAGMA user_version=99")
            con.commit()
        return False
    assert not install(path, launch_fn=lambda p, **kw: calls.append(kw) or Process(), health_fn=unhealthy)
    assert calls == [{}, {"updated": False}]
    assert (Path(value["install_dir"]) / "interview-agent.exe").read_bytes() == b"old-exe"
    with contextlib.closing(sqlite3.connect(data / "app.db")) as con:
        assert con.execute("SELECT value FROM sample").fetchone()[0] == "before"
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    assert updates.read_state(data)["phase"] == "rolled_back"


def test_recover_interrupted_swap(plan):
    path, value = plan
    from interview_agent.update_install import database_backup, journal
    data, app, incoming, backup, failed = paths(value)
    database_backup(data)
    app.rename(backup)
    journal(data, value, "swapping", had_database=True)
    recover(path)
    assert (app / "interview-agent.exe").read_bytes() == b"old-exe"
    assert not (data / "updates/installing.json").exists()


def test_overlap_and_downgrade_plans_rejected(plan):
    path, value = plan
    value["install_dir"] = value["data_dir"]
    atomic_json(path, value)
    with pytest.raises(UpdateError):
        validate_plan(path)


def test_gate_blocks_claim_and_new_jobs(settings, store, submitted):
    from interview_agent.files import atomic_json
    atomic_json(settings.data_dir / "updates/installing.json", {})
    assert store.claim("worker") is None
    with pytest.raises(ValueError, match="正在安装"):
        store.create("new", {}, {}, {})


def test_checksum_sidecar_fallback(tmp_path):
    content = archive_bytes(bundle(tmp_path / "bundle"))
    release = release_payload(content)
    digest = release["assets"][0].pop("digest")[7:]
    asset = release["assets"][0]
    release["assets"].append(dict(asset, name=asset["name"] + ".sha256", browser_download_url=asset["browser_download_url"] + ".sha256"))
    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=release)
        return httpx.Response(200, content=(digest + "  " + asset["name"] + "\n").encode())
    assert updates.latest(httpx.Client(transport=httpx.MockTransport(handler)))["sha256"] == digest


def test_busy_launcher_waits_before_install_and_starts_independent_helper(settings, store, submitted, tmp_path, monkeypatch):
    import desktop
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    instance = {"instance_token": "test-instance", "port": 8547}
    updates.status(settings.data_dir, phase="waiting", version="0.1.4", manifest_hash="pending")
    atomic_json(settings.data_dir / "updates/install-request.json", {"version": "0.1.4", "instance_token": instance["instance_token"]})
    calls = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **kwargs: calls.append(args))
    assert not desktop.prepare_update(settings, instance, headless=True, no_browser=True)
    assert not calls and not (settings.data_dir / "updates/installing.json").exists()
    with store.transaction() as con:
        con.execute("UPDATE jobs SET status='COMPLETED'")
    app = bundle(tmp_path.parent / (tmp_path.name + "-installed"), "0.1.3")
    staged = bundle(settings.data_dir / "updates/v0.1.4/staged/interview-agent")
    updates.status(settings.data_dir, phase="waiting", version="0.1.4", manifest_hash=sha256(staged / MANIFEST))
    monkeypatch.setattr(sys, "executable", str(app / "interview-agent.exe"))
    assert desktop.prepare_update(settings, instance, headless=True, no_browser=True), updates.read_state(settings.data_dir)
    assert len(calls) == 1
    assert Path(calls[0][0]).is_relative_to(settings.data_dir / "updates")
    assert not Path(calls[0][0]).is_relative_to(app)
    assert (settings.data_dir / "updates/installing.json").exists()


def test_second_rename_failure_recovers_old_files(plan, monkeypatch):
    path, value = plan
    original = Path.rename
    def fail_incoming(self, target):
        if self.name.startswith(".interview-agent-incoming-"):
            raise PermissionError("locked")
        return original(self, target)
    monkeypatch.setattr(Path, "rename", fail_incoming)
    assert not install(path, launch_fn=lambda *args, **kwargs: Process())
    assert (Path(value["install_dir"]) / "interview-agent.exe").read_bytes() == b"old-exe"


def test_recovery_finishes_database_restore_after_folder_already_restored(plan):
    from interview_agent.update_install import database_backup, journal
    path, value = plan
    data = Path(value["data_dir"])
    database_backup(data)
    with contextlib.closing(sqlite3.connect(data / "app.db")) as con:
        con.execute("UPDATE sample SET value='migration'")
        con.commit()
    journal(data, value, "restoring", had_database=True)
    recover(path)
    with contextlib.closing(sqlite3.connect(data / "app.db")) as con:
        assert con.execute("SELECT value FROM sample").fetchone()[0] == "before"
