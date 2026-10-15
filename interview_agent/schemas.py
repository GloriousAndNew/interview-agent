from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Segment(StrictModel):
    id: str = Field(min_length=1, max_length=120)
    text_raw: str = Field(min_length=1, max_length=30000)
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)
    speaker_id: str | None = Field(default=None, max_length=120)
    provider_confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def valid_times(self):
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("起止时间必须同时存在或同时为空")
        if self.start_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("结束时间必须大于开始时间")
        return self


class Transcript(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    transcript_id: str = ""
    duration_ms: int | None = Field(default=None, ge=0)
    segments: list[Segment] = Field(min_length=1, max_length=30000)

    @model_validator(mode="after")
    def valid_segments(self):
        ids = [s.id for s in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("片段 ID 重复")
        if self.duration_ms is not None and any(s.end_ms is not None and s.end_ms > self.duration_ms for s in self.segments):
            raise ValueError("片段时间超出录音时长")
        return self


class QA(StrictModel):
    local_id: str = Field(min_length=1, max_length=120)
    parent_local_id: str | None = None
    parent_existing_id: str | None = None
    continuation_of_id: str | None = None
    question: str = Field(min_length=1, max_length=4000)
    question_segment_ids: list[str] = Field(min_length=1)
    answer_summary: str | None = Field(default=None, max_length=20000)
    answer_segment_ids: list[str] = Field(default_factory=list)
    answer_status: Literal["answered", "partial", "unanswered", "unclear"]
    tags: list[str] = Field(default_factory=list, max_length=12)
    review_reasons: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_answer(self):
        if sum(v is not None for v in (self.parent_local_id, self.parent_existing_id, self.continuation_of_id)) > 1:
            raise ValueError("父引用和续接引用互斥")
        if self.answer_status == "unanswered" and (self.answer_summary or self.answer_segment_ids):
            raise ValueError("未回答不能包含回答")
        if self.answer_status in ("answered", "partial") and (not self.answer_summary or not self.answer_segment_ids):
            raise ValueError("实际回答必须有摘要和来源")
        if self.answer_summary and not self.answer_segment_ids:
            raise ValueError("回答摘要缺少来源")
        return self


class Disposition(StrictModel):
    segment_id: str
    category: Literal["context", "smalltalk", "closing", "needs_review"]
    reason: str = Field(min_length=1, max_length=1000)


class Correction(StrictModel):
    segment_id: str
    original_text: str = Field(min_length=1, max_length=500)
    suggested_text: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)


class RoleSuggestion(StrictModel):
    speaker_id: str
    role: Literal["interviewer", "candidate", "unknown"]
    source_segment_ids: list[str] = Field(min_length=1)


class Extraction(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    items: list[QA] = Field(default_factory=list, max_length=200)
    segment_dispositions: list[Disposition] = Field(default_factory=list)
    correction_suggestions: list[Correction] = Field(default_factory=list)
    role_suggestions: list[RoleSuggestion] = Field(default_factory=list)


class ExtractionValidationError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def validate_extraction(result: Extraction, segments: list[Segment], required_ids: set[str], existing: set[str], existing_sources: dict[str, set[str]] | None = None) -> None:
    sources = {s.id: s for s in segments}
    local = {q.local_id: q for q in result.items}
    if len(local) != len(result.items):
        raise ExtractionValidationError("DUPLICATE_QUESTION_ID", "问题局部 ID 重复")
    covered = set()
    for q in result.items:
        refs = set(q.question_segment_ids + q.answer_segment_ids)
        # Continuing a previous answer may cite its already-validated question.
        # New answer evidence must still be present in this request.
        prior = (existing_sources or {}).get(q.continuation_of_id, set()) if q.continuation_of_id else set()
        invalid = (set(q.question_segment_ids) - sources.keys() - prior) | (set(q.answer_segment_ids) - sources.keys())
        if invalid:
            raise ExtractionValidationError("INVALID_SOURCE_REFERENCE", "问答引用了当前输入之外的片段", question_id=q.local_id, invalid_ids=sorted(invalid)[:20])
        if q.parent_local_id and q.parent_local_id not in local:
            raise ExtractionValidationError("INVALID_LOCAL_PARENT", "局部父问题不存在", question_id=q.local_id, parent_id=q.parent_local_id)
        for reference in (q.parent_existing_id, q.continuation_of_id):
            if reference and reference not in existing:
                raise ExtractionValidationError("INVALID_CROSS_CHUNK_REFERENCE", "跨块引用不存在", question_id=q.local_id, reference_id=reference)
        seen = {q.local_id}
        parent = q.parent_local_id
        while parent:
            if parent in seen:
                raise ExtractionValidationError("QUESTION_CYCLE", "问题父引用形成环", question_id=q.local_id)
            seen.add(parent)
            parent = local[parent].parent_local_id
        covered |= refs
    for d in result.segment_dispositions:
        if d.segment_id not in sources:
            raise ExtractionValidationError("INVALID_DISPOSITION_SOURCE", "用途记录引用不存在的片段", segment_id=d.segment_id)
        covered.add(d.segment_id)
    if not required_ids <= covered:
        raise ExtractionValidationError("UNCOVERED_SEGMENTS", "存在未处理的片段", count=len(required_ids - covered), segment_ids=sorted(required_ids - covered)[:20])
    for c in result.correction_suggestions:
        if c.segment_id not in sources or c.original_text not in sources[c.segment_id].text_raw:
            raise ExtractionValidationError("CORRECTION_MISMATCH", "校对建议不对应原文", segment_id=c.segment_id)
    for r in result.role_suggestions:
        if any(s not in sources or sources[s].speaker_id != r.speaker_id for s in r.source_segment_ids):
            raise ExtractionValidationError("ROLE_SOURCE_MISMATCH", "角色建议引用不匹配", speaker_id=r.speaker_id)


def review_extraction(result: Extraction, segments: list[Segment], required_ids: set[str], existing: set[str], existing_sources: dict[str, set[str]] | None = None) -> tuple[Extraction, list[dict]]:
    """Quarantine optional bad suggestions; never relax answer-source checks."""
    result = result.model_copy(deep=True)
    sources = {s.id: s for s in segments}
    warnings = []
    good_corrections = [c for c in result.correction_suggestions if c.segment_id in sources and c.original_text in sources[c.segment_id].text_raw]
    if len(good_corrections) != len(result.correction_suggestions):
        warnings.append({"code": "CORRECTION_QUARANTINED", "message": "部分校对建议不匹配原文，未采用；原文保持不变。", "details": {"count": len(result.correction_suggestions) - len(good_corrections)}})
    result.correction_suggestions = good_corrections
    good_roles = [r for r in result.role_suggestions if all(s in sources and sources[s].speaker_id == r.speaker_id for s in r.source_segment_ids)]
    if len(good_roles) != len(result.role_suggestions):
        warnings.append({"code": "ROLE_QUARANTINED", "message": "部分角色建议来源不匹配，未采用，请人工确认说话人。", "details": {"count": len(result.role_suggestions) - len(good_roles)}})
    result.role_suggestions = good_roles
    good_dispositions = [d for d in result.segment_dispositions if d.segment_id in sources]
    if len(good_dispositions) != len(result.segment_dispositions):
        warnings.append({"code": "DISPOSITION_QUARANTINED", "message": "无效的片段用途建议未采用。", "details": {"count": len(result.segment_dispositions) - len(good_dispositions)}})
    result.segment_dispositions = good_dispositions
    covered = {sid for q in result.items for sid in q.question_segment_ids + q.answer_segment_ids}
    covered |= {d.segment_id for d in result.segment_dispositions}
    missing = required_ids - covered
    if missing:
        for sid in sorted(missing):
            result.segment_dispositions.append(Disposition(segment_id=sid, category="needs_review", reason="模型未明确归入问答，已保留原文等待人工核对。"))
        warnings.append({"code": "SEGMENTS_REQUIRE_REVIEW", "message": "部分片段未归入问答，已保留并标记待核对。", "details": {"count": len(missing), "segment_ids": sorted(missing)[:20]}})
    validate_extraction(result, segments, required_ids, existing, existing_sources)
    return result, warnings


def import_text(text: str) -> Transcript:
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    # Avoid unbounded paragraphs without fabricating timing or speaker labels.
    parts = [part[i:i + 2000] for part in parts for i in range(0, len(part), 2000)]
    return Transcript(segments=[Segment(id=f"s{i:05d}", text_raw=part) for i, part in enumerate(parts, 1)])
