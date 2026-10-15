from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .files import atomic_json
from .schemas import ExtractionValidationError


def describe_validation(exc: ValueError) -> dict:
    """Export validation location and rules, never Pydantic input values."""
    if isinstance(exc, ExtractionValidationError):
        return {"code": exc.code, "message": str(exc), "details": exc.details}
    issues = getattr(exc, "issues", None)
    if issues is None and isinstance(exc, ValidationError):
        issues = [{"path": ".".join(map(str, e["loc"])), "type": e["type"], "message": e["msg"]} for e in exc.errors(include_input=False, include_context=False, include_url=False)[:20]]
    return {"code": "OUTPUT_SCHEMA_INVALID", "message": "模型输出字段未符合约定结构。", "details": {"issues": issues or []}}


def save_response(directory: Path, call_id: str, provider, chunk_no: int, required: list[str]) -> Path | None:
    response = getattr(provider, "last_response", None)
    if response is None:
        return None
    target = directory / "attempts" / f"{call_id}.json"
    atomic_json(target, {"chunk_no": chunk_no, "required_segment_ids": required, "response": response})
    return target


def repair_instructions(issue: dict) -> str:
    return json.dumps({"error_code": issue["code"], "rule": issue["message"], "details": issue["details"], "instruction": "根据列出的具体错误和原始输入重新生成完整 JSON。不得编造片段 ID；回答只引用本批片段；续接已有问题时可引用该问题的已验证来源。"}, ensure_ascii=False)


def report(store, job_id: str) -> dict:
    job = store.one("SELECT id,status,stage,error,checkpoint,created,started_at,finished_at,stage_started_at,timing_estimated FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise ValueError("任务不存在")
    checkpoint = json.loads(job.pop("checkpoint"))
    calls = store.rows("SELECT id,stage,provider,model,status,request_id,usage,created FROM calls WHERE job_id=? ORDER BY created", (job_id,))
    for call in calls:
        call["usage"] = json.loads(call["usage"])
    return {
        "format_version": "1.0", "job": job,
        "progress": {"completed_chunks": len(checkpoint.get("chunks", [])), "total_chunks": len(checkpoint.get("chunk_plan", [])), "has_transcript": bool(checkpoint.get("transcript")), "last_diagnostic": checkpoint.get("last_diagnostic")},
        "events": store.events(job_id, limit=100),
        "calls": calls,
        "privacy": "此报告不包含密钥、音频地址、完整转写或模型回答正文；失败响应仅保存在本地 attempts 目录。",
    }
