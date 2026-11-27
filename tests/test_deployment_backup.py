"""Infrastructure snapshots on temporary synthetic input only."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

if sys.platform != "win32":
    from scripts.deploy import backup


@unittest.skipIf(sys.platform == "win32", "service wrapper uses POSIX file locking")
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir(mode=0o700)
        self.active = self.root / "db"
        self.active.mkdir()
        self.database = self.active / "ai.sqlite3"
        self.status = self.root / "status.json"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE runs(id INTEGER PRIMARY KEY)")
            db.execute("PRAGMA user_version=1")
            db.commit()

    def capture(self):
        return backup.run(self.database, self.vault, self.status, 14)

    def test_committed_wal_content_and_legacy_source_restored(self):
        (self.active / "sources.json").write_text('[{"id":"synthetic"}]')
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO runs VALUES (13)")
            writer.commit()
            writer.execute("INSERT INTO runs VALUES (14)")
            snap = self.capture()
            self.assertTrue(Path(str(self.database) + "-wal").exists())
        target = self.root / "admin-restore"
        backup.core.restore(snap, target)
        with closing(sqlite3.connect(target / "ai.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT * FROM runs").fetchall(), [(13,)])
        self.assertEqual((target / "sources.json").read_bytes(), (self.active / "sources.json").read_bytes())
        status = json.loads(self.status.read_text())
        self.assertEqual(status["status"], "ok")
        self.assertIn("last_restore_check", status)
        self.assertEqual(status["snapshot_id"], snap.name)
        self.assertEqual(status["source_schema"], 1)
        self.assertIs(status["source_quiescent"], False)
        self.assertNotIn("synthetic", self.status.read_text())

    def test_crc_failure_before_publication_preserves_recovery_point(self):
        first = self.capture()
        real_restore = backup.core.restore

        def corrupt(snapshot, target):
            with (snapshot / "ai.sqlite3").open("ab") as out:
                out.write(b"synthetic corruption")
            return real_restore(snapshot, target)

        with patch.object(backup.core, "restore", corrupt), self.assertRaises(ValueError):
            self.capture()
        self.assertEqual(list(self.vault.glob("pim-*")), [first])
        self.assertEqual(backup.core.verify(first)["sqlite_version"], 1)

    def test_retention_after_success_only(self):
        old = self.capture().rename(self.vault / "pim-20000101T000000Z-000000000001")
        keep = self.vault / "operator-hold"
        keep.mkdir()
        self.database.rename(self.active / "unavailable")
        with self.assertRaises(ValueError):
            self.capture()
        self.assertTrue(old.is_dir())
        (self.active / "unavailable").rename(self.database)
        newest = self.capture()
        self.assertFalse(old.exists())
        self.assertTrue(newest.is_dir() and keep.is_dir())

    def test_source_symlinks_fail_closed(self):
        real = self.database.rename(self.active / "real")
        self.database.symlink_to(real)
        with self.assertRaises(ValueError):
            self.capture()
        self.database.unlink()
        real.rename(self.database)
        (self.active / "sources.json").symlink_to(self.database)
        with self.assertRaises(ValueError):
            self.capture()
        self.assertFalse(list(self.vault.glob("pim-*")))

    @unittest.skipUnless(sys.platform == "linux" and getattr(os, "geteuid", lambda: -1)() == 0,
                         "requires root on a disposable Linux runner")
    def test_app_uid_cannot_read_mutate_or_delete_independent_snapshot(self):
        # Use only numeric UIDs and transient directories; no account changes.
        app_uid, backup_uid, app_gid, db_gid = 61101, 61102, 61103, 61104
        self.root.chmod(0o711)
        os.chown(self.vault, backup_uid, backup_uid)
        status_dir = self.root / "status"
        status_dir.mkdir()
        os.chown(status_dir, backup_uid, app_gid)
        status_dir.chmod(0o2750)
        self.status = status_dir / "status.json"
        os.chown(self.active, app_uid, db_gid)
        self.active.chmod(0o2750)
        os.chown(self.database, app_uid, db_gid)
        self.database.chmod(0o640)
        # Recreate trusted, world-readable installed source under the test
        # tree; the actual CI checkout may be in a non-traversable /home.
        scripts = self.root / "trusted/scripts"
        (scripts / "deploy").mkdir(parents=True)
        shutil.copyfile(backup.__file__, scripts / "deploy/backup.py")
        shutil.copyfile(backup.core.__file__, scripts / "pim_backup.py")
        runner = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from backup import run
os.umask(0o077)
print(run(*map(Path, sys.argv[2:]), 14).name)
"""
        proc = subprocess.run([sys.executable, "-IB", "-c", runner, str(scripts / "deploy"),
                               str(self.database), str(self.vault), str(self.status)],
                              check=True, capture_output=True, text=True,
                              user=backup_uid, group=backup_uid, extra_groups=[db_gid])
        snapshot = self.vault / proc.stdout.strip()
        check = """
import json, os, pathlib, sys
vault, snap, status = map(pathlib.Path, sys.argv[1:])
assert json.loads(status.read_text())['status'] == 'ok'
data = snap / 'ai.sqlite3'
for fn in (lambda: data.read_bytes(), lambda: data.open('r+b'), data.unlink,
           lambda: snap.rename(vault/'changed'), lambda: (vault/'injected').mkdir(),
           lambda: status.open('r+b'), status.unlink):
    try:
        fn()
    except PermissionError:
        continue
    raise SystemExit('unexpected access')
"""
        subprocess.run([sys.executable, "-IB", "-c", check, str(self.vault), str(snapshot),
                        str(self.status)], check=True, user=app_uid, group=app_gid,
                       extra_groups=[db_gid])
        self.assertEqual(backup.core.verify(snapshot)["sqlite_version"], 1)
