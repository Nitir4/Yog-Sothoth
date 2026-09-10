"""History migration and account switching, without network requests."""

import json
from pathlib import Path
import sqlite3
import unittest

import test_switcher


class HistoryTest(unittest.TestCase):
    setUp = test_switcher.SwitcherTest.setUp
    tearDown = test_switcher.SwitcherTest.tearDown
    invoke = test_switcher.SwitcherTest.invoke
    calls = test_switcher.SwitcherTest.calls

    def seed_history(self, home, thread_id, *, archived=False):
        folder = "archived_sessions" if archived else "sessions"
        path = home / folder / "2026" / (thread_id + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": thread_id}) + "\n")
        (home / "history.jsonl").write_text(json.dumps({"session_id": thread_id, "text": thread_id, "ts": 1}) + "\n")
        (home / "session_index.jsonl").write_text(json.dumps({"id": thread_id, "thread_name": thread_id}) + "\n")
        connection = sqlite3.connect(home / "state_5.sqlite")
        connection.executescript('''
            CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, checksum BLOB, success INTEGER, installed_on TEXT);
            INSERT INTO _sqlx_migrations VALUES (1, 'fixture', 1, 'varying-install-time');
            CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, archived INTEGER);
        ''')
        connection.execute("INSERT INTO threads VALUES (?,?,?)", (thread_id, str(path), archived))
        connection.commit()
        connection.close()
        connection = sqlite3.connect(home / "thread_history_1.sqlite")
        connection.executescript('''
            CREATE TABLE _sqlx_migrations (version INTEGER PRIMARY KEY, checksum BLOB, success INTEGER);
            INSERT INTO _sqlx_migrations VALUES (1, 'fixture', 1);
            CREATE TABLE thread_items (thread_id TEXT, item_id TEXT, item_json TEXT NOT NULL,
                                       PRIMARY KEY (thread_id, item_id));
        ''')
        connection.execute("INSERT INTO thread_items VALUES (?,?,?)", (thread_id, "item-1", '{"text":"saved message"}'))
        connection.commit()
        connection.close()
        return path

    def test_share_merges_original_and_accounts_and_preserves_logins(self):
        self.invoke("add", "personal", "--import-current")
        self.invoke("add", "work")
        homes = [self.source, self.store / "accounts" / "personal", self.store / "accounts" / "work"]
        auth_before = [(home / "auth.json").read_bytes() for home in homes]
        for home, thread_id in zip(homes, ("original", "personal", "work")):
            self.seed_history(home, thread_id, archived=thread_id == "work")
        self.assertIn("History: per account", self.invoke("history").stdout)
        self.invoke("history", "share")
        shared = self.store / "history"
        self.assertIn("History: shared", self.invoke("history").stdout)
        self.assertEqual(sum(1 for _ in shared.rglob("*.jsonl")), 5)
        with sqlite3.connect(shared / "state_5.sqlite") as db:
            rows = db.execute("SELECT id, rollout_path FROM threads").fetchall()
            self.assertEqual({row[0] for row in rows}, {"original", "personal", "work"})
            self.assertTrue(all(Path(row[1]).is_file() and str(shared) in row[1] for row in rows))
        with sqlite3.connect(shared / "thread_history_1.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM thread_items").fetchone()[0], 3)
        for home, auth in zip(homes, auth_before):
            self.assertEqual((home / "auth.json").read_bytes(), auth)
        self.assertFalse((self.source / "sessions").is_symlink())
        for home in homes[1:]:
            self.assertTrue((home / "sessions").is_symlink())
            self.assertTrue((home / ".history-backup" / "history.jsonl").is_file())
        before = (shared / "history.jsonl").read_bytes()
        self.invoke("history", "share")
        self.assertEqual((shared / "history.jsonl").read_bytes(), before)
        for name in ("personal", "work"):
            self.invoke("use", name)
            launch = json.loads(self.invoke("run", "--", "resume", "--last").stdout)
            self.assertEqual(Path(launch["home"]).name, name)
            self.assertIn("sqlite_home=" + json.dumps(str(shared)), launch["args"])
        # Writes made through the first account are immediately visible to the other.
        entry = json.dumps({"session_id": "new", "text": "after sharing", "ts": 2}) + "\n"
        with (homes[1] / "history.jsonl").open("a") as handle:
            handle.write(entry)
        self.assertTrue((homes[2] / "history.jsonl").read_text().endswith(entry))
        self.invoke("add", "new")
        self.invoke("run", "--account", "new", "--", "--version")
        self.assertEqual((self.store / "accounts" / "new" / "sessions").resolve(), shared / "sessions")
        self.invoke("run", "--account", "work", "--", "logout")
        self.assertEqual((shared / "history.jsonl").read_bytes(), before + entry.encode())

    def test_conflicting_transcripts_fail_without_moving_history(self):
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal"
        first = self.seed_history(self.source, "same")
        second = self.seed_history(home, "same")
        second.write_text('{"different":"conversation"}\n')
        self.invoke("history", "share", expected=1)
        self.assertFalse((self.store / "history.json").exists())
        self.assertFalse((self.store / "history").exists())
        self.assertTrue(first.is_file() and second.is_file())
        self.assertFalse((home / ".history-backup").exists())

    def test_wal_history_is_imported_and_different_schemas_fail_safely(self):
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal"
        self.seed_history(home, "personal")
        db = sqlite3.connect(home / "thread_history_1.sqlite")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO thread_items VALUES ('personal', 'in-wal', '{}')")
            db.commit()
            self.assertGreater(Path(str(home / "thread_history_1.sqlite") + "-wal").stat().st_size, 0)
            self.invoke("history", "share")
            with sqlite3.connect(self.store / "history" / "thread_history_1.sqlite") as shared:
                self.assertEqual(shared.execute("SELECT count(*) FROM thread_items").fetchone()[0], 2)
        finally:
            db.close()

    def test_incompatible_database_schemas_do_not_change_originals(self):
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal"
        self.seed_history(home, "personal")
        self.seed_history(self.source, "original")
        with sqlite3.connect(home / "state_5.sqlite") as db:
            db.execute("ALTER TABLE threads ADD COLUMN extra TEXT")
        self.invoke("history", "share", expected=1)
        self.assertFalse((self.store / "history.json").exists())
        self.assertFalse((home / "sessions").is_symlink())

    def test_unexpected_symlink_is_rejected(self):
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal"
        (home / "sessions").symlink_to(self.source, target_is_directory=True)
        self.invoke("history", "share", expected=1)
        self.assertFalse((self.store / "history.json").exists())

    def test_prompt_history_is_deduplicated_and_sorted_by_time(self):
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal"
        early = json.dumps({"session_id": "early", "text": "first", "ts": 1})
        late = json.dumps({"session_id": "late", "text": "second", "ts": 2})
        (self.source / "history.jsonl").write_text(late + "\n" + early + "\n")
        (home / "history.jsonl").write_text(early + "\n")
        self.invoke("history", "share")
        self.assertEqual((self.store / "history" / "history.jsonl").read_text(), early + "\n" + late + "\n")


if __name__ == "__main__":
    unittest.main()
