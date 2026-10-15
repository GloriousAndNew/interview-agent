from __future__ import annotations

import os
import time
from contextlib import contextmanager

from interview_agent.config import Settings
from interview_agent.db import Store
from interview_agent.files import atomic_json
from interview_agent.pipeline import Pipeline


@contextmanager
def single_worker(path):
    handle = open(path, "a+b")
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            raise SystemExit(0)
    else:
        import fcntl
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SystemExit(0)
    try:
        yield
    finally:
        handle.close()


def main():
    settings = Settings.load()
    with single_worker(settings.data_dir / "worker.lock"):
        store = Store(settings.data_dir)
        while True:
            # Settings are reloaded between jobs; keys never enter the database.
            settings = Settings.load()
            pipeline = Pipeline(settings, store)
            try:
                atomic_json(settings.data_dir / "worker-status.json", {"pid": os.getpid(), "updated": time.time()})
            except OSError:
                # A status-file sharing conflict must not terminate the worker.
                pass
            if pipeline.run_once():
                pipeline.maintenance()
                continue
            pipeline.maintenance()
            time.sleep(2)


if __name__ == "__main__":
    main()
