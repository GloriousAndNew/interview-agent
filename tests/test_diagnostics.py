import json

import httpx
import pytest

from interview_agent.diagnostics import describe_validation, report
from interview_agent.files import inside, read_json
from interview_agent.pipeline import Pipeline
from interview_agent.providers import DeepSeek, OutputValidationError
from interview_agent.schemas import Extraction, ExtractionValidationError, Segment, review_extraction, validate_extraction
from conftest import FakeLLM


def question(**overrides):
    data = {"local_id": "q1", "question": "问题", "question_segment_ids": ["s1"], "answer_summary": "回答", "answer_segment_ids": ["s2"], "answer_status": "answered"}
    data.update(overrides)
    return data


def test_valid_continuation_can_cite_previously_verified_question():
    segments = [Segment(id="s2", text_raw="继续回答")]
    result = Extraction(items=[question(continuation_of_id="existing-q")])
    validate_extraction(result, segments, {"s2"}, {"existing-q"}, {"existing-q": {"s1"}})
    result.items[0].answer_segment_ids = ["s1"]
    with pytest.raises(ExtractionValidationError) as error:
        validate_extraction(result, segments, {"s2"}, {"existing-q"}, {"existing-q": {"s1"}})
    assert error.value.code == "INVALID_SOURCE_REFERENCE"


def test_new_question_cannot_borrow_prior_source():
    result = Extraction(items=[question(parent_existing_id="existing-q")])
    with pytest.raises(ExtractionValidationError):
        review_extraction(result, [Segment(id="s2", text_raw="回答")], {"s2"}, {"existing-q"}, {"existing-q": {"s1"}})


def test_optional_suggestions_are_quarantined_without_changing_original():
    segments = [Segment(id="s1", text_raw="问题", speaker_id="A"), Segment(id="s2", text_raw="回答", speaker_id="B")]
    result = Extraction(items=[question()], correction_suggestions=[{"segment_id": "s1", "original_text": "并不存在", "suggested_text": "修改", "reason": "理由"}], role_suggestions=[{"speaker_id": "A", "role": "interviewer", "source_segment_ids": ["s2"]}])
    clean, warnings = review_extraction(result, segments, {"s1", "s2"}, set())
    assert len(clean.items) == 1
    assert not clean.correction_suggestions and not clean.role_suggestions
    assert {w["code"] for w in warnings} == {"CORRECTION_QUARANTINED", "ROLE_QUARANTINED"}
    assert segments[0].text_raw == "问题"
    assert len(result.correction_suggestions) == 1  # source result is unmodified


def test_uncovered_segments_are_explicitly_pending_not_invented():
    segment = Segment(id="s1", text_raw="含糊的一句话")
    clean, warnings = review_extraction(Extraction(), [segment], {"s1"}, set())
    assert clean.items == []
    assert clean.segment_dispositions[0].category == "needs_review"
    assert warnings[0]["code"] == "SEGMENTS_REQUIRE_REVIEW"


def test_wrong_answer_reference_remains_hard_failure():
    result = Extraction(items=[question(answer_segment_ids=["invented-id"])])
    with pytest.raises(ExtractionValidationError) as error:
        review_extraction(result, [Segment(id="s1", text_raw="问题"), Segment(id="s2", text_raw="回答")], {"s1", "s2"}, set())
    issue = describe_validation(error.value)
    assert issue["code"] == "INVALID_SOURCE_REFERENCE"
    assert issue["details"]["invalid_ids"] == ["invented-id"]


def test_schema_failure_preserves_local_response_but_not_diagnostic_body(settings):
    secret_text = "PRIVATE_TRANSCRIPT_SENTINEL"
    payload = {"items": [{"local_id": "q1", "question": secret_text, "question_segment_ids": ["s1"], "answer_status": "invalid_status"}]}
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "req-schema", "usage": {"completion_tokens": 10}, "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}]})))
    model = DeepSeek(settings, client)
    with pytest.raises(OutputValidationError) as error:
        model.extract([], [], [])
    issue = describe_validation(error.value)
    assert issue["details"]["issues"][0]["path"] == "items.0.answer_status"
    assert secret_text not in json.dumps(issue)
    assert "input" not in json.dumps(issue)
    assert secret_text in model.last_response["content"]


def test_precise_validation_errors_and_attempts_are_saved(settings, store, submitted):
    class SchemaOnce(FakeLLM):
        attempts = 0
        last_response = None

        def extract(self, segments, required, existing, repair=""):
            type(self).attempts += 1
            self.last_response = {"content": "PRIVATE_RESPONSE_SENTINEL", "finish_reason": "stop", "usage": {"total_tokens": 10}}
            if self.attempts == 1:
                raise OutputValidationError({"total_tokens": 10}, "req-one", [{"path": "items.0.answer_status", "type": "literal_error", "message": "状态不符合约定"}])
            assert "OUTPUT_SCHEMA_INVALID" in repair
            assert "items.0.answer_status" in repair
            return super().extract(segments, required, existing, repair)

    Pipeline(settings, store, llm_factory=SchemaOnce).run_once()
    diagnostic = report(store, submitted[1])
    assert diagnostic["job"]["status"] == "COMPLETED"
    assert any(e["code"] == "OUTPUT_SCHEMA_INVALID" for e in diagnostic["events"])
    assert any(e["code"] == "TASK_COMPLETED" for e in diagnostic["events"])
    assert "PRIVATE_RESPONSE_SENTINEL" not in json.dumps(diagnostic)
    assert "fake-deepseek" not in json.dumps(diagnostic)
    artifacts = list((settings.data_dir / "jobs" / submitted[0] / submitted[1] / "attempts").glob("*.json"))
    assert len(artifacts) == 2
    assert read_json(artifacts[0])["response"]["content"] == "PRIVATE_RESPONSE_SENTINEL"


def test_critical_validation_failure_splits_and_recovers(settings, store, submitted):
    class WrongReferenceInLargeChunk(FakeLLM):
        def extract(self, segments, required, existing, repair=""):
            result, usage, request = super().extract(segments, required, existing, repair)
            if len(required) > 1:
                result.items[0].question_segment_ids = ["not-in-input"]
            return result, usage, request

    Pipeline(settings, store, llm_factory=WrongReferenceInLargeChunk).run_once()
    job = store.one("SELECT status,checkpoint,error FROM jobs WHERE id=?", (submitted[1],))
    assert job["status"] == "COMPLETED", job["error"]
    cp = json.loads(job["checkpoint"])
    assert cp["split_reason"] == "validation" and len(cp["chunks"]) == 2
    assert cp["last_diagnostic"]["code"] == "INVALID_SOURCE_REFERENCE"
    assert cp["last_diagnostic"]["resolved"]
    assert any(e["code"] == "CHUNK_SPLIT" for e in store.events(submitted[1]))


def test_critical_single_chunk_failure_reports_batch_and_rule(settings, store, submitted):
    class AlwaysWrong(FakeLLM):
        def extract(self, *args, **kwargs):
            result, usage, request = super().extract(*args, **kwargs)
            result.items[0].question_segment_ids = ["not-in-input"]
            return result, usage, request

    Pipeline(settings, store, llm_factory=AlwaysWrong).run_once()
    row = store.one("SELECT status,error FROM jobs WHERE id=?", (submitted[1],))
    assert row["status"] == "FAILED"
    assert "INVALID_SOURCE_REFERENCE" in row["error"]
    assert "第" in row["error"] and "批" in row["error"]
    assert not store.search()


def test_missing_qa_persists_review_required_state(settings, store, submitted):
    class NoQuestions(FakeLLM):
        def extract(self, *args, **kwargs):
            return Extraction(), {}, "no-questions"
    Pipeline(settings, store, llm_factory=NoQuestions).run_once()
    row = store.one("SELECT active_revision FROM interviews WHERE id=?", (submitted[0],))
    result = json.loads(store.one("SELECT content FROM revisions WHERE id=?", (row["active_revision"],))["content"])
    assert result["review_status"] == "needs_review"
    assert len(result["dispositions"]) == 2 and result["items"] == []
    assert "2 个片段尚未归入问答" in result["summary"][-1]


def test_diagnostics_schema_migration_is_repeatable(store):
    from interview_agent.db import Store
    with store.transaction() as con:
        con.execute("PRAGMA user_version=1")
    Store(store.root)
    with store.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    Store(store.root)
