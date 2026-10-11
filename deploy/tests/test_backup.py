"""Exercise the real backup shell script using isolated, non-networked tools.

Run: python3 -m unittest discover -s deploy/tests -v
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup.sh"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "deploy/scripts").mkdir(parents=True)
        shutil.copy2(SCRIPT, self.root / "deploy/scripts/backup.sh")
        (self.root / "test.env").write_text("POSTGRES_USER=test\nPOSTGRES_DB=test\n")
        (self.root / "bin").mkdir()
        (self.root / "data/uploads").mkdir(parents=True)
        (self.root / "data/uploads/attachment.txt").write_text("synthetic attachment")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.old = self.backups / "db-old.dump"
        self.old.write_text("previous valid backup")
        os.utime(self.old, (time.time() - 30 * 86400,) * 2)
        self.metrics = self.root / "metrics"
        self.metrics.mkdir()
        (self.metrics / "backup.prom").write_text("node_textfile_backup_last_success_timestamp 123\n")
        self.env = {**os.environ, "PATH": f"{self.root}/bin:/usr/bin:/bin", "ENV_FILE": "test.env",
                    "BACKUP_DIR": str(self.backups), "NODE_TEXTFILE_DIR": str(self.metrics),
                    "TEST_ROOT": str(self.root), "BACKUP_REMOTE": "", "FAIL_STAGE": ""}
        self.tool("docker", '''#!/bin/bash
set -e
case " $* " in
  *" pg_dump "*) [ "$FAIL_STAGE" != db ] || exit 2; echo synthetic-dump ;;
  *" pg_restore "*) cat >/dev/null; [ "$FAIL_STAGE" != dump_check ] || exit 2 ;;
  *" tar "*)
    case "$FAIL_STAGE" in
      uploads) exit 2 ;;
      empty_upload) exit 0 ;;
      invalid_upload) echo corrupt; exit 0 ;;
    esac
    tar -C "$TEST_ROOT/data" -czf - uploads ;;
  *) exit 99 ;;
esac
''')
        self.tool("sha256sum", '#!/bin/bash\nexec shasum -a 256 "$@"\n')

    def tool(self, name, content):
        p = self.root / "bin" / name
        p.write_text(content)
        p.chmod(0o755)

    def run_backup(self, **env):
        return subprocess.run(["bash", str(self.root / "deploy/scripts/backup.sh")],
                              env={**self.env, **env}, text=True, capture_output=True, check=False)

    def assert_failed_safely(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("备份完成", result.stdout)
        self.assertTrue(self.old.exists(), "failed backup pruned the previous recovery set")
        self.assertEqual(list(self.backups.glob("SHA256SUMS-*")), [])
        self.assertEqual((self.metrics / "backup.prom").read_text(),
                         "node_textfile_backup_last_success_timestamp 123\n")

    def test_required_stages_fail_closed(self):
        for stage in ("db", "dump_check", "uploads", "empty_upload", "invalid_upload"):
            with self.subTest(stage=stage):
                self.assert_failed_safely(self.run_backup(FAIL_STAGE=stage))

    def test_configured_remote_requires_rclone(self):
        self.assert_failed_safely(self.run_backup(BACKUP_REMOTE="test:backups"))

    def test_remote_failure_preserves_previous_backup(self):
        self.tool("rclone", "#!/bin/bash\nexit 2\n")
        self.assert_failed_safely(self.run_backup(BACKUP_REMOTE="test:backups"))

    def test_success_contains_relocatable_verified_recovery_set(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifests = list(self.backups.glob("SHA256SUMS-*"))
        self.assertEqual(len(manifests), 1)
        for line in manifests[0].read_text().splitlines():
            self.assertFalse(Path(line.split(maxsplit=1)[1]).is_absolute())
        check = subprocess.run(["shasum", "-a", "256", "-c", manifests[0].name],
                               cwd=self.backups, capture_output=True, text=True, check=False)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertNotIn(" 123\n", (self.metrics / "backup.prom").read_text())
        self.assertFalse(self.old.exists())
        self.assertEqual(list(self.backups.glob(".backup-*")), [])

    def test_remote_gets_only_complete_current_set(self):
        self.tool("rclone", '''#!/bin/bash
set -e
if [ "$1" = copy ]; then
  [ "$4" = --exclude ] && [ "$5" = 'SHA256SUMS-*' ]
  cd "$2"
  shasum -a 256 -c SHA256SUMS-* >/dev/null
  [ "$(find . -type f | wc -l | tr -d ' ')" = 3 ]
  touch "$TEST_ROOT/data-copied"
elif [ "$1" = copyto ]; then
  [ -f "$TEST_ROOT/data-copied" ] && [ -s "$2" ]
else
  exit 99
fi
''')
        result = self.run_backup(BACKUP_REMOTE="test:backups")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
