"""Opt-in real PostgreSQL restore rehearsal, with a local Docker CLI adapter.

This checks actual pg_dump/pg_restore, transactional DB rename, migrations/readiness,
attachment preservation and service ordering; it is NOT a Docker deployment test.
Requires an isolated loopback PostgreSQL superuser and Redis, never production settings.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ADAPTER = r'''
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['RESTORE_TRACE'], 'a') as trace:
    trace.write(json.dumps(args) + '\n')
env = dict(os.environ)
root = Path(env['RESTORE_TEST_ROOT'])
def run(command, **kwargs):
    subprocess.run(command, env=env, check=True, **kwargs)
if 'exec' in args:
    service = args[args.index('exec') + 2]
    command = args[args.index(service) + 1:]
    if service == 'postgres':
        run([str(Path(env['RESTORE_PG_BIN']) / command[0]), *command[1:]])
    elif service == 'backend':
        if env.get('RESTORE_FAIL_STAGE') == 'readiness':
            raise SystemExit(15)
        run([sys.executable, '-c', 'from app.main import readyz; assert readyz().status_code == 200'], cwd=env['RESTORE_SOURCE_REPO'])
    else:
        raise SystemExit(91)
elif 'run' in args:
    if 'migrate' in args:
        if env.get('RESTORE_FAIL_STAGE') == 'migration':
            raise SystemExit(16)
        run([sys.executable, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head'], cwd=str(Path(env['RESTORE_SOURCE_REPO']) / 'backend'))
        run([sys.executable, '-m', 'app.admin', 'sync-roles'], cwd=env['RESTORE_SOURCE_REPO'])
    elif 'backend' in args:
        code = args[args.index('-c') + 1].replace("'/data/uploads'", repr(str(root / 'uploads')))
        run([sys.executable, '-c', code, *args[args.index('-c') + 2:]])
    else:
        raise SystemExit(92)
elif not any(action in args for action in ('stop', 'up')):
    raise SystemExit(93)
'''


@unittest.skipUnless(os.environ.get('RESTORE_PG_BIN'), 'Set RESTORE_PG_BIN for explicit isolated database rehearsal')
class PostgreSQLRestoreTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        self.sql, self.psycopg = sql, psycopg
        self.assertIn(os.environ.get('PGHOST'), ('127.0.0.1', 'localhost', '::1'))
        self.name = 'restore_audit_' + uuid.uuid4().hex[:12]
        self.generated_names = {self.name}
        self.conn = psycopg.connect(dbname='postgres', autocommit=True)
        self.conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.name)))
        self.addCleanup(self.cleanup_databases)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.collect_database_names)
        self.root = Path(self.tmp.name)
        shutil.copytree(REPO / 'deploy/scripts', self.root / 'deploy/scripts')
        (self.root / 'bin').mkdir()
        adapter = self.root / 'bin/docker'
        adapter.write_text(f'#!{sys.executable}\n' + ADAPTER)
        adapter.chmod(0o755)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        (self.uploads / 'restored.txt').write_text('before backup')
        self.trace = self.root / 'trace.jsonl'
        env_file = self.root / 'isolated.env'
        env_file.write_text(f'POSTGRES_USER={os.environ["PGUSER"]}\nPOSTGRES_DB={self.name}\n')
        self.env = {**os.environ, 'ENV_FILE': str(env_file), 'RESTORE_PYTHON': sys.executable,
            'PATH': str(self.root / 'bin') + ':' + os.environ['PATH'], 'RESTORE_SOURCE_REPO': str(REPO),
            'RESTORE_TEST_ROOT': str(self.root), 'RESTORE_TRACE': str(self.trace), 'TL_SECRETS_FILE': '/dev/null',
            'TL_DATABASE_URL': f'postgresql+psycopg://{os.environ["PGUSER"]}@{os.environ["PGHOST"]}:{os.environ["PGPORT"]}/{self.name}',
            'TL_REDIS_URL': os.environ['RESTORE_TEST_REDIS_URL'], 'TL_CELERY_EAGER': 'false', 'TL_ENV': 'test',
            'PYTHONPATH': str(REPO / 'backend')}
        subprocess.run([sys.executable, '-m', 'alembic', '-c', 'alembic.ini', 'upgrade', 'head'],
                       cwd=REPO / 'backend', env=self.env, check=True)
        with psycopg.connect(dbname=self.name, autocommit=True) as db:
            db.execute('CREATE TABLE restore_probe (value text NOT NULL)')
            db.execute("INSERT INTO restore_probe VALUES ('snapshot')")
        self.dump = self.root / 'db-20261011T000000Z.dump'
        self.archive = self.root / 'uploads-20261011T000000Z.tar.gz'
        with self.dump.open('wb') as output:
            subprocess.run([str(Path(os.environ['RESTORE_PG_BIN']) / 'pg_dump'), '-Fc', '-d', self.name],
                           check=True, stdout=output)
        with tarfile.open(self.archive, 'w:gz') as archive:
            archive.add(self.uploads, arcname='uploads')
        (self.root / 'SHA256SUMS-20261011T000000Z').write_text(''.join(
            hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n' for path in (self.dump, self.archive)))
        with psycopg.connect(dbname=self.name, autocommit=True) as db:
            db.execute("UPDATE restore_probe SET value='latest retained'")
        (self.uploads / 'restored.txt').write_text('latest retained attachment')
        self.original_databases = {row[0] for row in self.conn.execute('SELECT datname FROM pg_database')}

    def collect_database_names(self):
        names = self.generated_names
        if getattr(self, 'trace', None) and self.trace.exists():
            for line in self.trace.read_text().splitlines():
                for arg in json.loads(line):
                    if arg.startswith(('staged=tuoyu_restore_', 'previous=tuoyu_previous_')):
                        names.add(arg.split('=', 1)[1])
    def cleanup_databases(self):
        # Only generated names from THIS invocation, never other test/user databases.
        for name in self.generated_names:
            self.conn.execute(self.sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(self.sql.Identifier(name)))
        self.conn.close()

    def run_restore(self, fail=''):
        return subprocess.run(['bash', str(self.root / 'deploy/scripts/restore.sh'), str(self.dump), str(self.archive)],
            check=False, cwd=self.root, env={**self.env, 'RESTORE_FAIL_STAGE': fail}, input='RESTORE\n', text=True,
            capture_output=True, timeout=120)

    def assert_retained_pair(self):
        with self.psycopg.connect(dbname=self.name, autocommit=True) as db:
            self.assertEqual(db.execute('SELECT value FROM restore_probe').fetchone()[0], 'snapshot')
        previous = [row for row in self.conn.execute('SELECT datname, datallowconn FROM pg_database')
                    if row[0] not in self.original_databases and row[0].startswith('tuoyu_previous_')]
        self.assertEqual(len(previous), 1)
        self.assertFalse(previous[0][1])
        self.conn.execute(self.sql.SQL('ALTER DATABASE {} ALLOW_CONNECTIONS true').format(self.sql.Identifier(previous[0][0])))
        with self.psycopg.connect(dbname=previous[0][0], autocommit=True) as db:
            self.assertEqual(db.execute('SELECT value FROM restore_probe').fetchone()[0], 'latest retained')
        self.assertEqual((self.uploads / 'restored.txt').read_text(), 'before backup')
        retained = list((self.uploads / '.restore-recovery').glob('*/previous/restored.txt'))
        self.assertEqual([path.read_text() for path in retained], ['latest retained attachment'])
        commands = [json.loads(line) for line in self.trace.read_text().splitlines()]
        starts = [args for args in commands if 'up' in args]
        self.assertTrue(all(args[-1] == 'backend' for args in starts))
        self.assertFalse(any('DROP DATABASE' in ' '.join(args) for args in commands))
        return commands

    def test_actual_restore_retains_original_pair_and_waits_for_external_reconciliation(self):
        result = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('/readyz passed', result.stdout)
        self.assertIn('remain STOPPED', result.stdout)
        commands = self.assert_retained_pair()
        migration = next(i for i, args in enumerate(commands) if 'migrate' in args)
        backend_start = next(i for i, args in enumerate(commands) if 'up' in args)
        self.assertLess(migration, backend_start)
        # Rehearse the documented rollback: preserve the restored copy and reinstate the retained pair.
        previous = next(arg.split('=', 1)[1] for args in commands for arg in args if arg.startswith('previous='))
        replaced = 'restore_replaced_' + uuid.uuid4().hex[:12]
        self.generated_names.add(replaced)
        self.conn.execute('BEGIN')
        self.conn.execute(self.sql.SQL('ALTER DATABASE {} RENAME TO {}').format(self.sql.Identifier(self.name), self.sql.Identifier(replaced)))
        self.conn.execute(self.sql.SQL('ALTER DATABASE {} RENAME TO {}').format(self.sql.Identifier(previous), self.sql.Identifier(self.name)))
        self.conn.execute('COMMIT')
        import importlib.util
        spec = importlib.util.spec_from_file_location('restore_files', REPO / 'deploy/scripts/restore_files.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        token = previous.removeprefix('tuoyu_previous_')
        module.rollback(self.uploads, token)
        with self.psycopg.connect(dbname=self.name, autocommit=True) as db:
            self.assertEqual(db.execute('SELECT value FROM restore_probe').fetchone()[0], 'latest retained')
        self.assertEqual((self.uploads / 'restored.txt').read_text(), 'latest retained attachment')


    def test_migration_failure_retains_pair_and_does_not_start_backend_or_writers(self):
        result = self.run_restore('migration')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('phase=migration', result.stderr)
        commands = self.assert_retained_pair()
        self.assertFalse(any('up' in args for args in commands))

    def test_readiness_failure_retains_pair_and_never_starts_writers(self):
        result = self.run_restore('readiness')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('phase=readiness', result.stderr)
        self.assertNotIn('/readyz passed', result.stdout)
        self.assert_retained_pair()
