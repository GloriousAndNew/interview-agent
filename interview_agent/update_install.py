"""Independent installer: stdlib only, never imports the application/database model."""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

from .files import atomic_json, read_json
from .update_bundle import MANIFEST, UpdateError, sha256, verify_folder, version


@contextlib.contextmanager
def lock_file(path):
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise UpdateError("服务或另一个更新器仍在运行，未替换文件。") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise UpdateError("服务或另一个更新器仍在运行，未替换文件。") from None
        yield
    finally:
        stream.close()


@contextlib.contextmanager
def service_locks(data):
    with lock_file(data / "desktop.lock"), lock_file(data / "worker.lock"):
        yield


def validate_plan(path):
    path = path.resolve()
    plan = read_json(path)
    data, install, staged = (Path(plan[k]).resolve() for k in ("data_dir", "install_dir", "staged_dir"))
    token = plan.get("token", "")
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise UpdateError("更新事务标识无效。")
    if path != data / "updates" / "install-plan.json" or staged != data / "updates" / ("v" + plan["version"]) / "staged/interview-agent":
        raise UpdateError("更新计划不在指定数据目录。")
    if install.parent == install or install.is_relative_to(data) or data.is_relative_to(install):
        raise UpdateError("程序与数据目录必须分开，未执行目录替换。")
    if version(plan["version"]) <= version(plan["old_version"]) or not 1 <= plan["port"] <= 65535:
        raise UpdateError("更新目标版本或端口无效。")
    for name in ("install_dir", "staged_dir", "data_dir"):
        plan[name] = str(Path(plan[name]).resolve())
    return plan


def paths(plan):
    data, install = Path(plan["data_dir"]), Path(plan["install_dir"])
    # All rename/removal destinations are fixed children of the checked parent.
    incoming = install.parent / (".interview-agent-incoming-" + plan["token"])
    backup = install.parent / (".interview-agent-backup-" + plan["token"])
    failed = install.parent / (".interview-agent-failed-" + plan["token"])
    for target in (incoming, backup, failed):
        if target.resolve().parent != install.parent.resolve() or target == install:
            raise UpdateError("更新临时目录无效。")
    return data, install, incoming, backup, failed


def write_status(data, phase, message, **kwargs):
    atomic_json(data / "updates/status.json", dict(phase=phase, message=message, updated_at=time.time(), **kwargs))


def journal(data, plan, phase, **kwargs):
    atomic_json(data / "updates/install-journal.json", dict(token=plan["token"], phase=phase, **kwargs))


def wait_parent(pid, timeout=90):
    if not pid:
        return
    if os.name != "nt":
        raise UpdateError("自动安装仅支持 Windows。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, timeout * 1000) != 0:
                raise UpdateError("主程序未能退出，取消文件替换。")
        finally:
            kernel.CloseHandle(handle)


def launch(plan, *, updated=True):
    data = Path(plan["data_dir"])
    args = [str(Path(plan["install_dir"]) / "interview-agent.exe"), "--data-dir", str(data), "--port", str(plan["port"])]
    if updated:
        args += ["--update-token", plan["token"]]
    if plan.get("headless"):
        args += ["--headless"]
    if plan.get("no_browser"):
        args += ["--no-browser"]
    env = dict(os.environ, DATA_DIR=str(data), PYINSTALLER_RESET_ENVIRONMENT="1")
    return subprocess.Popen(args, cwd=data, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def healthy_new(plan, process, timeout=90):
    data = Path(plan["data_dir"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            return False
        try:
            instance = read_json(data / "desktop-instance.json")
            worker = read_json(data / "worker-status.json")
            if instance.get("update_token") == plan["token"] and instance.get("version") == plan["version"] and worker.get("pid") == instance["worker_pid"] and time.time() - worker["updated"] < 20:
                with opener.open(f"http://127.0.0.1:{instance['port']}/_stcore/health", timeout=1) as result:
                    if result.status == 200:
                        return True
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.5)
    return False


def stop_new(data, process):
    (data / "desktop-stop").touch()
    try:
        process.wait(timeout=25)
    except subprocess.TimeoutExpired:
        # Do not replace files or restore a database while unknown children live.
        raise UpdateError("新版未正常退出，已保留维护状态和备份，请查看更新日志。") from None


def database_backup(data):
    original = data / "app.db"
    target = data / "updates" / "database-before-update.db"
    if original.exists():
        with contextlib.closing(sqlite3.connect(original)) as source, contextlib.closing(sqlite3.connect(target)) as output:
            source.backup(output)
            if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise UpdateError("更新前数据库备份校验失败。")
        return True
    return False


def restore_database(data, had_database):
    database = data / "app.db"
    snapshot = data / "updates/database-before-update.db"
    if had_database:
        temporary = data / "app.db.update-restore"
        shutil.copy2(snapshot, temporary)
        temporary.replace(database)
    else:
        database.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)


def restore_program(plan, had_database, *, restore_db=False):
    data, install, incoming, backup, failed = paths(plan)
    if backup.exists():
        if install.exists():
            install.rename(failed)
        backup.rename(install)
    if restore_db:
        restore_database(data, had_database)
    if incoming.exists():
        shutil.rmtree(incoming)


def recover(plan_path):
    plan = validate_plan(plan_path)
    data = Path(plan["data_dir"])
    with lock_file(data / "updates/install.lock"), service_locks(data):
        entry = read_json(data / "updates/install-journal.json") if (data / "updates/install-journal.json").exists() else {}
        if entry.get("token") != plan["token"]:
            raise UpdateError("更新恢复记录不匹配，已保留维护状态。")
        if entry.get("phase") not in ("committed", "rolled_back"):
            restore_program(plan, entry.get("had_database", True), restore_db=entry.get("phase") in ("swapping", "launching", "restoring"))
            journal(data, plan, "rolled_back", had_database=entry.get("had_database", True))
            write_status(data, "rolled_back", "检测到上次更新中断，已恢复旧程序和数据库。")
        (data / "updates/installing.json").unlink(missing_ok=True)


def install(plan_path, *, launch_fn=launch, health_fn=healthy_new):
    plan = validate_plan(plan_path)
    data, install_dir, incoming, backup, failed = paths(plan)
    process = None
    had_database = True
    swapped = False
    with lock_file(data / "updates/install.lock"):
        try:
            wait_parent(plan.get("parent_pid", 0))
            with service_locks(data):
                if not (install_dir / "interview-agent.exe").is_file() or not (install_dir / "_internal").is_dir():
                    raise UpdateError("当前目录不是完整的应用程序包。")
                verify_folder(Path(plan["staged_dir"]), plan["version"], plan["manifest_hash"])
                if incoming.exists() or backup.exists() or failed.exists():
                    raise UpdateError("更新临时目录已存在，请先恢复上次更新。")
                write_status(data, "installing", "正在备份数据库并准备替换程序。", version=plan["version"])
                journal(data, plan, "preparing")
                had_database = database_backup(data)
                shutil.copytree(plan["staged_dir"], incoming)
                verify_folder(incoming, plan["version"], plan["manifest_hash"])
                journal(data, plan, "swapping", had_database=had_database)
                install_dir.rename(backup)
                swapped = True
                incoming.rename(install_dir)
                journal(data, plan, "launching", had_database=had_database)
            write_status(data, "restarting", "新版正在启动并验证服务。", version=plan["version"])
            process = launch_fn(plan)
            if not health_fn(plan, process):
                raise UpdateError("新版服务未通过启动验证，正在恢复旧版本。")
            journal(data, plan, "committed", had_database=had_database)
            write_status(data, "completed", "更新成功，已启动新版。", version=plan["version"], backup_dir=str(backup))
            (data / "updates/installing.json").unlink(missing_ok=True)
            return True
        except Exception as exc:
            if process is not None:
                stop_new(data, process)
            try:
                with service_locks(data):
                    if swapped:
                        journal(data, plan, "restoring", had_database=had_database)
                    restore_program(plan, had_database, restore_db=swapped)
                    journal(data, plan, "rolled_back", had_database=had_database)
                    (data / "updates/installing.json").unlink(missing_ok=True)
            except Exception:
                write_status(data, "repair_required", "自动恢复未完成，已保留维护状态及程序/数据库备份，请查看更新日志。")
                raise
            message = str(exc) if isinstance(exc, UpdateError) else "更新失败，原程序与数据已保留。请检查目录权限和磁盘空间。"
            write_status(data, "rolled_back", message)
            if swapped or (install_dir / "interview-agent.exe").exists():
                launch_fn(plan, updated=False)
            return False
