import io
import json
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest

from interview_agent.document_import import DocumentImportError, read_document
from interview_agent.files import read_json
from interview_agent.pipeline import Pipeline
from interview_agent.schemas import Transcript
from interview_agent.services import purge_deleted, request_delete, submit
from conftest import FakeLLM


def docx(body):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>')
    output.seek(0)
    return output


def paragraph(text):
    return '<w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p>'


def test_word_preserves_paragraph_table_order_and_current_text(tmp_path):
    source = docx(paragraph("第一部分") + '<w:tbl><w:tr><w:tc>' + paragraph("问题：幂等是什么？") + '</w:tc><w:tc>' + paragraph("回答：重复执行效果相同。") + '</w:tc></w:tr></w:tbl>' + '<w:p><w:del><w:r><w:delText>删除内容</w:delText></w:r></w:del><w:r><w:t>保留内容</w:t><w:br/><w:t>换行内容</w:t></w:r></w:p>')
    path = tmp_path / "sample.docx"
    path.write_bytes(source.read())
    assert read_document(path).splitlines() == ["第一部分", "问题：幂等是什么？ | 回答：重复执行效果相同。", "保留内容", "换行内容"]


def test_document_pipeline_skips_audio_and_keeps_original(settings, store):
    original = docx(paragraph("问题：幂等是什么？") + paragraph("回答：重复执行效果相同。"))
    original_bytes = original.getvalue()
    interview_id, job_id = submit(settings, store, original, "面试问答.DOCX", "文档面试", {})
    cp = json.loads(store.one("SELECT checkpoint FROM jobs WHERE id=?", (job_id,))["checkpoint"])
    assert (settings.data_dir / cp["source_document"]["path"]).read_bytes() == original_bytes
    transcript = Transcript.model_validate(read_json(settings.data_dir / cp["transcript"]))
    assert len(transcript.segments) == 2
    assert all(s.start_ms is None and s.speaker_id is None for s in transcript.segments)

    def unexpected_audio(*args):
        pytest.fail("Document import must not contact ASR or OSS")

    Pipeline(settings, store, asr_factory=unexpected_audio, storage_factory=unexpected_audio, llm_factory=FakeLLM).run_once()
    assert store.one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "COMPLETED"
    assert len(store.search("幂等")) == 1
    assert not store.rows("SELECT id FROM cleanup")
    request_delete(store, interview_id)
    purge_deleted(store)
    assert not (settings.data_dir / cp["source_document"]["path"]).exists()


@pytest.mark.parametrize("content", [b"not a docx", docx("").getvalue(), docx('<w:p><w:r><w:drawing/></w:r></w:p>').getvalue()])
def test_invalid_or_image_only_word_does_not_create_jobs(settings, store, content):
    with pytest.raises(DocumentImportError):
        submit(settings, store, io.BytesIO(content), "bad.docx", "bad", {})
    assert not store.rows("SELECT id FROM jobs")
    assert not list((settings.data_dir / "staging").iterdir())


def test_word_rejects_dtd(tmp_path):
    path = tmp_path / "bad.docx"
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", '<!DOCTYPE document [<!ENTITY a "hidden">]><document>&a;</document>')
    with pytest.raises(DocumentImportError):
        read_document(path)


def test_markdown_and_legacy_word_errors(settings, store):
    _, job_id = submit(settings, store, io.BytesIO("# 面试问答\n问题：缓存是什么？\n回答：用于加快读取。".encode("utf-8-sig")), "qa.md", "Markdown", {})
    cp = json.loads(store.one("SELECT checkpoint FROM jobs WHERE id=?", (job_id,))["checkpoint"])
    assert cp["source_document"]["filename"] == "qa.md"
    assert "问题：缓存是什么？" in str(read_json(settings.data_dir / cp["transcript"]))
    with pytest.raises(DocumentImportError, match="UTF-8"):
        submit(settings, store, io.BytesIO(b"\xff\xff"), "bad.md", "bad", {})
    with pytest.raises(DocumentImportError, match="DOCX"):
        submit(settings, store, io.BytesIO(b"legacy"), "old.doc", "old", {})
