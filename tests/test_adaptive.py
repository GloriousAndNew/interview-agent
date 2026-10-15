import io
import json

import httpx
import pytest

from interview_agent.files import atomic_json, inside, read_json
from interview_agent.pipeline import Pipeline
from interview_agent.providers import DeepSeek, OutputTruncated, ProviderError
from interview_agent.schemas import Transcript
from interview_agent.services import chunk_transcript, submit
from conftest import FakeLLM


def test_provider_preserves_truncation_reason_and_usage(settings):
    def handler(request):
        assert json.loads(request.content)["max_tokens"] == 8192
        return httpx.Response(200, json={"id": "truncated-request", "usage": {"completion_tokens": 8192}, "choices": [{"finish_reason": "length", "message": {"content": "{unfinished"}}]})
    with pytest.raises(OutputTruncated) as error:
        DeepSeek(settings, httpx.Client(transport=httpx.MockTransport(handler))).extract([], [], [])
    assert error.value.usage["completion_tokens"] == 8192
    assert error.value.finish_reason == "length"
    assert error.value.request_id == "truncated-request"


def test_chunking_limits_short_but_many_segments():
    from interview_agent.schemas import import_text
    transcript = import_text("\n".join(["短句"] * 192))
    chunks = chunk_transcript(transcript)
    assert len(chunks) == 4
    assert all(len(c["required"]) <= 48 for c in chunks)
    assert [sid for c in chunks for sid in c["required"]] == [s.id for s in transcript.segments]


def test_truncation_splits_and_does_not_redo_asr(settings, store, submitted):
    requests = []

    class TruncatedOnce(FakeLLM):
        def extract(self, segments, required, existing, repair=""):
            requests.append(list(required))
            if len(requests) == 1:
                raise OutputTruncated("truncated", usage={"completion_tokens": 8192, "_finish_reason": "length"}, request_id="req-cut")
            return super().extract(segments, required, existing, repair)

    Pipeline(settings, store, llm_factory=TruncatedOnce).run_once()
    row = store.one("SELECT status,checkpoint,error FROM jobs WHERE id=?", (submitted[1],))
    assert row["status"] == "COMPLETED", row["error"]
    cp = json.loads(row["checkpoint"])
    assert len(cp["chunks"]) == len(cp["chunk_plan"]) == 2
    assert cp["split_count"] == 1
    assert requests == [["s00001", "s00002"], ["s00001"], ["s00002"]]
    calls = store.rows("SELECT provider,status,usage FROM calls ORDER BY created")
    assert all(c["provider"] == "deepseek" for c in calls)
    assert calls[0]["status"] == "truncated"
    assert json.loads(calls[0]["usage"])["completion_tokens"] == 8192


def test_split_plan_resumes_successful_prefix(settings, store, submitted):
    requests = []

    class SplitThenNetworkFailure(FakeLLM):
        def extract(self, segments, required, existing, repair=""):
            requests.append(list(required))
            if len(requests) == 1:
                raise OutputTruncated("truncated")
            if len(requests) == 3:
                raise ProviderError("network", retryable=True)
            return super().extract(segments, required, existing, repair)

    pipeline = Pipeline(settings, store, llm_factory=SplitThenNetworkFailure)
    pipeline.run_once()
    row = store.one("SELECT status,checkpoint FROM jobs WHERE id=?", (submitted[1],))
    assert row["status"] == "RETRY_WAIT"
    assert len(json.loads(row["checkpoint"])["chunks"]) == 1
    with store.transaction() as con:
        con.execute("UPDATE jobs SET retry_at=0 WHERE id=?", (submitted[1],))
    pipeline.run_once()
    assert store.one("SELECT status FROM jobs WHERE id=?", (submitted[1],))["status"] == "COMPLETED"
    assert requests.count(["s00001"]) == 1
    assert requests.count(["s00002"]) == 2


def test_non_length_failure_does_not_split(settings, store, submitted):
    class Filtered(FakeLLM):
        def extract(self, *args, **kwargs):
            raise ProviderError("filtered", usage={"_finish_reason": "content_filter"}, finish_reason="content_filter")
    Pipeline(settings, store, llm_factory=Filtered).run_once()
    row = store.one("SELECT status,checkpoint FROM jobs WHERE id=?", (submitted[1],))
    assert row["status"] == "FAILED"
    assert len(json.loads(row["checkpoint"])["chunk_plan"]) == 1
    assert store.one("SELECT COUNT(*) n FROM calls")["n"] == 1


def test_single_segment_split_is_bounded(settings, store):
    _, job_id = submit(settings, store, io.BytesIO(b"single line"), "single.txt", "single", {})
    class AlwaysTruncated(FakeLLM):
        def extract(self, *args, **kwargs):
            raise OutputTruncated("truncated")
    Pipeline(settings, store, llm_factory=AlwaysTruncated).run_once()
    assert store.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "FAILED"
    assert store.one("SELECT COUNT(*) n FROM calls")["n"] == 1


def test_legacy_saved_chunk_keeps_grouping_and_logical_ids(settings, store):
    source = "\n".join(["原" * 2000] * 6)
    interview_id, job_id = submit(settings, store, io.BytesIO(source.encode()), "legacy.txt", "legacy", {})
    cp = json.loads(store.one("SELECT checkpoint FROM jobs WHERE id=?", (job_id,))["checkpoint"])
    transcript = Transcript.model_validate(read_json(inside(settings.data_dir, cp["transcript"])))
    old_chunks = chunk_transcript(transcript, char_limit=9000, max_segments=None)
    assert len(old_chunks) == 2
    old, _, _ = FakeLLM(settings).extract(old_chunks[0]["segments"], old_chunks[0]["required"], [])
    relative = f"jobs/{interview_id}/{job_id}/legacy-0.json"
    atomic_json(inside(settings.data_dir, relative), old.model_dump())
    cp["chunks"] = [relative]
    with store.transaction() as con:
        con.execute("UPDATE jobs SET checkpoint=? WHERE id=?", (json.dumps(cp), job_id))
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    row = store.one("SELECT status,checkpoint,error FROM jobs WHERE id=?", (job_id,))
    assert row["status"] == "COMPLETED", row["error"]
    saved = json.loads(row["checkpoint"])
    assert saved["chunk_plan"][0]["required"] == old_chunks[0]["required"]
    assert saved["chunks"][0] == relative
    assert store.one("SELECT COUNT(*) n FROM calls")["n"] == 1
