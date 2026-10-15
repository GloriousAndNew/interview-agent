from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st


def apply_style():
    st.markdown("<style>" + (Path(__file__).parent / "ui.css").read_text(encoding="utf-8") + "</style>", unsafe_allow_html=True)


def page_heading(title: str, description: str, section: str):
    st.markdown(f'<div class="page-eyebrow">面试知识库 / {escape(section)}</div>', unsafe_allow_html=True)
    st.title(title)
    st.markdown(f'<p class="page-description">{escape(description)}</p>', unsafe_allow_html=True)


def task_heading(title: str, status: str, label: str):
    tone = {"COMPLETED": "success", "RUNNING": "active", "FAILED": "danger", "RETRY_WAIT": "warning"}.get(status, "neutral")
    st.markdown(f'<div class="task-heading"><h3>{escape(title)}</h3><span class="status-badge {tone}">{escape(label)}</span></div>', unsafe_allow_html=True)


def tags(values: list[str]):
    if values:
        st.markdown('<div class="tag-list">' + "".join(f'<span class="topic-tag">{escape(value)}</span>' for value in values) + '</div>', unsafe_allow_html=True)


def navigate(page: str, interview_id: str | None = None):
    st.session_state["navigation"] = page
    if interview_id:
        st.session_state["selected_interview"] = interview_id
