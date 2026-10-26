"""Server accounts and sessions. Never accepts a tenant directory from a client."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from functools import lru_cache
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .config import Settings


def enabled():
    return os.environ.get("SERVER_MODE") == "1"


def server_root():
    value = os.environ.get("SERVER_DATA_DIR", "")
    if not value:
        raise ValueError("SERVER_DATA_DIR is required")
    return Path(value).resolve()


def gateway_secret():
    value = os.environ.get("GATEWAY_SECRET", "")
    if len(value) < 48:
        raise ValueError("GATEWAY_SECRET must contain at least 48 characters")
    return value.encode()


def password_hash(password, salt=None):
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度须为 12–128 个字符。")
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=64 * 1024 * 1024).hex()
    return salt + ":" + digest


def check_password(password, stored):
    try:
        return hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)
    except (ValueError, TypeError):
        return False


class Accounts:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "accounts.db"
        with self.transaction() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password TEXT NOT NULL,
                    admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                    monthly_budget REAL NOT NULL DEFAULT 50, storage_mb INTEGER NOT NULL DEFAULT 2048,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, failures INTEGER NOT NULL, until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS permits (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    kind TEXT NOT NULL, expires REAL NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS permits_user_kind ON permits(user_id,kind);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, created REAL NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS downloads (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
                    path TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
                    created REAL NOT NULL, PRIMARY KEY(user_id,id));
                CREATE TABLE IF NOT EXISTS web_sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    auth_session TEXT NOT NULL, created REAL NOT NULL);
            """)

    @contextmanager
    def transaction(self, *, write=True):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def audit(con, actor, action, target):
        con.execute("INSERT INTO audit(created,actor,action,target) VALUES (?,?,?,?)", (time.time(), actor, action, target))

    def create_user(self, username, password, *, admin=False, actor="bootstrap", monthly_budget=50, storage_mb=2048):
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,31}", username):
            raise ValueError("账号须为 3–32 位小写字母、数字、点、横线或下划线。")
        if not 1 <= monthly_budget <= 100000 or not 100 <= storage_mb <= 100000:
            raise ValueError("预算或存储配额不在允许范围内。")
        encoded = password_hash(password)
        user_id = secrets.token_hex(16)
        try:
            with self.transaction() as con:
                con.execute("INSERT INTO users VALUES (?,?,?,?,1,?,?,?)", (user_id, username, encoded, int(admin), monthly_budget, storage_mb, time.time()))
                self.audit(con, actor, "create_user", user_id)
        except sqlite3.IntegrityError:
            raise ValueError("账号已存在。") from None
        return user_id

    def users(self):
        with self.transaction(write=False) as con:
            return [dict(r) for r in con.execute("SELECT id,username,admin,active,monthly_budget,storage_mb,created FROM users ORDER BY created")]

    def user(self, user_id):
        with self.transaction(write=False) as con:
            row = con.execute("SELECT id,username,admin,active,monthly_budget,storage_mb FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def configure(self, actor_id, user_id, *, active, monthly_budget, storage_mb):
        if not 1 <= monthly_budget <= 100000 or not 100 <= storage_mb <= 100000:
            raise ValueError("预算或存储配额不在允许范围内。")
        with self.transaction() as con:
            actor = con.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor or not actor["active"] or not actor["admin"]:
                raise PermissionError("仅管理员可管理账号。")
            if user_id == actor_id and not active:
                raise ValueError("不能停用当前管理员自己。")
            con.execute("UPDATE users SET active=?,monthly_budget=?,storage_mb=? WHERE id=?", (int(active), monthly_budget, storage_mb, user_id))
            if not active:
                con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.audit(con, actor_id, "configure_user", user_id)

    def reset_password(self, actor_id, user_id, password):
        encoded = password_hash(password)
        with self.transaction() as con:
            actor = con.execute("SELECT * FROM users WHERE id=?", (actor_id,)).fetchone()
            if not actor or not actor["active"] or not actor["admin"]:
                raise PermissionError("仅管理员可重置密码。")
            con.execute("UPDATE users SET password=? WHERE id=?", (encoded, user_id))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.audit(con, actor_id, "reset_password", user_id)

    def change_password(self, user_id, old_password, password):
        with self.transaction() as con:
            row = con.execute("SELECT password FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if not row or not check_password(old_password, row[0]):
            raise ValueError("原密码不正确。")
        encoded = password_hash(password)
        with self.transaction() as con:
            current = con.execute("SELECT password FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
            if not current or current[0] != row[0]:
                raise ValueError("账号已变更，请重新登录。")
            con.execute("UPDATE users SET password=? WHERE id=?", (encoded, user_id))
            con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.audit(con, user_id, "change_password", user_id)

    def login(self, username, password, address):
        username = username.strip().lower()[:128]
        keys = ["user:" + username, "ip:" + address[:128]]
        now = time.time()
        with self.transaction() as con:
            for key in keys:
                row = con.execute("SELECT * FROM attempts WHERE key=?", (key,)).fetchone()
                if row and row["until"] > now and row["failures"] >= (5 if key.startswith('user:') else 25):
                    raise ValueError("登录失败或尝试过多，请稍后重试。")
            user = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        # Same scrypt work for unknown accounts; no password hashing under a DB write lock.
        stored = user['password'] if user else "00" * 16 + ":" + "00" * 64
        valid = check_password(password, stored)
        with self.transaction() as con:
            current = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not valid or not current or not current['active'] or current['password'] != stored:
                for key in keys:
                    con.execute("INSERT INTO attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET failures=CASE WHEN until<? THEN 1 ELSE failures+1 END, until=excluded.until", (key, now + 900, now))
                raise_after = True
            else:
                raise_after = False
                con.execute("DELETE FROM attempts WHERE key=?", (keys[0],))
                con.execute("DELETE FROM sessions WHERE expires<?", (now,))
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                session_id = hashlib.sha256(token.encode()).hexdigest()
                con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (session_id, current['id'], csrf, now + 8 * 3600))
                self.audit(con, current['id'], "login", current['id'])
        if raise_after:
            raise ValueError("登录失败或尝试过多，请稍后重试。")
        return token, csrf

    def session(self, *, token=None, session_id=None):
        if token is not None:
            session_id = hashlib.sha256(token.encode()).hexdigest()
        with self.transaction(write=False) as con:
            row = con.execute("SELECT u.id,u.username,u.admin,u.monthly_budget,u.storage_mb,s.id session_id,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires>? AND u.active=1", (session_id, time.time())).fetchone()
            return dict(row) if row else None

    def logout(self, token):
        with self.transaction() as con:
            con.execute("DELETE FROM sessions WHERE id=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def proof(self, user):
        return hmac.new(gateway_secret(), (user['id'] + ':' + user['session_id']).encode(), hashlib.sha256).hexdigest()

    def from_headers(self, headers):
        user_id = headers.get('X-IA-User', '')
        session_id = headers.get('X-IA-Session', '')
        proof = headers.get('X-IA-Proof', '')
        user = self.session(session_id=session_id)
        if not user or user['id'] != user_id or not hmac.compare_digest(self.proof(user), proof):
            raise PermissionError("登录已失效，请重新登录。")
        return user

    def tenant(self, user_id):
        if not re.fullmatch(r"[0-9a-f]{32}", user_id) or not self.user(user_id):
            raise PermissionError("无效的用户。")
        root = (self.root / 'users' / user_id).resolve()
        if not root.is_relative_to((self.root / 'users').resolve()):
            raise PermissionError("无效的用户目录。")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def settings(self, user_id):
        user = self.user(user_id)
        if not user or not user['active']:
            raise PermissionError("账号不可用。")
        shared = Settings.load(self.root / 'config')
        return replace(shared, data_dir=self.tenant(user_id), monthly_budget_cny=user['monthly_budget'], max_upload_mb=min(shared.max_upload_mb, int(os.environ.get('SERVER_UPLOAD_MB', '100'))))

    def acquire(self, user_id, kind, maximum, *, ttl=300):
        permit_id, now = secrets.token_hex(16), time.time()
        with self.transaction() as con:
            user = con.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
            if not user or not user[0]:
                return None
            con.execute("DELETE FROM permits WHERE expires<?", (now,))
            if con.execute("SELECT COUNT(*) FROM permits WHERE kind=?", (kind,)).fetchone()[0] >= maximum:
                return None
            if con.execute("SELECT 1 FROM permits WHERE user_id=? AND kind=?", (user_id,kind)).fetchone():
                return None
            con.execute("INSERT INTO permits VALUES (?,?,?,?)", (permit_id, user_id, kind, now + ttl))
        return permit_id

    def renew(self, permit_id, ttl=300):
        with self.transaction() as con:
            con.execute("UPDATE permits SET expires=? WHERE id=? AND expires>?", (time.time() + ttl, permit_id, time.time()))

    def release(self, permit_id):
        with self.transaction() as con:
            con.execute("DELETE FROM permits WHERE id=?", (permit_id,))

    def register_download(self, user_id, ticket, path, filename, mime):
        with self.transaction() as con:
            con.execute("INSERT OR REPLACE INTO downloads VALUES (?,?,?,?,?,?)", (user_id, ticket, path, filename, mime, time.time()))

    def download(self, user_id, ticket):
        with self.transaction(write=False) as con:
            row = con.execute("SELECT * FROM downloads WHERE user_id=? AND id=? AND created>?", (user_id, ticket, time.time() - 3600)).fetchone()
            return dict(row) if row else None

    def bind_web_session(self, web_id, user):
        with self.transaction() as con:
            existing = con.execute("SELECT user_id FROM web_sessions WHERE id=?", (web_id,)).fetchone()
            if existing and existing[0] != user['id']:
                raise PermissionError("上传会话不属于当前账号。")
            con.execute("DELETE FROM web_sessions WHERE created<?", (time.time() - 8 * 3600,))
            con.execute("INSERT OR REPLACE INTO web_sessions VALUES (?,?,?,?)", (web_id, user['id'], user['session_id'], time.time()))

    def owns_web_session(self, web_id, user):
        with self.transaction(write=False) as con:
            return bool(con.execute("SELECT 1 FROM web_sessions WHERE id=? AND user_id=? AND auth_session=? AND created>?", (web_id, user['id'], user['session_id'], time.time() - 8 * 3600)).fetchone())

    def purge_expired(self):
        from .files import inside
        cutoff = time.time() - 3600
        with self.transaction() as con:
            rows = con.execute("SELECT user_id,id,path FROM downloads WHERE created<?", (cutoff,)).fetchall()
            for row in rows:
                # Only generated caches, never originals, are eligible for unlink.
                if re.fullmatch(r'[0-9a-f]{32}', row['user_id']) and row['path'] == f"downloads/{row['id']}.bin" and re.fullmatch(r'[0-9a-f]{64}', row['id']):
                    inside(self.root / 'users' / row['user_id'], row['path']).unlink(missing_ok=True)
            con.execute("DELETE FROM downloads WHERE created<?", (cutoff,))
            con.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            con.execute("DELETE FROM attempts WHERE until<?", (time.time(),))
            con.execute("DELETE FROM web_sessions WHERE created<?", (time.time() - 8 * 3600,))


def bootstrap(accounts):
    """Creates only the very first account, never resets an existing deployment."""
    username, password = os.environ.get('BOOTSTRAP_ADMIN_USERNAME'), os.environ.get('BOOTSTRAP_ADMIN_PASSWORD')
    if not accounts.users():
        if not username or not password:
            raise ValueError("首次启动需设置 BOOTSTRAP_ADMIN_USERNAME 和 BOOTSTRAP_ADMIN_PASSWORD。")
        accounts.create_user(username, password, admin=True)


@lru_cache(maxsize=4)
def _cached_accounts(root):
    return Accounts(Path(root))


def get_accounts():
    # This cache contains only the DB path, never a user's identity or settings.
    return _cached_accounts(str(server_root()))
