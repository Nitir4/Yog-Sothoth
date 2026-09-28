"""Unified routing, read-only history, resume isolation, and terminal arguments."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import switcher_manager as manager
from codex_switcher import SwitcherError
import test_switcher
import test_claude_switcher
import test_agy_switcher

PROJECT = Path(__file__).resolve().parents[1]


class DesktopExecutableTest(unittest.TestCase):
    def test_user_installs_work_with_desktop_path_and_preserve_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / ".local" / "bin"
            local.mkdir(parents=True)
            path_bin = root / "path-bin"
            path_bin.mkdir()
            with patch("pathlib.Path.home", return_value=root), patch.dict(
                    os.environ, {"PATH": str(path_bin)}, clear=True):
                for tool in manager.TOOLS:
                    with self.subTest(tool=tool):
                        executable = local / tool
                        executable.write_text("#!/bin/sh\nexit 0\n")
                        executable.chmod(0o700)
                        native = manager.adapter(tool)
                        self.assertEqual(native.binary(), str(executable))
                        executable.chmod(0o600)
                        with self.assertRaises(SwitcherError):
                            native.binary()
                        executable.chmod(0o700)
                        path_executable = path_bin / tool
                        path_executable.write_text("#!/bin/sh\nexit 0\n")
                        path_executable.chmod(0o700)
                        self.assertEqual(native.binary(), str(path_executable))
                        override = f"{tool.upper()}_SWITCHER_{tool.upper()}"
                        with patch.dict(os.environ, {override: str(executable)}):
                            self.assertEqual(native.binary(), str(executable))
                        with patch.dict(os.environ, {override: str(root / "missing")}):
                            with self.assertRaises(SwitcherError):
                                native.binary()


def invoke_unified(case, *arguments, expected=0):
    env = dict(case.env)
    for tool in manager.TOOLS:
        env.setdefault(tool.upper() + "_SWITCHER_HOME", str(case.base / (tool + "-unused-store")))
    script = getattr(case, "app", PROJECT) / "switcher"
    result = subprocess.run([sys.executable, str(script), *arguments], env=env,
                            capture_output=True, text=True, timeout=15)
    case.assertEqual(result.returncode, expected, result.stderr + result.stdout)
    for secret in ("synthetic-status-secret", "synthetic-private-status", "synthetic-private-email", "synthetic-refresh"):
        case.assertNotIn(secret, result.stdout + result.stderr)
    return result


class UnifiedCodexTest(unittest.TestCase):
    setUp = test_switcher.SwitcherTest.setUp
    tearDown = test_switcher.SwitcherTest.tearDown

    def test_routes_selection_and_preserves_literal_arguments_and_exit(self):
        invoke_unified(self, "codex", "add", "work", "--import-current")
        invoke_unified(self, "codex", "add", "personal")
        invoke_unified(self, "codex", "use", "work")
        prompt = "literal $(touch /tmp/never-execute-switcher) `whoami` with spaces"
        result = invoke_unified(self, "codex", "run", "--account", "personal", "--", "exec", prompt)
        value = json.loads(result.stdout)
        self.assertEqual(Path(value["home"]).name, "personal")
        self.assertIn(prompt, value["args"])
        self.assertEqual(invoke_unified(self, "codex", "current").stdout.strip(), "work")
        self.env["FAKE_EXIT"] = "7"
        invoke_unified(self, "codex", "run", "--", "--version", expected=7)

    def test_shared_resume_uses_requested_account_and_saved_project(self):
        invoke_unified(self, "codex", "add", "work", "--import-current")
        invoke_unified(self, "codex", "add", "personal")
        home = self.store / "accounts" / "work"
        path = home / "sessions" / "test.jsonl"
        path.parent.mkdir()
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "session-1", "cwd": str(self.base)}}) + "\n")
        (home / "session_index.jsonl").write_text(json.dumps({"id": "session-1", "thread_name": "Saved title"}) + "\n")
        invoke_unified(self, "resume", "codex", "session-1", "--account", "personal", expected=1)
        invoke_unified(self, "codex", "history", "share", "--source-home", str(self.source))
        value = json.loads(invoke_unified(self, "resume", "codex", "session-1", "--account", "personal").stdout)
        self.assertEqual(Path(value["home"]).name, "personal")
        self.assertEqual(value["args"][-2:], ["resume", "session-1"])
        self.assertEqual(invoke_unified(self, "codex", "current").stdout.strip(), "work")
        rows = json.loads(invoke_unified(self, "history", "--tool", "codex", "--json").stdout)
        self.assertEqual(len(rows["conversations"]), 1)
        self.assertTrue(rows["conversations"][0]["shared"])

    def test_status_does_not_print_tokens_or_create_missing_stores(self):
        invoke_unified(self, "codex", "add", "work", "--import-current")
        status = json.loads(invoke_unified(self, "status", "--json").stdout)
        self.assertEqual(status[0]["accounts"][0]["login"], "Login cached")
        self.assertNotIn("access_token", json.dumps(status))
        self.assertFalse((self.base / "claude-unused-store").exists())
        self.assertFalse((self.base / "agy-unused-store").exists())

    def test_missing_conversation_and_bad_limit_report_clean_errors(self):
        result = invoke_unified(self, "resume", "codex", "missing", "--account", "work", expected=1)
        self.assertIn("Conversation not found", result.stderr)
        invoke_unified(self, "history", "--limit", "0", expected=1)
        self.assertNotIn("Traceback", result.stderr)


class UnifiedClaudeTest(unittest.TestCase):
    setUp = test_claude_switcher.ClaudeSwitcherTest.setUp
    tearDown = test_claude_switcher.ClaudeSwitcherTest.tearDown

    def test_routes_login_and_native_resume(self):
        invoke_unified(self, "claude", "add", "work")
        home = self.store / "accounts" / "work"
        path = home / "projects" / "project" / "claude-session.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "user", "sessionId": "claude-session", "cwd": str(self.base),
                                    "message": {"content": "Review authentication"}}) + "\n")
        value = json.loads(invoke_unified(self, "resume", "claude", "claude-session", "--account", "work").stdout)
        self.assertEqual(value["args"], ["--resume", "claude-session"])
        listing = json.loads(invoke_unified(self, "history", "--query", "authentication", "--json").stdout)
        self.assertEqual(listing["conversations"][0]["title"], "Review authentication")


@unittest.skipUnless(sys.platform.startswith("linux"), "Antigravity adapter supports Linux only")
class UnifiedAgyTest(unittest.TestCase):
    tearDown = test_agy_switcher.AgySwitcherTest.tearDown

    def setUp(self):
        test_agy_switcher.AgySwitcherTest.setUp(self)
        for name in ("switcher", "switcher_cli.py", "switcher_manager.py", "claude_switcher.py", "switcher_setup.py"):
            shutil.copy2(PROJECT / name, self.app / name)

    def test_repo_local_fallback_and_explicit_override(self):
        local = self.app / ".agy-switcher" / "accounts" / "work"
        local.mkdir(parents=True)
        env = dict(self.env)
        env.pop("AGY_SWITCHER_HOME")
        # HOME affects only default-store discovery, never the adapter's launched HOME.
        env["HOME"] = str(self.base / "empty-home")
        result = subprocess.run([sys.executable, str(self.app / "switcher"), "status", "--json"],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)[2]["root"], str(local.parents[1]))
        result = invoke_unified(self, "status", "--json")
        self.assertEqual(json.loads(result.stdout)[2]["root"], str(self.store))

    def test_routes_agy_and_resumes_with_native_conversation_flag(self):
        invoke_unified(self, "agy", "add", "work", "--import-current", "--source-home", str(self.original))
        root = self.store / "accounts" / "work" / "antigravity-cli"
        (root / "conversations").mkdir()
        with closing(sqlite3.connect(root / "conversations" / "agy-session.db")) as db, db:
            db.execute("CREATE TABLE steps (id TEXT)")
        (root / "cache").mkdir()
        (root / "cache" / "last_conversations.json").write_text(json.dumps({str(self.base): "agy-session"}))
        value = json.loads(invoke_unified(self, "resume", "agy", "agy-session", "--account", "work").stdout)
        self.assertEqual(value["args"], ["--conversation", "agy-session"])
        self.assertEqual(Path(value["home"]).name, "work")


class ManagerDataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {tool.upper() + "_SWITCHER_HOME": str(self.base / tool)
                                          for tool in manager.TOOLS})
        self.env.start()
        self.snapshot = []
        for tool, title in manager.TOOLS.items():
            root = self.base / tool
            for name in ("personal", "work"):
                (root / "accounts" / name).mkdir(parents=True)
            self.snapshot.append(manager.ToolState(tool, title, str(root), accounts=[manager.Account("personal", ""), manager.Account("work", "")]))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_reads_committed_wal_without_modifying_original_database(self):
        path = self.base / "codex/accounts/work/state_5.sqlite"
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at INTEGER)")
            db.execute("INSERT INTO threads VALUES ('wal-session', 'Live WAL title', '/project', 1700000000)")
            db.commit()
            before = {p.name: p.read_bytes() for p in path.parent.glob("state_5.sqlite*")}
            records, errors = manager.conversations(self.snapshot)
            self.assertFalse(errors)
            self.assertEqual(records[0].title, "Live WAL title")
            self.assertEqual(before, {p.name: p.read_bytes() for p in path.parent.glob("state_5.sqlite*")})
        finally:
            db.close()

    def test_all_tools_filters_indexless_claude_and_agy_uri(self):
        root = self.base / "claude/accounts/work/projects/demo"
        root.mkdir(parents=True)
        (root / "indexless.jsonl").write_text('{partial write\n' + json.dumps({
            "type": "user", "cwd": "/project api", "message": {"content": [{"type": "text", "text": "Fix auth"}]}}) + "\n")
        root = self.base / "agy/accounts/work/antigravity-cli"
        root.mkdir()
        with closing(sqlite3.connect(root / "conversation_summaries.db")) as db, db:
            db.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, workspace_uris TEXT, last_modified_time TEXT)")
            db.execute("INSERT INTO conversation_summaries VALUES (?,?,?,?)", ("agy-id", "Dashboard", '["file:///project%20api"]', "2026-10-05T12:00:00Z"))
        records, errors = manager.conversations(self.snapshot, project="PROJECT API")
        self.assertFalse(errors)
        self.assertEqual({r.tool for r in records}, {"claude", "agy"})
        records, _ = manager.conversations(self.snapshot, tool="claude", query="AUTH")
        self.assertEqual([r.title for r in records], ["Fix auth"])

    def test_shared_dedup_does_not_mark_other_private_records_shared(self):
        state = self.snapshot[0]
        state.shared = True
        for folder in (self.base / "codex/history", self.base / "codex/accounts/work"):
            folder.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(folder / "state_5.sqlite")) as db, db:
                db.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at INTEGER)")
                db.execute("INSERT INTO threads VALUES ('duplicate', 'Merged', '/project', 1)")
                if folder.name == "work":
                    db.execute("INSERT INTO threads VALUES ('private-new', 'Still private', '/project', 2)")
        records, errors = manager.conversations(self.snapshot)
        self.assertFalse(errors)
        by_id = {r.id: r for r in records}
        self.assertTrue(by_id["duplicate"].shared)
        self.assertFalse(by_id["private-new"].shared)

    def test_corrupt_database_reports_partial_results(self):
        (self.base / "codex/accounts/work/state_5.sqlite").write_bytes(b"not a database")
        records, errors = manager.conversations(self.snapshot)
        self.assertEqual(records, [])
        self.assertIn("could not read", errors[0])

    def test_resume_is_tool_and_account_scoped(self):
        record = manager.Conversation("codex", "session", "Title", "/project", 1, ["work"])
        self.assertEqual(manager.launch_arguments("codex", "work", record)[-2:], ["resume", "session"])
        with self.assertRaisesRegex(SwitcherError, "Share"):
            manager.launch_arguments("codex", "personal", record)
        with self.assertRaisesRegex(SwitcherError, "conversation's tool"):
            manager.launch_arguments("claude", "work", record)
        with self.assertRaises(SwitcherError):
            manager.launch_arguments("codex", "../escape", record)
        record.shared = True
        self.assertIn("personal", manager.launch_arguments("codex", "personal", record))

    def test_terminal_arguments_preserve_shell_characters(self):
        with patch.dict(os.environ, {"SWITCHER_TERMINAL": "kitty --title 'My sessions' --"}):
            argv = manager.terminal_command(["codex", "run", "--", "exec", "$(touch ignored)"], self.base,
                                            lookup=lambda _: "/usr/bin/kitty")
        self.assertEqual(argv[:4], ["kitty", "--title", "My sessions", "--"])
        self.assertEqual(argv[-1], "$(touch ignored)")
        self.assertIn(str(self.base), argv)
        with patch.dict(os.environ, {"SWITCHER_TERMINAL": ""}), patch("switcher_manager.sys.platform", "linux"):
            with self.assertRaisesRegex(SwitcherError, "No supported terminal"):
                manager.terminal_command(["codex", "run"], self.base, lookup=lambda _: None)

    def test_symlink_credentials_and_history_marker_are_rejected(self):
        root = self.base / "codex"
        token = self.base / "token"
        token.write_text(json.dumps({"tokens": {"access_token": "private", "refresh_token": "private"}}))
        (root / "accounts/work/auth.json").symlink_to(token)
        state = manager.tool_state("codex")
        self.assertEqual(next(a for a in state.accounts if a.name == "work").login, "Sign in needed")
        (root / "history.json").symlink_to(token)
        self.assertIn("symlink history marker", manager.tool_state("codex").error)


if __name__ == "__main__":
    unittest.main()
