from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class DataPathError(ValueError):
    """A safe, user-facing path-boundary error without absolute file paths."""


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        for attempt in range(7):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers/antivirus can briefly deny atomic replacement.
                # Preserve atomicity and fail normally on persistent permission errors.
                if attempt == 6:
                    raise
                time.sleep(0.01 * 2 ** attempt)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path: Path, value: object) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def inside(base: Path, relative: str) -> Path:
    result = (base / relative).resolve()
    if not result.is_relative_to(base.resolve()):
        raise DataPathError("文件路径超出数据目录，请检查数据目录配置或恢复原目录后重试。")
    return result

