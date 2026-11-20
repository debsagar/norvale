"""Opt a dedicated company run into staffed maintenance and provision its actors."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.error import HTTPError

from native.company import save, writable_run
from native.systems_v2 import Tickets, stack_for
from .maintenance_staff import ACTORS

REPO = '/repos/controller/operations'


def provision(run):
    run = writable_run(run)
    config = json.loads((run / 'config.json').read_text())
    sources = run / 'env/company-sources.json'
    if (config.get('company_authority') != 'integrations.company_run.authority:Authority'
            or not (run / 'env/site-created').is_file() or not sources.is_file()):
        raise ValueError('Choose a provisioned dedicated native company run')
    with (run / 'execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cfg = json.loads(sources.read_text())
        if not (run / 'ticket-check.json').is_file():
            subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] /
                            'native/provision_tickets_v2.py'), str(run)], check=True)
        admin = Tickets(run)
        if admin.request('GET', REPO).get('private') is not True:
            raise ValueError('Staffed maintenance requires a private operations repository')
        stack = stack_for(run)
        path = run / 'env/maintenance-tokens.json'
        tokens = json.loads(path.read_text()) if path.exists() else {}
        for role, login in ACTORS.items():
            try:
                user = admin.request('GET', '/users/' + login)
            except HTTPError as exc:
                if exc.code != 404:
                    raise
                password = secrets.token_urlsafe(32)
                try:
                    user = admin.request('POST', '/admin/users', dict(username=login, login_name=login,
                        email=login + '@norvale.test', password=password, must_change_password=False))
                except HTTPError:
                    raise RuntimeError(f'Could not provision maintenance {role}') from None
            if user.get('login') != login or user.get('is_admin'):
                raise ValueError('Maintenance actors must be non-administrator accounts')
            admin.request('PUT', REPO + '/collaborators/' + login, {'permission': 'read'})
            if role not in tokens:
                scope = 'read:issue' if role == 'reader' else 'write:issue'
                result = stack.compose('exec', '-T', '--user', 'git', 'tickets', 'gitea', 'admin', 'user',
                    'generate-access-token', '--username', login, '--token-name',
                    'maintenance-' + secrets.token_hex(6), '--scopes', scope, '--raw',
                    capture_output=True, text=True)
                tokens[role] = result.stdout.strip()
                save(path, tokens)
                path.chmod(0o600)
            Tickets(run, token=tokens[role]).records()
        cfg['maintenance_staff'] = tokens
        cfg.setdefault('restart_verifier', ACTORS['verifier'])
        cfg.setdefault('maintenance_ticket_reader', tokens['reader'])
        save(sources, cfg)
        sources.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    os.umask(0o077)
    provision(parser.parse_args().run)


if __name__ == '__main__':
    main()
