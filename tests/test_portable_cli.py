"""Native-process CLI checks shared by Linux, macOS, and Windows runners."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from switcher_runtime import cli_command, shell_function, external_environment

PROJECT = Path(__file__).resolve().parents[1]

FAKE = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
tool = os.environ['FIXTURE_TOOL']
home = pathlib.Path(os.environ['CODEX_HOME' if tool == 'codex' else 'CLAUDE_CONFIG_DIR'])
auth = home / ('auth.json' if tool == 'codex' else '.credentials.json')
if args == ['--help']:
    print('--no-daemon')
elif tool == 'codex' and 'login' in args:
    if 'status' in args:
        raise SystemExit(0 if auth.exists() else 1)
    auth.write_text(json.dumps({'tokens': {'access_token': 'synthetic-access', 'refresh_token': 'synthetic-refresh'}}))
elif args == ['auth', 'login']:
    auth.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-access', 'refreshToken': 'synthetic-refresh'}}))
elif args == ['auth', 'status']:
    print(json.dumps({'loggedIn': auth.exists(), 'authMethod': 'claude.ai', 'configDirectory': str(home)}))
elif args[-1:] == ['fixture-refresh']:
    value = json.loads(auth.read_text())
    key = 'tokens' if tool == 'codex' else 'claudeAiOauth'
    field = 'refresh_token' if tool == 'codex' else 'refreshToken'
    value[key][field] = 'synthetic-renewed'
    auth.write_text(json.dumps(value))
else:
    print(json.dumps({'home': str(home), 'args': args, 'api_key': ('OPENAI_API_KEY' if tool == 'codex' else 'ANTHROPIC_API_KEY') in os.environ}))
    if args[-1:] == ['fixture-fail']:
        raise SystemExit(7)
'''


def executable_fixture(path: Path) -> Path:
    if os.name == "nt":
        # pip's native console launcher is a real executable, so tests exercise
        # Windows CreateProcess rather than adding a production test bypass.
        from pip._vendor.distlib.scripts import ScriptMaker
        maker = ScriptMaker(None, None)
        launcher = maker._get_launcher("t")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("__main__.py", FAKE)
        path = path.with_suffix(".exe")
        path.write_bytes(launcher + ('#!"' + sys.executable + '"\n').encode() + archive.getvalue())
    else:
        # Unix shebangs cannot quote an interpreter path containing spaces.
        path.write_text("#!/usr/bin/env python3\n" + FAKE)
        path.chmod(0o700)
    return path


class PortableCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="yog cli spaces ")
        self.base = Path(self.temp.name)
        self.fake = executable_fixture(self.base / "native-fixture")
        self.env = dict(os.environ)
        self.env.pop("APPIMAGE", None)
        self.env.pop("APPDIR", None)
        for tool in ("codex", "claude", "agy"):
            self.env[tool.upper() + "_SWITCHER_HOME"] = str(self.base / tool)
        self.env.update(CODEX_SWITCHER_CODEX=str(self.fake), CLAUDE_SWITCHER_CLAUDE=str(self.fake),
                        OPENAI_API_KEY="synthetic-key", ANTHROPIC_API_KEY="synthetic-key", PYTHONUTF8="1")

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, tool, *arguments, expected=0):
        env = dict(self.env, FIXTURE_TOOL=tool)
        result = subprocess.run([sys.executable, str(PROJECT / "switcher_cli.py"), tool, *arguments],
                                cwd=self.base, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def test_accounts_refresh_arguments_and_exit_codes(self):
        literal = 'spaces; $(literal) `whoami` & | %PATH% "quoted" Unicode ✓'
        for tool in ("codex", "claude"):
            with self.subTest(tool=tool):
                self.invoke(tool, "add", "personal")
                self.invoke(tool, "add", "work")
                self.invoke(tool, "use", "work")
                result = json.loads(self.invoke(tool, "run", "--", literal).stdout)
                self.assertEqual(Path(result["home"]).name, "work")
                self.assertEqual(result["args"][-1], literal)
                self.assertFalse(result["api_key"])
                result = json.loads(self.invoke(tool, "run", "--account", "personal", "--", "once").stdout)
                self.assertEqual(Path(result["home"]).name, "personal")
                self.assertEqual(self.invoke(tool, "current").stdout.strip(), "work")
                self.invoke(tool, "run", "--", "fixture-refresh")
                self.invoke(tool, "use", "personal")
                self.invoke(tool, "use", "work")
                auth = self.base / tool / "accounts/work" / ("auth.json" if tool == "codex" else ".credentials.json")
                self.assertIn("synthetic-renewed", auth.read_text())
                self.invoke(tool, "run", "--", "fixture-fail", expected=7)

    def test_shell_integration_from_another_directory(self):
        import shutil
        for tool in ("codex", "claude"):
            self.invoke(tool, "add", "personal")
            shells = ("powershell",) if os.name == "nt" else ("bash", "zsh")
            for shell in shells:
                native = "pwsh" if shell == "powershell" else shell
                if not shutil.which(native):
                    continue
                code = self.invoke(tool, "shell-init", shell).stdout
                env = dict(self.env, FIXTURE_TOOL=tool)
                prompt = 'literal spaces; $(do-not-execute)'
                script = code + f"\n{tool} '{prompt}'"
                flags = ["-NoProfile", "-Command"] if shell == "powershell" else ["-c"]
                result = subprocess.run([native, *flags, script], env=env, cwd=self.base,
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["args"][-1], prompt)

    def test_appimage_launches_use_a_separate_runtime(self):
        with patch.dict(os.environ, {"APPIMAGE": "/tmp/Yog-Sothoth.AppImage", "APPDIR": "/tmp/mount"}):
            self.assertEqual(cli_command(["_terminal"]), ["/tmp/Yog-Sothoth.AppImage", "--appimage-extract-and-run", "_terminal"])
            self.assertEqual(cli_command(["run"], "codex"), ["/tmp/Yog-Sothoth.AppImage", "--appimage-extract-and-run", "codex", "run"])

    def test_powershell_paths_are_quoted_and_environment_is_restored(self):
        with patch.dict(os.environ, {}, clear=True):
            code = shell_function("codex", "C:/a user's folder/codex.exe", "powershell")
        self.assertIn("a user''s folder", code)
        self.assertIn("finally", code)
        self.assertIn("SetEnvironmentVariable", code)

    def test_bundled_libraries_do_not_leak_into_external_tools(self):
        with patch.dict(os.environ, {"YOG_SOTHOTH_BUNDLED": "1", "LD_LIBRARY_PATH": "/bundle/libs",
                                    "PYTHONHOME": "/bundle", "APPDIR": "/bundle", "APPIMAGE": "/app",
                                    "YOG_SOTHOTH_ORIGINAL_LD_LIBRARY_PATH": "/user/libs",
                                    "YOG_SOTHOTH_ORIGINAL_LD_LIBRARY_PATH_SET": "1"}, clear=True):
            env = external_environment()
        self.assertEqual(env, {"LD_LIBRARY_PATH": "/user/libs"})

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_credential_directories_have_protected_acls(self):
        self.invoke("codex", "add", "personal")
        script = r'''
$acl = Get-Acl $env:FIXTURE_ACL_PATH
@{protected=$acl.AreAccessRulesProtected; user=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
  principals=@($acl.Access | ForEach-Object {$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value})} | ConvertTo-Json
'''
        env = dict(self.env, FIXTURE_ACL_PATH=str(self.base / "codex/accounts/personal"))
        result = subprocess.run(["pwsh", "-NoProfile", "-Command", script], env=env,
                                capture_output=True, text=True, check=True)
        acl = json.loads(result.stdout)
        self.assertTrue(acl["protected"])
        self.assertEqual(set(acl["principals"]), {acl["user"], "S-1-5-18", "S-1-5-32-544"})

    @unittest.skipUnless(os.name == "nt", "Windows executable discovery")
    def test_windows_npm_binary_discovery_and_shell_shim_rejection(self):
        import platform
        import shutil
        from codex_switcher import find_executable, SwitcherError
        npm = self.base / "appdata/npm"
        arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        triple = "aarch64" if arch == "arm64" else "x86_64"
        executable = npm / "node_modules/@openai" / ("codex-win32-" + arch) / "vendor" / (triple + "-pc-windows-msvc") / "bin/codex.exe"
        executable.parent.mkdir(parents=True)
        shutil.copyfile(self.fake, executable)
        shim = npm / "codex.cmd"
        shim.write_text("@exit /b 99\n")
        env = dict(self.env, APPDATA=str(self.base / "appdata"), PATH=str(npm))
        env.pop("CODEX_SWITCHER_CODEX")
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(Path(find_executable("codex", "codex")), executable)
            os.environ["CODEX_SWITCHER_CODEX"] = str(shim)
            with self.assertRaisesRegex(SwitcherError, "native .exe"):
                find_executable("codex", "codex")

    @unittest.skipUnless(os.name == "nt", "Windows junctions")
    def test_windows_junction_account_directory_is_refused(self):
        from codex_switcher import private_directory, SwitcherError
        target, junction = self.base / "target", self.base / "junction"
        target.mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                       check=True, capture_output=True)
        with self.assertRaisesRegex(SwitcherError, "symlink"):
            private_directory(junction)

    @unittest.skipUnless(os.name == "nt", "Windows symbolic-link permissions")
    def test_windows_shared_history_or_clear_preflight_error(self):
        self.invoke("codex", "add", "personal")
        self.invoke("codex", "add", "work")
        home = self.base / "codex/accounts/personal"
        sessions = home / "sessions"
        sessions.mkdir()
        (sessions / "synthetic.jsonl").write_text('{"id":"synthetic-session"}\n')
        from switcher_runtime import check_history_links
        from codex_switcher import SwitcherError
        try:
            check_history_links(self.base)
        except SwitcherError:
            result = self.invoke("codex", "history", "share", "--source-home", str(home), expected=1)
            self.assertIn("Developer Mode", result.stderr)
            self.assertFalse((self.base / "codex/history.json").exists())
            self.assertFalse(sessions.is_symlink())
            self.assertTrue((sessions / "synthetic.jsonl").is_file())
        else:
            self.invoke("codex", "history", "share", "--source-home", str(home))
            self.assertTrue(sessions.is_symlink())
            self.assertTrue((self.base / "codex/accounts/work/sessions/synthetic.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
