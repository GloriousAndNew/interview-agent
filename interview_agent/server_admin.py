"""Trusted operator CLI, invoked through SSH/container exec, never through HTTP."""
import argparse
import getpass
import hashlib
import os
from pathlib import Path
import tarfile

from .server_auth import Accounts, server_root


def backup(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root):
        raise ValueError('Backup output must be outside the data directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    if output.exists():
        raise ValueError('Refusing to overwrite an existing backup')
    try:
        with tarfile.open(temporary, 'w:gz') as archive:
            for path in sorted(root.rglob('*')):
                if path.is_symlink():
                    raise ValueError('Symlinks are not allowed in the backup source')
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
        os.chmod(temporary, 0o600)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    with output.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    checksum = output.with_suffix(output.suffix + '.sha256')
    checksum.write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    os.chmod(checksum, 0o600)
    return output


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-user')
    create.add_argument('username')
    create.add_argument('--admin', action='store_true')
    create.add_argument('--budget', type=float, default=50)
    create.add_argument('--storage-mb', type=int, default=2048)
    commands.add_parser('list-users')
    reset = commands.add_parser('reset-password')
    reset.add_argument('username')
    copy = commands.add_parser('backup')
    copy.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.command == 'backup':
        print(backup(server_root(), args.output))
        return
    accounts = Accounts(server_root())
    if args.command == 'create-user':
        password = getpass.getpass('New password (12–128 characters): ')
        if password != getpass.getpass('Confirm password: '):
            raise ValueError('Passwords do not match')
        accounts.create_user(args.username, password, admin=args.admin, monthly_budget=args.budget, storage_mb=args.storage_mb, actor='ssh-operator')
        print('Account created')
    elif args.command == 'list-users':
        for user in accounts.users():
            print(user['username'], 'admin' if user['admin'] else 'user', 'active' if user['active'] else 'disabled', user['monthly_budget'], user['storage_mb'])
    elif args.command == 'reset-password':
        users = accounts.users()
        target = next((u for u in users if u['username'] == args.username), None)
        actor = next((u for u in users if u['admin'] and u['active']), None)
        if not target or not actor:
            raise ValueError('Account or active administrator not found')
        accounts.reset_password(actor['id'], target['id'], getpass.getpass('New password: '))
        print('Password reset; all sessions revoked')


if __name__ == '__main__':
    main()
