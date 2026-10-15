import json
import math
import struct
import wave

import httpx
import pytest

from interview_agent.media import prepare, probe
from interview_agent.providers import AliASR, DeepSeek, ProviderError, normalize_asr, request_json
from interview_agent.schemas import Extraction


def test_asr_submit_uses_async_protocol(settings):
    def handler(request):
        assert request.headers["X-DashScope-Async"] == "enable"
        assert request.headers["Authorization"] == "Bearer fake-ali"
        body = json.loads(request.content)
        assert body["parameters"]["diarization_enabled"] is True
        assert body["input"]["file_urls"] == ["https://audio.example/file.wav"]
        return httpx.Response(200, json={"output": {"task_id": "task-123"}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert AliASR(settings, client).submit("https://audio.example/file.wav") == "task-123"


def test_asr_result_never_forwards_model_key(settings):
    def handler(request):
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"transcripts": []})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    asr = AliASR(settings, client)
    assert asr.result({"output": {"results": [{"subtask_status": "SUCCEEDED", "transcription_url": "https://result.oss-cn-beijing.aliyuncs.com/file.json?Signature=test"}]}}) == {"transcripts": []}


def test_asr_subtask_failure_not_overall_success(settings):
    with pytest.raises(ProviderError, match="子任务"):
        AliASR(settings).result({"output": {"task_status": "SUCCEEDED", "results": [{"subtask_status": "FAILED"}]}})


def test_asr_result_rejects_unknown_host(settings):
    with pytest.raises(ProviderError, match="不受信任"):
        AliASR(settings).result({"output": {"results": [{"subtask_status": "SUCCEEDED", "transcription_url": "http://127.0.0.1/secret"}]}})


def test_submit_timeout_is_unknown_and_redacted(settings):
    def handler(request):
        raise httpx.ReadTimeout("secret-token-in-url", request=request)
    asr = AliASR(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError) as exc:
        asr.submit("https://example/audio?Signature=secret")
    assert exc.value.unknown
    assert "secret" not in str(exc.value)


def test_deepseek_non_thinking_json_request(settings):
    def handler(request):
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert "fake-deepseek" not in json.dumps(body)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": Extraction().model_dump_json()}}], "usage": {"total_tokens": 10}, "id": "req-1"})
    result, usage, req = DeepSeek(settings, httpx.Client(transport=httpx.MockTransport(handler))).extract([], [], [])
    assert result.items == [] and usage["total_tokens"] == 10 and req == "req-1"


@pytest.mark.parametrize("reason", ["length", "content_filter", "aborted", None])
def test_truncated_or_filtered_output_not_published(settings, reason):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": "{}"}}]})))
    with pytest.raises(ProviderError):
        DeepSeek(settings, client).extract([], [], [])


def test_asr_normalization_keeps_diarization_and_timing():
    raw = {"properties": {"original_duration_in_milliseconds": 1000}, "transcripts": [{"sentences": [{"begin_time": 0, "end_time": 500, "text": "你好", "speaker_id": 0}]}]}
    result = normalize_asr(raw)
    assert result.segments[0].speaker_id == "0"
    assert result.segments[0].start_ms == 0
    assert result.segments[0].provider_confidence is None
    assert normalize_asr(raw, channel=1).segments[0].speaker_id == "声道2"


def test_empty_asr_not_labeled_no_speech():
    with pytest.raises(ProviderError, match="EMPTY_TRANSCRIPT"):
        normalize_asr({"transcripts": []})


def test_non_ascii_key_reports_name_without_value(settings):
    settings.asr_key = "这不是有效密钥"
    with pytest.raises(ProviderError) as exc:
        AliASR(settings)
    assert "AliApiKey" in str(exc.value)
    assert settings.asr_key not in str(exc.value)


def test_whitespace_keepalive_has_total_deadline(monkeypatch):
    import interview_agent.providers as module
    times = iter([0, 181])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b" \n")))
    with pytest.raises(ProviderError, match="180") as exc:
        request_json(client, "GET", "https://api.deepseek.com/test")
    assert exc.value.retryable


def test_bundled_ffmpeg_probe_and_channel_extraction(tmp_path):
    source, target = tmp_path / "input.wav", tmp_path / "mono.wav"
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"".join(struct.pack("<hh", 0, int(10000 * math.sin(n * 0.1))) for n in range(16000)))
    info = probe(source)
    assert info["channels"] == 2 and abs(info["duration_ms"] - 1000) < 50
    prepare(source, target, channel=1)
    with wave.open(str(target), "rb") as audio:
        assert audio.getnchannels() == 1
        assert any(audio.readframes(100))


def test_bad_audio_has_specific_safe_error(tmp_path):
    from interview_agent.media import AudioProcessingError
    source = tmp_path / "private-name.wav"
    source.write_bytes(b"not audio")
    with pytest.raises(AudioProcessingError, match="无法解码") as exc:
        probe(source)
    assert "private-name" not in str(exc.value)
