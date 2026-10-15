import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from interview_agent.pipeline import Pipeline
from conftest import FakeLLM


APP = str(Path(__file__).resolve().parents[1] / "app.py")


def test_empty_app_navigation(settings, store, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception
    for page in ["处理任务", "问答知识库", "面试详情", "设置与数据", "导入面试"]:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, page


def test_knowledge_base_opens_detail_and_edits(settings, store, submitted, monkeypatch):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("问答知识库").run()
    assert not app.exception
    next(b for b in app.button if b.label == "查看面试与原文").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == "面试详情"
    next(t for t in app.text_area if t.label == "修改回答摘要").set_value("这是人工修改的摘要")
    next(b for b in app.button if b.label == "保存人工版本").click().run()
    assert not app.exception
    assert store.search("人工修改")


def test_tasks_show_diagnostic_timeline(settings, store, submitted, monkeypatch):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("处理任务").run()
    assert not app.exception
    assert any(e.label == "处理链路与错误详情" for e in app.expander)
    assert len(app.dataframe) >= 1
    metrics = {m.label: m.value for m in app.metric}
    assert "总历时" in metrics and metrics["总历时"] != "—"
    assert "最后阶段耗时" in metrics
    assert any("开始：" in c.value and "结束：" in c.value for c in app.caption)
    assert any(p.value == 100 and "全部处理完成" in p.proto.text for p in app.get("progress"))


def test_task_filters_search_and_result_navigation(settings, store, submitted, monkeypatch):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    store.create("待处理样本", {}, {}, {})
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("处理任务").run()
    app.get("button_group")[0].set_value("进行中").run()
    assert not app.exception
    assert any(b.label == "取消任务" for b in app.button)
    assert not any(b.label == "查看整理结果" for b in app.button)
    app.get("button_group")[0].set_value("已完成").run()
    assert any(b.label == "查看整理结果" for b in app.button)
    app.text_input(key="task-search").set_value("不存在的标题").run()
    assert not any(b.label == "查看整理结果" for b in app.button)
    assert any("没有符合条件" in e.value for e in app.info)
    app.text_input(key="task-search").set_value("后端").run()
    next(b for b in app.button if b.label == "查看整理结果").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == "面试详情"
    assert app.session_state["selected_interview"] == submitted[0]
