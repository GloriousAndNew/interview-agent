from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from .config import Settings
from .db import Store, uid
from .document_import import DocumentImportError, read_document
from .files import atomic_json, atomic_text, inside, read_json
from .schemas import Transcript, import_text


def submit(settings: Settings, store: Store, upload: BinaryIO, filename: str, title: str, metadata: dict, allow_duplicate: bool = False, stereo_mode: str = "mix") -> tuple[str, str]:
    from .server_ui import write_guard
    with write_guard(store, upload, queue=True):
        return _submit(settings, store, upload, filename, title, metadata, allow_duplicate, stereo_mode)


def _submit(settings: Settings, store: Store, upload: BinaryIO, filename: str, title: str, metadata: dict, allow_duplicate: bool = False, stereo_mode: str = "mix") -> tuple[str, str]:
    title = title.strip()[:200] or Path(filename).stem[:200] or "未命名面试"
    suffix = Path(filename).suffix.lower()
    if suffix not in (".mp3", ".m4a", ".wav", ".txt", ".json", ".docx", ".md"):
        raise DocumentImportError("支持 MP3、M4A、WAV、DOCX、Markdown、TXT 和标准 JSON；旧版 DOC 请先另存为 DOCX。")
    if stereo_mode not in ("mix", "separate"):
        raise ValueError("无效的声道模式。")
    staging = settings.data_dir / "staging"
    staging.mkdir(exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=staging, suffix=suffix)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as target:
            while block := upload.read(1024 * 1024):
                size += len(block)
                if size > settings.max_upload_mb * 1024 * 1024:
                    raise ValueError("文件超过上传大小限制。")
                digest.update(block)
                target.write(block)
        if size == 0:
            raise ValueError("上传文件为空。")
        checkpoint = {"stereo_mode": stereo_mode}
        config = {k: getattr(settings, k) for k in settings.public_fields()}
        media = None
        if suffix in (".txt", ".json", ".docx", ".md"):
            if size > 10 * 1024 * 1024:
                raise DocumentImportError("问答文档、转写文本或 JSON 最多 10 MB，请缩小后上传。")
            text = read_document(Path(temp_name)) if suffix in (".docx", ".md") else Path(temp_name).read_text(encoding="utf-8-sig")
            transcript = Transcript.model_validate_json(text) if suffix == ".json" else import_text(text)
            transcript.transcript_id = uid()
            relative = f"transcripts/{transcript.transcript_id}/normalized.json"
            atomic_json(inside(settings.data_dir, relative), transcript.model_dump())
            checkpoint["transcript"] = relative
            if suffix in (".docx", ".md"):
                original = f"transcripts/{transcript.transcript_id}/source{suffix}"
                try:
                    shutil.copyfile(temp_name, inside(settings.data_dir, original))
                except OSError:
                    inside(settings.data_dir, relative).unlink(missing_ok=True)
                    inside(settings.data_dir, original).unlink(missing_ok=True)
                    raise
                checkpoint["source_document"] = {"path": original, "filename": Path(filename).name[:200]}
        else:
            old = store.one("SELECT m.*,i.id interview_id FROM media m LEFT JOIN interviews i ON i.media_id=m.id AND i.deleted=0 WHERE m.sha256=? LIMIT 1", (digest.hexdigest(),))
            if old and old.get("interview_id") and not allow_duplicate:
                raise ValueError("此录音已经导入。需要另一场记录时请勾选允许重复。")
            if old:
                media = {"id": old["id"], "sha256": old["sha256"], "filename": old["filename"], "path": old["path"], "size": old["size"]}
                # A deleted interview may have left a media tombstone.
                target = inside(settings.data_dir, old["path"])
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(temp_name, target)
            else:
                media_id = uid()
                relative = f"media/{media_id}/original{suffix}"
                target = inside(settings.data_dir, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temp_name, target)
                media = {"id": media_id, "sha256": digest.hexdigest(), "filename": Path(filename).name[:200], "path": relative, "size": size}
        try:
            return store.create(title, metadata, config, checkpoint, media)
        except Exception:
            if media and not store.one("SELECT id FROM media WHERE id=?", (media["id"],)):
                inside(settings.data_dir, media["path"]).unlink(missing_ok=True)
            if checkpoint.get("transcript"):
                inside(settings.data_dir, checkpoint["transcript"]).unlink(missing_ok=True)
            if checkpoint.get("source_document"):
                inside(settings.data_dir, checkpoint["source_document"]["path"]).unlink(missing_ok=True)
            raise
    finally:
        Path(temp_name).unlink(missing_ok=True)


def chunk_transcript(transcript: Transcript, char_limit: int = 2200, max_segments: int | None = 48) -> list[dict]:
    """Conservative character cap; not presented as an exact token count."""
    groups, current, length = [], [], 0
    for segment in transcript.segments:
        if len(segment.text_raw) > char_limit:
            raise ValueError("单个转写片段过长，请先按句拆分后导入。")
        if current and (length + len(segment.text_raw) > char_limit or (max_segments is not None and len(current) >= max_segments)):
            groups.append(current)
            current, length = [], 0
        current.append(segment)
        length += len(segment.text_raw)
    if current:
        groups.append(current)
    result = []
    previous = []
    for group in groups:
        result.append({"segments": previous + group, "required": [s.id for s in group]})
        previous = group[-2:]
    return result


def timestamp(ms: int) -> str:
    seconds = ms // 1000
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def markdown_text(store: Store, interview_id: str, revision: dict) -> str:
    from .reference_answers import answer_state, content_markdown
    interview = store.one("SELECT * FROM interviews WHERE id=?", (interview_id,))
    content = json.loads(revision["content"])
    transcript = Transcript.model_validate(read_json(inside(store.root, revision["transcript_path"])))
    segments = {s.id: s for s in transcript.segments}
    # Front matter strings are JSON-quoted, which are valid YAML scalars.
    lines = ["---", f"interview_id: {interview_id}", f"revision_id: {revision['id']}"]
    for key in ("title", "company", "position", "date"):
        lines.append(f"{key}: {json.dumps(interview[key], ensure_ascii=False)}")
    lines += ["---", "", f"# {interview['title']}", "", "> 自动整理内容尚需核对；原始回答与 AI 补充分别保存。", "", "## 概览", ""]
    for item in content.get("summary", []):
        lines += [f"- {item}"]
    media = store.one("SELECT * FROM media WHERE id=?", (interview.get("media_id"),))
    if media:
        # Every revision file lives under notes/<interview_id>/.
        rel = os.path.relpath(inside(store.root, media["path"]), store.root / "notes" / interview_id).replace("\\", "/")
        lines += ["", f"[原始录音]({rel})"]
    lines += ["", "## 问答", ""]
    has_reference = False
    for i, qa in enumerate(content["items"], 1):
        lines += [f"### {i}. {qa['question']}", "", f"标签：{'、'.join(qa['tags'])}", "", "**回答摘要**", "", qa.get("answer_summary") or "未作答或无法辨认。", "", "**原文依据**", ""]
        for sid in dict.fromkeys(qa["question_segment_ids"] + qa["answer_segment_ids"]):
            s = segments[sid]
            timing = f"[{timestamp(s.start_ms)}–{timestamp(s.end_ms)}] " if s.start_ms is not None else ""
            label = content.get("speakers", {}).get(s.speaker_id or "", {}).get("display_name") or s.speaker_id or "未标注"
            # Quote every original line, not just the first.
            for line in f"{timing}{label}：{s.text_raw}".splitlines():
                lines.append(f"> {line}")
            lines.append("")
        if qa.get("parent_id"):
            lines += [f"关联主问题：`{qa['parent_id']}`", ""]
        if qa.get("review_reasons"):
            lines += ["待核对：" + "；".join(qa["review_reasons"]), ""]
        reference, _ = answer_state(store, revision["id"], qa["id"])
        if reference:
            has_reference = True
            lines += ["#### 面试高分答案", "", content_markdown(reference["content"]), ""]
    lines += ["## AI 补充建议", ""]
    lines += content.get("ai_advice") or (["高分参考答案已在对应问题下单独标注，面试原答保持不变。"] if has_reference else ["未生成补充答案；以上回答仅根据录音整理。"])
    return "\n".join(lines) + "\n"


def export_revision(store: Store, revision_id: str) -> Path:
    revision = store.one("SELECT * FROM revisions WHERE id=?", (revision_id,))
    if not revision:
        raise ValueError("版本不存在。")
    directory = store.root / "notes" / revision["interview_id"]
    result = directory / f"{revision_id}.md"
    text = markdown_text(store, revision["interview_id"], revision)
    atomic_text(result, text)
    current = store.one("SELECT active_revision FROM interviews WHERE id=?", (revision["interview_id"],))
    if current and current["active_revision"] == revision_id:
        atomic_text(directory / "interview.md", text)
    with store.transaction() as con:
        con.execute("UPDATE revisions SET export_status='done' WHERE id=?", (revision_id,))
    return result


def request_delete(store: Store, interview_id: str) -> None:
    with store.transaction() as con:
        con.execute("UPDATE interviews SET deleted=1 WHERE id=?", (interview_id,))
        con.execute("UPDATE jobs SET cancel=1 WHERE interview_id=?", (interview_id,))


def purge_deleted(store: Store) -> None:
    # Called from maintenance, never delete files still owned by a live worker.
    for interview in store.rows("SELECT * FROM interviews WHERE deleted=1"):
        with store.transaction() as con:
            active = con.execute("SELECT 1 FROM jobs WHERE interview_id=? AND status='RUNNING' AND lease_until>?", (interview["id"], time.time())).fetchone()
            if active:
                continue
            rows = con.execute("SELECT transcript_path FROM revisions WHERE interview_id=?", (interview["id"],)).fetchall()
            jobs = con.execute("SELECT checkpoint FROM jobs WHERE interview_id=?", (interview["id"],)).fetchall()
            paths = {r[0] for r in rows}
            paths |= {json.loads(j[0]).get("transcript") for j in jobs}
            con.execute("DELETE FROM qa WHERE revision_id IN (SELECT id FROM revisions WHERE interview_id=?)", (interview["id"],))
            con.execute("DELETE FROM revisions WHERE interview_id=?", (interview["id"],))
            # Retain only the minimal tombstone until cloud cleanup is done.
            con.execute("UPDATE interviews SET title='已删除',company='',position='',date='',active_revision=NULL WHERE id=?", (interview["id"],))
            con.execute("UPDATE jobs SET status='CANCELLED',checkpoint='{}',error='' WHERE interview_id=?", (interview["id"],))
            shared = con.execute("SELECT 1 FROM interviews WHERE media_id=? AND deleted=0", (interview["media_id"],)).fetchone()
            media = con.execute("SELECT path FROM media WHERE id=?", (interview["media_id"],)).fetchone()
            if media and not shared:
                inside(store.root, media[0]).unlink(missing_ok=True)
            for relative in paths:
                if relative:
                    path = inside(store.root, relative)
                    # Per-transcript directories hold raw ASR and model outputs.
                    if path.parent.parent == store.root / "transcripts":
                        shutil.rmtree(path.parent, ignore_errors=True)
        for name in ("notes", "jobs"):
            target = inside(store.root, f"{name}/{interview['id']}")
            if target.exists():
                shutil.rmtree(target)
