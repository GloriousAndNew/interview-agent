from __future__ import annotations

import re
import subprocess
from pathlib import Path

UPLOAD_BITRATE_KBPS = 64
UPLOAD_SAMPLE_RATE = 16000


class AudioProcessingError(ValueError):
    """Known audio errors whose messages do not expose subprocess inputs."""


def executable() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe(path: Path) -> dict:
    try:
        process = subprocess.run([executable(), "-hide_banner", "-i", str(path)], capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise AudioProcessingError("读取音频信息超时，请检查文件是否完整或从检查点重试。") from None
    output = process.stderr.decode("utf-8", errors="replace")
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    audio = re.search(r"Audio:.*", output)
    if not duration or not audio:
        raise AudioProcessingError("无法解码音频或无法获取时长，请检查录音是否完整，支持 MP3、M4A 或 WAV。")
    hours, minutes, seconds = map(float, duration.groups())
    desc = audio.group(0)
    channels = 2 if "stereo" in desc else 1 if "mono" in desc else None
    if channels is None:
        raise AudioProcessingError("第一版仅支持单声道或双声道音频。")
    return {"duration_ms": round((hours * 3600 + minutes * 60 + seconds) * 1000), "channels": channels}


def prepare(path: Path, output: Path, channel: int | None = None) -> None:
    if path.resolve() == output.resolve():
        raise AudioProcessingError("上传副本不能覆盖原始录音。")
    if output.suffix.lower() not in (".mp3", ".wav"):
        raise AudioProcessingError("上传副本仅支持 MP3 或兼容的 WAV 格式。")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [executable(), "-v", "error", "-y", "-i", str(path), "-vn", "-map_metadata", "-1"]
    if channel is not None:
        command += ["-af", f"pan=mono|c0=c{channel}"]
    command += ["-ac", "1", "-ar", str(UPLOAD_SAMPLE_RATE)]
    if output.suffix.lower() == ".mp3":
        command += ["-c:a", "libmp3lame", "-b:a", f"{UPLOAD_BITRATE_KBPS}k"]
    else:
        # Retain PCM support for older utilities and existing checkpoints.
        command += ["-c:a", "pcm_s16le"]
    command.append(str(output))
    try:
        result = subprocess.run(command, capture_output=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        output.unlink(missing_ok=True)
        raise AudioProcessingError("音频格式转换超过 10 分钟，请检查文件完整性和电脑负载后重试。") from None
    if result.returncode:
        output.unlink(missing_ok=True)
        raise AudioProcessingError("音频格式转换失败，请检查源录音是否完整。")
