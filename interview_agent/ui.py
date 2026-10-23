from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st


def apply_style():
    st.markdown("<style>" + (Path(__file__).parent / "ui.css").read_text(encoding="utf-8") + "</style>", unsafe_allow_html=True)


def page_heading(title: str, description: str, section: str):
    st.title(title)
    st.markdown(f'<p class="page-description">{escape(description)}</p>', unsafe_allow_html=True)


def task_heading(title: str, status: str, label: str):
    tone = {"COMPLETED": "success", "RUNNING": "active", "FAILED": "danger", "RETRY_WAIT": "warning"}.get(status, "neutral")
    heading = f'<h3>{escape(title)}</h3>' if title else ''
    st.markdown(f'<div class="task-heading">{heading}<span class="status-badge {tone}">{escape(label)}</span></div>', unsafe_allow_html=True)


def tags(values: list[str]):
    if values:
        st.markdown('<div class="tag-list">' + "".join(f'<span class="topic-tag">{escape(value)}</span>' for value in values) + '</div>', unsafe_allow_html=True)


def navigate(page: str, interview_id: str | None = None):
    st.session_state["navigation"] = page
    if interview_id:
        st.session_state["selected_interview"] = interview_id


def section_label(number: str, title: str, description: str = ""):
    st.markdown(f'<div class="section-label"><span>{escape(number)}</span><div><h3>{escape(title)}</h3><p>{escape(description)}</p></div></div>', unsafe_allow_html=True)


def connection_cards(asr: bool, llm: bool, storage: bool):
    cards = []
    for name, detail, ready in (("语音转写", "阿里云 · AliApiKey", asr), ("问答整理", "DeepSeek · deepseek", llm), ("录音存储", "阿里云 OSS", storage)):
        cards.append(f'<div class="connection-card"><span class="connection-name">{name}</span><span class="connection-state {"ready" if ready else "missing"}">{"已读取配置" if ready else "待配置"}</span><small>{detail}</small></div>')
    st.markdown('<div class="connection-grid">' + ''.join(cards) + '</div>', unsafe_allow_html=True)


def empty_state(title: str, description: str):
    st.markdown(f'<div class="empty-state"><span class="empty-symbol" aria-hidden="true">◇</span><h3>{escape(title)}</h3><p>{escape(description)}</p></div>', unsafe_allow_html=True)


def recent_heading(title: str, metadata: str):
    st.markdown(f'<div class="recent-heading"><strong>{escape(title)}</strong><small>{escape(metadata)}</small></div>', unsafe_allow_html=True)


def page_rows(rows: list[dict], key: str, size: int = 8) -> list[dict]:
    """Bound visible cards, and reset the page when search results change."""
    signature = tuple((r.get('revision_id', ''), r['id']) for r in rows)
    if st.session_state.get(key + '-results') != signature:
        st.session_state[key] = 1
        st.session_state[key + '-results'] = signature
    if len(rows) <= size:
        return rows
    pages = (len(rows) + size - 1) // size
    page = st.selectbox("页码", list(range(1, pages + 1)), key=key, format_func=lambda n: f"第 {n} / {pages} 页")
    start = (page - 1) * size
    st.caption(f"显示第 {start + 1}–{min(start + size, len(rows))} 条")
    return rows[start:start + size]
