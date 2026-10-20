"""Explicit, small OSS connection test, independent of ASR and original media."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from .db import uid
from .files import atomic_json
from .providers import OSSStorage, ProviderError
from .uploads import PART_SIZE, upload_summary


def test_upload(settings, store, progress=lambda _: None, *, storage_factory=OSSStorage):
    storage = storage_factory(settings)
    key, cleanup_id = f"interview-agent/upload-tests/{uid()}.bin", uid()
    state = {}
    started = time.monotonic()
    config = {"oss_bucket": settings.oss_bucket, "oss_endpoint": settings.oss_endpoint}
    with store.transaction() as con:
        con.execute("INSERT INTO cleanup(id,job_id,object_key,config,eligible) VALUES (?,NULL,?,?,?)", (cleanup_id, key, json.dumps(config), time.time() + 49 * 3600))

    def save(value):
        config["upload_id"] = value.get("upload_id")
        with store.transaction() as con:
            con.execute("UPDATE cleanup SET config=? WHERE id=?", (json.dumps(config), cleanup_id))
        progress(upload_summary({"upload_state": value}))

    result = {"created_at": time.time(), "size_bytes": PART_SIZE, "status": "failed", "cleanup_pending": True, "cleanup_id": cleanup_id}
    try:
        with tempfile.TemporaryDirectory(prefix="interview-upload-test-") as directory:
            path = Path(directory) / "test.bin"
            path.write_bytes(os.urandom(PART_SIZE))
            storage.upload(key, path, state=state, save=save)
        result.update(status="completed", upload=upload_summary({"upload_state": state}))
    except ProviderError as exc:
        result["error"] = str(exc)
    except Exception:
        result["error"] = "上传测试未完成，请检查网络、OSS 配置或本地数据目录权限。"
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        try:
            if state.get("upload_id"):
                storage.abort(key, state["upload_id"])
            storage.delete(key)
            with store.transaction() as con:
                con.execute("UPDATE cleanup SET status='done' WHERE id=?", (cleanup_id,))
            result["cleanup_pending"] = False
        except Exception:
            with store.transaction() as con:
                con.execute("UPDATE cleanup SET eligible=? WHERE id=?", (time.time(), cleanup_id))
        atomic_json(settings.data_dir / "upload-test.json", result)
    return result
