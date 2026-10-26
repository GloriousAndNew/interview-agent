"""Server UI boundary and authenticated exports; desktop behavior is unchanged."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from html import escape
import os
from pathlib import Path
import shutil
import time

from .server_auth import Accounts, enabled, server_root, get_accounts


def current_user():
    import streamlit as st
    return get_accounts().from_headers(st.context.headers)


def require_user():
    import streamlit as st
    try:
        return current_user()
    except (PermissionError, ValueError):
        st.error("登录已失效，请重新登录。")
        st.link_button("前往登录", "/auth/login")
        st.stop()


def context():
    from .db import Store
    user = require_user()
    accounts = get_accounts()
    from streamlit.runtime.scriptrunner import get_script_run_ctx
    script_context = get_script_run_ctx()
    if script_context is not None:
        accounts.bind_web_session(script_context.session_id, user)
    settings = accounts.settings(user['id'])
    return settings, Store(settings.data_dir)


def owner(store):
    accounts = get_accounts()
    user = accounts.user(store.root.name)
    if not user or not user['active'] or accounts.tenant(user['id']) != store.root.resolve():
        raise PermissionError("用户目录与账号不匹配。")
    return accounts, user


def used_bytes(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())


def check_capacity(store, incoming=0, *, queue=False):
    if not enabled():
        return
    accounts, user = owner(store)
    if used_bytes(store.root) + incoming > user['storage_mb'] * 1024 * 1024:
        raise ValueError("个人存储配额不足，请清理文件或联系管理员。")
    if shutil.disk_usage(store.root).free < max(incoming, 2 * 1024 ** 3):
        raise ValueError("服务器剩余空间不足，已暂停新增文件。")
    if queue and store.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT')")["n"] >= 3:
        raise ValueError("每人最多保留 3 个待处理任务，请等待完成后再提交。")


@contextmanager
def write_guard(store, stream=None, *, queue=False):
    if not enabled():
        yield
        return
    accounts, user = owner(store)
    permit = accounts.acquire(user['id'], 'write', 128, ttl=1200)
    if not permit:
        raise ValueError("此账号另有文件正在写入，请稍后重试。")
    try:
        incoming = 0
        if stream is not None:
            position = stream.tell()
            stream.seek(0, 2)
            incoming = stream.tell()
            stream.seek(position)
        check_capacity(store, incoming * 3, queue=queue)
        yield
    finally:
        accounts.release(permit)


def export_link(store, *, data=None, path=None, filename, mime):
    """Tickets only select files in the signed-in user's directory."""
    from .files import inside
    accounts, user = owner(store)
    if path is not None:
        source = Path(path).resolve()
        if not source.is_relative_to(store.root.resolve()) or not source.is_file():
            raise ValueError("文件不可用。")
        relative = source.relative_to(store.root).as_posix()
        ticket = hashlib.sha256((relative + str(source.stat().st_mtime_ns)).encode()).hexdigest()
    else:
        content = data.encode('utf-8') if isinstance(data, str) else bytes(data)
        ticket = hashlib.sha256(content + filename.encode() + mime.encode()).hexdigest()
        relative = f"downloads/{ticket}.bin"
        source = inside(store.root, relative)
        if not source.exists():
            check_capacity(store, len(content))
            source.parent.mkdir(parents=True, exist_ok=True)
            temporary = source.with_suffix('.' + os.urandom(8).hex() + '.tmp')
            try:
                temporary.write_bytes(content)
                os.replace(temporary, source)
            finally:
                temporary.unlink(missing_ok=True)
    accounts.register_download(user['id'], ticket, relative, filename, mime)
    if path is None and not source.exists():
        # An expiry sweep may have removed a cache immediately before registration.
        source.write_bytes(content)
    return f"/files/{ticket}"


def download_button(label, data, *, file_name, mime='application/octet-stream', key=None, **kwargs):
    import streamlit as st
    if not enabled():
        return st.download_button(label, data, file_name=file_name, mime=mime, key=key, **kwargs)
    require_user()
    from .db import Store
    store = Store(get_accounts().tenant(current_user()['id']))
    url = export_link(store, data=data, filename=file_name, mime=mime)
    return st.link_button(label, url, **kwargs)


def audio(path, *, start_time=0):
    import streamlit as st
    if not enabled():
        return st.audio(path, start_time=start_time)
    user = require_user()
    from .db import Store
    store = Store(get_accounts().tenant(user['id']))
    url = export_link(store, path=path, filename=Path(path).name, mime='audio/' + ('mpeg' if Path(path).suffix == '.mp3' else 'mp4' if Path(path).suffix == '.m4a' else 'wav'))
    st.markdown(f'<audio controls preload="none" style="width:100%" src="{escape(url)}#t={max(0,int(start_time))}"></audio>', unsafe_allow_html=True)


def account_page(settings, store):
    import streamlit as st
    from .ui import page_heading
    user = require_user()
    page_heading("账号与配额", "资料仅在你的账号内可见。", "账号")
    st.write("当前账号：", user['username'])
    st.write(f"每月调用预留上限：¥{user['monthly_budget']:.2f}")
    st.write(f"已用存储：{used_bytes(store.root) / 1024 ** 2:.1f} / {user['storage_mb']} MB")
    st.caption("费用预留并非供应商实际账单。配额由管理员设置；上传、处理和导出产生的文件均计入存储。")
    st.link_button("账号管理与退出", "/auth/account")
    if user['admin']:
        st.link_button("管理用户与配额", "/auth/admin")


@contextmanager
def generation_guard(store):
    if not enabled():
        yield
        return
    from .providers import ProviderError
    accounts, user = owner(store)
    permit = accounts.acquire(user['id'], 'answer', int(os.environ.get('SERVER_ANSWER_CONCURRENCY', '2')))
    if not permit:
        raise ProviderError("服务器或此账号的高分答案生成名额已满，请稍后重试。")
    try:
        check_capacity(store)
        yield
    finally:
        accounts.release(permit)
