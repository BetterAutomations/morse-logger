"""Tests for prune: archive or delete logs older than N days.

Nothing is removed unless asked, the two canonical stores are pruned to the
same cutoff, and unparsable JSONL lines are never dropped silently.
"""

import gzip
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from morse_logger import retention  # noqa: E402

DAY = "%Y-%m-%d"


class PruneCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.logs = os.path.join(self.home, "logs")
        os.makedirs(self.logs)
        self.old = datetime.now() - timedelta(days=40)
        self.new = datetime.now() - timedelta(days=1)
        self.cutoff = (datetime.now() - timedelta(days=30)).strftime(DAY)
        self.write_text(self.old, "OLD LINE\n")
        self.write_text(self.new, "NEW LINE\n")
        with open(os.path.join(self.logs, "notes.txt"), "w") as f:
            f.write("not ours\n")
        with open(os.path.join(self.logs, retention.JSONL), "w") as f:
            f.write(json.dumps({"text": "old", "logged_at":
                                self.old.strftime("%Y-%m-%dT12:00:00")}) + "\n")
            f.write(json.dumps({"text": "new", "logged_at":
                                self.new.strftime("%Y-%m-%dT12:00:00")}) + "\n")
            f.write("not json at all\n")
        db = sqlite3.connect(os.path.join(self.logs, retention.DB))
        db.execute("CREATE TABLE captures (id INTEGER PRIMARY KEY, "
                   "started_utc TEXT, text TEXT)")
        db.execute("INSERT INTO captures (started_utc, text) VALUES (?, ?)",
                   (self.old.strftime("%Y-%m-%dT12:00:00+00:00"), "old"))
        db.execute("INSERT INTO captures (started_utc, text) VALUES (?, ?)",
                   (self.new.strftime("%Y-%m-%dT12:00:00+00:00"), "new"))
        db.commit()
        db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def write_text(self, day, text):
        path = os.path.join(self.logs, f"morse-{day.strftime(DAY)}.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    def text(self, day):
        return os.path.join(self.logs, f"morse-{day.strftime(DAY)}.txt")

    def jsonl(self):
        with open(os.path.join(self.logs, retention.JSONL)) as f:
            return f.read().splitlines()

    def rows(self):
        db = sqlite3.connect(os.path.join(self.logs, retention.DB))
        try:
            return db.execute("SELECT text FROM captures ORDER BY id").fetchall()
        finally:
            db.close()

    def run_prune(self, **kw):
        return retention.prune(self.home, self.logs, 30, **kw)


class DryRunTest(PruneCase):
    def test_reports_without_touching_anything(self):
        out = self.run_prune(dry_run=True)
        self.assertEqual(out["files"], 1)
        self.assertEqual(out["jsonl_lines"], 1)
        self.assertEqual(out["rows"], 1)
        self.assertTrue(os.path.exists(self.text(self.old)))
        self.assertEqual(len(self.jsonl()), 3)
        self.assertEqual(len(self.rows()), 2)
        self.assertFalse(os.path.exists(os.path.join(self.home, "archive")))
        self.assertIn("dry run", retention.summary_text(out))


class ArchiveTest(PruneCase):
    def test_old_daily_file_is_gzipped_and_removed(self):
        out = self.run_prune()
        self.assertEqual(out["files"], 1)
        self.assertFalse(os.path.exists(self.text(self.old)))
        gz = os.path.join(self.home, "archive", f"morse-{self.old:%Y-%m-%d}.txt.gz")
        self.assertTrue(os.path.exists(gz))
        with gzip.open(gz, "rt") as f:
            self.assertEqual(f.read(), "OLD LINE\n")

    def test_new_daily_file_and_foreign_files_stay(self):
        self.run_prune()
        self.assertTrue(os.path.exists(self.text(self.new)))
        self.assertTrue(os.path.exists(os.path.join(self.logs, "notes.txt")))

    def test_old_jsonl_lines_are_archived_and_rewritten(self):
        out = self.run_prune()
        self.assertEqual(out["jsonl_lines"], 1)
        kept = self.jsonl()
        self.assertEqual(len(kept), 2)
        self.assertIn('"new"', kept[0])
        self.assertEqual(kept[1], "not json at all")   # never dropped
        gz = os.path.join(self.home, "archive",
                          f"{retention.JSONL}.{self.cutoff}.gz")
        with gzip.open(gz, "rt") as f:
            self.assertIn('"old"', f.read())

    def test_old_sqlite_rows_are_deleted(self):
        out = self.run_prune()
        self.assertEqual(out["rows"], 1)
        self.assertEqual(self.rows(), [("new",)])

    def test_second_run_is_a_no_op(self):
        self.run_prune()
        again = self.run_prune()
        self.assertEqual((again["files"], again["jsonl_lines"], again["rows"]),
                         (0, 0, 0))

    def test_summary_names_the_action(self):
        text = retention.summary_text(self.run_prune())
        self.assertIn("archived", text)
        self.assertIn("files        1 archived", text)


class DeleteTest(PruneCase):
    def test_files_are_removed_not_archived(self):
        out = self.run_prune(delete=True)
        self.assertFalse(os.path.exists(self.text(self.old)))
        self.assertEqual(out["files"], 1)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, "archive",
                         f"morse-{self.old:%Y-%m-%d}.txt.gz")))
        self.assertIn("deleted", retention.summary_text(out))


class EmptyTest(unittest.TestCase):
    def test_an_empty_log_directory_is_fine(self):
        with tempfile.TemporaryDirectory() as home:
            logs = os.path.join(home, "logs")
            os.makedirs(logs)
            out = retention.prune(home, logs, 7)
            self.assertEqual((out["files"], out["jsonl_lines"], out["rows"]),
                             (0, 0, 0))

    def test_a_missing_directory_is_fine(self):
        with tempfile.TemporaryDirectory() as home:
            out = retention.prune(home, os.path.join(home, "absent"), 7)
            self.assertEqual(out["files"], 0)


if __name__ == "__main__":
    unittest.main()
