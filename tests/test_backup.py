import gzip
import importlib.util
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("backup", Path(__file__).parents[1] / "lib/backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)

STEM = "backup_2026-01-01-0000_fixture_123456789abc"
SQL = "".join(f"CREATE TABLE {name} (id INT);\n" for name in ("wp_options", "wp_posts", "wp_users"))


class BackupTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.out = self.root / "stage"
        self.out.mkdir()

    def write_zip(self, name, *entries):
        with zipfile.ZipFile(self.root / name, "w") as archive:
            for entry in entries:
                archive.writestr(entry, b"fixture")
        return self.root / name

    def updraft(self):
        with gzip.open(self.root / (STEM + "-db.gz"), "wt") as stream:
            stream.write(SQL)
        backup.stage_backup(self.out, 1, "wp_", self.root, STEM, "", "")

    def test_split_archives_merge(self):
        self.write_zip(STEM + "-uploads.zip", "uploads/file-a")
        self.write_zip(STEM + "-uploads2.zip", "uploads/file-b")
        self.updraft()
        self.assertEqual(len(list((self.out / "content/uploads").iterdir())), 2)

    def test_missing_part(self):
        self.write_zip(STEM + "-uploads.zip", "uploads/file-a")
        self.write_zip(STEM + "-uploads3.zip", "uploads/file-b")
        with self.assertRaisesRegex(ValueError, "Missing UpdraftPlus"):
            self.updraft()

    def test_traversal_is_blocked(self):
        self.write_zip(STEM + "-others.zip", "../outside")
        with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
            self.updraft()
        self.assertFalse((self.root / "outside").exists())

    def test_symlink_is_blocked(self):
        entry = zipfile.ZipInfo("wp-content/link")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.write_zip("files.zip", entry)
        with self.assertRaisesRegex(ValueError, "Unsupported ZIP entry"):
            backup.Stage(self.out, 1024).zip(archive, None)

    def test_normal_backup_and_prefix_check(self):
        archive = self.write_zip("files.zip", "wp-content/themes/style.css", "wp-config.php")
        sql = self.root / "dump.sql"
        sql.write_text(SQL, encoding="ascii")
        with self.assertRaisesRegex(ValueError, "expected CREATE TABLE"):
            backup.stage_backup(self.out, 1, "wrong_", "", "", archive, sql)
        self.assertTrue((self.out / "content/themes/style.css").is_file())
        self.assertFalse((self.out / "content/wp-config.php").exists())

    def test_limit(self):
        archive = self.write_zip("files.zip", "wp-content/file")
        with self.assertRaisesRegex(ValueError, "size limit"):
            backup.Stage(self.out, 1).zip(archive, None)

    def test_duplicate_file_is_rejected(self):
        first = self.write_zip("first.zip", "uploads/file")
        second = self.write_zip("second.zip", "uploads/file")
        stage = backup.Stage(self.out, 1024)
        stage.zip(first, "uploads")
        with self.assertRaises(FileExistsError):
            stage.zip(second, "uploads")

    def test_snapshot_detects_corruption(self):
        (self.out / "content").mkdir()
        (self.out / "db.sql").write_text(SQL, encoding="ascii")
        before = backup.snapshot_manifest(self.out)
        (self.out / "content/new-file").write_bytes(b"new")
        self.assertNotEqual(before, backup.snapshot_manifest(self.out))


if __name__ == "__main__":
    unittest.main()
