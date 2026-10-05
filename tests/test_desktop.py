"""Native terminal launch plans and frozen runtime isolation."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_switcher import SwitcherError
from switcher_manager import open_terminal, terminal_command, workspace
from switcher_runtime import cli_command, external_environment


class DesktopLaunchTest(unittest.TestCase):
    def test_project_file_uri_preserves_drive_spaces_and_literal_percent(self):
        uri = '["file:///C:/workspace%20folder/literal%2520"]'
        expected = r"C:\workspace folder\literal%20" if os.name == "nt" else "/C:/workspace folder/literal%20"
        self.assertEqual(workspace(uri), expected)

    @unittest.skipUnless(os.name == "nt", "Windows network project paths")
    def test_windows_network_project_uri(self):
        self.assertEqual(workspace(['file://build-server/shared%20project']), r"\\build-server\shared project")

    def test_frozen_gui_uses_console_helper(self):
        with tempfile.TemporaryDirectory(prefix="desktop spaces ") as directory:
            base = Path(directory)
            helper = base / ("yog-sothoth.exe" if os.name == "nt" else "yog-sothoth")
            helper.touch()
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(base / "Yog-Sothoth-Desktop")):
                self.assertEqual(cli_command(["use", "work"], "codex"), [str(helper), "codex", "use", "work"])
                helper.unlink()
                with self.assertRaisesRegex(SwitcherError, "bundled CLI"):
                    cli_command(["status"])

    def test_mac_terminal_receives_literal_arguments_as_data(self):
        prompt = 'a "quote"; $(touch SHOULD-NOT-EXIST) `whoami`\nsecond line'
        with tempfile.TemporaryDirectory(prefix="project spaces ") as directory:
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, "platform", "darwin"):
                argv = terminal_command(["codex", "run", "--", prompt], Path(directory))
            self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
            self.assertNotIn(prompt, argv[2])
            payload = shlex.split(argv[3])
            self.assertEqual(payload[-1], prompt)
            self.assertIn(directory, payload)

    def test_windows_launch_has_a_new_console_and_no_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "platform", "win32"), patch.dict(os.environ, {}, clear=True), \
                 patch.object(subprocess, "CREATE_NEW_CONSOLE", 16, create=True), \
                 patch("switcher_manager.subprocess.Popen") as popen:
                open_terminal(["codex", "run", "--", "literal & %PATH%"], Path(directory))
            args, options = popen.call_args
            self.assertEqual(args[0][-1], "literal & %PATH%")
            self.assertEqual(options["creationflags"], 16)
            self.assertNotIn("shell", options)

    def test_frozen_external_environment_restores_user_libraries(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", "/bundle", create=True), \
             patch.dict(os.environ, {"LD_LIBRARY_PATH": "/bundle", "LD_LIBRARY_PATH_ORIG": "/user/lib",
                                     "PATH": os.pathsep.join(("/bundle", "/bundle/bin", "/user/bin")),
                                     "QT_PLUGIN_PATH": "/bundle/plugins"}, clear=True):
            env = external_environment()
        self.assertEqual(env["LD_LIBRARY_PATH"], "/user/lib")
        self.assertEqual(env["PATH"], "/user/bin")
        self.assertNotIn("QT_PLUGIN_PATH", env)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")


if __name__ == "__main__":
    unittest.main()
