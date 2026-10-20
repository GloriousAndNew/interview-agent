from __future__ import annotations

import sys
import time

import streamlit as st

from . import __version__
from .files import atomic_json
from .progress import local_time
from .updates import auto_check, check, folder, read_state, request_install, start_download, status
from .update_bundle import UpdateError


@st.fragment(run_every=3)
def update_panel(data):
    st.subheader("程序更新")
    st.caption(f"当前版本 v{__version__} · 更新来源：GitHub / GloriousAndNew/interview-agent")
    preferences = read_state(data, "preferences.json")
    enabled = st.checkbox("启动时自动检查更新（每天最多一次）", value=bool(preferences.get("auto_check")), key="update-auto-check")
    if enabled != bool(preferences.get("auto_check")):
        atomic_json(folder(data) / "preferences.json", {"auto_check": enabled})
        if enabled:
            auto_check(data)
    if st.button("检查更新", key="update-check"):
        with st.spinner("正在检查 GitHub 正式发布版本…"):
            check(data)
    result = read_state(data, "check.json")
    if result.get("checked_at"):
        st.caption("最近检查：" + local_time(result["checked_at"]))
    if result.get("error"):
        st.warning(result["error"])
    elif result.get("available"):
        st.info(f"发现新版 v{result['version']}，程序包约 {result['size'] / 1048576:.1f} MB。")
        with st.expander("更新说明"):
            st.text(result.get("notes", ""))
    elif result:
        st.success("当前版本无需更新。")
    state = read_state(data)
    phase = state.get("phase")
    if result.get("available") and phase not in ("waiting", "installing", "restarting", "repair_required"):
        if st.button("下载新版 / 重试下载", key="update-download"):
            start_download(data)
    if phase == "downloading":
        received, total = state.get("received", 0), max(1, state.get("total", 1))
        st.progress(min(1.0, received / total), text=f"下载新版：{received / 1048576:.1f} / {total / 1048576:.1f} MB")
        if time.time() - state.get("updated_at", 0) > 45:
            st.caption("下载暂未更新，网络中断或程序重启后可点击重试下载。")
    elif phase == "verifying":
        st.info("正在校验并解压更新包…")
    elif state.get("message"):
        if phase in ("failed", "repair_required", "rolled_back"):
            st.warning(state["message"])
        else:
            st.info(state["message"])
    if phase == "ready":
        st.success(f"v{state['version']} 已下载并通过完整性校验。")
        if getattr(sys, "frozen", False):
            st.caption("确认后等待任务队列清空，再备份数据库、退出服务并安装。安装期间暂停操作，完成后自动重新打开程序。")
            confirmed = st.checkbox("同意在任务结束后安装更新并重启", key="update-install-confirm")
            if st.button("安装更新并重启", disabled=not confirmed, key="update-install"):
                try:
                    request_install(data)
                    st.rerun(scope="fragment")
                except (UpdateError, OSError, KeyError) as exc:
                    st.error(str(exc) if isinstance(exc, UpdateError) else "更新启动器未连接，请重新打开 EXE。")
        else:
            st.caption("当前为源码运行方式。自动安装用于 EXE 版；请先安装带更新器的 v0.1.3 完整程序包。")
    if phase == "waiting" and st.button("取消本次安装", key="update-cancel-install"):
        # Once the gate is set the updater owns the operation; do not cancel it.
        if not (folder(data) / "installing.json").exists():
            (folder(data) / "install-request.json").unlink(missing_ok=True)
            status(data, **dict(state, phase="ready", message="已取消安装，可稍后再安装。"))
            st.rerun(scope="fragment")
    st.caption("网络不可用不会影响正常使用。更新只访问公开发布信息，不发送录音、问答或模型密钥。")
