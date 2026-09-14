"""Antigravity integration checks using a pinned synthetic executable only."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from agy_switcher import APP_DIRECTORY, AUTH_OVERRIDES, SUPPORTED_SHA256, TOKEN_FILENAME


PROJECT = Path(__file__).resolve().parents[1]
FAKE_AGY = r'''#!/usr/bin/env python3
import json, os, pathlib, sqlite3, sys
argv = sys.argv[1:]
assert argv[1] == "--app_data_dir=antigravity-cli"
assert argv[2] == "--use_host_auth=false"
home = pathlib.Path(argv[0].split("=", 1)[1])
args = argv[3:]
with open(os.environ["FAKE_AGY_LOG"], "a") as log:
    log.write(json.dumps({"home": str(home), "args": args,
                          "env": {key: value for key, value in os.environ.items()
                                  if key in ("HOME", "SSH_CONNECTION", "AGY_ADC_AUTH", "AGY_CLI_DISABLE_AUTO_UPDATE")
                                  or value == "synthetic-auth-override"}}) + "\n")
auth = home / "antigravity-cli" / "antigravity-oauth-token"
if not args:
    if os.environ.get("FAKE_LOGIN_FAIL"):
        raise SystemExit(7)
    if not os.environ.get("FAKE_LOGIN_EMPTY"):
        auth.parent.mkdir(mode=0o700, exist_ok=True)
        auth.write_text(json.dumps({"token": {"access_token": "synthetic-private-" + home.name,
                                              "refresh_token": "synthetic-private-refresh"},
                                    "auth_method": "google", "id_token": "synthetic-private-id"}))
    raise SystemExit(0)
if args == ["--help"]:
    print("Usage of synthetic agy")
    raise SystemExit(0)
if args == ["-p", "refresh-fixture"]:
    value = json.loads(auth.read_text())
    value["token"]["refresh_token"] = "synthetic-private-refreshed"
    auth.write_text(json.dumps(value))
app = home / "antigravity-cli"
if args == ["history-fixture-list"]:
    with sqlite3.connect(app / "conversation_summaries.db") as db:
        print(json.dumps([row[0] for row in db.execute("SELECT conversation_id FROM conversation_summaries ORDER BY conversation_id")]))
    raise SystemExit(0)
if args[:1] == ["history-fixture-update-cache"]:
    workspace, conversation_id, timestamp = args[1:]
    cache = app / "cache"
    cache.mkdir(exist_ok=True)
    for name, value in (("last_conversations.json", {workspace: conversation_id}),
                        ("conversation_metadata.json", {"conversations": {conversation_id: {"last_modified_time": timestamp}}})):
        temporary = cache / (name + ".tmp")
        temporary.write_text(json.dumps(value))
        os.replace(temporary, cache / name)
print(json.dumps({"home": str(home), "args": args}))
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class AgySwitcherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = self.base / "agy-store"
        self.original = self.base / "original"
        self.source = self.original / APP_DIRECTORY / TOKEN_FILENAME
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps({
            "token": {"access_token": "synthetic-private-original", "refresh_token": "synthetic-private-refresh"},
            "auth_method": "google", "id_token": "synthetic-private-id",
        }))
        self.original_bytes = self.source.read_bytes()
        self.fake = self.base / "fake-agy"
        self.fake.write_text(FAKE_AGY)
        self.fake.chmod(0o700)
        self.log = self.base / "calls.jsonl"
        # Trust the fixture only in a temporary copy of the application. The
        # shipped compatibility check has no environment-variable bypass.
        self.app = self.base / "app"
        self.app.mkdir()
        for name in ("agy-switch", "codex_switcher.py", "codex_history.py", "local_history.py"):
            shutil.copy2(PROJECT / name, self.app / name)
        fixture_hash = hashlib.sha256(self.fake.read_bytes()).hexdigest()
        module = (PROJECT / "agy_switcher.py").read_text()
        self.assertEqual(module.count(SUPPORTED_SHA256), 1)
        (self.app / "agy_switcher.py").write_text(module.replace(SUPPORTED_SHA256, fixture_hash, 1))
        self.script = self.app / "agy-switch"
        self.env = dict(os.environ, AGY_SWITCHER_HOME=str(self.store), AGY_SWITCHER_AGY=str(self.fake),
                        FAKE_AGY_LOG=str(self.log), SSH_CONNECTION="synthetic-original-ssh",
                        AGY_ADC_AUTH="true", AGY_CLI_DISABLE_AUTO_UPDATE="false",
                        CODEX_SWITCHER_HOME=str(self.base / "codex-store"),
                        CLAUDE_SWITCHER_HOME=str(self.base / "claude-store"))
        self.env.update({key: "synthetic-auth-override" for key in AUTH_OVERRIDES})

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(self.script), *args], capture_output=True,
                                text=True, env=env or self.env)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("synthetic-private-", result.stdout + result.stderr)
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        return result

    def import_account(self, name):
        return self.invoke("add", name, "--import-current", "--source-home", str(self.original))

    def auth(self, name):
        return self.store / "accounts" / name / APP_DIRECTORY / TOKEN_FILENAME

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_accounts_and_refresh_remain_separate_from_original_and_other_tools(self):
        self.import_account("personal")
        self.invoke("add", "work")
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")
        self.invoke("run", "--", "-p", "refresh-fixture")
        self.invoke("use", "work")
        result = json.loads(self.invoke("run", "--", "-p", "a prompt with spaces").stdout)
        self.assertEqual(Path(result["home"]).name, "work")
        self.assertEqual(result["args"], ["-p", "a prompt with spaces"])
        once = json.loads(self.invoke("run", "--account", "personal", "--", "mcp", "list").stdout)
        self.assertEqual(Path(once["home"]).name, "personal")
        self.assertEqual(self.invoke("current").stdout.strip(), "work")
        self.invoke("use", "personal")
        self.assertEqual(json.loads(self.auth("personal").read_text())["token"]["refresh_token"],
                         "synthetic-private-refreshed")
        self.assertEqual(json.loads(self.auth("work").read_text())["token"]["refresh_token"],
                         "synthetic-private-refresh")
        self.assertFalse((self.base / "codex-store").exists())
        self.assertFalse((self.base / "claude-store").exists())

    def test_native_environment_clears_auth_overrides_without_replacing_home(self):
        self.invoke("add", "personal")
        self.invoke("run", "--", "-p", "example")
        for call in self.calls():
            env = call["env"]
            for key in AUTH_OVERRIDES:
                self.assertNotIn(key, env)
            self.assertEqual(env["HOME"], self.env["HOME"])
            self.assertEqual(env["SSH_CONNECTION"], "127.0.0.1 0 127.0.0.1 0")
            self.assertEqual(env["AGY_ADC_AUTH"], "false")
            self.assertEqual(env["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")
        self.assertEqual(self.env["SSH_CONNECTION"], "synthetic-original-ssh")

    def test_binary_update_fails_before_profile_creation_or_selection_changes(self):
        self.import_account("personal")
        self.import_account("work")
        self.fake.write_text(FAKE_AGY + "\n# synthetic update\n")
        self.assertIn("unverified", self.invoke("doctor", expected=1).stdout)
        for args in (("use", "work"), ("add", "new"), ("login", "personal"),
                     ("run",), ("shell-init", "zsh")):
            self.assertIn("not been verified", self.invoke(*args, expected=1).stderr)
        self.assertFalse((self.store / "accounts" / "new").exists())
        self.assertFalse(self.log.exists())
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")
        self.assertIn("work", self.invoke("list").stdout)

    def test_production_module_does_not_trust_fixture(self):
        result = subprocess.run([sys.executable, str(PROJECT / "agy-switch"), "add", "personal"],
                                capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("not been verified", result.stderr)
        self.assertFalse(self.store.exists())

    def test_failed_or_unfinished_login_recovers_without_premature_selection(self):
        self.invoke("add", "personal", env=dict(self.env, FAKE_LOGIN_FAIL="1"), expected=7)
        self.invoke("current", expected=1)
        self.invoke("login", "personal", env=dict(self.env, FAKE_LOGIN_EMPTY="1"), expected=1)
        self.invoke("current", expected=1)
        self.invoke("login", "personal")
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")

    def test_logout_or_bad_cache_rejects_only_affected_profile(self):
        self.import_account("personal")
        self.invoke("add", "work")
        self.auth("work").unlink()
        self.assertIn("work  (login needed)", self.invoke("list").stdout)
        self.invoke("use", "work", expected=1)
        self.invoke("run", "--account", "work", expected=1)
        self.invoke("run", "--account", "work", "--", "--help")
        self.auth("work").write_text('{"synthetic-private-invalid": true}')
        self.invoke("use", "work", expected=1)
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")

    def test_symlink_cache_and_app_directory_cannot_redirect_authentication(self):
        self.import_account("personal")
        auth = self.auth("personal")
        auth.unlink()
        auth.symlink_to(self.source)
        self.invoke("run", expected=1)
        self.invoke("login", "personal", expected=1)
        auth.unlink()
        auth.parent.rmdir()
        auth.parent.symlink_to(self.source.parent, target_is_directory=True)
        self.invoke("run", expected=1)
        self.invoke("login", "personal", expected=1)
        self.assertFalse(self.log.exists())

    def test_restricted_flags_and_daemons_are_blocked_but_prompt_values_pass(self):
        self.import_account("personal")
        for args in (("--gemini_dir=/shared",), ("-app_data_dir", "shared"),
                     ("--use_host_auth=true",), ("--host_bridge_url=example",),
                     ("--remote-control",), ("remote-control", "start"), ("update",),
                     ("--model", "example", "remote-control", "start"), ("install",)):
            self.invoke("run", "--", *args, expected=1)
        self.assertFalse(self.log.exists())
        for prompt in ("remote-control", "update", "install"):
            result = json.loads(self.invoke("run", "--", "-p", prompt).stdout)
            self.assertEqual(result["args"], ["-p", prompt])

    def test_owner_only_permissions_duplicate_add_and_malformed_import(self):
        self.import_account("personal")
        home = self.store / "accounts" / "personal"
        for path in (self.store, self.store / "accounts", home, self.auth("personal").parent):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.auth("personal"), self.store / "selected.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.invoke("add", "personal", expected=1)
        self.assertEqual(self.auth("personal").read_bytes(), self.original_bytes)
        bad = self.base / "bad-source" / APP_DIRECTORY / TOKEN_FILENAME
        bad.parent.mkdir(parents=True)
        bad.write_text("synthetic-private-not-json")
        self.invoke("add", "bad", "--import-current", "--source-home", str(bad.parent.parent), expected=1)
        self.assertFalse((self.store / "accounts" / "bad").exists())

    def test_missing_executable_unknown_account_and_invalid_names(self):
        self.invoke("add", "personal", env=dict(self.env, AGY_SWITCHER_AGY="/no/such/agy"), expected=1)
        self.assertFalse(self.store.exists())
        self.invoke("run", expected=1)
        self.assertIn("agy-switch add work", self.invoke("use", "work", expected=1).stderr)
        self.invoke("add", "../escape", expected=2)
        self.invoke("add", "invalid-source", "--source-home", str(self.original), expected=1)
        self.assertFalse((self.store / "accounts" / "invalid-source").exists())

    def test_shell_forwards_literal_args_and_preserves_exit_status(self):
        self.import_account("personal")
        self.invoke("run", "--", "-p", "example", env=dict(self.env, FAKE_EXIT="42"), expected=42)
        for shell in ("zsh", "bash"):
            if not shutil.which(shell):
                continue
            code = self.invoke("shell-init", shell).stdout
            result = subprocess.run([shell, "-c", code + "\nagy -p 'spaces; $(literal)'"],
                                    capture_output=True, text=True, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["args"], ["-p", "spaces; $(literal)"])


if __name__ == "__main__":
    unittest.main()
