"""Exercise the wrapper across processes without real accounts or network calls."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "codex-switch"

FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
if args == ["--help"]:
    print("Usage: codex [OPTIONS]\n--no-daemon")
    raise SystemExit(0)
home = pathlib.Path(os.environ["CODEX_HOME"])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps({"home": str(home), "args": args,
                          "api_key": "OPENAI_API_KEY" in os.environ,
                          "access_token": "CODEX_ACCESS_TOKEN" in os.environ,
                          "sqlite_env": "CODEX_SQLITE_HOME" in os.environ}) + "\n")
auth = home / "auth.json"
if "login" in args:
    if "status" in args:
        # Deliberately emit a secret; the wrapper must suppress status output.
        print("synthetic-status-secret", file=sys.stderr)
        raise SystemExit(0 if auth.exists() else 1)
    if os.environ.get("FAKE_LOGIN_FAIL"):
        raise SystemExit(7)
    auth.write_text(json.dumps({"tokens": {"access_token": "synthetic-access-" + home.name,
                                          "refresh_token": "synthetic-refresh"}}))
    raise SystemExit(0)
if "logout" in args:
    auth.unlink(missing_ok=True)
    raise SystemExit(0)
if "refresh-fixture" in args:
    value = json.loads(auth.read_text())
    value["tokens"]["refresh_token"] = "synthetic-refreshed"
    auth.write_text(json.dumps(value))
print(json.dumps({"home": str(home), "args": args}))
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class SwitcherTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.store = self.base / "store"
        self.source = self.base / "source"
        self.source.mkdir()
        self.source.joinpath("config.toml").write_text('model = "test-model"\nsqlite_home = "/old/state"\n')
        self.source.joinpath("work.config.toml").write_text('model = "profile-model"\n')
        self.source.joinpath("auth.json").write_text(json.dumps({"tokens": {
            "access_token": "synthetic-original", "refresh_token": "synthetic-original-refresh"}}))
        self.fake = self.base / "fake-codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o700)
        self.log = self.base / "calls.jsonl"
        self.env = dict(os.environ, CODEX_SWITCHER_HOME=str(self.store),
                        CODEX_SWITCHER_CODEX=str(self.fake), CODEX_HOME=str(self.source),
                        FAKE_LOG=str(self.log), OPENAI_API_KEY="synthetic-key",
                        CODEX_ACCESS_TOKEN="synthetic-token", CODEX_SQLITE_HOME="/old/env/state")

    def tearDown(self):
        self.temporary.cleanup()

    def invoke(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(SCRIPT), *args], env=env or self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        self.assertNotIn("synthetic-status-secret", result.stdout + result.stderr)
        return result

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_switching_isolated_accounts_preserves_source_and_running_choice(self):
        before = self.source.joinpath("auth.json").read_bytes()
        self.invoke("add", "personal", "--import-current")
        self.invoke("add", "work")
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")
        self.invoke("use", "work")
        launch = json.loads(self.invoke("run", "--", "exec", "a prompt with spaces", "--json").stdout)
        self.assertEqual(Path(launch["home"]).name, "work")
        self.assertIn("a prompt with spaces", launch["args"])
        self.assertIn("--no-daemon", launch["args"])
        self.invoke("run", "--account", "personal", "--", "--version")
        self.assertEqual(self.invoke("current").stdout.strip(), "work")
        self.assertEqual(self.source.joinpath("auth.json").read_bytes(), before)
        for call in self.calls():
            self.assertFalse(call["api_key"])
            self.assertFalse(call["access_token"])
            self.assertFalse(call["sqlite_env"])
            self.assertIn('cli_auth_credentials_store="file"', call["args"])
            self.assertIn("sqlite_home=" + json.dumps(call["home"]), call["args"])

    def test_settings_copied_and_storage_private(self):
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal"
        for name in ("config.toml", "work.config.toml"):
            self.assertEqual((home / name).read_bytes(), (self.source / name).read_bytes())
        for path in (self.store, self.store / "accounts", home):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (home / "auth.json", home / "config.toml", self.store / "selected.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.invoke("add", "clean", "--no-copy-config")
        self.assertFalse((self.store / "accounts" / "clean" / "config.toml").exists())

    def test_token_refresh_stays_with_its_account(self):
        self.invoke("add", "personal", "--import-current")
        self.invoke("add", "work")
        work_auth = self.store / "accounts" / "work" / "auth.json"
        work_before = work_auth.read_bytes()
        self.invoke("run", "--", "refresh-fixture")
        self.invoke("use", "work")
        self.invoke("use", "personal")
        personal = json.loads((self.store / "accounts" / "personal" / "auth.json").read_text())
        self.assertEqual(personal["tokens"]["refresh_token"], "synthetic-refreshed")
        self.assertEqual(work_auth.read_bytes(), work_before)

    def test_failed_login_can_retry_without_losing_selection(self):
        self.invoke("add", "personal", "--import-current")
        failure_env = dict(self.env, FAKE_LOGIN_FAIL="1")
        self.invoke("add", "work", env=failure_env, expected=7)
        self.invoke("use", "work", expected=1)
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")
        self.invoke("login", "work", "--device-auth")
        self.assertTrue(any("--device-auth" in call["args"] for call in self.calls()))
        self.invoke("use", "work")

    def test_first_failed_login_selects_account_when_retried(self):
        self.invoke("add", "personal", env=dict(self.env, FAKE_LOGIN_FAIL="1"), expected=7)
        self.invoke("current", expected=1)
        self.invoke("login", "personal")
        self.assertEqual(self.invoke("current").stdout.strip(), "personal")

    def test_no_selection_unknown_accounts_and_duplicate_add(self):
        self.invoke("run", expected=1)
        self.invoke("use", "missing", expected=1)
        self.invoke("add", "personal", "--import-current")
        home = self.store / "accounts" / "personal" / "auth.json"
        before = home.read_bytes()
        self.invoke("add", "personal", expected=1)
        self.assertEqual(home.read_bytes(), before)

    def test_account_names_cannot_escape_storage(self):
        for name in ("../escape", "/tmp/escape", ".", "a/b", "a b", "x" * 49):
            self.invoke("add", name, expected=2)
        self.assertFalse(self.store.exists())

    def test_missing_or_invalid_import_does_not_create_account_or_leak_data(self):
        auth = self.source / "auth.json"
        for value in ('{"secret":"synthetic-private', '[]', '{"OPENAI_API_KEY":"synthetic-private"}'):
            auth.write_text(value)
            result = self.invoke("add", "personal", "--import-current", expected=1)
            self.assertNotIn("synthetic-private", result.stderr)
            self.assertFalse((self.store / "accounts" / "personal").exists())
        auth.unlink()
        self.invoke("add", "personal", "--import-current", expected=1)

    def test_storage_symlinks_and_insecure_root_rejected(self):
        destination = self.base / "other"
        destination.mkdir()
        self.store.symlink_to(destination)
        self.invoke("add", "personal", expected=1)
        self.assertEqual(list(destination.iterdir()), [])
        self.store.unlink()
        self.store.mkdir(mode=0o755)
        self.store.chmod(0o755)
        self.invoke("add", "personal", expected=1)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o755)

    def test_remote_and_storage_overrides_rejected(self):
        self.invoke("add", "personal", "--import-current")
        for forwarded in (("--remote", "ws://example"), ("--remote=ws://example",),
                          ("-c", "sqlite_home='/other'"),
                          ("--config=cli_auth_credentials_store='keyring'",)):
            self.invoke("run", "--", *forwarded, expected=1)
        self.invoke("run", "--", "-c", "model='example'", "--", "--remote=prompt-text")

    def test_list_never_displays_credentials_and_logout_stays_isolated(self):
        self.invoke("add", "personal", "--import-current")
        self.invoke("add", "work")
        self.invoke("run", "--account", "work", "--", "logout")
        listing = self.invoke("list").stdout
        self.assertIn("* personal  (login cached)", listing)
        self.assertIn("work  (login needed)", listing)
        self.assertNotIn("synthetic-", listing)
        self.assertTrue((self.source / "auth.json").exists())

    def test_child_exit_status_propagates(self):
        self.invoke("add", "personal", "--import-current")
        self.invoke("run", "--", "exec", "example", env=dict(self.env, FAKE_EXIT="42"), expected=42)

    def test_shell_function_forwards_arguments_and_captures_real_binary(self):
        self.invoke("add", "personal", "--import-current")
        for shell in ("zsh", "bash"):
            if not shutil.which(shell):
                continue
            code = self.invoke("shell-init", shell).stdout
            result = subprocess.run([shell, "-c", code + "\ncodex exec 'spaces; $(literal)'"],
                                    capture_output=True, text=True, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("spaces; $(literal)", json.loads(result.stdout)["args"])


if __name__ == "__main__":
    unittest.main()
