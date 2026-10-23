from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from interview_agent import __version__
from interview_agent.config import Settings
from interview_agent.db import BudgetExceeded, Store
from interview_agent.document_import import DocumentImportError
from interview_agent.diagnostics import report as diagnostic_report
from interview_agent.files import inside, read_json
from interview_agent.schemas import Transcript
from interview_agent.providers import ProviderError, validate_key
from interview_agent.progress import duration, local_time, task_progress
from interview_agent.services import export_revision, request_delete, submit, timestamp
from interview_agent.ui import apply_style, navigate, page_heading, tags, task_heading, section_label, connection_cards, empty_state, recent_heading, page_rows
from interview_agent.reference_answers import answer_state, generate as generate_reference, markdown as reference_markdown
from interview_agent.uploads import upload_summary
from interview_agent.upload_test import test_upload


st.set_page_config(page_title="面试录音整理助手", page_icon="🎙️", layout="wide")


def context():
    settings = Settings.load()
    return settings, Store(settings.data_dir)


def home(settings, store):
    page_heading("导入面试", "上传录音或问答文档，自动整理到知识库。", "导入面试")
    interviews = store.one("SELECT COUNT(*) n FROM interviews WHERE deleted=0")["n"]
    questions = store.one("SELECT COUNT(*) n FROM qa q JOIN interviews i ON i.active_revision=q.revision_id WHERE i.deleted=0")["n"]
    pending = store.one("SELECT COUNT(*) n FROM jobs j JOIN interviews i ON i.id=j.interview_id WHERE i.deleted=0 AND j.status IN ('QUEUED','RUNNING','RETRY_WAIT')")["n"]
    st.caption(f"{interviews} 场面试 · {questions} 条问答 · {pending} 个待处理任务")
    with st.form("upload", clear_on_submit=False):
        upload = st.file_uploader("录音或面试问答文档", type=["mp3", "m4a", "wav", "docx", "md", "txt", "json"], max_upload_size=settings.max_upload_mb, help="文档最多 10 MB。Word 读取正文和表格；Markdown/TXT 使用 UTF-8。旧版 DOC 请先另存为 DOCX。")
        title = st.text_input("面试标题", max_chars=200, placeholder="例如：后端开发 · 第一轮")
        a, b, c = st.columns(3)
        company = a.text_input("公司（可选）", max_chars=100)
        position = b.text_input("岗位（可选）", max_chars=100)
        date = c.date_input("面试日期（可选）", value=None)
        with st.expander("录音高级选项"):
            mode = st.radio("双声道录音的处理方式", ["普通立体声：合并识别并区分说话人", "双方分别占一个声道：拆开识别"], help="拆开识别会分别计费。单声道录音不受此选项影响。")
            duplicate = st.checkbox("允许同一录音创建另一场面试")
        st.caption("录音由阿里云转写，文字由 DeepSeek 整理，结果保存在本机。问答文档跳过语音转写。")
        with st.expander("如何准备问答文档"):
            st.write("可按「问题：…… / 回答：……」逐条编写，也可在 Word 中使用问题、回答两列表格。无需固定模板。")
            st.caption("Word 读取正文段落与表格，保留原文件；图片文字、批注、页眉页脚不参与整理。文档不生成录音时间戳或回听入口。")
        clicked = st.form_submit_button("开始整理", type="primary")
    if clicked:
        if not upload:
            st.warning("请先选择一个文件。")
        else:
            try:
                upload.seek(0)
                interview_id, job_id = submit(settings, store, upload, upload.name, title, {"company": company, "position": position, "date": date.isoformat() if date else ""}, duplicate, "separate" if mode.startswith("双方") else "mix")
                st.success("已加入后台队列，可在「处理任务」查看进度。")
                st.session_state["selected_interview"] = interview_id
            except DocumentImportError as exc:
                st.error(str(exc))
            except (ValueError, OSError):
                st.error("导入失败：文件为空、重复、超过限制、编码/结构不正确或无法写入。TXT 请使用 UTF-8；JSON 请使用标准转写结构。")
    if not settings.deepseek_key:
        st.info("尚未读取到系统环境变量 deepseek，可在设置中检查。")
    if settings.missing_asr():
        st.info("录音自动转写还需补齐阿里云/OSS 配置；现在可以直接导入 DOCX、Markdown、TXT 或 JSON 整理问答。")

    st.divider()
    section_label("最近", "最近导入的面试", "继续查看问答、核对原文，或跟踪处理进度。")
    recent = store.rows("SELECT i.*, (SELECT j.status FROM jobs j WHERE j.interview_id=i.id ORDER BY j.created DESC LIMIT 1) task_status FROM interviews i WHERE i.deleted=0 ORDER BY i.created DESC LIMIT 4")
    if not recent:
        empty_state("从第一场面试开始", "导入后，最近的面试会显示在这里。")
    for item in recent:
        with st.container(border=True, key="recent-" + item["id"]):
            title_column, state_column, action_column = st.columns([4, 1.2, 1.3], vertical_alignment="center")
            with title_column:
                recent_heading(item["title"], " · ".join(filter(None, [item["position"], item["company"], item["date"]])) or "未填写岗位与日期")
            with state_column:
                task_heading("", item["task_status"] or "QUEUED", STATUS.get(item["task_status"], "待处理"))
            with action_column:
                st.button("查看面试" if item["active_revision"] else "查看进度", key="recent-open-" + item["id"], on_click=navigate, args=("面试详情" if item["active_revision"] else "处理任务", item["id"]), width="stretch")


STATUS = {"QUEUED": "排队中", "RUNNING": "处理中", "RETRY_WAIT": "等待重试", "FAILED": "失败", "CANCELLED": "已取消", "COMPLETED": "已完成"}
STAGE = {"PREPROCESSING": "音频预处理", "ASR_UPLOADING": "上传录音", "ASR_SUBMITTING": "提交转写", "ASR_WAITING": "等待转写", "TRANSCRIPT_READY": "原文已保存", "EXTRACTING": "整理问答", "VALIDATING": "校验来源", "PERSISTING": "保存知识库", "EXPORTING": "导出笔记"}


@st.fragment(run_every="3s")
def tasks(store):
    page_heading("处理任务", "跟踪每一次整理，及时查看进度与需要处理的问题。", "任务中心")
    counts = {r["status"]: r["n"] for r in store.rows("SELECT j.status,COUNT(*) n FROM jobs j JOIN interviews i ON i.id=j.interview_id WHERE i.deleted=0 GROUP BY j.status")}
    summary = st.columns(3)
    summary[0].metric("进行中", sum(counts.get(k, 0) for k in ("QUEUED", "RUNNING", "RETRY_WAIT")))
    summary[1].metric("需处理", sum(counts.get(k, 0) for k in ("FAILED", "CANCELLED")))
    summary[2].metric("已完成", counts.get("COMPLETED", 0))
    filter_column, search_column = st.columns([1.3, 1], vertical_alignment="center")
    with filter_column:
        selected = st.segmented_control("任务状态", ["全部", "进行中", "需处理", "已完成"], default="全部", key="task-filter", label_visibility="collapsed")
    with search_column:
        keyword = st.text_input("搜索任务", placeholder="输入面试标题查找任务", key="task-search", label_visibility="collapsed")
    conditions, values = ["i.deleted=0"], []
    states = {"进行中": "('QUEUED','RUNNING','RETRY_WAIT')", "需处理": "('FAILED','CANCELLED')", "已完成": "('COMPLETED')"}
    if selected in states:
        conditions.append("j.status IN " + states[selected])
    if keyword.strip():
        conditions.append("instr(lower(i.title),lower(?))>0")
        values.append(keyword.strip())
    rows = store.rows("SELECT j.*,i.title,i.media_id FROM jobs j JOIN interviews i ON i.id=j.interview_id WHERE " + " AND ".join(conditions) + " ORDER BY CASE WHEN j.status IN ('RUNNING','QUEUED','RETRY_WAIT') THEN 0 WHEN j.status='FAILED' THEN 1 ELSE 2 END,j.created DESC LIMIT 50", tuple(values))
    st.caption(f"显示 {len(rows)} 条 · 进行中任务置顶 · 每 3 秒自动刷新")
    now = time.time()
    if not rows:
        st.info("没有符合条件的任务。可切换状态或调整搜索词。" if any(counts.values()) else "还没有任务，先导入一场面试。")
    for row in page_rows(rows, "task-page"):
        with st.container(border=True, key="task-card-" + row["id"]):
            task_heading(row["title"], row["status"], STATUS.get(row["status"], row["status"]))
            st.caption(STAGE.get(row["stage"], row["stage"]))
            cp = json.loads(row["checkpoint"])
            progress = task_progress(row, cp, now)
            label = "全部处理完成" if row["status"] == "COMPLETED" else f"第 {progress['stage_index']} / {progress['stage_count']} 阶段 · {STAGE.get(row['stage'], row['stage'])}"
            if row["status"] == "QUEUED":
                label = "排队中，等待后台开始或恢复"
            st.progress(progress["fraction"], text="流程进度：" + label)
            clocks = st.columns(3)
            clocks[0].metric("总历时" if row["status"] not in ("QUEUED", "RUNNING", "RETRY_WAIT") else "已历时", duration(progress["elapsed"]))
            clocks[1].metric("当前阶段耗时" if row["status"] == "RUNNING" else "最后阶段耗时", duration(progress["stage_elapsed"]))
            clocks[2].metric("已保存整理批次", f"{progress['done_chunks']} / {progress['total_chunks']}" if progress["total_chunks"] else "尚未分批")
            if cp.get("upload_audio") or any(t.get("upload_state") for t in cp.get("tracks", [])):
                with st.expander("录音上传详情", expanded=row["status"] == "RUNNING" and row["stage"] == "ASR_UPLOADING"):
                    upload_audio = cp.get("upload_audio")
                    if upload_audio:
                        st.caption(f"上传副本：MP3 · {upload_audio['bitrate_kbps']} kbps / 路 · {upload_audio['track_count']} 路，共 {upload_audio['prepared_bytes'] / 1024 ** 2:.2f} MB；原始录音保留。")
                    for track_no, track in enumerate(cp.get("tracks", []), 1):
                        transfer = upload_summary(track)
                        if not transfer or not transfer.get("total_bytes"):
                            continue
                        total = transfer["total_bytes"]
                        sent = transfer.get("transferred_bytes", 0)
                        confirmed = transfer.get("confirmed_bytes", 0)
                        active = row["status"] == "RUNNING" and row["stage"] == "ASR_UPLOADING" and transfer["status"] == "uploading"
                        fresh = active and now - transfer.get("updated_at", 0) < 5
                        rate = transfer.get("current_bps", 0) if fresh else 0
                        finished = transfer["status"] == "completed"
                        st.progress(min(1.0, sent / total), text=f"第 {track_no} 路上传：{sent / 1048576:.2f} / {total / 1048576:.2f} MB" + (" · 已完成" if finished else " · 等待恢复" if not active else ""))
                        st.caption(f"已确认 {confirmed / 1048576:.2f} MB / {transfer.get('confirmed_parts', 0)} 分片 · 当前 {rate / 1024:.1f} KB/s · 记录上传耗时 {duration(transfer.get('active_seconds', 0))} · 恢复 {transfer.get('resume_count', 0)} 次")
                        with st.expander(f"第 {track_no} 路上传记录"):
                            st.caption("已确认分片可续传；未确认分片可能重传。耗时不含关闭程序和等待重试时间；异常退出时最后约一秒可能未记录。进度达到 100% 后仍需 OSS 合并确认。")
                            st.dataframe([{"开始": local_time(a["started_at"]), "状态": {"uploading": "上传中", "interrupted": "中断", "cancelled": "已取消", "completed": "完成"}.get(a["status"], a["status"]), "接续 MB": round(a.get("resumed_bytes", 0) / 1048576, 2), "本次发送 MB": round(a["sent_bytes"] / 1048576, 2), "耗时秒": a["elapsed_seconds"], "平均 KB/s": round(a["average_bps"] / 1024, 1)} for a in transfer.get("attempts", [])], hide_index=True)
            with st.expander("时间记录", expanded=False):
                st.caption(f"提交：{local_time(row['created'])}　开始：{local_time(row['started_at'])}　结束：{local_time(row['finished_at'])}")
                st.caption("北京时间 · 历时包含重试等待、暂停和程序关闭时间。" + ("历史任务时间按已有记录估算。" if row["timing_estimated"] else ""))
            if progress["waiting"] is not None:
                st.caption("排队等待：" + duration(progress["waiting"]))
            if progress["total_segments"]:
                st.progress(progress["saved_segments"] / progress["total_segments"], text=f"问答整理：已保存 {progress['saved_segments']} / {progress['total_segments']} 个原文片段")
            if row["status"] == "RUNNING" and row["lease_until"] < now:
                st.warning("后台心跳已过期，正在等待工作进程恢复；已保存进度保留。")
            if row["status"] == "RETRY_WAIT":
                st.caption(f"距离下次自动重试：{duration(max(0, row['retry_at'] - now))}")
            if row["status"] == "RUNNING" and row["stage"] == "ASR_WAITING":
                tracks = cp.get("tracks", [])
                finished_tracks = sum(bool(t.get("result_path")) for t in tracks)
                st.caption(f"转写：已完成 {finished_tracks} / {len(tracks)} 路；云端状态：{cp.get('asr_status', '等待查询')}。")
            if cp.get("split_count"):
                st.caption(f"已因输出长度或校验问题自动拆分 {cp['split_count']} 次；已完成的分块保持不变。")
            if row["status"] == "RUNNING" and cp.get("current_chunk"):
                st.caption(f"当前处理第 {cp['current_chunk']} / {len(cp.get('chunk_plan', []))} 批")
                if row["stage"] == "EXTRACTING" and cp.get("request_started_at") and cp["current_chunk"] > len(cp.get("chunks", [])):
                    st.caption(f"本批第 {cp.get('current_attempt', 1)} 次尝试 · 本次模型请求已等待 {duration(now - cp['request_started_at'])}")
            if row["cancel"] and row["status"] in ("RUNNING", "QUEUED", "RETRY_WAIT"):
                st.caption("已请求取消，等待当前请求结束。")
            if row["error"]:
                st.error(row["error"])
            recent = cp.get("last_diagnostic")
            if recent:
                st.caption(f"{'已恢复的最近问题' if recent.get('resolved') else '最近失败点'}：整理问答 → 第 {recent['chunk_no']} 批 → {recent['message']} [{recent['code']}]")
            if cp.get("validation_warnings"):
                st.warning("部分内容需核对：非匹配的附加建议未采用，未归类片段保留原文。请在面试详情查看。")
            if row["status"] == "COMPLETED":
                if st.button("查看整理结果", key="result-" + row["id"], on_click=navigate, args=("面试详情", row["interview_id"]), type="primary"):
                    st.rerun(scope="app")
            with st.expander("处理链路与错误详情", expanded=False):
                diagnostic = diagnostic_report(store, row["id"])
                if diagnostic["events"]:
                    st.dataframe([{"时间": datetime.fromtimestamp(e["created"], ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S"), "阶段": STAGE.get(e["stage"], e["stage"]), "批次": str(e["chunk_no"] or "—"), "尝试": str(e["attempt"] or "—"), "级别": e["severity"], "规则": e["code"], "说明": e["message"]} for e in diagnostic["events"][:20]], hide_index=True, width="stretch")
                else:
                    st.caption("此历史任务未记录字段级诊断；新处理或重试后会记录具体阶段、批次和规则。")
                if recent:
                    st.json(recent)
                st.download_button("下载诊断报告（不含录音正文和密钥）", json.dumps(diagnostic, ensure_ascii=False, indent=2), file_name=f"diagnostic-{row['id'][:8]}.json", mime="application/json", key="diagnostic-" + row["id"])
            if row["status"] in ("FAILED", "CANCELLED"):
                if st.button("从检查点重试", key="retry-" + row["id"]):
                    try:
                        store.retry(row["id"])
                        st.rerun(scope="fragment")
                    except ValueError as exc:
                        st.error(str(exc))
            elif row["status"] in ("QUEUED", "RUNNING", "RETRY_WAIT"):
                if st.button("取消任务", key="cancel-" + row["id"]):
                    store.cancel(row["id"])
                    st.rerun(scope="fragment")
    pending = store.one("SELECT COUNT(*) n FROM cleanup WHERE status='pending'")["n"]
    if pending:
        st.caption(f"有 {pending} 个云端临时音频等待安全清理；未完成的转写文件不会提前删除。")
    st.caption("流程进度按阶段和已保存片段计算，不代表耗时比例；云端暂不提供准确剩余时间。")


def high_score_panel(settings, store, revision_id, qa_id, question, original, location):
    key = f"{location}-{revision_id}-{qa_id}"
    with st.expander("面试高分答案", expanded=False):
        st.caption("AI 练习参考 · 与面试原答独立保存 · 个人事实请核对")
        saved, latest = answer_state(store, revision_id, qa_id)
        busy = bool(latest and latest["status"] == "RUNNING" and latest["lease_until"] > time.time())
        if saved:
            content = saved["content"]
            st.markdown(content["answer"])
            with st.expander("得分要点与练习建议"):
                for name, label in (("key_points", "得分要点"), ("improvements", "原答改进建议"), ("followup_questions", "可能的追问"), ("needs_personalization", "需要按个人情况补充"), ("conflicts", "资料冲突，需确认")):
                    if content.get(name):
                        st.markdown("**" + label + "**")
                        for value in content[name]:
                            st.markdown("- " + value)
            personal = content.get("personal_context")
            if personal is not None:
                st.caption(f"个性化答案 · {personal['position']} · {personal['length']} · 使用生成时的资料快照")
                with st.expander("查看个人资料参考依据"):
                    cited = set(content.get("citation_ids", []))
                    if not personal["evidence"]:
                        st.info("未检索到可用个人资料，个人经历需补充。")
                    evidence = personal["evidence"]
                    if evidence:
                        lookup = {item["id"]: item for item in evidence}
                        evidence_id = st.selectbox("选择参考片段", list(lookup), format_func=lambda cid: f"{lookup[cid]['filename']} · 片段 {lookup[cid]['ordinal']} · {'模型引用' if cid in cited else '检索候选'}", key="evidence-" + key)
                        item = lookup[evidence_id]
                        st.text(f"{'模型引用' if evidence_id in cited else '检索候选'} · 版本 {item['document_id'][:8]}")
                        st.text(item["text"])
            st.caption(f"生成于 {local_time(saved['created'])} · {saved['model']}")
            st.download_button("下载高分答案 Markdown", reference_markdown(question, original, content), file_name=f"high-score-{qa_id[:12]}.md", mime="text/markdown", key="reference-download-" + key)
        else:
            st.info("结合当前问题、岗位、原始回答和相关原文生成参考答案。")
        if latest and latest["status"] == "FAILED":
            st.warning(latest["error"])
        elif latest and latest["status"] == "RUNNING" and not busy:
            st.warning("上次生成中断或超时，可重新生成；已有答案保留。")
        if busy:
            st.info("正在生成，请稍候。完成后刷新页面查看；重复点击不会再次调用模型。")
            if st.button("刷新答案状态", key="reference-refresh-" + key):
                st.rerun()
        from interview_agent.personal import profile
        prefs = profile(store)
        st.caption(("结合个人资料生成" if prefs["enabled"] else "可到「个人信息」开启个性化") + f" · 每次预留 ¥{settings.llm_reserve_per_chunk:.2f}，非实际账单")
        if st.button("重新生成高分答案" if saved else "生成高分答案", key="reference-generate-" + key, disabled=busy, type="secondary"):
            try:
                with st.spinner("正在生成面试高分答案，请稍候…"):
                    generate_reference(settings, store, revision_id, qa_id)
            except (ProviderError, BudgetExceeded) as exc:
                st.error(str(exc))
            except ValueError:
                st.error("问答版本已变化、结果校验失败或请求已失效，请刷新后重试；已有答案保留。")
            except Exception:
                st.error("生成或保存失败，请稍后重试；已有答案保留。")
            else:
                st.rerun()


def library(settings, store):
    page_heading("问答知识库", "把经历变成积累，从真实回答中找到下一次面试的准备方向。", "知识沉淀")
    a, b = st.columns([3, 1])
    keyword = a.text_input("搜索问题、回答、主题或面试标题", placeholder="例如：Redis、项目难点")
    position = b.text_input("按岗位筛选")
    rows = store.search(keyword, position)
    st.caption(f"找到 {len(rows)} 条问答" + (" · 最多检索 100 条，可添加关键词缩小范围" if len(rows) == 100 else ""))
    visible = page_rows(rows, "knowledge-page")
    for question_no, row in enumerate(visible, 1):
        with st.container(border=True, key="knowledge-" + row["revision_id"] + row["id"]):
            st.markdown(f'<div class="question-index">QUESTION {question_no:02d} · 面试问答</div>', unsafe_allow_html=True)
            st.subheader(row["question"])
            st.caption("面试原答")
            st.write(row["answer"] or "未作答或无法辨认。")
            tags([t for t in row["tags"].split("、") if t])
            st.caption(" · ".join(filter(None, [row["title"], row["company"], row["position"], row["date"]])))
            high_score_panel(settings, store, row["revision_id"], row["id"], row["question"], row["answer"], "library")
            def open_interview(selected_id):
                st.session_state["selected_interview"] = selected_id
                st.session_state["navigation"] = "面试详情"
            st.button("查看面试与原文", key="open-" + row["revision_id"] + row["id"], on_click=open_interview, args=(row["interview_id"],))
    if not rows:
        empty_state("暂未找到问答", "试试其他关键词，或先导入一场面试。")


def details(settings, store):
    page_heading("面试详情", "对照原文和录音，核对问答、补充标签并保留修改版本。", "面试档案")
    interviews = store.rows("SELECT * FROM interviews WHERE deleted=0 ORDER BY created DESC")
    if not interviews:
        st.info("还没有面试。")
        return
    lookup = {r["id"]: r for r in interviews}
    preferred = st.session_state.get("selected_interview")
    ids = list(lookup)
    interview_id = st.selectbox("选择面试", ids, index=ids.index(preferred) if preferred in ids else 0, format_func=lambda k: f"{lookup[k]['title']} · {lookup[k]['date']} · {k[:6]}")
    st.session_state["selected_interview"] = interview_id
    interview = lookup[interview_id]
    st.subheader(interview["title"])
    st.caption(" · ".join(filter(None, [interview["company"], interview["position"], interview["date"]])) or "未填写公司、岗位与日期")
    revision = store.one("SELECT * FROM revisions WHERE id=?", (interview["active_revision"],))
    latest = store.one("SELECT checkpoint FROM jobs WHERE interview_id=? ORDER BY created DESC LIMIT 1", (interview_id,))
    cp = json.loads(latest["checkpoint"]) if latest else {}
    document = cp.get("source_document")
    if document:
        original = inside(settings.data_dir, document["path"])
        if original.exists():
            st.download_button("下载原始问答文档", original.read_bytes(), file_name=document["filename"], mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document" if original.suffix == ".docx" else "text/markdown")
    media = store.one("SELECT * FROM media WHERE id=?", (interview["media_id"],))
    if media:
        source = inside(settings.data_dir, media["path"])
        if source.exists():
            st.audio(str(source), start_time=st.session_state.get("audio_start_" + interview_id, 0))
    if not revision:
        st.info("还没有整理结果，请查看处理任务。")
        if cp.get("transcript"):
            transcript = Transcript.model_validate(read_json(inside(settings.data_dir, cp["transcript"])))
            st.text_area("已保存的转写原文", "\n".join(s.text_raw for s in transcript.segments), height=300, disabled=True)
        return
    content = json.loads(revision["content"])
    transcript = Transcript.model_validate(read_json(inside(settings.data_dir, revision["transcript_path"])))
    segments = {s.id: s for s in transcript.segments}
    for line in content.get("summary", []):
        st.write(line)
    st.caption("回答摘要由模型整理；请对照原文。人工修改会创建新版本。")
    qa_tab, transcript_tab, role_tab, history_tab = st.tabs(["问答与回听", "完整原文", "角色与校对", "版本与导出"])
    with qa_tab:
        if not content["items"]:
            st.info("本场未提取到面试问题；原文仍已保存。")
        for n, qa in enumerate(content["items"], 1):
            with st.expander(f"{n}. {qa['question']}", expanded=n == 1):
                st.caption("面试原答")
                st.write(qa.get("answer_summary") or "未作答或无法辨认。")
                high_score_panel(settings, store, revision["id"], qa["id"], qa["question"], qa.get("answer_summary"), "detail")
                st.caption("标签：" + "、".join(qa["tags"]))
                if qa.get("parent_id"):
                    st.caption("追问关联：" + next((q["question"] for q in content["items"] if q["id"] == qa["parent_id"]), "待核对"))
                for sid in dict.fromkeys(qa["question_segment_ids"] + qa["answer_segment_ids"]):
                    segment = segments[sid]
                    mapping = content.get("speakers", {}).get(segment.speaker_id or "", {})
                    label = mapping.get("display_name") or segment.speaker_id or "未标注"
                    st.write(f"{label}：{segment.text_raw}")
                    if media and segment.start_ms is not None:
                        if st.button("回听 " + timestamp(segment.start_ms), key=f"listen-{qa['id']}-{sid}"):
                            st.session_state["audio_start_" + interview_id] = segment.start_ms / 1000
                            st.rerun()
                if qa.get("review_reasons"):
                    st.warning("；".join(qa["review_reasons"]))
                with st.form("edit-" + revision["id"] + qa["id"]):
                    question = st.text_input("修改问题", qa["question"], max_chars=4000)
                    answer = st.text_area("修改回答摘要", qa.get("answer_summary") or "", max_chars=20000)
                    tags = st.text_input("标签（用中文逗号分隔）", "，".join(qa["tags"]))
                    if st.form_submit_button("保存人工版本"):
                        if not question.strip():
                            st.error("问题不能为空。")
                        else:
                            updated = copy.deepcopy(content)
                            target = next(q for q in updated["items"] if q["id"] == qa["id"])
                            target.update(question=question.strip(), answer_summary=answer.strip() or None, tags=[t.strip()[:50] for t in tags.replace(",", "，").split("，") if t.strip()][:12])
                            target["answer_status"] = "answered" if answer.strip() else "unanswered"
                            try:
                                new_id = store.edit_revision(interview_id, revision["id"], updated)
                                export_revision(store, new_id)
                                st.rerun()
                            except ValueError as exc:
                                st.error(str(exc))
    with transcript_tab:
        st.download_button("下载标准转写 JSON", json.dumps(transcript.model_dump(), ensure_ascii=False, indent=2), file_name="transcript.json", mime="application/json")
        pending = {d["segment_id"] for d in content.get("dispositions", []) if d["category"] == "needs_review"}
        if pending:
            with st.expander(f"待核对的未归类片段（{len(pending)} 个）", expanded=True):
                st.warning("这些片段尚未被可靠归入问答。保存原文不代表已完成语义整理，请对照录音核对。")
                for segment in transcript.segments:
                    if segment.id in pending:
                        st.text(f"{segment.id}：{segment.text_raw}")
                        if media and segment.start_ms is not None and st.button("回听 " + timestamp(segment.start_ms), key=f"unclassified-{revision['id']}-{segment.id}"):
                            st.session_state["audio_start_" + interview_id] = segment.start_ms / 1000
                            st.rerun()
        for segment in transcript.segments:
            marker = timestamp(segment.start_ms) if segment.start_ms is not None else "无时间戳"
            st.text(f"[{marker}] {segment.speaker_id or '未标注'}：{segment.text_raw}")
    with role_tab:
        for warning in content.get("validation_warnings", []):
            st.warning(f"第 {warning['chunk_no']} 批：{warning['message']}")
        speakers = list(dict.fromkeys(s.speaker_id for s in transcript.segments if s.speaker_id is not None))
        with st.form("roles-" + revision["id"]):
            updated_speakers = {}
            for speaker in speakers:
                previous = content.get("speakers", {}).get(speaker, {})
                left, right = st.columns(2)
                name = left.text_input(f"说话人 {speaker} 的昵称", previous.get("display_name", speaker), key=f"name-{revision['id']}-{speaker}", max_chars=50)
                role = right.selectbox(f"{speaker} 的角色", ["unknown", "interviewer", "candidate"], index=["unknown", "interviewer", "candidate"].index(previous.get("role", "unknown")), format_func=lambda x: {"unknown": "未确定", "interviewer": "面试官", "candidate": "候选人"}[x], key=f"role-{revision['id']}-{speaker}")
                updated_speakers[speaker] = {"display_name": name.strip() or speaker, "role": role}
            if st.form_submit_button("保存角色映射"):
                updated = copy.deepcopy(content)
                updated["speakers"] = updated_speakers
                try:
                    new_id = store.edit_revision(interview_id, revision["id"], updated)
                    export_revision(store, new_id)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        for n, suggestion in enumerate(content.get("corrections", [])):
            st.write(f"建议：{suggestion['original_text']} → {suggestion['suggested_text']}")
            st.caption(suggestion["reason"] + " · " + suggestion["status"])
            if suggestion["status"] == "pending":
                accept, reject = st.columns(2)
                action = "accepted" if accept.button("接受建议", key=f"accept-{revision['id']}-{n}") else "rejected" if reject.button("拒绝建议", key=f"reject-{revision['id']}-{n}") else None
                if action:
                    updated = copy.deepcopy(content)
                    updated["corrections"][n]["status"] = action
                    new_id = store.edit_revision(interview_id, revision["id"], updated)
                    export_revision(store, new_id)
                    st.rerun()
        st.caption("接受校对建议只更新校对层，不覆盖转写原文或历史问答。")
    with history_tab:
        history = store.rows("SELECT id,origin,created,export_status FROM revisions WHERE interview_id=? ORDER BY created DESC", (interview_id,))
        selected = st.selectbox("历史版本", [r["id"] for r in history], format_func=lambda k: f"{k[:10]} · {next(r['origin'] for r in history if r['id'] == k)}")
        if st.button("将所选版本设为当前版本"):
            store.set_active(interview_id, selected)
            export_revision(store, selected)
            st.rerun()
        path = export_revision(store, selected)
        st.download_button("下载所选版本 Markdown", path.read_text(encoding="utf-8"), file_name=f"interview-{selected[:8]}.md", mime="text/markdown")
        st.caption("本地笔记：" + str(path.parent))
        st.caption("外部修改 Markdown 不会自动写回数据库。媒体链接指向本机原录音。")
        if st.checkbox("确认删除本场面试及本地结果，备份单独保留", key="delete-confirm-" + interview_id):
            if st.button("删除面试", type="secondary"):
                request_delete(store, interview_id)
                st.rerun()


def settings_page(settings, store):
    page_heading("设置与数据", "管理模型连接、调用预算与本地数据。", "应用设置")
    connection_tab, update_tab, data_tab = st.tabs(["模型与存储", "程序更新", "调用与备份"])
    with connection_tab:
        st.write("模型密钥只从系统环境变量读取。这里不展示、不保存密钥值。")
        connection_cards(bool(settings.asr_key), bool(settings.deepseek_key), bool(settings.oss_access_id and settings.oss_access_secret and settings.oss_bucket and settings.oss_endpoint))
        for name, value in (("AliApiKey", settings.asr_key), ("deepseek", settings.deepseek_key)):
            if value:
                try:
                    validate_key(value, name)
                except ProviderError as exc:
                    st.warning(str(exc))
        st.caption("已读取不等于已通过云端鉴权。修改系统环境变量后可刷新页面；Windows 会回读用户/系统环境。")
        with st.expander("OSS 上传测试"):
            st.caption("使用已保存的 OSS 配置，上传 1 MB 随机测试数据并清理；产生少量 OSS 请求和存储费用，不调用 ASR 或文本模型。小文件测速仅供参考。修改下方配置后请先保存。")
            if st.button("开始上传测试（1 MB）"):
                indicator = st.progress(0.0, text="正在连接 OSS…")
                def show_test(value):
                    total = value.get("total_bytes", 1)
                    indicator.progress(min(1.0, value.get("transferred_bytes", 0) / total), text=f"测试上传：{value.get('current_bps', 0) / 1024:.1f} KB/s")
                try:
                    test_upload(settings, store, show_test)
                except ProviderError as exc:
                    st.error(str(exc))
            test_path = settings.data_dir / "upload-test.json"
            if test_path.exists():
                try:
                    tested = read_json(test_path)
                    st.caption("最近测试：" + local_time(tested["created_at"]))
                    if tested["status"] == "completed":
                        st.success(f"上传成功，总耗时 {tested['elapsed_seconds']:.2f} 秒。")
                        attempts = tested.get("upload", {}).get("attempts", [])
                        if attempts:
                            st.caption(f"本次平均速度 {attempts[-1]['average_bps'] / 1024:.1f} KB/s（含上传协议开销）。")
                    else:
                        st.error(tested.get("error", "上传测试失败。"))
                    if tested.get("cleanup_pending"):
                        cleanup = store.one("SELECT status FROM cleanup WHERE id=?", (tested.get("cleanup_id", ""),))
                        if cleanup and cleanup["status"] == "done":
                            st.caption("测试数据已由后台清理。")
                        else:
                            st.warning("测试数据尚未清理，后台会重试；请检查 OSS 删除与取消分片权限。")
                except (ValueError, OSError, KeyError):
                    st.caption("最近测试记录不可读取，请重新测试。")
        with st.form("settings"):
            st.subheader("录音转写")
            asr_model = st.text_input("阿里云 ASR 模型", settings.asr_model)
            asr_base = st.text_input("阿里云 API 基础地址", settings.asr_base)
            storage_column, endpoint_column = st.columns(2)
            bucket = storage_column.text_input("私有 OSS bucket", settings.oss_bucket)
            endpoint = endpoint_column.text_input("OSS endpoint（HTTPS）", settings.oss_endpoint, placeholder="https://oss-cn-beijing.aliyuncs.com")
            st.subheader("文本整理")
            llm_model = st.text_input("DeepSeek 模型", settings.llm_model)
            llm_base = st.text_input("DeepSeek API 基础地址", settings.llm_base)
            with st.expander("预算与上传限制"):
                st.subheader("预算与限制")
                budget = st.number_input("每月调用费用预留上限（元）", min_value=1.0, value=float(settings.monthly_budget_cny))
                audio_reserve = st.number_input("每小时音频预留费用（每声道，元）", min_value=0.1, value=float(settings.audio_reserve_per_hour))
                chunk_reserve = st.number_input("每次文本调用预留费用（元）", min_value=0.01, value=float(settings.llm_reserve_per_chunk))
                max_mb = st.number_input("上传上限（MB）", min_value=1, max_value=500, value=int(settings.max_upload_mb))
                max_minutes = st.number_input("音频时长上限（分钟）", min_value=1, max_value=120, value=int(settings.max_audio_minutes))
            if st.form_submit_button("保存非敏感配置", type="primary"):
                settings.asr_model, settings.asr_base = asr_model.strip(), asr_base.strip()
                settings.oss_bucket, settings.oss_endpoint = bucket.strip(), endpoint.strip()
                settings.llm_model, settings.llm_base = llm_model.strip(), llm_base.strip()
                settings.monthly_budget_cny, settings.audio_reserve_per_hour, settings.llm_reserve_per_chunk = budget, audio_reserve, chunk_reserve
                settings.max_upload_mb, settings.max_audio_minutes = max_mb, max_minutes
                try:
                    settings.save()
                    st.success("已保存。已提交任务保留其模型配置，新预算在下一个任务生效。")
                except ValueError as exc:
                    st.error(str(exc))
        st.caption("费用为保守预留额，并非实际账单。失败和超时调用也保留预留，防止低估；实际用量另存。")
    with update_tab:
        from interview_agent.update_ui import update_panel
        update_panel(settings.data_dir)
    with data_tab:
        section_label("数据", "调用记录与本地备份", "查看费用预留，备份你的面试知识库。")
        calls = store.rows("SELECT provider,model,status,reserve_cny,usage,created FROM calls ORDER BY created DESC LIMIT 50")
        if calls:
            st.dataframe(calls, hide_index=True, width="stretch")
        st.write("数据目录：", str(settings.data_dir))
        if st.button("创建一致性数据库备份"):
            backup = store.backup()
            st.success("数据库备份完成：" + str(backup))
        st.caption("此按钮备份数据库；完整恢复还需同时保留 media、transcripts 和 jobs 目录。")


def main():
    apply_style()
    try:
        settings, store = context()
    except (ValueError, OSError):
        st.error("本地配置无法加载，请检查数据目录 settings.json 的格式和权限。")
        st.stop()
    if (settings.data_dir / "updates/installing.json").exists():
        st.info("程序正在更新，操作已暂停。完成后会自动重新启动。")
        from interview_agent.update_ui import update_panel
        update_panel(settings.data_dir)
        st.stop()
    with st.sidebar:
        st.markdown('<div class="sidebar-brand"><span class="brand-icon">面</span><div><strong>面试知识库</strong><small>记录 · 整理 · 复盘</small></div></div>', unsafe_allow_html=True)
        navigation = st.radio("导航", ["导入面试", "处理任务", "问答知识库", "个人信息", "面试详情", "设置与数据"], key="navigation", label_visibility="collapsed")
        st.divider()
        st.caption(f"v{__version__} · 本地应用 · 云端模型")
        st.markdown('<div class="sidebar-note">把每一次面试，变成下一次的准备。</div>', unsafe_allow_html=True)
        status_file = settings.data_dir / "worker-status.json"
        alive = False
        if status_file.exists():
            try:
                alive = time.time() - read_json(status_file)["updated"] < 120
            except (ValueError, KeyError):
                pass
        st.markdown(f'<div class="worker-status"><span class="status-dot {"online" if alive else ""}"></span>{"后台已连接" if alive else "后台未连接"}</div>', unsafe_allow_html=True)
        if not alive:
            st.caption("请使用 start.ps1 启动后台")
    from interview_agent.personal_ui import personal_page
    {"导入面试": lambda: home(settings, store), "处理任务": lambda: tasks(store), "问答知识库": lambda: library(settings, store), "个人信息": lambda: personal_page(settings, store), "面试详情": lambda: details(settings, store), "设置与数据": lambda: settings_page(settings, store)}[navigation]()


if __name__ == "__main__":
    main()
