import math
import struct
import subprocess
import wave

import pytest

from interview_agent.media import AudioProcessingError, executable, prepare, probe


def write_audio(path, seconds=10, stereo=False):
    with wave.open(str(path), "wb") as file:
        file.setparams((2 if stereo else 1, 2, 16000, 0, "NONE", "not compressed"))
        file.writeframes(b"".join(struct.pack("<hh" if stereo else "<h", *([0, int(10000 * math.sin(i * 0.1))] if stereo else [int(10000 * math.sin(i * 0.1))])) for i in range(16000 * seconds)))


def decode(path):
    result = subprocess.run([executable(), "-v", "error", "-i", str(path), "-f", "s16le", "-acodec", "pcm_s16le", "-"], capture_output=True, check=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return [value[0] for value in struct.iter_unpack("<h", result.stdout)]


def test_compressed_copy_is_decodable_smaller_and_original_untouched(tmp_path):
    source, target = tmp_path / "原始录音.wav", tmp_path / "上传副本.mp3"
    write_audio(source)
    original = source.read_bytes()
    prepare(source, target)
    assert source.read_bytes() == original
    assert target.stat().st_size < source.stat().st_size * 0.3
    info = probe(target)
    assert info["channels"] == 1 and abs(info["duration_ms"] - 10000) < 200
    audio = decode(target)
    assert abs(len(audio) - 160000) < 1600
    assert max(audio) > 1000


def test_mp3_channel_extraction_keeps_sides_separate(tmp_path):
    source = tmp_path / "stereo.wav"
    write_audio(source, seconds=3, stereo=True)
    sides = []
    for channel in (0, 1):
        target = tmp_path / f"side-{channel}.mp3"
        prepare(source, target, channel)
        sides.append(decode(target))
    assert max(map(abs, sides[0])) < 20
    assert max(map(abs, sides[1])) > 1000


def test_cannot_overwrite_original(tmp_path):
    source = tmp_path / "original.mp3"
    source.write_bytes(b"original-content")
    with pytest.raises(AudioProcessingError, match="原始录音"):
        prepare(source, source)
    assert source.read_bytes() == b"original-content"


def test_failed_compression_removes_partial_copy_only(tmp_path, monkeypatch):
    source, output = tmp_path / "private-source.wav", tmp_path / "partial.mp3"
    source.write_bytes(b"original")
    output.write_bytes(b"partial")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("ffmpeg", 600)))
    with pytest.raises(AudioProcessingError, match="10 分钟") as error:
        prepare(source, output)
    assert source.read_bytes() == b"original" and not output.exists()
    assert source.name not in str(error.value)
