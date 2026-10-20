"""GitHub release checks and downloads; installation is owned by desktop/updater."""
from __future__ import annotations

import json
import re
import shutil
import sys
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import __version__
from .files import atomic_json, read_json
from .update_bundle import UpdateError, extract_bundle, sha256, version, MANIFEST

REPOSITORY = "GloriousAndNew/interview-agent"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
MAX_DOWNLOAD = 800 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, source, code, message, headers, url):
        return None


class _NetworkResponse:
    def __init__(self, raw):
        self.raw = raw
        self.status_code = raw.code
        self.headers = raw.headers
        self.url = httpx.URL(raw.geturl())

    def iter_bytes(self, chunk_size=65536):
        while block := self.raw.read(chunk_size):
            yield block

    def close(self):
        self.raw.close()


class NetworkClient:
    """Use Windows proxy/certificate settings; redirects remain validated here."""
    def __init__(self, timeout=30):
        self.opener = urllib.request.build_opener(_NoRedirect())
        self.timeout = timeout

    def build_request(self, method, url, headers):
        return urllib.request.Request(url, headers=headers, method=method)

    def send(self, request, **kwargs):
        try:
            raw = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raw = exc
        return _NetworkResponse(raw)

    def close(self):
        pass


def folder(data: Path) -> Path:
    path = data / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_state(data: Path, name="status.json") -> dict:
    try:
        return read_json(folder(data) / name)
    except (OSError, ValueError):
        return {}


def status(data, **values):
    atomic_json(folder(data) / "status.json", dict(values, updated_at=time.time()))


def allowed_url(url: str, *, asset=False):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise UpdateError("更新下载地址无效。")
    if asset:
        if host != "github.com" or not parsed.path.startswith(f"/{REPOSITORY}/releases/download/"):
            raise UpdateError("更新包不属于指定 GitHub 仓库。")
    elif host not in ("api.github.com", "github.com") and not host.endswith(".githubusercontent.com"):
        raise UpdateError("更新服务器重定向到非受信任地址。")


def response(client, url):
    for _ in range(6):
        allowed_url(url)
        request = client.build_request("GET", url, headers={"User-Agent": f"InterviewAgent/{__version__}", "Accept": "application/vnd.github+json" if url == API_URL else "application/octet-stream"})
        result = client.send(request, stream=True, follow_redirects=False)
        if result.status_code in (301, 302, 303, 307, 308):
            url = str(result.url.join(result.headers.get("location", "")))
            result.close()
            continue
        if result.status_code != 200:
            code = result.status_code
            result.close()
            raise UpdateError("GitHub 请求失败" + ("（暂无正式发布版本）。" if code == 404 else f"（HTTP {code}），请稍后重试。"))
        return result
    raise UpdateError("更新下载重定向次数过多。")


def small_get(client, url, limit):
    result = response(client, url)
    try:
        output = bytearray()
        for block in result.iter_bytes():
            output.extend(block)
            if len(output) > limit:
                raise UpdateError("更新服务器返回内容超出限制。")
        return bytes(output)
    finally:
        result.close()


def latest(client, current=__version__):
    release = json.loads(small_get(client, API_URL, 2 * 1024 * 1024))
    target = release.get("tag_name", "")
    version(target)
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError("仅支持正式发布版本。")
    if version(target) <= version(current):
        return {"available": False, "remote_version": target.removeprefix("v")}
    release_version = target.removeprefix("v")
    name = f"interview-agent-v{release_version}-windows-x64.zip"
    assets = {item["name"]: item for item in release.get("assets", [])}
    asset = assets.get(name)
    if not asset or asset.get("state") != "uploaded" or not 0 < asset.get("size", 0) <= MAX_DOWNLOAD:
        raise UpdateError("新版尚未提供完整的 Windows x64 程序包。")
    url = asset.get("browser_download_url", "")
    allowed_url(url, asset=True)
    if urlsplit(url).path != f"/{REPOSITORY}/releases/download/{target}/{name}":
        raise UpdateError("更新包版本与发布标签不匹配。")
    digest = asset.get("digest") or ""
    if not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest):
        checksum = assets.get(name + ".sha256", {})
        checksum_url = checksum.get("browser_download_url", "")
        allowed_url(checksum_url, asset=True)
        if urlsplit(checksum_url).path != f"/{REPOSITORY}/releases/download/{target}/{name}.sha256":
            raise UpdateError("更新校验文件与版本不匹配。")
        line = small_get(client, checksum_url, 4096).decode("ascii").strip()
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?" + re.escape(name), line)
        if not match:
            raise UpdateError("新版 SHA-256 校验文件无效。")
        digest = "sha256:" + match[1]
    return {"available": True, "version": release_version, "url": url, "size": asset["size"], "sha256": digest[7:].lower(),
            "notes": str(release.get("body") or "暂无更新说明。")[:12000], "published_at": str(release.get("published_at") or "")}


def check(data: Path, *, client=None):
    own = client is None
    client = client or NetworkClient(timeout=15)
    try:
        result = dict(latest(client), checked_at=time.time())
    except UpdateError as exc:
        result = {"error": str(exc), "checked_at": time.time()}
    except Exception:
        result = {"error": "检查更新失败，请检查 GitHub 网络连接后重试。", "checked_at": time.time()}
    finally:
        if own:
            client.close()
    atomic_json(folder(data) / "check.json", result)
    return result


def auto_check(data):
    preferences = read_state(data, "preferences.json")
    if preferences.get("auto_check") and time.time() - read_state(data, "check.json").get("checked_at", 0) > 86400:
        threading.Thread(target=check, args=(data,), daemon=True).start()


def download(data: Path, *, client=None):
    from worker import single_worker
    try:
        with single_worker(folder(data) / "download.lock"):
            _download(data, client=client)
    except SystemExit:
        return


def _download(data, *, client=None):
    own = client is None
    client = client or NetworkClient(timeout=30)
    try:
        # Re-fetch trusted release data instead of trusting editable cached URLs.
        candidate = latest(client)
        if not candidate["available"]:
            raise UpdateError("当前没有可下载的新版本。")
        if (folder(data) / "installing.json").exists():
            raise UpdateError("正在安装更新，请稍候。")
        target = folder(data) / ("v" + candidate["version"])
        target.mkdir(exist_ok=True)
        partial = target / "package.zip.part"
        archive = target / "package.zip"
        status(data, phase="downloading", version=candidate["version"], received=0, total=candidate["size"])
        started, tick = time.monotonic(), 0
        received = 0
        result = response(client, candidate["url"])
        try:
            with partial.open("wb") as output:
                for block in result.iter_bytes(1024 * 1024):
                    received += len(block)
                    if received > candidate["size"] or time.monotonic() - started > 2700:
                        raise UpdateError("更新包大小不匹配或下载超时。")
                    output.write(block)
                    if time.monotonic() - tick > 0.5:
                        tick = time.monotonic()
                        status(data, phase="downloading", version=candidate["version"], received=received, total=candidate["size"])
        finally:
            result.close()
        if received != candidate["size"] or sha256(partial) != candidate["sha256"]:
            raise UpdateError("更新包 SHA-256 或大小校验失败，请重新下载。")
        partial.replace(archive)
        status(data, phase="verifying", version=candidate["version"])
        stage = target / "staged"
        if stage.exists():
            shutil.rmtree(stage)
        app = extract_bundle(archive, stage, candidate["version"])
        status(data, phase="ready", version=candidate["version"], manifest_hash=sha256(app / MANIFEST), message="下载与完整性校验完成，可安装。")
    except UpdateError as exc:
        status(data, phase="failed", message=str(exc))
    except Exception:
        status(data, phase="failed", message="新版下载或解压失败，请检查网络、磁盘空间和目录权限。")
    finally:
        if own:
            client.close()


def start_download(data):
    threading.Thread(target=download, args=(data,), daemon=True).start()


def request_install(data):
    state = read_state(data)
    instance = read_json(data / "desktop-instance.json")
    if not getattr(sys, "frozen", False) or not instance.get("frozen"):
        raise UpdateError("自动安装仅支持 EXE 启动方式，请使用新版完整程序包。")
    if state.get("phase") != "ready" or version(state.get("version", "")) <= version(__version__):
        raise UpdateError("请先下载并校验更新包。")
    atomic_json(folder(data) / "install-request.json", {"version": state["version"], "instance_token": instance["instance_token"]})
    status(data, **dict(state, phase="waiting", message="已确认安装，等待当前任务全部结束。"))
