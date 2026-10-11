"""Restore preflight and attachment cutover use isolated files and CLI fault injection."""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(SCRIPTS, self.root / 'deploy/scripts')
        # Brace the legacy prompt variable in the copied fixture so its locale bug does not mask unsafe restore order.
        script = self.root / 'deploy/scripts/restore.sh'
        script.write_text(script.read_text().replace('$POSTGRES_DB，', '${POSTGRES_DB}，'))
        (self.root / 'test.env').write_text('POSTGRES_USER=test\nPOSTGRES_DB=test\n')
        (self.root / 'bin').mkdir()
        self.calls = self.root / 'calls'
        docker = self.root / 'bin/docker'
        docker.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$RESTORE_CALLS"\nexit 99\n')
        docker.chmod(0o755)
        self.env = {**os.environ, 'PATH': f'{self.root}/bin:' + os.environ['PATH'],
                    'ENV_FILE': str(self.root / 'test.env'), 'RESTORE_CALLS': str(self.calls), 'RESTORE_PYTHON': sys.executable}
        self.db = self.root / 'db-20261011T000000Z.dump'
        self.up = self.root / 'uploads-20261011T000000Z.tar.gz'
        self.manifest = self.root / 'SHA256SUMS-20261011T000000Z'
        self.db.write_bytes(b'synthetic dump')
        self.archive('uploads/a.txt')
        self.hashes()

    def archive(self, name, kind=None):
        with tarfile.open(self.up, 'w:gz') as tar:
            info = tarfile.TarInfo(name)
            if kind:
                info.type, info.linkname = kind, '/outside'
            else:
                info.size = 4
            tar.addfile(info, None if kind else io.BytesIO(b'data'))

    def hashes(self):
        self.manifest.write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
                                         for p in (self.db, self.up)))

    def run_restore(self):
        return subprocess.run(['bash', str(self.root / 'deploy/scripts/restore.sh'), str(self.db), str(self.up)],
                              check=False, env=self.env, input='RESTORE\n', text=True, errors='replace', capture_output=True)

    def test_missing_attachments_stop_before_any_docker_operation(self):
        self.up.unlink()
        result = self.run_restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists(), result.stdout + result.stderr)

    def test_mismatched_checksum_stops_before_any_docker_operation(self):
        self.db.write_bytes(b'corrupt')
        result = self.run_restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists(), result.stdout + result.stderr)

    def test_archive_traversal_links_devices_and_reserved_recovery_paths_are_rejected(self):
        for name, kind in [('uploads/../../escape', None), ('/absolute', None), ('uploads/link', tarfile.SYMTYPE),
                           ('uploads/hard', tarfile.LNKTYPE), ('uploads/device', tarfile.CHRTYPE),
                           ('uploads/.restore-recovery/old', None)]:
            with self.subTest(name=name):
                self.calls.unlink(missing_ok=True)
                self.archive(name, kind)
                self.hashes()
                result = self.run_restore()
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists(), result.stdout + result.stderr)

    def test_valid_set_reaches_dump_verification_before_stopping_services(self):
        result = self.run_restore()
        self.assertNotEqual(result.returncode, 0)  # The isolated docker deliberately rejects the dump check.
        self.assertTrue(self.calls.exists(), result.stdout + result.stderr)
        calls = self.calls.read_text()
        self.assertIn('pg_restore -l', calls)
        self.assertNotIn('stop ', calls)

    def test_staging_and_cutover_keep_previous_files_and_journal(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('restore_files', SCRIPTS / 'restore_files.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = self.root / 'active-uploads'
        root.mkdir()
        (root / 'previous.txt').write_text('keep this original')
        token = 'a' * 24
        with self.up.open('rb') as stream:
            module.stage(root, token, stream)
        self.assertEqual((root / 'previous.txt').read_text(), 'keep this original')
        module.cutover(root, token)
        self.assertEqual((root / 'a.txt').read_bytes(), b'data')
        self.assertEqual((root / '.restore-recovery' / token / 'previous/previous.txt').read_text(), 'keep this original')
        self.assertIn('"phase": "complete"', (root / '.restore-recovery' / token / 'journal.json').read_text())
        with self.assertRaises((ValueError, FileExistsError)):
            module.cutover(root, token)
        module.rollback(root, token)
        self.assertEqual((root / "previous.txt").read_text(), "keep this original")
        self.assertFalse((root / "a.txt").exists())
        self.assertEqual((root / ".restore-recovery" / token / "replaced-restored/a.txt").read_bytes(), b"data")
