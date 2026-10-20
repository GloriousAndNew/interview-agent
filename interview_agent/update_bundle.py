"""Standard-library bundle validation shared with the independent updater."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

APP_ID = "interview-agent"
MANIFEST = "update-manifest.json"
MAX_UNPACKED = 2 * 1024 ** 3


class UpdateError(RuntimeError):
    pass


def version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"v?\d{1,5}\.\d{1,5}\.\d{1,5}", value):
        raise UpdateError("版本号不是受支持的正式版本格式。")
    return tuple(map(int, value.removeprefix("v").split(".")))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    if not name or "\\" in name or ":" in name or "\x00" in name or path.is_absolute() or ".." in path.parts:
        raise UpdateError("更新包包含不安全路径。")
    for part in path.parts:
        if part.endswith((".", " ")) or part.split(".")[0].upper() in reserved or any(ord(c) < 32 or c in '<>"|?*' for c in part):
            raise UpdateError("更新包包含不支持的 Windows 文件名。")
    return path.as_posix()


def write_manifest(folder: Path, release_version: str):
    version(release_version)
    files = {f.relative_to(folder).as_posix(): sha256(f) for f in sorted(folder.rglob("*")) if f.is_file() and f.name != MANIFEST}
    value = {"format": 1, "app_id": APP_ID, "platform": "windows-x64", "version": release_version, "files": files}
    (folder / MANIFEST).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_folder(folder: Path, release_version: str, manifest_hash: str | None = None):
    manifest_path = folder / MANIFEST
    if not manifest_path.is_file():
        raise UpdateError("更新包缺少版本清单。")
    if manifest_hash and sha256(manifest_path) != manifest_hash:
        raise UpdateError("更新清单校验失败。")
    if manifest_path.stat().st_size > 8 * 1024 * 1024:
        raise UpdateError("更新清单过大。")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (value.get("format"), value.get("app_id"), value.get("platform"), value.get("version")) != (1, APP_ID, "windows-x64", release_version):
        raise UpdateError("更新包与目标版本或平台不匹配。")
    files = value.get("files")
    if not isinstance(files, dict) or not {"interview-agent.exe", "interview-agent-updater.exe"}.issubset(files):
        raise UpdateError("更新包缺少程序或独立更新器。")
    actual = set()
    for path in folder.rglob("*"):
        if path.is_symlink():
            raise UpdateError("更新包包含符号链接。")
        if path.is_file() and path.name != MANIFEST:
            name = safe_name(path.relative_to(folder).as_posix())
            if not path.resolve().is_relative_to(folder.resolve()):
                raise UpdateError("更新文件超出程序目录。")
            actual.add(name)
    if actual != set(files) or not any(name.startswith("_internal/") for name in actual):
        raise UpdateError("更新包文件列表不完整或包含额外文件。")
    for name, expected in files.items():
        if safe_name(name) != name or not isinstance(expected, str) or not re.fullmatch("[a-f0-9]{64}", expected) or sha256(folder / name) != expected:
            raise UpdateError("更新文件完整性校验失败，请重新下载。")
    return value


def extract_bundle(archive: Path, destination: Path, release_version: str):
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with ZipFile(archive) as zipped:
            infos = zipped.infolist()
            if len(infos) > 20000 or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise UpdateError("更新包解压大小超出限制。")
            names = set()
            for info in infos:
                name = safe_name(info.filename)
                if not name.startswith(APP_ID + "/") or (info.external_attr >> 16) & 0o170000 not in (0, 0o100000, 0o040000):
                    raise UpdateError("更新包结构无效。")
                key = name.rstrip("/").casefold()
                if key in names:
                    raise UpdateError("更新包包含重复路径。")
                names.add(key)
            for info in infos:
                target = destination / safe_name(info.filename)
                if not target.resolve().is_relative_to(destination.resolve()):
                    raise UpdateError("更新路径超出临时目录。")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(info) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
        folder = destination / APP_ID
        verify_folder(folder, release_version)
        return folder
    except BaseException:
        shutil.rmtree(destination)
        raise
