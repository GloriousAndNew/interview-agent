"""Display persisted task clocks and measured work, without guessing cloud ETA."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


STAGES = (
    "PREPROCESSING", "ASR_UPLOADING", "ASR_SUBMITTING", "ASR_WAITING",
    "TRANSCRIPT_READY", "EXTRACTING", "VALIDATING", "PERSISTING", "EXPORTING",
)
ACTIVE = {"QUEUED", "RUNNING", "RETRY_WAIT"}


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def local_time(value: float | None) -> str:
    return datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S") if value is not None else "—"


def task_progress(row: dict, cp: dict, now: float) -> dict:
    # Imported transcripts skip the audio stages; media_id comes from the task query.
    stages = STAGES if row.get("media_id") else STAGES[4:]
    stage = row["stage"]
    index = stages.index(stage) if stage in stages else 0
    completed = row["status"] == "COMPLETED"
    fraction = 1.0 if completed else index / len(stages)
    plan = cp.get("chunk_plan", [])
    done = min(len(cp.get("chunks", [])), len(plan))
    required = sum(len(n["required"]) for n in plan)
    saved = sum(len(n["required"]) for n in plan[:done])
    if stage == "EXTRACTING" and required and not completed:
        fraction = (index + saved / required) / len(stages)
    start = row.get("started_at")
    end = now if row["status"] in ACTIVE else row.get("finished_at")
    elapsed = max(0, end - start) if start is not None and end is not None else None
    stage_start = row.get("stage_started_at")
    stage_elapsed = max(0, end - stage_start) if stage_start is not None and end is not None else None
    return {
        "fraction": fraction, "stage_index": index + 1, "stage_count": len(stages),
        "done_chunks": done, "total_chunks": len(plan),
        "saved_segments": saved, "total_segments": required,
        "elapsed": elapsed, "stage_elapsed": stage_elapsed,
        "waiting": max(0, now - row["created"]) if start is None and row["status"] in ACTIVE else None,
    }
