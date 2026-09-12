"""Cross-process Claude adapter checks using synthetic logins only."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from claude_switcher import AUTH_VARIABLES


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "claude-switch"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
home = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"home": str(home), "args": args,
                          "auth_env": [key for key in os.environ if key.startswith("ANTHROPIC_")
                                       or key in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
                                                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                                                  "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_ANTHROPIC_AWS")]}) + "\n")
auth = home / ".credentials.json"
if args == ["auth", "status"]:
    print("synthetic-private-status", file=sys.stderr)
    print(json.dumps({"loggedIn": auth.exists(),
                      "authMethod": os.environ.get("FAKE_AUTH_METHOD", "claude.ai") if auth.exists() else "none",
                      "configDirectory": os.environ.get("FAKE_REPORTED_HOME", str(home)),
                      "email": "synthetic-private-email@example.invalid"}))
    raise SystemExit(0 if auth.exists() else 1)
if args == ["auth", "login"]:
    if os.environ.get("FAKE_LOGIN_FAIL"):
        raise SystemExit(7)
    auth.write_text(json.dumps({"claudeAiOauth": {"accessToken": "synthetic-access-" + home.name,
                                               "refreshToken": "synthetic-refresh"}}))
    raise SystemExit(0)
if args == ["auth", "logout"]:
    auth.unlink(missing_ok=True)
    raise SystemExit(0)
if "refresh-fixture" in args:
    value = json.loads(auth.read_text())
    value["claudeAiOauth"]["refreshToken"] = "synthetic-refreshed"
    auth.write_text(json.dumps(value))
if args == ["history-fixture-list"]:
    print(json.dumps(sorted(path.stem for path in (home / "projects").rglob("*.jsonl"))))
    raise SystemExit(0)
if args[:1] == ["history-fixture-create"]:
    path = home / "projects" / "same-project" / (args[1] + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sessionId": args[1], "message": "new conversation"}) + "\n")
print(json.dumps({"home": str(home), "args": args}))
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class ClaudeSwitcherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = self.base / "claude-store"
        self.original = self.base / "original"
        self.original.mkdir()
        self.original.joinpath(".credentials.json").write_text("synthetic-original")
        self.original.joinpath("settings.json").write_text('{"apiKeyHelper":"synthetic-helper"}')
        self.fake = self.base / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE)
        self.fake.chmod(0o700)
        self.log = self.base / "calls.jsonl"
        self.env = dict(os.environ, CLAUDE_SWITCHER_HOME=str(self.store),
                        CLAUDE_SWITCHER_CLAUDE=str(self.fake), CLAUDE_CONFIG_DIR=str(self.original),
                        FAKE_CLAUDE_LOG=str(self.log), CODEX_SWITCHER_HOME=str(self.base / "codex-store"))
        self.env.update({key: "synthetic-auth-override" for key in AUTH_VARIABLES})

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                                text=True, env=env or self.env)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn("synthetic-private-", result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_saved_accounts_switch_independently_of_codex_and_original_login(self):
        self.invoke("add", "personal")
        self.invoke("add", "work")
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")
        self.invoke("use", "work")
        result = json.loads(self.invoke("run", "--", "-p", "a prompt with spaces").stdout)
        self.assertEqual(Path(result["home"]).name, "work")
        self.assertEqual(result["args"], ["-p", "a prompt with spaces"])
        once = json.loads(self.invoke("run", "--account", "personal", "--", "--version").stdout)
        self.assertEqual(Path(once["home"]).name, "personal")
        self.assertEqual(self.invoke("current").stdout.strip(), "work")
        self.assertEqual(self.original.joinpath(".credentials.json").read_text(), "synthetic-original")
        self.assertFalse((self.base / "codex-store").exists())
        self.assertFalse((self.store / "accounts" / "work" / "settings.json").exists())
        for call in self.calls():
            self.assertEqual(call["auth_env"], [])

    def test_token_refresh_is_not_rolled_back_by_switching(self):
        self.invoke("add", "personal")
        self.invoke("add", "work")
        self.invoke("run", "--", "refresh-fixture")
        self.invoke("use", "work")
        self.invoke("use", "personal")
        auth = json.loads((self.store / "accounts" / "personal" / ".credentials.json").read_text())
        self.assertEqual(auth["claudeAiOauth"]["refreshToken"], "synthetic-refreshed")

    def test_failed_initial_login_recovers_and_selects_only_after_success(self):
        self.invoke("add", "personal", env=dict(self.env, FAKE_LOGIN_FAIL="1"), expected=7)
        self.invoke("current", expected=1)
        self.invoke("login", "personal")
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")

    def test_api_credentials_and_wrong_home_are_not_mistaken_for_selected_account(self):
        self.invoke("add", "personal")
        self.invoke("add", "work")
        for changed in ({"FAKE_AUTH_METHOD": "api_key"}, {"FAKE_AUTH_METHOD": "third_party"},
                        {"FAKE_REPORTED_HOME": str(self.original)}):
            env = dict(self.env, **changed)
            self.invoke("use", "work", env=env, expected=1)
            self.invoke("run", "--", "-p", "example", env=env, expected=1)
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")

    def test_logout_only_affects_selected_profile_and_list_hides_status_details(self):
        self.invoke("add", "personal")
        self.invoke("add", "work")
        self.invoke("run", "--account", "work", "--", "auth", "logout")
        listing = self.invoke("list").stdout
        self.assertIn("* personal  (subscription login cached)", listing)
        self.assertIn("work  (subscription login needed)", listing)
        self.invoke("run", "--account", "work", "--", "-p", "example", expected=1)
        self.invoke("run", "--account", "work", "--", "auth", "login")
        self.invoke("use", "work")

    def test_private_permissions_and_duplicate_add(self):
        self.invoke("add", "personal")
        home = self.store / "accounts" / "personal"
        auth = home / ".credentials.json"
        before = auth.read_bytes()
        for path in (self.store, self.store / "accounts", home):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (auth, self.store / "selected.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.invoke("add", "personal", expected=1)
        self.assertEqual(auth.read_bytes(), before)

    def test_missing_executable_and_unknown_accounts_report_useful_errors(self):
        missing = dict(self.env, CLAUDE_SWITCHER_CLAUDE="/no/such/claude")
        self.invoke("add", "personal", env=missing, expected=1)
        self.assertFalse(self.store.exists())
        self.invoke("run", expected=1)
        self.assertIn("claude-switch add work", self.invoke("use", "work", expected=1).stderr)
        self.invoke("add", "../escape", expected=2)

    def test_shell_forwarding_and_exit_codes(self):
        self.invoke("add", "personal")
        self.invoke("run", "--", "-p", "example", env=dict(self.env, FAKE_EXIT="42"), expected=42)
        for shell in ("zsh", "bash"):
            if not shutil.which(shell):
                continue
            code = self.invoke("shell-init", shell).stdout
            result = subprocess.run([shell, "-c", code + "\nclaude -p 'spaces; $(literal)'"],
                                    capture_output=True, text=True, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["args"], ["-p", "spaces; $(literal)"])


if __name__ == "__main__":
    unittest.main()
