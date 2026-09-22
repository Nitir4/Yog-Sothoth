"""Cross-account history and migration for Claude Code and Antigravity."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest

import test_agy_switcher
import test_claude_switcher


class ClaudeHistoryTest(unittest.TestCase):
    setUp = test_claude_switcher.ClaudeSwitcherTest.setUp
    tearDown = test_claude_switcher.ClaudeSwitcherTest.tearDown
    invoke = test_claude_switcher.ClaudeSwitcherTest.invoke

    def seed(self, home, session_id, timestamp):
        project = home / "projects" / "same-project"
        project.mkdir(parents=True)
        transcript = project / (session_id + ".jsonl")
        transcript.write_text(json.dumps({"sessionId": session_id, "message": "saved conversation"}) + "\n")
        (project / "sessions-index.json").write_text(json.dumps({"version": 1, "entries": [
            {"sessionId": session_id, "fullPath": str(transcript), "modified": "2026-10-05T10:00:00Z"}]}))
        (home / "history.jsonl").write_text(json.dumps({"display": session_id, "timestamp": timestamp}) + "\n")
        checkpoints = home / "file-history" / session_id
        checkpoints.mkdir(parents=True)
        (checkpoints / "snapshot").write_text("before edit")

    def test_migration_shares_existing_and_future_conversations_and_keeps_auth_separate(self):
        self.invoke("add", "personal")
        self.invoke("add", "work")
        homes = [self.original, self.store / "accounts" / "personal", self.store / "accounts" / "work"]
        before = [(home / ".credentials.json").read_bytes() for home in homes]
        settings = (self.original / "settings.json").read_bytes()
        for home, name, timestamp in zip(homes, ("original", "personal", "work"), (3, 1, 2)):
            self.seed(home, name, timestamp)
        self.assertIn("History: per account", self.invoke("history").stdout)
        self.invoke("history", "share", "--source-home", str(self.original))
        self.assertIn("History: shared", self.invoke("history").stdout)
        shared = self.store / "history"
        self.assertEqual([row["timestamp"] for row in map(json.loads, (shared / "history.jsonl").read_text().splitlines())], [1, 2, 3])
        index = json.loads((shared / "projects" / "same-project" / "sessions-index.json").read_text())
        self.assertEqual({entry["sessionId"] for entry in index["entries"]}, {"original", "personal", "work"})
        self.assertTrue(all(str(shared) in entry["fullPath"] and Path(entry["fullPath"]).is_file() for entry in index["entries"]))
        for home, original_auth in zip(homes, before):
            self.assertEqual((home / ".credentials.json").read_bytes(), original_auth)
        self.assertEqual((self.original / "settings.json").read_bytes(), settings)
        self.assertFalse((self.original / "projects").is_symlink())
        for home in homes[1:]:
            self.assertTrue((home / ".history-backup" / "projects").is_dir())
            self.assertTrue((home / "file-history" / "original" / "snapshot").is_file())
        for account in ("personal", "work"):
            self.invoke("use", account)
            self.assertEqual(json.loads(self.invoke("run", "--", "history-fixture-list").stdout), ["original", "personal", "work"])
        self.invoke("run", "--account", "personal", "--", "history-fixture-create", "new-session")
        self.assertIn("new-session", json.loads(self.invoke("run", "--account", "work", "--", "history-fixture-list").stdout))
        self.invoke("history", "share")
        self.invoke("run", "--account", "work", "--", "auth", "logout")
        self.assertEqual((homes[1] / ".credentials.json").read_bytes(), before[1])
        self.assertTrue((shared / "projects" / "same-project" / "new-session.jsonl").is_file())
        self.invoke("add", "new")
        self.assertIn("new-session", json.loads(self.invoke("run", "--account", "new", "--", "history-fixture-list").stdout))

    def test_conflicting_transcripts_abort_without_publishing_or_moving_files(self):
        self.invoke("add", "personal")
        home = self.store / "accounts" / "personal"
        self.seed(home, "same", 1)
        self.seed(self.original, "same", 1)
        path = home / "projects" / "same-project" / "same.jsonl"
        path.write_text('{"message":"different saved conversation"}\n')
        self.invoke("history", "share", expected=1)
        self.assertFalse((self.store / "history.json").exists())
        self.assertFalse((home / "projects").is_symlink())
        self.assertFalse((home / ".history-backup").exists())

    def test_symlink_history_is_rejected(self):
        self.invoke("add", "personal")
        home = self.store / "accounts" / "personal"
        (home / "projects").symlink_to(self.original, target_is_directory=True)
        self.invoke("history", "share", expected=1)
        self.assertFalse((self.store / "history.json").exists())

    def test_checkpoint_files_with_database_extensions_are_preserved_as_files(self):
        self.invoke("add", "personal")
        home = self.store / "accounts" / "personal"
        self.seed(home, "personal", 1)
        checkpoint = home / "file-history" / "personal" / "uploaded.db"
        checkpoint.write_bytes(b"opaque saved file contents")
        self.invoke("history", "share")
        self.assertEqual((self.store / "history" / "file-history" / "personal" / "uploaded.db").read_bytes(), b"opaque saved file contents")


@unittest.skipUnless(sys.platform.startswith("linux"), "Antigravity fingerprints cover Linux builds")
class AgyHistoryTest(unittest.TestCase):
    setUp = test_agy_switcher.AgySwitcherTest.setUp
    tearDown = test_agy_switcher.AgySwitcherTest.tearDown
    invoke = test_agy_switcher.AgySwitcherTest.invoke
    import_account = test_agy_switcher.AgySwitcherTest.import_account
    auth = test_agy_switcher.AgySwitcherTest.auth

    def seed(self, root, conversation_id, timestamp):
        home = root / "antigravity-cli"
        conversation = home / "conversations" / (conversation_id + ".db")
        conversation.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(conversation)) as db, db:
            db.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, payload TEXT)")
            db.execute("INSERT INTO steps VALUES (0, 'saved message')")
        brain = home / "brain" / conversation_id
        brain.mkdir(parents=True)
        (brain / "artifact.md").write_text("saved artifact")
        with closing(sqlite3.connect(home / "conversation_summaries.db")) as db, db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, last_modified_time TEXT, app_data_dir TEXT)")
            db.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?)", (conversation_id, timestamp, str(home)))
        cache = home / "cache"
        cache.mkdir(mode=0o700)
        (cache / "last_conversations.json").write_text(json.dumps({"/same/workspace": conversation_id}))
        (cache / "conversation_metadata.json").write_text(json.dumps({"conversations": {
            conversation_id: {"last_modified_time": timestamp, "summary": {"app_data_dir": str(home)}}}}))
        (cache / "default_project_id.txt").write_text("account-project-" + root.name)
        (cache / "onboarding.json").write_text('{"account-specific":true}')
        (home / "history.jsonl").write_text(json.dumps({"display": conversation_id, "timestamp": 1}) + "\n")

    def migrate(self):
        self.import_account("personal")
        self.import_account("work")
        roots = [self.original, self.store / "accounts" / "personal", self.store / "accounts" / "work"]
        for root, name, time in zip(roots, ("original", "personal", "work"), ("2026-10-05T09:00:00Z", "2026-10-05T10:00:00Z", "2026-10-05T11:00:00Z")):
            self.seed(root, name, time)
        self.invoke("history", "share", "--source-home", str(self.original))
        return roots

    def test_shared_databases_artifacts_prompt_history_and_isolated_project_cache(self):
        roots = self.migrate()
        shared = self.store / "history"
        for name in ("personal", "work"):
            self.invoke("use", name)
            self.assertEqual(json.loads(self.invoke("run", "--", "history-fixture-list").stdout), ["original", "personal", "work"])
        for root in roots[1:]:
            home = root / "antigravity-cli"
            self.assertTrue((home / "brain" / "original" / "artifact.md").is_file())
            self.assertTrue((home / ".history-backup" / "cache" / "last_conversations.json").is_file())
            self.assertEqual((home / "cache" / "default_project_id.txt").read_text(), "account-project-" + root.name)
            self.assertEqual((home / "cache" / "onboarding.json").read_text(), '{"account-specific":true}')
            self.assertFalse((home / "cache").is_symlink())
        with closing(sqlite3.connect(shared / "conversation_summaries.db")) as db, db:
            self.assertEqual({row[0] for row in db.execute("SELECT app_data_dir FROM conversation_summaries")}, {str(shared)})
        self.assertEqual(json.loads((shared / "cache" / "last_conversations.json").read_text())["/same/workspace"], "work")
        before = self.auth("work").read_bytes()
        self.invoke("run", "--account", "personal", "--", "-p", "refresh-fixture")
        self.assertEqual(self.auth("work").read_bytes(), before)
        self.invoke("history", "share")
        self.invoke("add", "new")
        self.assertEqual(json.loads(self.invoke("run", "--account", "new", "--", "history-fixture-list").stdout), ["original", "personal", "work"])

    def test_atomic_native_cache_updates_are_visible_after_switching_accounts(self):
        self.migrate()
        self.invoke("run", "--account", "personal", "--", "history-fixture-update-cache", "/same/workspace", "personal", "2026-10-05T12:00:00Z")
        self.invoke("use", "work")
        self.invoke("run", "--", "history-fixture-list")
        cache = self.store / "accounts" / "work" / "antigravity-cli" / "cache"
        self.assertEqual(json.loads((cache / "last_conversations.json").read_text())["/same/workspace"], "personal")
        self.assertEqual(json.loads((cache / "conversation_metadata.json").read_text())["conversations"]["personal"]["last_modified_time"], "2026-10-05T12:00:00Z")

    def test_conversation_wal_messages_survive_migration(self):
        self.import_account("personal")
        root = self.store / "accounts" / "personal"
        self.seed(root, "personal", "2026-10-05T10:00:00Z")
        connection = sqlite3.connect(root / "antigravity-cli" / "conversations" / "personal.db")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("INSERT INTO steps VALUES (1, 'message in WAL')")
            connection.commit()
            self.invoke("history", "share", "--source-home", str(self.original))
            with closing(sqlite3.connect(self.store / "history" / "conversations" / "personal.db")) as db, db:
                self.assertEqual(db.execute("SELECT count(*) FROM steps").fetchone()[0], 2)
        finally:
            connection.close()

    def test_incompatible_summary_schemas_abort_before_history_is_moved(self):
        self.import_account("personal")
        root = self.store / "accounts" / "personal"
        self.seed(root, "personal", "2026-10-05T10:00:00Z")
        self.seed(self.original, "original", "2026-10-05T09:00:00Z")
        with closing(sqlite3.connect(root / "antigravity-cli" / "conversation_summaries.db")) as db, db:
            db.execute("ALTER TABLE conversation_summaries ADD COLUMN extra TEXT")
        self.invoke("history", "share", "--source-home", str(self.original), expected=1)
        self.assertFalse((self.store / "history.json").exists())
        self.assertFalse((root / "antigravity-cli" / "conversations").is_symlink())

    def test_unused_older_summary_schemas_do_not_block_migration(self):
        self.import_account("personal")
        self.import_account("work")
        self.seed(self.original, "original", "2026-10-05T09:00:00Z")
        home = self.store / "accounts" / "work" / "antigravity-cli"
        with closing(sqlite3.connect(home / "conversation_summaries.db")) as db, db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)")
        self.invoke("history", "share", "--source-home", str(self.original))
        self.assertEqual(json.loads(self.invoke("run", "--account", "work", "--", "history-fixture-list").stdout), ["original"])


if __name__ == "__main__":
    unittest.main()
