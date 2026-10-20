"""Offline executable check; uses disposable data and explicitly disables API keys."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import wave


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, default=Path(__file__).resolve().parents[1] / "dist/interview-agent/interview-agent.exe")
    parser.add_argument("--port", type=int, default=8537)
    parser.add_argument("--keep-running", action="store_true")
    args = parser.parse_args()
    executable = args.exe.resolve()
    temporary = Path(tempfile.mkdtemp(prefix="interview-exe-check-"))
    data = temporary / "独立测试数据"
    env = dict(os.environ, DATA_DIR=str(data), PATH=str(Path(os.environ["SystemRoot"]) / "System32"))
    env.pop("PYTHONPATH", None)
    for key in ("deepseek", "AliApiKey", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_SESSION_TOKEN"):
        env[key] = ""
    process = subprocess.Popen([str(executable), "--headless", "--no-browser", "--port", str(args.port), "--data-dir", str(data)], cwd=temporary, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    keep = False
    try:
        deadline = time.monotonic() + 60
        state = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Launcher exited ({process.returncode}); logs: {data / 'logs'}")
            try:
                state = json.loads((data / "desktop-instance.json").read_text())
                with opener.open(f"http://127.0.0.1:{state['port']}/_stcore/health", timeout=1) as response:
                    assert response.status == 200
                worker = json.loads((data / "worker-status.json").read_text())
                if time.time() - worker["updated"] < 15:
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError(f"Executable startup timed out; logs: {data / 'logs'}")
        with opener.open(f"http://127.0.0.1:{state['port']}/", timeout=5) as response:
            assert b"<html" in response.read().lower()
        ffmpeg = next((executable.parent / "_internal/imageio_ffmpeg/binaries").glob("*.exe"))
        source, compressed, output = temporary / "示例录音.wav", temporary / "upload-copy.mp3", temporary / "converted.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0\0" * 16000)
        encoded = subprocess.run([str(ffmpeg), "-v", "error", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "64k", str(compressed)], capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        assert encoded.returncode == 0 and compressed.stat().st_size < source.stat().st_size
        result = subprocess.run([str(ffmpeg), "-v", "error", "-y", "-i", str(compressed), str(output)], capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0
        with wave.open(str(output), "rb") as audio:
            assert audio.getframerate() == 16000 and audio.getnframes() == 16000
        report = {"health": "ok", "worker": "ok", "ffmpeg": "ok", "mp3_compression": "ok", "no_python_on_path": True, "no_cloud_calls": True, "port": state["port"], "data_dir": str(data), "launcher_pid": process.pid, "server_pid": state["server_pid"], "worker_pid": state["worker_pid"]}
        print(json.dumps(report, ensure_ascii=True), flush=True)
        keep = args.keep_running
    finally:
        if not keep:
            data.mkdir(parents=True, exist_ok=True)
            (data / "desktop-stop").touch()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                raise RuntimeError("Launcher shutdown timed out") from None


if __name__ == "__main__":
    main()
