from io import BytesIO
import json
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from interview_agent import personal
from interview_agent.db import Store
from interview_agent.document_import import DocumentImportError
from interview_agent.pipeline import Pipeline
from interview_agent.providers import ProviderError
from interview_agent.reference_answers import HighScoreAnswer, answer_state, generate, content_markdown
from conftest import FakeLLM
from test_ui import APP


def add(store, text, name="简历.txt", kind="简历"):
    return personal.add_document(store, BytesIO(text.encode()), name, kind)


def enable(store):
    personal.save_profile(store, enabled=True, position="后端开发", job_description="负责订单幂等与支付可靠性", length=personal.LENGTHS[0])


def test_resume_versions_activation_deletion_and_backup(store):
    old = add(store, "旧简历：Java 开发")
    new = add(store, "新简历：订单幂等实现")
    script = add(store, "订单服务使用唯一请求号解决重复提交", "讲稿.md", "面试讲稿")
    assert [r["active"] for r in personal.documents(store) if r["kind"] == "简历"] == [1, 0]
    with pytest.raises(DocumentImportError, match="相同"):
        add(store, "新简历：订单幂等实现")
    personal.set_active(store, old, True)
    assert store.one("SELECT active FROM personal_documents WHERE id=?", (new,))["active"] == 0
    backup = store.backup()
    with sqlite3.connect(backup) as con:
        assert con.execute("SELECT original FROM personal_documents WHERE id=?", (script,)).fetchone()[0].decode() == "订单服务使用唯一请求号解决重复提交"
    personal.delete_document(store, script)
    assert not store.rows("SELECT * FROM personal_chunks WHERE document_id=?", (script,))
    assert len(personal.documents(Store(store.root))) == 2


def test_extract_docx_pdf_text_and_validation(store):
    from zipfile import ZipFile
    from pypdf import PdfWriter
    docx = BytesIO()
    with ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>讲稿项目难点</w:t></w:r></w:p></w:body></w:document>')
    docx.seek(0)
    personal.add_document(store, docx, "面试.docx", "面试讲稿")
    assert "讲稿项目难点" in personal.retrieve(store, "项目难点")[0]["text"]
    # Real PDF text extraction, without Office or a font dependency.
    from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 100 Td (Python Redis project) Tj ET")
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    page[NameObject("/Contents")] = stream
    pdf = BytesIO()
    writer.write(pdf)
    assert "Python Redis" in personal.extract(pdf.getvalue(), ".pdf")
    blank = PdfWriter()
    blank.add_blank_page(width=300, height=200)
    output = BytesIO()
    blank.write(output)
    with pytest.raises(DocumentImportError, match="OCR"):
        personal.extract(output.getvalue(), ".pdf")
    for data, name in [(b"", "x.txt"), (b"x", "x.exe"), (b"\xff", "x.txt"), (b"bad-pdf", "x.pdf"), (b"x" * (10 * 1024 * 1024 + 1), "x.txt")]:
        with pytest.raises(DocumentImportError):
            personal.add_document(store, BytesIO(data), name, "简历")


def test_retrieval_selects_relevant_active_sources_and_bounds_context(store):
    old = add(store, "幂等旧指标 999 万")
    add(store, "后端开发，负责订单系统")
    target = add(store, "\n".join(["项目使用 Redis Lua 实现订单幂等，个人负责重复请求处理。"] * 50), "项目讲稿.txt", "面试讲稿")
    add(store, "摄影旅行爱好记录", "无关.txt", "项目材料")
    results = personal.retrieve(store, "Redis 幂等如何实现？")
    assert results[0]["document_id"] == target
    assert old not in {r["document_id"] for r in results}
    assert len(results) <= 8 and all(len(r["text"]) <= 900 for r in results)
    assert not any(r["filename"] == "无关.txt" for r in results)
    assert personal.answer_context(store, "问题", "原答", "岗位") is None
    enable(store)
    assert personal.answer_context(store, "幂等", "原答", "岗位")["length"] == personal.LENGTHS[0]


def test_personalized_generation_citations_snapshot_and_unknown_reference(settings, store, submitted):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    document = add(store, "我负责订单幂等，使用唯一请求号和数据库唯一约束。")
    enable(store)
    question = store.search()[0]
    class Coach:
        def __init__(self, settings): pass
        def close(self): pass
        def high_score(self, context):
            evidence = context["personal_context"]["evidence"]
            assert context["personal_context"]["position"] == "后端开发"
            return HighScoreAnswer(answer="结合订单项目说明唯一约束和重试边界。", key_points=["定义幂等"], citation_ids=[evidence[0]["id"]]), {}, "mock"
    result = generate(settings, store, question["revision_id"], question["id"], provider_factory=Coach)
    personal.delete_document(store, document)
    assert answer_state(store, question["revision_id"], question["id"])[0]["content"] == result
    assert "我负责订单幂等" in content_markdown(result)
    class Invalid(Coach):
        def high_score(self, context):
            return HighScoreAnswer(answer="错误引用", key_points=["要点"], citation_ids=["invented"]), {}, "mock"
    with pytest.raises(ProviderError, match="未提供"):
        generate(settings, store, question["revision_id"], question["id"], provider_factory=Invalid)
    assert answer_state(store, question["revision_id"], question["id"])[0]["content"] == result


def test_personal_ui_preferences_preview_activation_and_deletion(settings, store, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    doc = add(store, "Redis 幂等项目经历")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("个人信息").run()
    assert not app.exception
    assert any(t.label == "提取正文" and "Redis" in t.value for t in app.text_area)
    next(c for c in app.checkbox if c.label == "生成高分答案时使用个人资料").check()
    next(t for t in app.text_input if t.label == "目标岗位").set_value("Python 后端")
    next(b for b in app.button if b.label == "保存生成偏好").click().run()
    assert not app.exception and personal.profile(store)["position"] == "Python 后端"
    next(b for b in app.button if b.label == "停用此版本").click().run()
    assert not app.exception and not personal.documents(store)[0]["active"]
    next(c for c in app.checkbox if c.label == "确认永久删除此资料").check().run()
    next(b for b in app.button if b.label == "删除资料").click().run()
    assert not app.exception and not personal.documents(store)


def test_maintenance_prevents_personal_writes(store):
    (store.root / "updates").mkdir()
    (store.root / "updates/installing.json").write_text("{}")
    with pytest.raises(ValueError, match="更新"):
        add(store, "简历")


def test_library_personal_generation_displays_evidence(settings, store, submitted, monkeypatch):
    from interview_agent.providers import DeepSeek
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    add(store, "我在订单项目中负责幂等请求处理。")
    enable(store)
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("deepseek", "fake-key")
    def high_score(self, context):
        evidence = context["personal_context"]["evidence"]
        return HighScoreAnswer(answer="我的订单幂等项目参考答案", key_points=["区分个人职责"], citation_ids=[evidence[0]["id"]], conflicts=["指标口径需本人确认"]), {}, "mock"
    monkeypatch.setattr(DeepSeek, "high_score", high_score)
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("问答知识库").run()
    next(b for b in app.button if b.label == "生成高分答案").click().run()
    assert not app.exception
    assert any(e.label == "查看个人资料参考依据" for e in app.expander)
    assert any("模型引用" in t.value for t in app.text)
    assert any("个性化答案" in t.value for t in app.caption)


def test_personal_page_only_renders_selected_document(settings, store, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    resume = add(store, "订单项目经历", "示例简历.txt")
    script = add(store, "缓存一致性讲稿", "示例讲稿.txt", "面试讲稿")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("个人信息").run()
    assert not app.exception
    assert len([t for t in app.text_area if t.label == "提取正文"]) == 1
    app.selectbox(key="personal-selected-document").set_value(resume).run()
    assert next(t for t in app.text_area if t.label == "提取正文").value == "订单项目经历"
    app.selectbox(key="personal-kind-filter").set_value("面试讲稿").run()
    assert not app.exception
    assert app.selectbox(key="personal-selected-document").value == script
    assert next(t for t in app.text_area if t.label == "提取正文").value == "缓存一致性讲稿"
