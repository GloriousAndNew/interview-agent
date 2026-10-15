import io

import pytest

from interview_agent.config import Settings
from interview_agent.db import Store
from interview_agent.schemas import Extraction
from interview_agent.services import submit


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, deepseek_key="fake-deepseek", asr_key="fake-ali", oss_access_id="fake-oss-id", oss_access_secret="fake-oss-secret", oss_bucket="test-bucket", oss_endpoint="https://oss-cn-beijing.aliyuncs.com")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir)


@pytest.fixture
def submitted(settings, store):
    return submit(settings, store, io.BytesIO("什么是幂等？\n重复请求与执行一次产生相同效果。".encode()), "interview.txt", "后端面试", {"position": "后端", "date": "2026-10-15"})


class FakeLLM:
    calls = 0

    def __init__(self, settings):
        pass

    def close(self):
        pass

    def extract(self, segments, required, existing, repair=""):
        type(self).calls += 1
        ids = [s.id for s in segments if s.id in required]
        data = {"items": [{"local_id": "q1", "question": segments[0].text_raw, "question_segment_ids": [ids[0]], "answer_summary": segments[-1].text_raw if len(ids) > 1 else None, "answer_segment_ids": ids[1:], "answer_status": "answered" if len(ids) > 1 else "unanswered", "tags": ["幂等"], "review_reasons": []}]}
        return Extraction.model_validate(data), {"prompt_tokens": 100, "completion_tokens": 30}, "request-test"
