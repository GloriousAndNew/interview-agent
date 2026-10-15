from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from contextlib import contextmanager
from pathlib import Path


def uid() -> str:
    return uuid.uuid4().hex


class LostLease(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "app.db"
        with closing(self.connect()) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise RuntimeError("数据库版本高于本程序，请升级程序。")
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
                    path TEXT NOT NULL, size INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS interviews (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL DEFAULT '',
                    position TEXT NOT NULL DEFAULT '', date TEXT NOT NULL DEFAULT '',
                    media_id TEXT REFERENCES media(id), active_revision TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id),
                    status TEXT NOT NULL, stage TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '{}',
                    config TEXT NOT NULL, owner TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, cancel INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, retry_at, created);
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id),
                    origin TEXT NOT NULL, transcript_path TEXT NOT NULL, content TEXT NOT NULL,
                    created REAL NOT NULL, export_status TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS qa (
                    id TEXT NOT NULL, revision_id TEXT NOT NULL REFERENCES revisions(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL, question TEXT NOT NULL, answer TEXT, tags TEXT NOT NULL,
                    PRIMARY KEY (id, revision_id));
                CREATE INDEX IF NOT EXISTS qa_revision ON qa(revision_id);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), stage TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                    reserve_cny REAL NOT NULL, usage TEXT NOT NULL DEFAULT '{}',
                    request_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cleanup (
                    id TEXT PRIMARY KEY, job_id TEXT REFERENCES jobs(id), object_key TEXT NOT NULL UNIQUE,
                    config TEXT NOT NULL, eligible REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS job_events (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    call_id TEXT, created REAL NOT NULL, stage TEXT NOT NULL,
                    chunk_no INTEGER, attempt INTEGER, code TEXT NOT NULL,
                    severity TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_job ON job_events(job_id,created);
            """)
            con.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
            for name, kind in (("started_at", "REAL"), ("finished_at", "REAL"), ("stage_started_at", "REAL"), ("timing_estimated", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
            if "started_at" not in columns:
                # Older jobs have no exact start clock. Keep the approximation explicit.
                con.execute("""UPDATE jobs SET started_at=COALESCE(
                    (SELECT MIN(created) FROM job_events WHERE job_id=jobs.id AND code='TASK_STARTED'),
                    (SELECT MIN(created) FROM calls WHERE job_id=jobs.id)),
                    timing_estimated=1 WHERE status<>'QUEUED'""")
                con.execute("UPDATE jobs SET finished_at=updated WHERE status IN ('COMPLETED','FAILED','CANCELLED')")
            con.execute("PRAGMA user_version=3")
            con.commit()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=15000")
        return con

    @contextmanager
    def transaction(self):
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def rows(self, sql: str, values=()) -> list[dict]:
        con = self.connect()
        try:
            return [dict(r) for r in con.execute(sql, values).fetchall()]
        finally:
            con.close()

    def one(self, sql: str, values=()) -> dict | None:
        rows = self.rows(sql, values)
        return rows[0] if rows else None

    def event(self, job: dict, code: str, message: str, *, severity: str = "info", chunk_no: int | None = None, attempt: int | None = None, call_id: str | None = None, details: dict | None = None) -> None:
        with self.transaction() as con:
            self.guard(con, job, allow_cancel=True)
            con.execute("INSERT INTO job_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uid(), job["id"], call_id, time.time(), job["stage"], chunk_no, attempt, code, severity, message, json.dumps(details or {}, ensure_ascii=False)))

    def events(self, job_id: str, limit: int = 30) -> list[dict]:
        rows = self.rows("SELECT created,stage,chunk_no,attempt,code,severity,message,details,call_id FROM job_events WHERE job_id=? ORDER BY created DESC LIMIT ?", (job_id, limit))
        for row in rows:
            row["details"] = json.loads(row["details"])
        return rows

    def create(self, title: str, metadata: dict, config: dict, checkpoint: dict, media: dict | None = None) -> tuple[str, str]:
        now, interview_id, job_id = time.time(), uid(), uid()
        with self.transaction() as con:
            media_id = None
            if media:
                old = con.execute("SELECT id FROM media WHERE sha256=?", (media["sha256"],)).fetchone()
                media_id = old[0] if old else media["id"]
                if not old:
                    con.execute("INSERT INTO media VALUES (?,?,?,?,?)", (media_id, media["sha256"], media["filename"], media["path"], media["size"]))
            con.execute("INSERT INTO interviews(id,title,company,position,date,media_id,created) VALUES (?,?,?,?,?,?,?)", (interview_id, title, metadata.get("company", ""), metadata.get("position", ""), metadata.get("date", ""), media_id, now))
            con.execute("INSERT INTO jobs(id,interview_id,status,stage,checkpoint,config,created,updated) VALUES (?,?,?,?,?,?,?,?)", (job_id, interview_id, "QUEUED", "PREPROCESSING", json.dumps(checkpoint), json.dumps(config), now, now))
        return interview_id, job_id

    def claim(self, owner: str) -> dict | None:
        now = time.time()
        with self.transaction() as con:
            # Queued cancellations need no network calls or active lease.
            con.execute("UPDATE jobs SET status='CANCELLED',updated=?,finished_at=? WHERE cancel=1 AND (status IN ('QUEUED','RETRY_WAIT') OR (status='RUNNING' AND lease_until<?))", (now, now, now))
            row = con.execute("SELECT j.* FROM jobs j JOIN interviews i ON i.id=j.interview_id WHERE i.deleted=0 AND j.cancel=0 AND ((j.status IN ('QUEUED','RETRY_WAIT') AND j.retry_at<=?) OR (j.status='RUNNING' AND j.lease_until<?)) ORDER BY j.created LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            con.execute("UPDATE jobs SET status='RUNNING',owner=?,epoch=epoch+1,lease_until=?,updated=?,started_at=COALESCE(started_at,?),stage_started_at=?,finished_at=NULL WHERE id=?", (owner, now + 90, now, now, now, row["id"]))
            result = dict(con.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        result["checkpoint"] = json.loads(result["checkpoint"])
        result["config"] = json.loads(result["config"])
        return result

    @staticmethod
    def guard(con, job: dict, allow_cancel: bool = False):
        row = con.execute("SELECT j.*,i.deleted FROM jobs j JOIN interviews i ON i.id=j.interview_id WHERE j.id=?", (job["id"],)).fetchone()
        if not row or row["status"] != "RUNNING" or row["owner"] != job["owner"] or row["epoch"] != job["epoch"] or row["lease_until"] < time.time():
            raise LostLease("任务租约已失效")
        if (row["cancel"] or row["deleted"]) and not allow_cancel:
            raise InterruptedError("任务已取消")
        return row

    def heartbeat(self, job: dict) -> bool:
        with self.transaction() as con:
            try:
                self.guard(con, job, allow_cancel=True)
            except LostLease:
                return False
            con.execute("UPDATE jobs SET lease_until=?,updated=? WHERE id=?", (time.time() + 90, time.time(), job["id"]))
        return True

    def check(self, job: dict) -> None:
        with self.transaction() as con:
            self.guard(con, job)

    def checkpoint(self, job: dict, stage: str | None = None) -> None:
        with self.transaction() as con:
            row = self.guard(con, job)
            con.execute("UPDATE jobs SET checkpoint=?,stage=?,updated=?,error='' WHERE id=?", (json.dumps(job["checkpoint"]), stage or job["stage"], time.time(), job["id"]))
            if stage and stage != row["stage"]:
                con.execute("UPDATE jobs SET stage_started_at=? WHERE id=?", (time.time(), job["id"]))
            if stage and stage != job["stage"]:
                con.execute("INSERT INTO job_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uid(), job["id"], None, time.time(), stage, None, None, "STAGE_ENTERED", "info", "进入处理阶段", "{}"))
        if stage:
            job["stage"] = stage

    def finish(self, job: dict, status: str, error: str = "", retry: bool = False) -> None:
        with self.transaction() as con:
            row = self.guard(con, job, allow_cancel=True)
            if row["cancel"]:
                status = "CANCELLED"
            attempts = row["attempts"] + (1 if error else 0)
            if retry and attempts < 3 and not row["cancel"]:
                status = "RETRY_WAIT"
            con.execute("UPDATE jobs SET status=?,error=?,attempts=?,retry_at=?,owner=NULL,lease_until=0,updated=? WHERE id=?", (status, error, attempts, time.time() + min(120, 5 * 2 ** attempts), time.time(), job["id"]))
            con.execute("UPDATE jobs SET finished_at=? WHERE id=?", (None if status == "RETRY_WAIT" else time.time(), job["id"]))
            severity = "error" if status == "FAILED" else "warning" if status == "RETRY_WAIT" else "info"
            message = error or ("问答、原文及笔记已保存" if status == "COMPLETED" else "任务状态已更新")
            con.execute("INSERT INTO job_events VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uid(), job["id"], None, time.time(), job["stage"], None, None, "TASK_" + status, severity, message, "{}"))

    def reserve(self, job: dict, provider: str, model: str, amount: float, budget: float) -> str:
        call_id = uid()
        with self.transaction() as con:
            self.guard(con, job)
            from datetime import datetime
            from zoneinfo import ZoneInfo
            month = datetime.now(ZoneInfo("Asia/Shanghai")).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
            used = con.execute("SELECT COALESCE(SUM(reserve_cny),0) FROM calls WHERE created>=?", (month,)).fetchone()[0]
            if used + amount > budget:
                raise BudgetExceeded("本月费用预留达到预算，请在设置中调整或等待下月。")
            con.execute("INSERT INTO calls(id,job_id,stage,provider,model,status,reserve_cny,created) VALUES (?,?,?,?,?,'reserved',?,?)", (call_id, job["id"], job["stage"], provider, model, amount, time.time()))
        return call_id

    def record_call(self, call_id: str, status: str, usage: dict | None = None, request_id: str = "") -> None:
        with self.transaction() as con:
            if usage is None:
                con.execute("UPDATE calls SET status=? WHERE id=?", (status, call_id))
            else:
                con.execute("UPDATE calls SET status=?,usage=?,request_id=? WHERE id=?", (status, json.dumps(usage), request_id, call_id))

    def save_revision(self, job: dict, content: dict, transcript_path: str) -> str:
        with self.transaction() as con:
            self.guard(con, job)
            revision_id = content["revision_id"]
            con.execute("INSERT OR IGNORE INTO revisions(id,interview_id,origin,transcript_path,content,created) VALUES (?,?,?,?,?,?)", (revision_id, job["interview_id"], "model", transcript_path, json.dumps(content, ensure_ascii=False), time.time()))
            for i, q in enumerate(content["items"]):
                con.execute("INSERT OR IGNORE INTO qa VALUES (?,?,?,?,?,?)", (q["id"], revision_id, i, q["question"], q["answer_summary"], "、".join(q["tags"])))
            # A newly generated revision never replaces a human-edited one.
            current = con.execute("SELECT r.origin FROM interviews i LEFT JOIN revisions r ON r.id=i.active_revision WHERE i.id=?", (job["interview_id"],)).fetchone()
            if not current or current[0] != "human":
                con.execute("UPDATE interviews SET active_revision=? WHERE id=?", (revision_id, job["interview_id"]))
        return revision_id

    def edit_revision(self, interview_id: str, expected: str, content: dict) -> str:
        with self.transaction() as con:
            old = con.execute("SELECT r.* FROM interviews i JOIN revisions r ON r.id=i.active_revision WHERE i.id=? AND i.deleted=0", (interview_id,)).fetchone()
            if not old or old["id"] != expected:
                raise ValueError("内容已被其他窗口更新，请刷新后重试。")
            new_id = uid()
            content["revision_id"] = new_id
            con.execute("INSERT INTO revisions(id,interview_id,origin,transcript_path,content,created) VALUES (?,?,?,?,?,?)", (new_id, interview_id, "human", old["transcript_path"], json.dumps(content, ensure_ascii=False), time.time()))
            for i, q in enumerate(content["items"]):
                con.execute("INSERT INTO qa VALUES (?,?,?,?,?,?)", (q["id"], new_id, i, q["question"], q.get("answer_summary"), "、".join(q["tags"])))
            con.execute("UPDATE interviews SET active_revision=? WHERE id=?", (new_id, interview_id))
        return new_id

    def set_active(self, interview_id: str, revision_id: str) -> None:
        with self.transaction() as con:
            if not con.execute("SELECT 1 FROM revisions WHERE id=? AND interview_id=?", (revision_id, interview_id)).fetchone():
                raise ValueError("版本不属于此面试")
            con.execute("UPDATE interviews SET active_revision=? WHERE id=? AND deleted=0", (revision_id, interview_id))

    def cancel(self, job_id: str) -> None:
        with self.transaction() as con:
            con.execute("UPDATE jobs SET cancel=1 WHERE id=? AND status IN ('QUEUED','RUNNING','RETRY_WAIT')", (job_id,))

    def retry(self, job_id: str) -> None:
        with self.transaction() as con:
            row = con.execute("SELECT checkpoint FROM jobs WHERE id=? AND status IN ('FAILED','CANCELLED')", (job_id,)).fetchone()
            if not row:
                return
            cp = json.loads(row[0])
            if cp.get("submission_unknown"):
                raise ValueError("远端提交状态不明，不能自动重提；请先在阿里云确认任务。")
            con.execute("UPDATE jobs SET status='QUEUED',attempts=0,cancel=0,retry_at=0,error='',finished_at=NULL WHERE id=?", (job_id,))

    def search(self, keyword: str = "", position: str = "") -> list[dict]:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pos = position.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self.rows("SELECT i.id interview_id,i.title,i.company,i.position,i.date,q.* FROM interviews i JOIN qa q ON q.revision_id=i.active_revision WHERE i.deleted=0 AND (q.question LIKE ? ESCAPE '\\' OR COALESCE(q.answer,'') LIKE ? ESCAPE '\\' OR q.tags LIKE ? ESCAPE '\\' OR i.title LIKE ? ESCAPE '\\') AND i.position LIKE ? ESCAPE '\\' ORDER BY i.created DESC,q.ordinal LIMIT 100", (*([f"%{escaped}%"] * 4), f"%{pos}%"))

    def backup(self) -> Path:
        target = self.root / "backups" / f"app-{time.time_ns()}.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        source, dest = self.connect(), sqlite3.connect(target)
        try:
            source.backup(dest)
        finally:
            source.close()
            dest.close()
        return target
