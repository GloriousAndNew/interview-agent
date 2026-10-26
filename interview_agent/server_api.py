"""Authentication service used by Caddy; no public access to the Streamlit port."""
from __future__ import annotations

import hashlib
import asyncio
import hmac
from html import escape
import os
import re
import secrets
from urllib.parse import urlsplit, unquote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from .files import inside
from .server_auth import Accounts, bootstrap, gateway_secret, server_root


STYLE = """body{font:15px system-ui,sans-serif;color:#233b34;background:#f5f8f6;margin:0;padding:32px 16px}main{max-width:860px;margin:20px auto}h1{font-size:24px}section{background:white;border:1px solid #dfe8e2;border-radius:10px;padding:20px;margin:16px 0}input,button{box-sizing:border-box;padding:10px;border:1px solid #cbd9d0;border-radius:6px;font:inherit}input{max-width:100%;margin:6px 0 12px}label{display:block}button{cursor:pointer;background:#087f70;color:white}a{color:#087f70}table{width:100%;border-collapse:collapse}td,th{text-align:left;border-bottom:1px solid #e4ebe6;padding:10px}.error{color:#a34037}.muted{color:#687f72}form{margin:12px 0}small{display:block} .row{display:flex;gap:12px;flex-wrap:wrap} """


def page(title, content, error="", status=200):
    return HTMLResponse(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · 面试知识库</title><style>{STYLE}</style><main><h1>{escape(title)}</h1><p class="error">{escape(error)}</p>{content}</main></html>', status_code=status)


def create_app(accounts=None):
    accounts = accounts or Accounts(server_root())
    gateway_secret()
    origin = os.environ.get('PUBLIC_ORIGIN', '').rstrip('/')
    parsed = urlsplit(origin)
    insecure = os.environ.get('SERVER_ALLOW_HTTP') == '1'
    if parsed.scheme not in (('http', 'https') if insecure else ('https',)) or not parsed.netloc or parsed.path or parsed.username or parsed.password:
        raise ValueError("PUBLIC_ORIGIN must be the site's HTTPS origin")
    cookie_name = 'ia_session' if insecure else '__Host-ia_session'
    csrf_cookie = 'ia_login_csrf' if insecure else '__Host-ia_login_csrf'
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    hash_slots = asyncio.Semaphore(4)

    @app.middleware('http')
    async def security_headers(request, call_next):
        if request.url.path != '/healthz' and request.headers.get('host') != parsed.netloc:
            return Response(status_code=400)
        if request.method == 'POST':
            if request.headers.get('origin') != origin:
                return Response(status_code=403)
            try:
                length = int(request.headers.get('content-length', '0'))
            except ValueError:
                return Response(status_code=400)
            if not 0 < length <= 16384:
                return Response(status_code=413)
        result = await call_next(request)
        result.headers['Cache-Control'] = 'no-store'
        result.headers['X-Content-Type-Options'] = 'nosniff'
        result.headers['Referrer-Policy'] = 'same-origin'
        result.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        return result

    def logged_in(request):
        user = accounts.session(token=request.cookies.get(cookie_name, ''))
        if not user:
            raise HTTPException(401, '请重新登录。')
        return user

    def csrf(request, value):
        user = logged_in(request)
        if not hmac.compare_digest(user['csrf'], value):
            raise HTTPException(403, '请求校验失败。')
        return user

    def admin(request, value=None):
        user = csrf(request, value) if value is not None else logged_in(request)
        if not user['admin']:
            raise HTTPException(403, '仅管理员可访问。')
        return user

    def hidden(user):
        return f'<input type="hidden" name="csrf_token" value="{user["csrf"]}">'

    def signed_nonce(nonce):
        return hmac.new(gateway_secret(), nonce.encode(), hashlib.sha256).hexdigest()

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/auth/login')
    def login_page():
        nonce = secrets.token_hex(24)
        token = nonce + '.' + signed_nonce(nonce)
        result = page('登录面试知识库', f'<section><form method="post" action="/auth/login"><input type="hidden" name="csrf_token" value="{token}"><label>账号<input name="username" autocomplete="username" required maxlength="32"></label><label>密码<input name="password" type="password" autocomplete="current-password" required maxlength="128"></label><button>登录</button></form><p class="muted">账号由管理员创建。每个人的面试、简历和讲稿独立保存。</p></section>')
        result.set_cookie(csrf_cookie, token, max_age=900, httponly=True, secure=not insecure, samesite='lax', path='/')
        return result

    @app.post('/auth/login')
    async def login(request: Request, username: str = Form(max_length=32), password: str = Form(max_length=128), csrf_token: str = Form(max_length=128)):
        nonce, _, signature = csrf_token.partition('.')
        if not nonce or not hmac.compare_digest(csrf_token, request.cookies.get(csrf_cookie, '')) or not hmac.compare_digest(signature, signed_nonce(nonce)):
            raise HTTPException(403, '登录请求失效，请刷新登录页。')
        address = request.headers.get('x-forwarded-for', request.client.host if request.client else '').split(',')[-1].strip()
        try:
            async with hash_slots:
                token, _ = await run_in_threadpool(accounts.login, username, password, address)
        except ValueError:
            return page('登录未成功', '<p>账号、密码不正确或尝试过多。请稍后重试。</p><a href="/auth/login">返回登录</a>', status=401)
        result = RedirectResponse('/', status_code=303)
        result.set_cookie(cookie_name, token, max_age=8 * 3600, httponly=True, secure=not insecure, samesite='lax', path='/')
        result.delete_cookie(csrf_cookie, secure=not insecure, httponly=True, samesite='lax', path='/')
        return result

    @app.get('/auth/verify')
    def verify(request: Request):
        user = accounts.session(token=request.cookies.get(cookie_name, ''))
        if not user:
            return RedirectResponse('/auth/login', status_code=303)
        path = unquote(urlsplit(request.headers.get('x-forwarded-uri', '/')).path)
        if path == '/_stcore/stream' and request.headers.get('origin') != origin:
            return Response(status_code=403)
        if path.startswith('/_stcore/upload_file'):
            match = re.fullmatch(r'/_stcore/upload_file/([^/]+)/[^/]+', path)
            if not match or not accounts.owns_web_session(match[1], user):
                return Response(status_code=403)
        return Response(status_code=200, headers={'X-IA-User': user['id'], 'X-IA-Session': user['session_id'], 'X-IA-Proof': accounts.proof(user)})

    @app.get('/auth/account')
    def account(request: Request):
        user = logged_in(request)
        admin_link = '<p><a href="/auth/admin">管理用户与配额</a></p>' if user['admin'] else ''
        return page('账号', f'<section><p>当前账号：{escape(user["username"])}</p><p>每月调用预留上限 ¥{user["monthly_budget"]:.2f} · 存储 {user["storage_mb"]} MB</p><p><a href="/">返回知识库</a></p>{admin_link}<form action="/auth/logout" method="post">{hidden(user)}<button>退出登录</button></form></section><section><h2>修改密码</h2><form method="post" action="/auth/password">{hidden(user)}<label>原密码<input name="old_password" type="password" required maxlength="128" autocomplete="current-password"></label><label>新密码<input name="password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><button>保存并重新登录</button></form></section>')

    @app.post('/auth/password')
    async def change_password(request: Request, csrf_token: str = Form(max_length=128), old_password: str = Form(max_length=128), password: str = Form(max_length=128)):
        user = csrf(request, csrf_token)
        try:
            async with hash_slots:
                await run_in_threadpool(accounts.change_password, user['id'], old_password, password)
        except ValueError as exc:
            return page('修改失败', '<a href="/auth/account">返回账号</a>', str(exc), 400)
        result = RedirectResponse('/auth/login', status_code=303)
        result.delete_cookie(cookie_name, secure=not insecure, httponly=True, samesite='lax', path='/')
        return result

    @app.post('/auth/logout')
    def logout(request: Request, csrf_token: str = Form(max_length=128)):
        csrf(request, csrf_token)
        accounts.logout(request.cookies.get(cookie_name, ''))
        result = RedirectResponse('/auth/login', status_code=303)
        result.delete_cookie(cookie_name, secure=not insecure, httponly=True, samesite='lax', path='/')
        return result

    @app.get('/auth/admin')
    def admin_page(request: Request):
        user = admin(request)
        rows = ''.join(f'<tr><td>{escape(u["username"])}</td><td>{"管理员" if u["admin"] else "用户"}</td><td>{"启用" if u["active"] else "停用"}</td><td>¥{u["monthly_budget"]:.2f}</td><td>{u["storage_mb"]} MB</td></tr>' for u in accounts.users())
        options = ''.join(f'<option value="{u["id"]}">{escape(u["username"])}</option>' for u in accounts.users())
        return page('管理用户与配额', f'''<p><a href="/">返回知识库</a> · <a href="/auth/account">账号与退出</a></p>
<section><table><tr><th>账号</th><th>角色</th><th>状态</th><th>月预留上限</th><th>存储</th></tr>{rows}</table></section>
<section><h2>创建普通用户</h2><form method="post" action="/auth/admin/create">{hidden(user)}<label>账号<input name="username" required pattern="[a-z0-9][a-z0-9_.-]{{2,31}}" maxlength="32"></label><label>初始密码<input name="password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><button>创建账号</button></form></section>
<section><h2>调整账号</h2><form method="post" action="/auth/admin/configure">{hidden(user)}<label>账号<select name="user_id">{options}</select></label><label>状态<select name="active"><option value="1">启用</option><option value="0">停用并撤销登录</option></select></label><label>月调用预留上限（元）<input name="monthly_budget" type="number" value="50" min="1" max="100000" required></label><label>存储配额（MB）<input name="storage_mb" type="number" value="2048" min="100" max="100000" required></label><button>保存配额与状态</button></form></section>
<section><h2>重置密码</h2><form method="post" action="/auth/admin/password">{hidden(user)}<label>账号<select name="user_id">{options}</select></label><label>新密码<input name="password" type="password" required minlength="12" maxlength="128" autocomplete="new-password"></label><button>重置并撤销该账号所有登录</button></form></section>''')

    @app.post('/auth/admin/create')
    async def create_user(request: Request, csrf_token: str = Form(max_length=128), username: str = Form(max_length=32), password: str = Form(max_length=128)):
        user = admin(request, csrf_token)
        try:
            async with hash_slots:
                await run_in_threadpool(accounts.create_user, username, password, actor=user['id'])
        except ValueError as exc:
            return page('创建失败', '<a href="/auth/admin">返回管理</a>', str(exc), 400)
        return RedirectResponse('/auth/admin', status_code=303)

    @app.post('/auth/admin/configure')
    def configure(request: Request, csrf_token: str = Form(max_length=128), user_id: str = Form(max_length=32), active: int = Form(ge=0, le=1), monthly_budget: float = Form(ge=1, le=100000), storage_mb: int = Form(ge=100, le=100000)):
        user = admin(request, csrf_token)
        try:
            accounts.configure(user['id'], user_id, active=active, monthly_budget=monthly_budget, storage_mb=storage_mb)
        except ValueError as exc:
            return page('保存失败', '<a href="/auth/admin">返回管理</a>', str(exc), 400)
        return RedirectResponse('/auth/admin', status_code=303)

    @app.post('/auth/admin/password')
    async def reset(request: Request, csrf_token: str = Form(max_length=128), user_id: str = Form(max_length=32), password: str = Form(max_length=128)):
        user = admin(request, csrf_token)
        try:
            async with hash_slots:
                await run_in_threadpool(accounts.reset_password, user['id'], user_id, password)
        except ValueError as exc:
            return page('重置失败', '<a href="/auth/admin">返回管理</a>', str(exc), 400)
        return RedirectResponse('/auth/admin', status_code=303)

    @app.get('/files/{ticket}')
    def download(ticket: str, request: Request):
        user = logged_in(request)
        if not re.fullmatch(r'[0-9a-f]{64}', ticket):
            raise HTTPException(404)
        item = accounts.download(user['id'], ticket)
        if not item:
            raise HTTPException(404)
        source = inside(accounts.tenant(user['id']), item['path'])
        if not source.is_file():
            raise HTTPException(404)
        return FileResponse(source, media_type=item['mime'], filename=item['filename'], content_disposition_type='inline' if item['mime'].startswith('audio/') else 'attachment')

    return app


def main():
    import uvicorn
    accounts = Accounts(server_root())
    bootstrap(accounts)
    uvicorn.run(create_app(accounts), host='0.0.0.0', port=8000, access_log=False)


if __name__ == '__main__':
    main()
