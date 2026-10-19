from __future__ import annotations

import copy
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from interview_agent.config import Settings
from interview_agent.db import BudgetExceeded, Store
from interview_agent.document_import import DocumentImportError
from interview_agent.diagnostics import report as diagnostic_report
from interview_agent.files import inside, read_json
from interview_agent.schemas import Transcript
from interview_agent.providers import ProviderError, validate_key
from interview_agent.progress import duration, local_time, task_progress
from interview_agent.services import export_revision, request_delete, submit, timestamp
from interview_agent.ui import apply_style, navigate, page_heading, tags, task_heading
from interview_agent.reference_answers import answer_state, generate as generate_reference, markdown as reference_markdown


st.set_page_config(page_title="面试录音整理助手", page_icon="🎙️", layout="wide")


def context():
    settings = Settings.load()
    return settings, Store(settings.data_dir)


def home(settings, store):
    page_heading("每一场面试，都有迹可循", "上传录音，留下清晰的问答与原始依据。", "导入面试")
    columns = st.columns(3)
    columns[0].metric("已导入面试", store.one("SELECT COUNT(*) n FROM interviews WHERE deleted=0")["n"])
    columns[1].metric("已整理问答", store.one("SELECT COUNT(*) n FROM qa q JOIN interviews i ON i.active_revision=q.revision_id WHERE i.deleted=0")["n"])
    columns[2].metric("待处理任务", store.one("SELECT COUNT(*) n FROM jobs j JOIN interviews i ON i.id=j.interview_id WHERE i.deleted=0 AND j.status IN ('QUEUED','RUNNING','RETRY_WAIT')")["n"])
    st.markdown('<div class="workflow"><div class="workflow-step"><b>01 · 导入录音</b><small>音频 / 转写文本</small></div><div class="workflow-step"><b>02 · 自动整理</b><small>转写与来源校验</small></div><div class="workflow-step"><b>03 · 沉淀知识</b><small>搜索、回听、复盘</small></div></div>', unsafe_allow_html=True)
    with st.form("upload", clear_on_submit=False):
        st.subheader("导入新面试")
        upload = st.file_uploader("录音或面试问答文档", type=["mp3", "m4a", "wav", "docx", "md", "txt", "json"], max_upload_size=settings.max_upload_mb, help="文档最多 10 MB。Word 读取正文和表格；Markdown/TXT 使用 UTF-8。旧版 DOC 请先另存为 DOCX。")
        title = st.text_input("面试标题", max_chars=200, placeholder="例如：后端开发 · 第一轮")
        a, b, c = st.columns(3)
        company = a.text_input("公司（可选）", max_chars=100)
        position = b.text_input("岗位（可选）", max_chars=100)
        date = c.date_input("面试日期（可选）", value=None)
        mode = st.radio("双声道录音的处理方式", ["普通立体声：合并识别并区分说话人", "双方分别占一个声道：拆开识别"], help="拆开识别会分别计费。单声道录音不受此选项影响。")
        duplicate = st.checkbox("允许同一录音创建另一场面试")
        st.caption("录音发送至阿里云转写；DOCX、Markdown、TXT、JSON 直接整理文字，不上传 OSS、不调用语音识别。文字发送至 DeepSeek，结果保存在本机。")
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
    selected = st.segmented_control("任务状态", ["全部", "进行中", "需处理", "已完成"], default="全部", key="task-filter", label_visibility="collapsed")
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
    for row in rows:
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
        st.caption("AI 生成的练习参考，与面试原答独立保存，不保证获得高分。涉及个人经历、项目与指标时请按真实情况补充。")
        saved, latest = answer_state(store, revision_id, qa_id)
        busy = bool(latest and latest["status"] == "RUNNING" and latest["lease_until"] > time.time())
        if saved:
            content = saved["content"]
            st.markdown(content["answer"])
            for name, label in (("key_points", "得分要点"), ("improvements", "原答改进建议"), ("followup_questions", "可能的追问"), ("needs_personalization", "需要按个人情况补充")):
                if content.get(name):
                    st.markdown("**" + label + "**")
                    for value in content[name]:
                        st.markdown("- " + value)
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
        st.caption(f"每次生成会调用一次 DeepSeek，按设置预留 ¥{settings.llm_reserve_per_chunk:.2f}，计入月预算；这不是实际账单。")
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
    st.caption(f"显示 {len(rows)} 条，最多 100 条。")
    for row in rows:
        with st.container(border=True):
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
        st.info("暂未找到问答。完成一次整理后即可在这里搜索。")


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
    st.write("模型密钥只从系统环境变量读取。这里不展示、不保存密钥值。")
    st.write("千问 AliApiKey：" + ("已读取" if settings.asr_key else "未配置"))
    st.write("DeepSeek deepseek：" + ("已读取" if settings.deepseek_key else "未配置"))
    for name, value in (("AliApiKey", settings.asr_key), ("deepseek", settings.deepseek_key)):
        if value:
            try:
                validate_key(value, name)
            except ProviderError as exc:
                st.warning(str(exc))
    st.write("OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET：" + ("已读取" if settings.oss_access_id and settings.oss_access_secret else "未配置"))
    st.caption("已读取不等于已通过云端鉴权。修改系统环境变量后可刷新页面；Windows 会回读用户/系统环境。")
    with st.form("settings"):
        st.subheader("录音转写")
        asr_model = st.text_input("阿里云 ASR 模型", settings.asr_model)
        asr_base = st.text_input("阿里云 API 基础地址", settings.asr_base)
        bucket = st.text_input("私有 OSS bucket", settings.oss_bucket)
        endpoint = st.text_input("OSS endpoint（HTTPS）", settings.oss_endpoint, placeholder="https://oss-cn-beijing.aliyuncs.com")
        st.subheader("文本整理")
        llm_model = st.text_input("DeepSeek 模型", settings.llm_model)
        llm_base = st.text_input("DeepSeek API 基础地址", settings.llm_base)
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
    with st.sidebar:
        st.markdown('<div class="sidebar-brand"><span class="brand-icon">面</span><div><strong>面试知识库</strong><small>记录 · 整理 · 复盘</small></div></div>', unsafe_allow_html=True)
        navigation = st.radio("导航", ["导入面试", "处理任务", "问答知识库", "面试详情", "设置与数据"], key="navigation", label_visibility="collapsed")
        st.divider()
        st.caption("本地应用 · 云端模型")
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
    {"导入面试": lambda: home(settings, store), "处理任务": lambda: tasks(store), "问答知识库": lambda: library(settings, store), "面试详情": lambda: details(settings, store), "设置与数据": lambda: settings_page(settings, store)}[navigation]()


if __name__ == "__main__":
    main()
