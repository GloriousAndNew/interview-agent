"""Persistent multipart upload; progress contains no signed URLs or credentials."""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

import oss2


PART_SIZE = 1024 * 1024
METADATA_HASH = "x-oss-meta-source-sha256"


def multipart_upload(bucket, key: str, path: Path, state: dict, save, check):
    """Save session before sending parts; reconcile server state after any restart.

    `save(state)` is mandatory at session/part boundaries, throttled within a part.
    Callbacks can raise cancellation or lease errors; these must reach the worker.
    """
    check()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(PART_SIZE), b""):
            check()
            digest.update(block)
    fingerprint, total = digest.hexdigest(), path.stat().st_size
    if not total:
        raise ValueError("上传副本为空")
    if state.get("sha256") and (state["sha256"] != fingerprint or state["total_bytes"] != total):
        raise ValueError("上传副本发生变化，请重新导入录音；未继续混合不同文件的分片。")
    previous = state.get("attempts", [])
    if previous and previous[-1]["status"] == "uploading":
        previous[-1]["status"] = "interrupted"
    attempt = {"started_at": time.time(), "status": "uploading", "sent_bytes": 0, "elapsed_seconds": 0.0, "average_bps": 0.0}
    state.update(sha256=fingerprint, total_bytes=total, part_size=state.get("part_size", PART_SIZE),
                 started_at=state.get("started_at", time.time()), attempts=previous + [attempt],
                 status="uploading", current_bps=0.0, updated_at=time.time())
    state["resume_count"] = len(state["attempts"]) - 1
    started = last_tick = time.monotonic()
    last_sent = 0
    confirmed = 0

    def publish(sent=None, *, force=False):
        nonlocal last_tick, last_sent
        check()
        now = time.monotonic()
        if sent is not None:
            attempt["sent_bytes"] = sent
        if not force and now - last_tick < 1:
            return
        elapsed = max(0.000001, now - started)
        rate = max(0, attempt["sent_bytes"] - last_sent) / max(0.000001, now - last_tick)
        attempt.update(elapsed_seconds=round(elapsed, 3), average_bps=round(attempt["sent_bytes"] / elapsed, 1))
        state.update(confirmed_bytes=confirmed, transferred_bytes=min(total, confirmed + state.get("part_inflight", 0)),
                     current_bps=round(rate, 1), updated_at=time.time(),
                     active_seconds=round(sum(a["elapsed_seconds"] for a in state["attempts"]), 3))
        last_tick, last_sent = now, attempt["sent_bytes"]
        save(state)

    def completed():
        nonlocal confirmed
        confirmed = total
        state.update(part_inflight=0, status="completed", completed_at=time.time())
        attempt["status"] = "completed"
        publish(force=True)

    try:
        state["part_inflight"] = 0
        publish(force=True)
        # Completion may have succeeded just before the process/response was lost.
        if state.get("upload_id"):
            try:
                head = bucket.head_object(key)
            except oss2.exceptions.NoSuchKey:
                head = None
            if head is not None and head.content_length == total and head.headers.get(METADATA_HASH) == fingerprint:
                completed()
                return
        parts = {}
        if state.get("upload_id"):
            try:
                for part in oss2.PartIterator(bucket, key, state["upload_id"]):
                    check()
                    expected = min(state["part_size"], total - (part.part_number - 1) * state["part_size"])
                    if 1 <= part.part_number <= (total + state["part_size"] - 1) // state["part_size"] and part.size == expected:
                        parts[part.part_number] = part
            except oss2.exceptions.NoSuchUpload:
                state.pop("upload_id", None)
                state["session_restarts"] = state.get("session_restarts", 0) + 1
                parts = {}
        if not state.get("upload_id"):
            check()
            state["upload_id"] = bucket.init_multipart_upload(key, headers={"x-oss-object-acl": "private", METADATA_HASH: fingerprint}).upload_id
            publish(force=True)
        confirmed = sum(p.size for p in parts.values())
        attempt["resumed_bytes"] = confirmed
        state["confirmed_parts"] = len(parts)
        publish(force=True)
        with path.open("rb") as source:
            for number, offset in enumerate(range(0, total, state["part_size"]), 1):
                check()
                if number in parts:
                    continue
                source.seek(offset)
                data = source.read(min(state["part_size"], total - offset))
                before = attempt["sent_bytes"]

                def progress(consumed, _total):
                    state["part_inflight"] = min(consumed, len(data))
                    publish(before + consumed)

                result = bucket.upload_part(key, state["upload_id"], number, data, progress_callback=progress)
                parts[number] = oss2.models.PartInfo(number, result.etag, size=len(data))
                confirmed += len(data)
                state.update(confirmed_parts=len(parts), part_inflight=0)
                publish(before + len(data), force=True)
        check()
        bucket.complete_multipart_upload(key, state["upload_id"], [parts[n] for n in sorted(parts)])
        completed()
    except BaseException as exc:
        state.update(status="cancelled" if isinstance(exc, InterruptedError) else "interrupted", current_bps=0.0)
        attempt["status"] = state["status"]
        attempt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        attempt["average_bps"] = round(attempt["sent_bytes"] / max(0.000001, attempt["elapsed_seconds"]), 1)
        state["active_seconds"] = round(sum(a["elapsed_seconds"] for a in state["attempts"]), 3)
        state["updated_at"] = time.time()
        # Save without a cancellation check; a lost lease must still block writes.
        save(state)
        raise


def upload_summary(track: dict) -> dict | None:
    state = track.get("upload_state")
    if not state:
        return None
    fields = ("status", "total_bytes", "transferred_bytes", "confirmed_bytes", "confirmed_parts", "part_size",
              "current_bps", "active_seconds", "started_at", "completed_at", "updated_at", "resume_count", "session_restarts", "attempts")
    return {name: state[name] for name in fields if name in state}
