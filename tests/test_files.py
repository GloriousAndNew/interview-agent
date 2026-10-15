from pathlib import Path

import pytest

from interview_agent.config import canonical_directory, Settings
from interview_agent.files import atomic_text, inside


def test_canonical_directory_resolves_created_child(tmp_path, monkeypatch):
    logical = tmp_path / "logical"
    physical = tmp_path / "redirected"
    physical.mkdir()
    original = Path.resolve

    def redirected_resolve(path, *args, **kwargs):
        if path.parent == logical and path.name.startswith(".path-check-"):
            return physical / path.name
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    assert canonical_directory(logical) == physical
    assert not list(logical.glob(".path-check-*"))


def test_settings_uses_canonical_root(tmp_path, monkeypatch):
    import interview_agent.config as config
    physical = tmp_path / "physical"
    physical.mkdir()
    monkeypatch.setattr(config, "canonical_directory", lambda _: physical)
    monkeypatch.setattr(config, "registry_values", lambda _: [])
    settings = Settings.load(tmp_path / "logical")
    assert settings.data_dir == physical
    assert inside(settings.data_dir, "media/a.wav") == physical / "media/a.wav"


def test_atomic_replace_retries_transient_windows_sharing(tmp_path, monkeypatch):
    import interview_agent.files as files
    actual = files.os.replace
    attempts = []

    def temporarily_locked(source, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("sharing violation")
        return actual(source, target)

    monkeypatch.setattr(files.os, "replace", temporarily_locked)
    monkeypatch.setattr(files.time, "sleep", lambda _: None)
    target = tmp_path / "worker-status.json"
    target.write_text("old", encoding="utf-8")
    atomic_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert len(attempts) == 3
    assert not list(tmp_path.glob(".pending-*"))


def test_atomic_replace_preserves_old_file_on_permanent_error(tmp_path, monkeypatch):
    import interview_agent.files as files
    monkeypatch.setattr(files.os, "replace", lambda *_: (_ for _ in ()).throw(PermissionError("denied")))
    monkeypatch.setattr(files.time, "sleep", lambda _: None)
    target = tmp_path / "file.txt"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(PermissionError):
        atomic_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".pending-*"))
