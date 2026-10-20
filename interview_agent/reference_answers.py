"""Optional AI study answers, separate from the factual interview record."""
from __future__ import annotations

from datetime import datetime
import json
import time
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field

from .db import BudgetExceeded, uid
from .files import inside, read_json
from .providers import DeepSeek, ProviderError
from .schemas import StrictModel, Transcript


Point = Annotated[str, Field(min_length=1, max_length=600)]


class HighScoreAnswer(StrictModel):
    answer: str = Field(min_length=1, max_length=12000)
    key_points: list[Point] = Field(min_length=1, max_length=8)
    improvements: list[Point] = Field(default_factory=list, max_length=6)
    followup_questions: list[Point] = Field(default_factory=list, max_length=5)
    needs_personalization: list[Point] = Field(default_factory=list, max_length=6)


class AnswerBusy(ValueError):
    pass


def answer_state(store, revision_id: str, qa_id: str) -> tuple[dict | None, dict | None]:
    rows = store.rows("SELECT * FROM reference_answers WHERE revision_id=? AND qa_id=? ORDER BY created DESC,rowid DESC", (revision_id, qa_id))
    latest = rows[0] if rows else None
    saved = next((r for r in rows if r["status"] == "SUCCEEDED"), None)
    if saved:
        saved["content"] = json.loads(saved["content"])
    return saved, latest


def reserve(store, revision_id: str, qa_id: str, settings) -> str:
    now, generation_id = time.time(), uid()
    with store.transaction() as con:
        if (store.root / "updates/installing.json").exists():
            raise AnswerBusy("正在安装更新，请在重启后生成高分答案。")
        source = con.execute("SELECT 1 FROM qa q JOIN revisions r ON r.id=q.revision_id JOIN interviews i ON i.id=r.interview_id WHERE q.id=? AND q.revision_id=? AND i.deleted=0 AND i.active_revision=?", (qa_id, revision_id, revision_id)).fetchone()
        if not source:
            raise ValueError("问答版本已变更或已删除，请刷新后生成。")
        if con.execute("SELECT 1 FROM reference_answers WHERE revision_id=? AND qa_id=? AND status='RUNNING' AND lease_until>?", (revision_id, qa_id, now)).fetchone():
            raise AnswerBusy("这道问题正在生成高分答案，请稍候刷新查看。")
        con.execute("UPDATE reference_answers SET status='FAILED',error='上次生成中断或超时，可重新生成。',updated=? WHERE revision_id=? AND qa_id=? AND status='RUNNING'", (now, revision_id, qa_id))
        month = datetime.now(ZoneInfo("Asia/Shanghai")).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        used = con.execute("SELECT COALESCE(SUM(reserve_cny),0) FROM calls WHERE created>=?", (month,)).fetchone()[0]
        if used + settings.llm_reserve_per_chunk > settings.monthly_budget_cny:
            raise BudgetExceeded("本月费用预留达到预算，请在设置中调整或等待下月。")
        con.execute("INSERT INTO calls(id,job_id,stage,provider,model,status,reserve_cny,created) VALUES (?,NULL,'HIGH_SCORE','deepseek',?,'reserved',?,?)", (generation_id, settings.llm_model, settings.llm_reserve_per_chunk, now))
        con.execute("INSERT INTO reference_answers(id,revision_id,qa_id,status,model,created,updated,lease_until) VALUES (?,?,?,'RUNNING',?,?,?,?)", (generation_id, revision_id, qa_id, settings.llm_model, now, now, now + 300))
    return generation_id


def context(store, revision_id: str, qa_id: str) -> dict:
    row = store.one("SELECT r.*,i.position FROM revisions r JOIN interviews i ON i.id=r.interview_id WHERE r.id=? AND i.deleted=0", (revision_id,))
    if not row:
        raise ValueError("面试已删除，请刷新。")
    content = json.loads(row["content"])
    question = next((q for q in content["items"] if q["id"] == qa_id), None)
    if not question:
        raise ValueError("问答不存在，请刷新。")
    transcript = Transcript.model_validate(read_json(inside(store.root, row["transcript_path"])))
    ids = set(question["question_segment_ids"] + question["answer_segment_ids"])
    raw = "\n".join(s.text_raw for s in transcript.segments if s.id in ids)
    parent = next((q["question"] for q in content["items"] if q["id"] == question.get("parent_id")), None)
    return {"question": question["question"], "original_answer": question.get("answer_summary"), "position": row["position"], "parent_question": parent, "source_excerpt": raw[:12000], "source_truncated": len(raw) > 12000}


def generate(settings, store, revision_id: str, qa_id: str, *, provider_factory=DeepSeek) -> dict:
    source = context(store, revision_id, qa_id)
    provider = provider_factory(settings)
    generation_id = None
    try:
        generation_id = reserve(store, revision_id, qa_id, settings)
        result, usage, request_id = provider.high_score(source)
        result = HighScoreAnswer.model_validate(result)
        store.record_call(generation_id, "received", usage, request_id)
        with store.transaction() as con:
            exists = con.execute("SELECT 1 FROM reference_answers a JOIN revisions r ON r.id=a.revision_id JOIN interviews i ON i.id=r.interview_id WHERE a.id=? AND a.status='RUNNING' AND a.lease_until>? AND i.deleted=0", (generation_id, time.time())).fetchone()
            if not exists:
                raise ValueError("生成请求已过期或面试已删除，未保存此结果。")
            con.execute("UPDATE reference_answers SET status='SUCCEEDED',content=?,updated=?,lease_until=0 WHERE id=?", (result.model_dump_json(), time.time(), generation_id))
            con.execute("UPDATE revisions SET export_status='pending' WHERE id=?", (revision_id,))
        from .services import export_revision
        try:
            export_revision(store, revision_id)
        except (OSError, ValueError):
            # The answer is saved; exporting can be retried independently.
            pass
        return result.model_dump()
    except Exception as exc:
        if generation_id:
            safe = str(exc) if isinstance(exc, (ProviderError, BudgetExceeded)) else "高分答案生成或保存失败，请稍后重试；已有答案保留。"
            store.record_call(generation_id, "failed", getattr(exc, "usage", None), getattr(exc, "request_id", ""))
            with store.transaction() as con:
                con.execute("UPDATE reference_answers SET status='FAILED',error=?,updated=?,lease_until=0 WHERE id=? AND status='RUNNING'", (safe, time.time(), generation_id))
        raise
    finally:
        provider.close()


def content_markdown(content: dict) -> str:
    lines = ["> AI 生成的练习参考，不是面试原答，也不保证获得高分；个人经历请按真实情况补充。", "", content["answer"]]
    for key, label in (("key_points", "得分要点"), ("improvements", "原答改进建议"), ("followup_questions", "可能的追问"), ("needs_personalization", "需要按个人情况补充")):
        if content.get(key):
            lines += ["", "### " + label, ""] + ["- " + item for item in content[key]]
    return "\n".join(lines) + "\n"


def markdown(question: str, original: str | None, content: dict) -> str:
    return "\n".join(["# " + question, "", "## 面试原答", "", original or "未作答或无法辨认。", "", "## 面试高分答案", "", content_markdown(content)])


def carry_forward(con, old_revision: str, new_revision: str, old_content: dict, new_content: dict):
    old_items = {q["id"]: q for q in old_content["items"]}
    new_items = {q["id"]: q for q in new_content["items"]}
    fields = ("question", "answer_summary", "question_segment_ids", "answer_segment_ids", "parent_id")
    for qa_id, question in new_items.items():
        previous = old_items.get(qa_id)
        if not previous or any(previous.get(k) != question.get(k) for k in fields):
            continue
        parent = question.get("parent_id")
        if parent and old_items.get(parent, {}).get("question") != new_items.get(parent, {}).get("question"):
            continue
        saved = con.execute("SELECT * FROM reference_answers WHERE revision_id=? AND qa_id=? AND status='SUCCEEDED' ORDER BY created DESC,rowid DESC LIMIT 1", (old_revision, qa_id)).fetchone()
        if saved:
            con.execute("INSERT INTO reference_answers(id,revision_id,qa_id,status,content,model,created,updated,lease_until) VALUES (?,?,?,'SUCCEEDED',?,?,?,?,0)", (uid(), new_revision, qa_id, saved["content"], saved["model"], saved["created"], time.time()))
