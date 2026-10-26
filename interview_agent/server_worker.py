"""One scheduler, bounded workers, at most one running recording per user."""
from concurrent.futures import ThreadPoolExecutor
import logging
import os
import signal
import threading
import time

from .db import Store
from .files import atomic_json
from .pipeline import Pipeline
from .server_auth import Accounts, server_root
from .server_ui import check_capacity


log = logging.getLogger('interview-server-worker')


def execute(accounts, user_id):
    try:
        settings = accounts.settings(user_id)
        store = Store(settings.data_dir)
        pipeline = Pipeline(settings, store)
        # Cleanup must still run when storage admission blocks new work.
        pipeline.maintenance()
        try:
            check_capacity(store)
        except ValueError as exc:
            from .db import uid
            job = store.claim(uid())
            if job:
                store.finish(job, 'FAILED', str(exc))
            return False
        worked = pipeline.run_once()
        pipeline.maintenance()
        return worked
    except Exception:
        # Do not log supplier exceptions, signed URLs, keys or transcript content.
        log.warning('Tenant processing paused; user_id=%s', user_id)
        return False


def tick(accounts, executor, running, *, maximum=2, cursor=0, execute_fn=execute):
    now = time.time()
    for user_id, (future, permit, renewed) in list(running.items()):
        if future.done():
            try:
                future.result()
            except Exception:
                log.warning('Tenant worker stopped; user_id=%s', user_id)
            accounts.release(permit)
            del running[user_id]
        elif now - renewed >= 15:
            accounts.renew(permit)
            running[user_id] = (future, permit, now)
    users = [u for u in accounts.users() if u['active']]
    if not users:
        return 0
    cursor %= len(users)
    ordered = users[cursor:] + users[:cursor]
    next_cursor = cursor
    for offset, user in enumerate(ordered):
        if len(running) >= maximum:
            break
        user_id = user['id']
        if user_id in running:
            continue
        root = accounts.tenant(user_id)
        if not (root / 'app.db').exists():
            continue
        store = Store(root)
        atomic_json(root / 'worker-status.json', {'updated': now})
        ready = store.one("SELECT id FROM jobs WHERE status IN ('QUEUED','RETRY_WAIT','RUNNING') AND retry_at<=? AND (status<>'RUNNING' OR lease_until<?) LIMIT 1", (now, now))
        cleanup = store.one("SELECT id FROM cleanup WHERE status='pending' AND eligible<=? LIMIT 1", (now,))
        deleted = store.one("SELECT id FROM interviews WHERE deleted=1 LIMIT 1")
        if not (ready or cleanup or deleted):
            continue
        permit = accounts.acquire(user_id, 'job', maximum)
        if permit:
            running[user_id] = (executor.submit(execute_fn, accounts, user_id), permit, now)
            next_cursor = (cursor + offset + 1) % len(users)
    return next_cursor


def main():
    from worker import single_worker
    maximum = int(os.environ.get('SERVER_JOB_CONCURRENCY', '2'))
    if not 1 <= maximum <= 4:
        raise ValueError('SERVER_JOB_CONCURRENCY must be 1–4')
    accounts = Accounts(server_root())
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    with single_worker(accounts.root / 'server-worker.lock'), ThreadPoolExecutor(max_workers=maximum) as executor:
        running, cursor, last_cleanup = {}, 0, 0
        while not stopping.is_set():
            if time.time() - last_cleanup >= 60:
                accounts.purge_expired()
                last_cleanup = time.time()
            cursor = tick(accounts, executor, running, maximum=maximum, cursor=cursor)
            atomic_json(accounts.root / 'server-worker-status.json', {'updated': time.time(), 'running': len(running), 'capacity': maximum})
            stopping.wait(2)
        # No new jobs. Containers allow a grace period; checkpoints survive forced exit.
        while running:
            for user_id, (future, permit, renewed) in list(running.items()):
                if future.done():
                    accounts.release(permit)
                    del running[user_id]
                else:
                    accounts.renew(permit)
            time.sleep(1)


if __name__ == '__main__':
    main()
