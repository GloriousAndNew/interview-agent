"""Start UI + one worker, both bound to the same local data directory."""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

from interview_agent.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    root = Path(__file__).resolve().parent
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            raise SystemExit(f"端口 {args.port} 已被使用。请打开已有应用或选择另一端口。")
    environment = dict(os.environ, DATA_DIR=str(settings.data_dir), PYTHONUTF8="1")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    with open(log_dir / "worker.log", "ab") as log:
        worker = subprocess.Popen([sys.executable, str(root / "worker.py")], cwd=root, env=environment, stdout=log, stderr=log, creationflags=flags)
        try:
            print(f"本地面试知识库：http://127.0.0.1:{args.port}", flush=True)
            subprocess.run([sys.executable, "-m", "streamlit", "run", str(root / "app.py"), "--server.address=127.0.0.1", f"--server.port={args.port}", f"--server.maxUploadSize={settings.max_upload_mb}", "--browser.gatherUsageStats=false", f"--server.headless={'true' if args.headless else 'false'}"], cwd=root, env=environment, check=False)
        finally:
            if worker.poll() is None:
                # Termination leaves durable task leases/checkpoints for restart.
                worker.terminate()
                worker.wait(timeout=10)


if __name__ == "__main__":
    main()
