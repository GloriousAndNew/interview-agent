"""Local personal documents and bounded, Chinese-aware lexical retrieval.

No embeddings or cloud requests are made while importing or retrieving documents.
Originals and immutable chunks live in SQLite, so consistent backups include both.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import re
import tempfile
import time

from .db import uid
from .document_import import DocumentImportError, read_document


KINDS = ("简历", "面试讲稿", "项目材料")
FORMATS = ("pdf", "docx", "txt", "md")
DEFAULT_PROFILE = {"enabled": False, "position": "", "job_description": "", "length": "标准版（约 2 分钟）"}
LENGTHS = ("简短版（约 1 分钟）", "标准版（约 2 分钟）", "深入版（约 3 分钟）")


def guard(store):
    if (store.root / "updates/installing.json").exists():
        raise ValueError("正在安装更新，请重启后修改个人资料。")


def profile(store):
    row = store.one("SELECT content FROM personal_profile WHERE id=1")
    return DEFAULT_PROFILE | (json.loads(row["content"]) if row else {})


def save_profile(store, *, enabled, position, job_description, length):
    if len(position) > 100 or len(job_description) > 6000 or length not in LENGTHS:
        raise ValueError("目标岗位或岗位描述超过上限。")
    with store.transaction() as con:
        guard(store)
        con.execute("INSERT OR REPLACE INTO personal_profile VALUES (1,?)", (json.dumps(dict(enabled=bool(enabled), position=position.strip(), job_description=job_description.strip(), length=length), ensure_ascii=False),))


def extract(data: bytes, suffix: str) -> str:
    if suffix in (".txt", ".md"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError:
            raise DocumentImportError("TXT / Markdown 请使用 UTF-8 编码。") from None
    elif suffix == ".pdf":
        from pypdf import PdfReader
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted or len(reader.pages) > 100:
                raise ValueError()
            parts, size, has_text = [], 0, False
            for number, page in enumerate(reader.pages, 1):
                part = page.extract_text() or ""
                has_text = has_text or bool(part.strip())
                size += len(part)
                if size > 200_000:
                    raise ValueError()
                parts.append(f"[第 {number} 页]\n{part}")
            if not has_text:
                raise DocumentImportError("PDF 没有可提取的文字，扫描件请先进行 OCR。")
            text = "\n\n".join(parts)
        except DocumentImportError:
            raise
        except Exception:
            raise DocumentImportError("无法读取 PDF，请使用未加密、100 页以内的文字版 PDF。") from None
    else:
        with tempfile.TemporaryDirectory(prefix="interview-personal-") as folder:
            path = Path(folder) / "document.docx"
            path.write_bytes(data)
            text = read_document(path)
    text = text.replace("\x00", "").strip()
    if not text or len(text) > 200_000:
        raise DocumentImportError("资料需包含文字，且不超过 20 万字符。")
    return text


def add_document(store, stream, filename: str, kind: str) -> str:
    from .server_ui import write_guard
    with write_guard(store, stream):
        return _add_document(store, stream, filename, kind)


def _add_document(store, stream, filename: str, kind: str) -> str:
    guard(store)
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1][:200]
    suffix = Path(filename).suffix.lower()
    if kind not in KINDS or suffix.lstrip(".") not in FORMATS:
        raise DocumentImportError("请选择简历、面试讲稿或项目材料，上传 PDF、DOCX、TXT、Markdown。")
    data = stream.read(10 * 1024 * 1024 + 1)
    if not data or len(data) > 10 * 1024 * 1024:
        raise DocumentImportError("个人资料文件需非空且不超过 10 MB。")
    digest = sha256(data).hexdigest()
    text = extract(data, suffix)
    document_id = uid()
    with store.transaction() as con:
        guard(store)
        if con.execute("SELECT 1 FROM personal_documents WHERE kind=? AND sha256=?", (kind, digest)).fetchone():
            raise DocumentImportError("此分类已存在相同文件，请直接启用已有版本。")
        if con.execute("SELECT COUNT(*) FROM personal_documents").fetchone()[0] >= 50:
            raise DocumentImportError("最多保留 50 份个人资料，请删除旧版本后上传。")
        # Only one resume version is current; scripts/materials can coexist.
        if kind == "简历":
            con.execute("UPDATE personal_documents SET active=0 WHERE kind='简历'")
        con.execute("INSERT INTO personal_documents VALUES (?,?,?,?,?,?,1,?)", (document_id, kind, filename, digest, data, text, time.time()))
        for number, start in enumerate(range(0, len(text), 700), 1):
            con.execute("INSERT INTO personal_chunks VALUES (?,?,?,?)", (f"{document_id}:{number}", document_id, number, text[start:start + 900]))
    return document_id


def documents(store):
    return store.rows("SELECT id,kind,filename,active,created,length(text) characters FROM personal_documents ORDER BY created DESC,rowid DESC")


def set_active(store, document_id, active):
    with store.transaction() as con:
        guard(store)
        row = con.execute("SELECT kind FROM personal_documents WHERE id=?", (document_id,)).fetchone()
        if not row:
            raise ValueError("资料已删除，请刷新。")
        if active and row[0] == "简历":
            con.execute("UPDATE personal_documents SET active=0 WHERE kind='简历'")
        con.execute("UPDATE personal_documents SET active=? WHERE id=?", (int(active), document_id))


def delete_document(store, document_id):
    with store.transaction() as con:
        guard(store)
        con.execute("DELETE FROM personal_documents WHERE id=?", (document_id,))


def tokens(text):
    text = text.lower()
    english = re.findall(r"[a-z0-9_+#.]+", text)
    chinese = re.findall(r"[\u4e00-\u9fff]+", text)
    return english + [s[i:i + 2] for s in chinese for i in range(len(s) - 1)]


def retrieve(store, question: str, position: str = "", job_description: str = "") -> list[dict]:
    rows = store.rows("SELECT c.id,c.text,c.ordinal,d.id document_id,d.filename,d.kind,d.created FROM personal_chunks c JOIN personal_documents d ON d.id=c.document_id WHERE d.active=1 ORDER BY d.created DESC,c.ordinal")
    query = Counter(tokens(question))
    secondary = set(tokens(position + " " + job_description[:6000]))
    counts = [Counter(tokens(row["text"])) for row in rows]
    df = Counter(t for count in counts for t in count)
    average = sum(sum(c.values()) for c in counts) / max(1, len(counts))
    scored = []
    for row, count in zip(rows, counts):
        score = 0.0
        document_length = sum(count.values())
        for term in query.keys() | secondary:
            freq = count[term]
            if freq:
                idf = math.log(1 + (len(rows) - df[term] + .5) / (df[term] + .5))
                score += (3 if term in query else .35) * idf * freq * 2.2 / (freq + 1.2 * (.25 + .75 * document_length / max(1, average)))
        if score:
            scored.append((score, row))
    ranked = [row for _, row in sorted(scored, key=lambda item: item[0], reverse=True)]
    # A compact current-resume context helps broad self-introduction questions.
    resume = [row for row in rows if row["kind"] == "简历"][:2]
    selected, per_document = [], Counter()
    for row in ranked[:4] + resume + ranked[4:]:
        if row["id"] in {r["id"] for r in selected} or per_document[row["document_id"]] >= 3:
            continue
        selected.append(row)
        per_document[row["document_id"]] += 1
        if len(selected) >= 8:
            break
    return selected


def answer_context(store, question: str, original: str, position: str):
    prefs = profile(store)
    if not prefs["enabled"]:
        return None
    evidence = retrieve(store, question, prefs["position"] or position, prefs["job_description"])
    # Historical answers remain labeled interview records, never personal truth.
    history = store.search("", "")
    terms = set(tokens(question))
    history = sorted(history, key=lambda r: len(terms & set(tokens(r["question"]))), reverse=True)
    history = [{"question": r["question"], "answer": (r["answer"] or "")[:1200], "title": r["title"]} for r in history if r["question"] != question and terms & set(tokens(r["question"]))][:3]
    return {"position": prefs["position"] or position, "job_description": prefs["job_description"], "length": prefs["length"], "evidence": evidence, "history_for_review_only": history}
