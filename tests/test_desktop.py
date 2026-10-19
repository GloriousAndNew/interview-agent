import socket
import sys
from pathlib import Path

import desktop


def test_frozen_children_use_explicit_modes_not_python_arguments(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Application/interview-agent.exe")
    args = desktop.command("--worker", 8502, tmp_path)
    assert args == ["C:/Application/interview-agent.exe", "--worker", "--port", "8502", "--data-dir", str(tmp_path)]
    monkeypatch.setattr(sys, "frozen", False)
    assert Path(desktop.command("--server", 8502, tmp_path)[1]).name == "desktop.py"


def test_occupied_port_is_skipped():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert desktop.available_port(port) != port


def test_existing_data_and_explicit_path_take_priority(monkeypatch, tmp_path):
    base = tmp_path / "default"
    old = tmp_path / "Packages/OpenAI.Codex_example/LocalCache/Local/InterviewAgent"
    old.mkdir(parents=True)
    (old / "app.db").touch()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(desktop, "default_dir", lambda: base)
    monkeypatch.setattr(desktop, "environment", lambda name: "")
    monkeypatch.setattr(desktop.Settings, "load", lambda path: path)
    assert desktop.load_settings(None) == old
    explicit = tmp_path / "explicit"
    assert desktop.load_settings(explicit) == explicit
    base.mkdir()
    (base / "app.db").touch()
    assert desktop.load_settings(None) == base


def test_exit_stops_owned_active_children_only():
    class Child:
        def __init__(self, status):
            self.status, self.terminated, self.waited = status, False, False
        def poll(self):
            return self.status
        def terminate(self):
            self.terminated = True
        def wait(self, timeout):
            self.waited = True
    running, exited = Child(None), Child(0)
    desktop.stop_children([running, exited])
    assert running.terminated and running.waited
    assert not exited.terminated
