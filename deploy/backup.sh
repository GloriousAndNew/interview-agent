#!/usr/bin/env bash
set -euo pipefail
umask 077
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$task_root"
mkdir -p backups
task_filename="backup-$(date +%Y%m%d-%H%M%S).tar.gz"
task_compose=(docker compose --env-file deploy/.env -f deploy/compose.yaml)
trap '"${task_compose[@]}" up -d auth web worker >/dev/null' EXIT
"${task_compose[@]}" stop auth web worker
"${task_compose[@]}" run --rm --no-deps --user 0:0 -v "$task_root/backups:/backup" auth python -m interview_agent.server_admin backup "/backup/$task_filename"
# Retain the seven newest successful archives. Never remove the live data volume.
python3 - <<'PY'
from pathlib import Path
import re
root = Path('backups').resolve()
files = sorted((p for p in root.glob('backup-*.tar.gz') if re.fullmatch(r'backup-\d{8}-\d{6}\.tar\.gz', p.name)), reverse=True)
for path in files[7:]:
    if path.parent != root:
        raise RuntimeError('Backup path outside expected folder')
    path.unlink()
    path.with_suffix(path.suffix + '.sha256').unlink(missing_ok=True)
PY
