"""Exercise the real independent updater with disposable data and two EXE bundles."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop import healthy
from interview_agent.db import Store, uid
from interview_agent.files import atomic_json, read_json
from interview_agent.update_bundle import MANIFEST, extract_bundle, safe_name, sha256


def wait_until(predicate, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise RuntimeError("Verification timeout")


def main():
    repo = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", type=Path, default=repo / "dist/interview-agent-v0.1.2-windows-x64.zip")
    parser.add_argument("--new", type=Path, default=repo / "dist/interview-agent-v0.1.3-windows-x64.zip")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="interview-update-check-")).resolve()
    data = root / "独立数据"
    data.mkdir()
    (data / "logs").mkdir()
    with ZipFile(args.old) as archive:
        for info in archive.infolist():
            name = safe_name(info.filename)
            if not name.startswith("interview-agent/"):
                raise RuntimeError("Invalid old bundle")
        archive.extractall(root)
    app = root / "interview-agent"
    staged = extract_bundle(args.new, data / "updates/v0.1.3/staged", "0.1.3")
    store = Store(data)
    interview, job = store.create("更新保留样本", {}, {}, {})
    with store.transaction() as con:
        con.execute("UPDATE jobs SET status='FAILED' WHERE id=?", (job,))
    env = dict(os.environ, DATA_DIR=str(data), PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
               deepseek="", AliApiKey="", OSS_ACCESS_KEY_ID="", OSS_ACCESS_KEY_SECRET="", PYINSTALLER_RESET_ENVIRONMENT="1")
    env.pop("PYTHONPATH", None)
    port = 8547
    launcher = subprocess.Popen([str(app / "interview-agent.exe"), "--headless", "--no-browser", "--port", str(port), "--data-dir", str(data)], env=env, cwd=data)
    report = {"cloud_calls": 0, "root": str(root)}
    try:
        wait_until(lambda: (data / "desktop-instance.json").exists() and healthy(port))
        (data / "desktop-stop").touch()
        launcher.wait(timeout=30)
        token = uid()
        helper_dir = data / "updates" / ("helper-" + token)
        helper_dir.mkdir()
        helper = helper_dir / "interview-agent-updater.exe"
        shutil.copy2(staged / helper.name, helper)
        plan = {"token": token, "version": "0.1.3", "old_version": "0.1.2", "data_dir": str(data), "install_dir": str(app),
                "staged_dir": str(staged), "manifest_hash": sha256(staged / MANIFEST), "parent_pid": 0, "port": port, "headless": True, "no_browser": True}
        atomic_json(data / "updates/install-plan.json", plan)
        atomic_json(data / "updates/installing.json", {"token": token})
        atomic_json(data / "updates/install-journal.json", {"token": token, "phase": "pending"})
        updater = subprocess.Popen([str(helper), "--plan", str(data / "updates/install-plan.json")], cwd=data, env=env)
        updater.wait(timeout=150)
        state = read_json(data / "updates/status.json")
        assert updater.returncode == 0 and state["phase"] == "completed", state
        instance = read_json(data / "desktop-instance.json")
        assert instance["version"] == "0.1.3" and instance["update_token"] == token
        assert healthy(instance["port"])
        assert not (data / "updates/installing.json").exists()
        with closing(sqlite3.connect(data / "app.db")) as con:
            assert con.execute("SELECT title FROM interviews WHERE id=?", (interview,)).fetchone()[0] == "更新保留样本"
            assert con.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()[0] == "FAILED"
        assert (data / "updates/database-before-update.db").exists()
        assert (Path(state["backup_dir"]) / "interview-agent.exe").exists()
        report.update(status="ok", old_version="0.1.2", new_version="0.1.3", database_preserved=True, backup=True,
                      independent_updater=True, health="ok", no_python_on_path=True)
    finally:
        (data / "desktop-stop").touch()
        if launcher.poll() is None:
            launcher.wait(timeout=30)
        wait_until(lambda: not (data / "desktop-instance.json").exists(), 30)
        (repo / "build/frozen-update-smoke.json").write_text(json.dumps(report), encoding="utf-8")
        if not args.keep:
            if root.parent != Path(tempfile.gettempdir()).resolve() or not root.name.startswith("interview-update-check-"):
                raise RuntimeError("Cleanup boundary mismatch")
            shutil.rmtree(root)
    print(json.dumps(report))


if __name__ == "__main__":
    main()
