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


def test_generate_high_score_in_library_and_read_in_detail(settings, store, submitted, monkeypatch):
    from interview_agent.providers import DeepSeek
    from interview_agent.reference_answers import HighScoreAnswer, answer_state
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("deepseek", "fake-deepseek")
    monkeypatch.setattr(DeepSeek, "high_score", lambda self, context: (HighScoreAnswer(answer="这是一份独立保存的参考答案", key_points=["解释边界条件"]), {}, "ui-mock"))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("问答知识库").run()
    next(b for b in app.button if b.label == "生成高分答案").click().run()
    assert not app.exception
    assert any(b.label == "重新生成高分答案" for b in app.button)
    question = store.search()[0]
    assert answer_state(store, question["revision_id"], question["id"])[0]["content"]["answer"] == "这是一份独立保存的参考答案"
    next(b for b in app.button if b.label == "查看面试与原文").click().run()
    assert not app.exception
    assert any(m.value == "这是一份独立保存的参考答案" for m in app.markdown)


def test_task_upload_progress_and_attempt_records(settings, store, submitted, monkeypatch):
    import time
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    state = {"status": "uploading", "total_bytes": 2097152, "transferred_bytes": 1572864,
             "confirmed_bytes": 1048576, "confirmed_parts": 1, "current_bps": 102400,
             "active_seconds": 15, "updated_at": time.time(), "resume_count": 1,
             "attempts": [{"started_at": time.time(), "status": "uploading", "sent_bytes": 524288,
                           "elapsed_seconds": 5, "average_bps": 104857.6, "resumed_bytes": 1048576}]}
    job = store.claim("ui-test")
    job["checkpoint"]["tracks"] = [{"upload_state": state}]
    store.checkpoint(job, "ASR_UPLOADING")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("处理任务").run()
    assert not app.exception
    assert any("已确认 1.00 MB / 1 分片" in c.value and "100.0 KB/s" in c.value for c in app.caption)
    assert any(p.value == 75 and "第 1 路上传" in p.proto.text for p in app.get("progress"))
    assert any(e.label == "第 1 路上传记录" for e in app.expander)


def test_settings_upload_test_button_and_saved_result(settings, store, monkeypatch):
    from test_uploads import Bucket
    bucket = Bucket()
    # App's imported function still resolves this module's factory default, so
    # replace the OSS instance initializer, keeping all transfer logic exercised.
    from interview_agent.providers import OSSStorage
    monkeypatch.setattr(OSSStorage, "__init__", lambda self, _: setattr(self, "bucket", bucket))
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("设置与数据").run()
    next(b for b in app.button if b.label == "开始上传测试（1 MB）").click().run()
    assert not app.exception
    assert any("上传成功" in s.value for s in app.success)
    assert any("平均速度" in c.value for c in app.caption)
    assert not bucket.objects and not bucket.sessions
    assert (settings.data_dir / "upload-test.json").exists()


def test_update_panel_check_download_state_and_maintenance(settings, store, monkeypatch):
    import httpx
    from interview_agent import updates
    from interview_agent.files import atomic_json
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    # The source application must never initiate automatic installation.
    atomic_json(settings.data_dir / "updates/check.json", {"checked_at": 1, "available": True, "version": "0.1.5", "size": 1048576, "notes": "Update notes"})
    updates.status(settings.data_dir, phase="ready", version="0.1.5")
    app = AppTest.from_file(APP, default_timeout=20).run()
    app.sidebar.radio[0].set_value("设置与数据").run()
    assert not app.exception
    assert any("发现新版 v0.1.5" in item.value for item in app.info)
    assert any("源码运行" in item.value for item in app.caption)
    assert not any(button.label == "安装更新并重启" for button in app.button)
    atomic_json(settings.data_dir / "updates/installing.json", {})
    app.run()
    assert not app.exception
    assert any("操作已暂停" in item.value for item in app.info)
    assert not app.sidebar.radio


def test_home_recent_interview_navigation_and_settings_groups(settings, store, submitted, monkeypatch):
    Pipeline(settings, store, llm_factory=FakeLLM).run_once()
    monkeypatch.setenv("DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(APP, default_timeout=20).run()
    assert not app.exception
    assert any(e.label == "录音高级选项" for e in app.expander)
    next(b for b in app.button if b.label == "查看面试").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == "面试详情"
    assert app.session_state["selected_interview"] == submitted[0]
    app.sidebar.radio[0].set_value("设置与数据").run()
    assert not app.exception
    assert {"模型与存储", "程序更新", "调用与备份"}.issubset({tab.label for tab in app.tabs})
    assert any(e.label == "预算与上传限制" for e in app.expander)
