from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def registry_values(name: str) -> list[str]:
    values = []
    if os.name == "nt":
        import winreg
        for hive, path in (
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    value, _ = winreg.QueryValueEx(key, name)
                    if isinstance(value, str):
                        values.append(value.strip())
            except OSError:
                continue
    return values


def environment(name: str) -> str:
    """Process override, then user/system environment; never log values."""
    if name in os.environ:
        return os.environ[name].strip()
    return next((os.path.expandvars(v) for v in registry_values(name) if v), "")


def credential_environment(name: str) -> str:
    """Skip malformed stale credentials when a valid-format value exists.

    Explicitly empty process variables still disable the credential. Format
    checks do not assert authenticity and never trigger network fallback.
    """
    process = os.environ.get(name)
    if process is not None and not process.strip():
        return ""
    candidates = [process.strip() if process is not None else "", *registry_values(name)]
    valid = next((v for v in candidates if v and v.isascii() and all(33 <= ord(c) <= 126 for c in v)), None)
    return valid if valid is not None else next((v for v in candidates if v), "")


def default_dir() -> Path:
    return Path(environment("DATA_DIR") or Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "InterviewAgent")


def canonical_directory(path: Path) -> Path:
    """Resolve the real writable directory, including Windows MSIX redirection.

    Resolving an AppData directory alone may leave its logical path unchanged,
    while an existing child resolves into the package's LocalCache directory.
    Probe a newly-created child so all processes use the same physical root.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path, prefix=".path-check-") as marker:
        return Path(marker).resolve().parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_dir)
    asr_model: str = "qwen-audio-3.1-asr-flash-filetrans"
    asr_base: str = "https://dashscope.aliyuncs.com/api/v1"
    llm_model: str = "deepseek-flash"
    llm_base: str = "https://api.deepseek.com"
    oss_bucket: str = ""
    oss_endpoint: str = ""
    max_upload_mb: int = 500
    max_audio_minutes: int = 120
    monthly_budget_cny: float = 50.0
    # Conservative reservation, not a supplier price guarantee.
    audio_reserve_per_hour: float = 5.0
    llm_reserve_per_chunk: float = 0.5
    asr_key: str = field(default="", repr=False)
    deepseek_key: str = field(default="", repr=False)
    oss_access_id: str = field(default="", repr=False)
    oss_access_secret: str = field(default="", repr=False)
    oss_token: str = field(default="", repr=False)

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Settings":
        base = canonical_directory(data_dir or default_dir())
        public = {}
        config = base / "settings.json"
        if config.exists():
            public = json.loads(config.read_text(encoding="utf-8"))
        allowed = set(cls.public_fields())
        public = {k: v for k, v in public.items() if k in allowed}
        for env, field_name in {
            "ASR_MODEL": "asr_model", "ASR_BASE_URL": "asr_base",
            "LLM_MODEL": "llm_model", "LLM_BASE_URL": "llm_base",
            "ASR_STORAGE_BUCKET": "oss_bucket", "OSS_ENDPOINT": "oss_endpoint",
        }.items():
            if environment(env):
                public[field_name] = environment(env)
        settings = cls(data_dir=Path(base), **public)
        settings.asr_key = credential_environment("AliApiKey")
        settings.deepseek_key = credential_environment("deepseek")
        settings.oss_access_id = credential_environment("OSS_ACCESS_KEY_ID")
        settings.oss_access_secret = credential_environment("OSS_ACCESS_KEY_SECRET")
        settings.oss_token = credential_environment("OSS_SESSION_TOKEN")
        settings.validate()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        return settings

    @staticmethod
    def public_fields() -> tuple[str, ...]:
        return ("asr_model", "asr_base", "llm_model", "llm_base", "oss_bucket", "oss_endpoint", "max_upload_mb", "max_audio_minutes", "monthly_budget_cny", "audio_reserve_per_hour", "llm_reserve_per_chunk")

    def validate(self) -> None:
        from urllib.parse import urlsplit
        if self.asr_model.startswith("deepseek"):
            raise ValueError("DeepSeek 不提供音频转写；请配置 ASR 模型。")
        # Keys may only be sent to the selected supplier's official TLS hosts.
        for value, suffix in ((self.asr_base, ".aliyuncs.com"), (self.llm_base, ".deepseek.com")):
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if parsed.scheme != "https" or not host.endswith(suffix) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None, 443):
                raise ValueError("模型地址必须为对应服务商的官方 HTTPS 地址。")
        if self.oss_endpoint:
            parsed = urlsplit(self.oss_endpoint)
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".aliyuncs.com") or parsed.query or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("OSS endpoint 必须为阿里云 HTTPS 地址。")
        if not 1 <= int(self.max_upload_mb) <= 500 or not 1 <= int(self.max_audio_minutes) <= 120:
            raise ValueError("上传限制须在 1–500 MB、1–120 分钟内。")
        if self.monthly_budget_cny <= 0 or self.audio_reserve_per_hour <= 0 or self.llm_reserve_per_chunk <= 0:
            raise ValueError("预算和费用预留额必须大于零。")

    def save(self) -> None:
        self.validate()
        from .files import atomic_json
        atomic_json(self.data_dir / "settings.json", {k: getattr(self, k) for k in self.public_fields()})

    def missing_asr(self) -> list[str]:
        return [name for name, value in (("AliApiKey", self.asr_key), ("OSS_ACCESS_KEY_ID", self.oss_access_id), ("OSS_ACCESS_KEY_SECRET", self.oss_access_secret), ("ASR_STORAGE_BUCKET", self.oss_bucket), ("OSS_ENDPOINT", self.oss_endpoint)) if not value]

