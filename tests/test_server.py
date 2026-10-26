from concurrent.futures import Future
from io import BytesIO
import hashlib
import json
import re
import time

import pytest
from fastapi.testclient import TestClient

from interview_agent.db import Store, BudgetExceeded
from interview_agent.personal import add_document, documents
from interview_agent.server_api import create_app
from interview_agent.server_auth import Accounts, bootstrap, check_password
from interview_agent.server_ui import export_link, write_guard
from interview_agent.server_worker import tick
from interview_agent.services import submit


PASSWORD = 'testing-password-123!'


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv('SERVER_MODE', '1')
    monkeypatch.setenv('SERVER_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('GATEWAY_SECRET', 's' * 64)
    monkeypatch.setenv('PUBLIC_ORIGIN', 'https://testserver')
    monkeypatch.setenv('deepseek', 'fake-key')
    monkeypatch.setenv('AliApiKey', '')
    monkeypatch.setenv('OSS_ACCESS_KEY_ID', '')
    monkeypatch.setenv('OSS_ACCESS_KEY_SECRET', '')
    accounts = Accounts(tmp_path)
    admin = accounts.create_user('admin', PASSWORD, admin=True)
    alice = accounts.create_user('alice', PASSWORD)
    bob = accounts.create_user('bob', PASSWORD)
    return accounts, admin, alice, bob


def login(client, username='alice', password=PASSWORD):
    response = client.get('/auth/login')
    nonce = re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]
    result = client.post('/auth/login', data={'username': username, 'password': password, 'csrf_token': nonce}, headers={'origin': 'https://testserver'}, follow_redirects=False)
    assert result.status_code == 303, result.text
    verified = client.get('/auth/verify')
    assert verified.status_code == 200
    user = client.get('/auth/account')
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', user.text)[1]
    return verified, csrf


def test_password_session_revoke_headers_and_rate_limit(server):
    accounts, admin, alice, bob = server
    with accounts.transaction() as con:
        stored = con.execute('SELECT password FROM users WHERE id=?', (alice,)).fetchone()[0]
    assert stored != PASSWORD and check_password(PASSWORD, stored)
    token, _ = accounts.login('alice', PASSWORD, 'test-ip')
    user = accounts.session(token=token)
    assert user['id'] == alice
    headers = {'X-IA-User': alice, 'X-IA-Session': user['session_id'], 'X-IA-Proof': accounts.proof(user)}
    assert accounts.from_headers(headers)['id'] == alice
    with pytest.raises(PermissionError):
        accounts.from_headers(dict(headers, **{'X-IA-User': bob}))
    accounts.configure(admin, alice, active=False, monthly_budget=5, storage_mb=200)
    assert accounts.session(token=token) is None
    with pytest.raises(PermissionError):
        accounts.from_headers(headers)
    for _ in range(5):
        with pytest.raises(ValueError):
            accounts.login('bob', 'incorrect-password', 'bad-ip')
    with pytest.raises(ValueError):
        accounts.login('bob', PASSWORD, 'different-ip')
    with pytest.raises(ValueError):
        accounts.configure(admin, admin, active=False, monthly_budget=5, storage_mb=200)


def test_tenant_database_documents_ids_and_budget_are_isolated(server):
    accounts, admin, alice, bob = server
    a_settings, b_settings = accounts.settings(alice), accounts.settings(bob)
    a, b = Store(a_settings.data_dir), Store(b_settings.data_dir)
    doc = add_document(a, BytesIO('Alice 的简历'.encode()), 'resume.txt', '简历')
    assert len(documents(a)) == 1 and documents(b) == []
    assert b.one('SELECT * FROM personal_documents WHERE id=?', (doc,)) is None
    interview, job = submit(a_settings, a, BytesIO('问题：幂等\n回答：唯一约束'.encode()), 'qa.txt', 'Alice 的面试', {})
    assert b.one('SELECT * FROM interviews WHERE id=?', (interview,)) is None
    with pytest.raises(ValueError):
        b.set_active(interview, 'not-a-revision')
    claimed = a.claim('a-worker')
    a.reserve(claimed, 'deepseek', 'test', 50, 50)
    with pytest.raises(BudgetExceeded):
        a.reserve(claimed, 'deepseek', 'test', 1, 50)
    b.create('Bob', {}, {}, {})
    b_job = b.claim('b-worker')
    assert b.reserve(b_job, 'deepseek', 'test', 1, 50)
    with pytest.raises(PermissionError):
        accounts.tenant('../' + alice)
    # A user's old local settings cannot elevate their server budget or limits.
    (a.root / 'settings.json').write_text(json.dumps({'monthly_budget_cny': 100000, 'max_upload_mb': 500}))
    assert accounts.settings(alice).monthly_budget_cny == 50
    assert accounts.settings(alice).max_upload_mb == 100


def test_login_csrf_admin_and_file_ownership(server):
    accounts, admin, alice, bob = server
    app = create_app(accounts)
    with TestClient(app, base_url='https://testserver') as a, TestClient(app, base_url='https://testserver') as b:
        assert a.get('/auth/admin').status_code == 401
        assert a.get('/auth/verify', follow_redirects=False).status_code == 303
        verify, csrf = login(a)
        login(b, 'bob')
        assert a.get('/auth/admin').status_code == 403
        assert a.post('/auth/logout', data={'csrf_token': csrf}, headers={'origin':'https://evil.example'}).status_code == 403
        assert a.post('/auth/logout', data={'csrf_token':'forged'}, headers={'origin':'https://testserver'}).status_code == 403
        store = Store(accounts.tenant(alice))
        url = export_link(store, data='private resume', filename='简历.txt', mime='text/plain')
        assert a.get(url).text == 'private resume'
        assert b.get(url).status_code == 404
        assert TestClient(app, base_url='https://testserver').get(url).status_code == 401
        assert a.get(url).headers['cache-control'] == 'no-store'
        # Range requests used by audio replay retain authorization checks.
        media = store.root / 'media/voice.wav'
        media.parent.mkdir()
        media.write_bytes(b'0123456789')
        audio_url = export_link(store, path=media, filename='voice.wav', mime='audio/wav')
        assert a.get(audio_url, headers={'Range':'bytes=2-4'}).content == b'234'
        assert b.get(audio_url, headers={'Range':'bytes=2-4'}).status_code == 404
        assert a.post('/auth/logout', data={'csrf_token':csrf}, headers={'origin':'https://testserver'}, follow_redirects=False).status_code == 303
        assert a.get(url).status_code == 401
        with pytest.raises(PermissionError):
            accounts.from_headers(verify.headers)


def test_admin_creation_and_password_reset(server):
    accounts, admin, alice, bob = server
    with TestClient(create_app(accounts), base_url='https://testserver') as client:
        _, csrf = login(client, 'admin')
        headers = {'origin':'https://testserver'}
        response = client.post('/auth/admin/create', data={'username':'charlie','password':PASSWORD,'csrf_token':csrf}, headers=headers, follow_redirects=False)
        assert response.status_code == 303
        token, _ = accounts.login('alice', PASSWORD, 'ip')
        response = client.post('/auth/admin/password', data={'user_id':alice, 'password':'replacement-password-123', 'csrf_token':csrf}, headers=headers, follow_redirects=False)
        assert response.status_code == 303 and accounts.session(token=token) is None
        assert accounts.login('alice', 'replacement-password-123', 'ip')


def test_change_password_and_secure_cookies(server):
    accounts, admin, alice, bob = server
    with TestClient(create_app(accounts), base_url='https://testserver') as client:
        _, csrf = login(client)
        token = client.cookies.get('__Host-ia_session')
        response = client.post('/auth/password', data={'csrf_token':csrf,'old_password':PASSWORD,'password':'new-password-12345'}, headers={'origin':'https://testserver'}, follow_redirects=False)
        assert response.status_code == 303 and accounts.session(token=token) is None
        assert accounts.login('alice', 'new-password-12345', 'ip')
        response = client.get('/auth/login')
        cookie = response.headers['set-cookie']
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie


def test_quota_queue_limit_and_write_serialization(server):
    accounts, admin, alice, bob = server
    settings = accounts.settings(alice)
    store = Store(settings.data_dir)
    for _ in range(3):
        submit(settings, store, BytesIO('问题\n回答'.encode()), 'qa.txt', 'test', {})
    with pytest.raises(ValueError, match='3 个'):
        submit(settings, store, BytesIO('问题\n回答'.encode()), 'qa.txt', 'overflow', {})
    permit = accounts.acquire(alice, 'write', 128)
    with pytest.raises(ValueError, match='正在写入'):
        add_document(store, BytesIO(b'text'), 'resume.txt', '简历')
    accounts.release(permit)
    accounts.configure(admin, alice, active=True, monthly_budget=5, storage_mb=100)
    with (store.root / 'large-file').open('wb') as output:
        output.seek(99 * 1024 * 1024)
        output.write(b'x')
    with pytest.raises(ValueError, match='存储配额'):
        with write_guard(store, BytesIO(b'x' * 1024 * 1024)):
            pytest.fail('quota bypassed')


def test_worker_two_slots_one_job_per_user_and_fairness(server):
    accounts, admin, alice, bob = server
    third = accounts.create_user('charlie', PASSWORD)
    for user_id in (alice, bob, third):
        Store(accounts.tenant(user_id)).create('queued', {}, {}, {})
    class Executor:
        def submit(self, *args): return Future()
    running = {}
    cursor = tick(accounts, Executor(), running, maximum=2)
    assert set(running) == {alice, bob}
    assert accounts.acquire(alice, 'job', 2) is None
    tick(accounts, Executor(), running, maximum=2, cursor=cursor)
    assert len(running) == 2
    running[alice][0].set_result(True)
    tick(accounts, Executor(), running, maximum=2, cursor=cursor)
    assert set(running) == {bob, third}
    accounts.configure(admin, third, active=False, monthly_budget=5, storage_mb=100)
    assert accounts.acquire(third, 'answer', 2) is None


def test_bootstrap_never_overwrites_accounts(server, monkeypatch):
    accounts, admin, alice, bob = server
    monkeypatch.setenv('BOOTSTRAP_ADMIN_USERNAME','other-admin')
    monkeypatch.setenv('BOOTSTRAP_ADMIN_PASSWORD','other-password-123')
    bootstrap(accounts)
    assert not any(u['username']=='other-admin' for u in accounts.users())


def test_upload_session_and_websocket_origin_are_bound_to_login(server):
    accounts, admin, alice, bob = server
    with TestClient(create_app(accounts), base_url='https://testserver') as a, TestClient(create_app(accounts), base_url='https://testserver') as b:
        login(a)
        login(b, 'bob')
        user = accounts.session(token=a.cookies.get('__Host-ia_session'))
        accounts.bind_web_session('alice-web', user)
        upload = {'x-forwarded-uri':'/_stcore/upload_file/alice-web/file-id'}
        assert a.get('/auth/verify', headers=upload).status_code == 200
        assert b.get('/auth/verify', headers=upload).status_code == 403
        assert b.get('/auth/verify', headers={'x-forwarded-uri':'/_stcore/upload_file/%61lice-web/file-id'}).status_code == 403
        assert a.get('/auth/verify', headers={'x-forwarded-uri':'/_stcore/stream', 'origin':'https://evil.example'}).status_code == 403
        assert a.get('/auth/verify', headers={'x-forwarded-uri':'/_stcore/stream', 'origin':'https://testserver'}).status_code == 200


def test_expired_download_cache_cleanup_preserves_original(server):
    accounts, admin, alice, bob = server
    store = Store(accounts.tenant(alice))
    cache = export_link(store, data=b'cache', filename='cache.txt', mime='text/plain').rsplit('/',1)[-1]
    original = store.root / 'original.wav'
    original.write_bytes(b'original')
    export_link(store, path=original, filename='original.wav', mime='audio/wav')
    with accounts.transaction() as con:
        con.execute('UPDATE downloads SET created=?', (time.time() - 4000,))
    accounts.purge_expired()
    assert original.read_bytes() == b'original'
    assert not (store.root / f'downloads/{cache}.bin').exists()


def test_quiesced_backup_contains_all_tenants_and_hashed_accounts(server, tmp_path):
    import tarfile
    from interview_agent.server_admin import backup
    accounts, admin, alice, bob = server
    add_document(Store(accounts.tenant(alice)), BytesIO('Alice 简历'.encode()), 'resume.txt', '简历')
    add_document(Store(accounts.tenant(bob)), BytesIO('Bob 简历'.encode()), 'resume.txt', '简历')
    output = backup(accounts.root, tmp_path.parent / (tmp_path.name + '-backup.tar.gz'))
    with tarfile.open(output) as archive:
        names = archive.getnames()
        assert 'accounts.db' in names and f'users/{alice}/app.db' in names and f'users/{bob}/app.db' in names
    with pytest.raises(ValueError):
        backup(accounts.root, accounts.root/'bad.tar.gz')
