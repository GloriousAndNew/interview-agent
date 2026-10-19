import copy
import json
import time

import httpx
import pytest

from interview_agent.db import BudgetExceeded
from interview_agent.pipeline import Pipeline
from interview_agent.providers import DeepSeek, ProviderError
from interview_agent.reference_answers import AnswerBusy, HighScoreAnswer, answer_state, generate, reserve
from interview_agent.services import purge_deleted, request_delete
from conftest import FakeLLM


class Coach:
    calls = 0
    def __init__(self, settings):
        pass
    def close(self):
        pass
    def high_score(self, context):
        type(self).calls += 1
        assert context["question"] and context["source_excerpt"]
        return HighScoreAnswer(answer="幂等关注重复执行的最终效果，可用唯一请求号和原子状态更新实现，并说明并发与失效边界。", key_points=["先定义，再给方案及适用边界"], improvements=["补充并发处理方式"], followup_questions=["幂等键过期后如何处理？"]), {"total_tokens": 100}, "coach-request"


@pytest.fixture
def question(settings, store, submitted):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    return store.search()[0]


def test_generate_persists_separate_answer_budget_and_markdown(settings, store, question):
    before = store.one("SELECT content FROM revisions WHERE id=?", (question["revision_id"],))["content"]
    generated = generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    saved, latest = answer_state(store, question["revision_id"], question["id"])
    assert saved["content"] == generated and latest["status"] == "SUCCEEDED"
    assert store.one("SELECT content FROM revisions WHERE id=?", (question["revision_id"],))["content"] == before
    call = store.one("SELECT * FROM calls WHERE stage='HIGH_SCORE'")
    assert call["reserve_cny"] == settings.llm_reserve_per_chunk and call["status"] == "received"
    assert json.loads(call["usage"])["total_tokens"] == 100
    note = (settings.data_dir / "notes" / question["interview_id"] / "interview.md").read_text(encoding="utf-8")
    assert "面试高分答案" in note and generated["answer"] in note and question["answer"] in note


def test_regeneration_failure_keeps_successful_answer(settings, store, question):
    original = generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    class Failing(Coach):
        def high_score(self, context):
            raise ProviderError("云端连接失败或超时。", usage={"total_tokens": 9})
    with pytest.raises(ProviderError):
        generate(settings, store, question["revision_id"], question["id"], provider_factory=Failing)
    saved, latest = answer_state(store, question["revision_id"], question["id"])
    assert saved["content"] == original and latest["status"] == "FAILED"
    assert store.one("SELECT usage FROM calls WHERE id=?", (latest["id"],))["usage"] == '{"total_tokens": 9}'


def test_duplicate_generation_and_budget_rejected_before_call(settings, store, question):
    Coach.calls = 0
    first = reserve(store, question["revision_id"], question["id"], settings)
    with pytest.raises(AnswerBusy):
        generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    assert Coach.calls == 0
    with store.transaction() as con:
        con.execute("UPDATE reference_answers SET lease_until=? WHERE id=?", (time.time() - 1, first))
    settings.monthly_budget_cny = 0.01
    with pytest.raises(BudgetExceeded):
        generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    assert Coach.calls == 0
    settings.monthly_budget_cny = 50
    generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    assert store.one("SELECT status FROM reference_answers WHERE id=?", (first,))["status"] == "FAILED"


def test_unchanged_question_carries_answer_but_changed_answer_does_not(settings, store, question):
    generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    content = json.loads(store.one("SELECT content FROM revisions WHERE id=?", (question["revision_id"],))["content"])
    content["items"][0]["tags"] = ["新标签"]
    new_revision = store.edit_revision(question["interview_id"], question["revision_id"], copy.deepcopy(content))
    assert answer_state(store, new_revision, question["id"])[0] is not None
    content["items"][0]["answer_summary"] = "人工补充后的回答"
    newer = store.edit_revision(question["interview_id"], new_revision, content)
    assert answer_state(store, newer, question["id"])[0] is None
    with pytest.raises(ValueError, match="版本"):
        generate(settings, store, new_revision, question["id"], provider_factory=Coach)


def test_delete_discards_inflight_generation_and_cascades_content(settings, store, question):
    generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    class Deleted(Coach):
        def high_score(self, context):
            request_delete(store, question["interview_id"])
            purge_deleted(store)
            return super().high_score(context)
    with pytest.raises(ValueError, match="删除"):
        generate(settings, store, question["revision_id"], question["id"], provider_factory=Deleted)
    assert not store.rows("SELECT id FROM reference_answers")


def test_provider_prompt_and_structured_output(settings):
    def handler(request):
        payload = json.loads(request.content)
        assert "禁止编造" in payload["messages"][0]["content"]
        assert "[待补充" in payload["messages"][0]["content"]
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["thinking"] == {"type": "disabled"}
        assert "fake-deepseek" not in json.dumps(payload)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": HighScoreAnswer(answer="参考内容", key_points=["关键点"]).model_dump_json()}}], "usage": {"total_tokens": 10}, "id": "request-id"})
    provider = DeepSeek(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    answer, usage, request_id = provider.high_score({"question": "问题"})
    assert answer.answer == "参考内容" and usage["total_tokens"] == 10 and request_id == "request-id"


@pytest.mark.parametrize("reason,content", [("length", "{}"), ("stop", '{"answer":"secret-input"}')])
def test_provider_rejects_partial_or_invalid_output_without_leaking_input(settings, reason, content):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": content}}], "usage": {"total_tokens": 10}})))
    with pytest.raises(ProviderError) as error:
        DeepSeek(settings, client).high_score({"question": "问题"})
    assert "secret-input" not in str(error.value) and error.value.usage["total_tokens"] == 10
