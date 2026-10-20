"""Windows launcher and frozen worker/server entry points."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from interview_agent import __version__
from interview_agent.config import Settings, default_dir, environment
from interview_agent.files import atomic_json
from interview_agent.update_bundle import UpdateError, verify_folder, version


def resource_root() -> Path:
    return Path(__file__).resolve().parent


def command(mode: str, port: int, data_dir: Path) -> list[str]:
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
    return prefix + [mode, "--port", str(port), "--data-dir", str(data_dir)]


def load_settings(explicit: Path | None) -> Settings:
    target = explicit or default_dir()
    # Reuse this user's Codex-managed data on first Explorer launch, without moving it.
    if not explicit and not environment("DATA_DIR") and not (target / "app.db").exists():
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
        candidates = list(packages.glob("OpenAI.Codex_*/LocalCache/Local/InterviewAgent/app.db")) if packages.is_dir() else []
        if len(candidates) == 1:
            target = candidates[0].parent
    return Settings.load(target)


def available_port(preferred: int) -> int:
    for port in range(preferred, min(preferred + 20, 65536)):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("本地端口均被占用，请关闭其他实例或使用 --port 指定端口。")


def healthy(port: int) -> bool:
    try:
        # Local checks must not be sent through a system proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def server(settings: Settings, port: int):
    from streamlit.web import bootstrap
    flags = {
        "global.developmentMode": False,
        "server.address": "127.0.0.1", "server.port": port,
        "server.headless": True, "server.fileWatcherType": "none",
        "server.maxUploadSize": settings.max_upload_mb, "browser.gatherUsageStats": False,
        "theme.base": "light", "theme.primaryColor": "#087f75",
        "theme.backgroundColor": "#f5f7f9", "theme.secondaryBackgroundColor": "#edf2f4",
        "theme.textColor": "#1b3040",
    }
    bootstrap.load_config_options(flags)
    bootstrap.run(str(resource_root() / "app.py"), False, [], flags)


def stop_children(children):
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def prepare_update(settings, instance, *, headless, no_browser):
    from interview_agent.db import Store, uid
    from interview_agent.updates import read_state, status
    data = settings.data_dir
    request = data / "updates/install-request.json"
    if not request.exists():
        return False
    try:
        value = json.loads(request.read_text(encoding="utf-8"))
        state = read_state(data)
        if not getattr(sys, "frozen", False) or value.get("instance_token") != instance["instance_token"] or value.get("version") != state.get("version") or state.get("phase") != "waiting":
            raise UpdateError("更新请求已失效，请重新下载或确认安装。")
        if version(state["version"]) <= version(__version__):
            raise UpdateError("目标版本不高于当前版本。")
        store = Store(data)
        busy_query = "SELECT id FROM jobs WHERE status IN ('RUNNING','QUEUED','RETRY_WAIT') UNION ALL SELECT id FROM reference_answers WHERE status='RUNNING' AND lease_until>? LIMIT 1"
        if store.one(busy_query, (time.time(),)):
            return False
        staged = data / "updates" / ("v" + state["version"]) / "staged/interview-agent"
        verify_folder(staged, state["version"], state["manifest_hash"])
        install = Path(sys.executable).resolve().parent
        if data.resolve().is_relative_to(install) or install.is_relative_to(data.resolve()):
            raise UpdateError("自动更新要求知识库数据保存在程序目录之外。")
        token = uid()
        probe = install.parent / (".interview-agent-write-test-" + token)
        probe.touch(exist_ok=False)
        probe.unlink()
        gate = data / "updates/installing.json"
        atomic_json(gate, {"token": token})
        with store.transaction() as con:
            if con.execute(busy_query, (time.time(),)).fetchone():
                gate.unlink(missing_ok=True)
                return False
        plan = {"token": token, "version": state["version"], "old_version": __version__, "manifest_hash": state["manifest_hash"],
                "data_dir": str(data.resolve()), "install_dir": str(install), "staged_dir": str(staged.resolve()),
                "port": instance["port"], "parent_pid": os.getpid(), "headless": headless, "no_browser": no_browser}
        plan_path = data / "updates/install-plan.json"
        atomic_json(plan_path, plan)
        atomic_json(data / "updates/install-journal.json", {"token": token, "phase": "pending"})
        helper_dir = data / "updates" / ("helper-" + token)
        helper_dir.mkdir()
        helper = helper_dir / "interview-agent-updater.exe"
        shutil.copy2(install / helper.name, helper)
        subprocess.Popen([str(helper), "--plan", str(plan_path)], cwd=data, env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        request.unlink(missing_ok=True)
        return True
    except Exception as exc:
        (data / "updates/installing.json").unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        status(data, phase="failed", message=str(exc) if isinstance(exc, UpdateError) else "无法启动更新器，请检查程序目录、权限和磁盘空间。")
        return False


def recover_update(settings, update_token):
    data = settings.data_dir
    gate = data / "updates/installing.json"
    if not gate.exists():
        return
    value = json.loads(gate.read_text(encoding="utf-8"))
    if update_token and update_token == value.get("token"):
        return
    if not getattr(sys, "frozen", False):
        raise UpdateError("数据目录处于更新维护状态，请使用原 EXE 完成恢复。")
    plan_path = data / "updates/install-plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    helper = data / "updates" / ("helper-" + plan["token"]) / "interview-agent-updater.exe"
    subprocess.Popen([str(helper), "--plan", str(plan_path), "--recover", "--recover-parent", str(os.getpid())], cwd=data, env=dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    raise SystemExit(0)


def run_launcher(settings: Settings, port: int, *, headless=False, no_browser=False, update_token=""):
    from worker import single_worker
    state = settings.data_dir / "desktop-instance.json"
    stop_file = settings.data_dir / "desktop-stop"
    lock = single_worker(settings.data_dir / "desktop.lock")
    try:
        lock.__enter__()
    except SystemExit:
        if state.exists():
            previous = json.loads(state.read_text(encoding="utf-8"))
            previous_port = previous.get("port", 0)
            if isinstance(previous_port, int) and 1 <= previous_port <= 65535 and healthy(previous_port):
                if not no_browser:
                    webbrowser.open(f"http://127.0.0.1:{previous_port}")
                return
        raise RuntimeError("另一个启动器正在启动，请稍候再试。") from None
    children, streams = [], []
    window = None
    try:
        port = available_port(port)
        stop_file.unlink(missing_ok=True)
        logs = settings.data_dir / "logs"
        logs.mkdir(exist_ok=True)
        env = dict(os.environ, DATA_DIR=str(settings.data_dir), PYTHONUTF8="1")
        for mode, name in (("--worker", "desktop-worker.log"), ("--server", "desktop-server.log")):
            stream = open(logs / name, "ab")
            streams.append(stream)
            children.append(subprocess.Popen(command(mode, port, settings.data_dir), cwd=settings.data_dir, env=env, stdout=stream, stderr=stream, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
        from interview_agent.db import uid
        instance = {"pid": os.getpid(), "server_pid": children[1].pid, "worker_pid": children[0].pid, "port": port, "started_at": time.time(),
                    "instance_token": uid(), "frozen": bool(getattr(sys, "frozen", False)), "version": __version__, "update_token": update_token}
        atomic_json(state, instance)
        url = f"http://127.0.0.1:{port}"
        if not headless:
            import tkinter as tk
            from tkinter import ttk
            window = tk.Tk()
            window.title(f"面试知识库 v{__version__} · 本地服务")
            window.geometry("510x305")
            window.minsize(470, 290)
            window.configure(bg="#f5f7f9")
            frame = ttk.Frame(window, padding=24)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="面试知识库", font=("Microsoft YaHei UI", 19, "bold")).pack(anchor="w")
            status_text = tk.StringVar(value="正在启动本地服务…")
            ttk.Label(frame, textvariable=status_text, wraplength=440).pack(anchor="w", pady=(12, 8))
            ttk.Label(frame, text=url).pack(anchor="w")
            ttk.Label(frame, text="关闭浏览器不会停止处理；退出此窗口会停止本地服务。", wraplength=440).pack(anchor="w", pady=12)
            buttons = ttk.Frame(frame)
            buttons.pack(fill="x", pady=8)
            ttk.Button(buttons, text="打开页面", command=lambda: webbrowser.open(url)).pack(side="left")
            ttk.Button(buttons, text="打开日志", command=lambda: os.startfile(str(logs))).pack(side="left", padx=8)
            ttk.Button(buttons, text="停止服务并退出", command=window.destroy).pack(side="right")
        else:
            status_text = None
        started, opened = time.monotonic(), False

        def tick():
            nonlocal opened
            if stop_file.exists():
                return False
            if not (settings.data_dir / "updates/installing.json").exists() and prepare_update(settings, instance, headless=headless, no_browser=no_browser):
                return False
            if children[1].poll() is not None:
                raise RuntimeError("页面服务未能运行，请查看 logs/desktop-server.log。")
            # An existing source worker can own the same data directory. Retry the
            # lock after it exits; never terminate or duplicate that other worker.
            if children[0].poll() is not None:
                if children[0].returncode:
                    raise RuntimeError("后台任务进程启动失败，请查看 logs/desktop-worker.log。")
                children[0] = subprocess.Popen(command("--worker", port, settings.data_dir), cwd=settings.data_dir, env=env, stdout=streams[0], stderr=streams[0], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if not opened and healthy(port):
                opened = True
                if status_text:
                    status_text.set("本地服务已启动，任务会在后台处理。")
                if not no_browser:
                    webbrowser.open(url)
                if not update_token:
                    from interview_agent.updates import auto_check
                    auto_check(settings.data_dir)
            if not opened and time.monotonic() - started > 90:
                raise RuntimeError("启动等待超时，请查看本地日志。")
            return True

        if window:
            def refresh():
                try:
                    if tick():
                        window.after(2000, refresh)
                    else:
                        window.destroy()
                except Exception as exc:
                    from tkinter import messagebox
                    messagebox.showerror("启动失败", str(exc), parent=window)
                    window.destroy()
            window.after(100, refresh)
            window.mainloop()
        else:
            while tick():
                time.sleep(2)
    finally:
        stop_children(children)
        for stream in streams:
            stream.close()
        state.unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        lock.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--worker", action="store_true")
    modes.add_argument("--server", action="store_true")
    modes.add_argument("--stop", action="store_true")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--update-token", default="")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    settings = load_settings(args.data_dir)
    os.environ["DATA_DIR"] = str(settings.data_dir)
    # Windowed frozen executables have no stdout/stderr, even with redirected handles.
    logs = settings.data_dir / "logs"
    logs.mkdir(exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(logs / "desktop-console.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    if args.stop:
        (settings.data_dir / "desktop-stop").touch()
    elif args.worker:
        from worker import main as worker_main
        worker_main()
    elif args.server:
        server(settings, args.port)
    else:
        try:
            recover_update(settings, args.update_token)
            run_launcher(settings, args.port, headless=args.headless, no_browser=args.no_browser, update_token=args.update_token)
        except Exception:
            import traceback
            traceback.print_exc()
            if not args.headless:
                from tkinter import messagebox
                messagebox.showerror("面试知识库启动失败", "请查看数据目录 logs 下的 desktop 日志。")
            raise SystemExit(1) from None


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        raise SystemExit(1) from None
