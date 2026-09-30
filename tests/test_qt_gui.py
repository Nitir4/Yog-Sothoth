"""Qt dropdown persistence, conversation ownership, and render guards."""

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    from PySide6.QtWidgets import QApplication
    from switcher_qt import ManagerWindow
except ImportError:
    QApplication = None

from switcher_manager import Account, Conversation, ToolState


@unittest.skipIf(QApplication is None, "Optional Qt desktop dependencies")
class QtManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qt profiles ")
        self.base = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {t.upper() + "_SWITCHER_HOME": str(self.base / t) for t in ("codex", "claude", "agy")})
        self.environment.start()
        self.window = ManagerWindow(auto_refresh=False)
        self.window.setup_offered = True
        self.snapshot = [ToolState(t, label, str(self.base / t), installed=True, available=True,
                                   selected="personal", accounts=[Account("personal", "Login cached", True), Account("work", "Login cached")])
                         for t, label in (("codex", "Codex"), ("claude", "Claude Code"), ("agy", "Antigravity"))]
        for tool in self.snapshot:
            for account in tool.accounts:
                (self.base / tool.tool / "accounts" / account.name).mkdir(parents=True)
        self.window.loaded((self.snapshot, [], []))

    def tearDown(self):
        deadline = time.monotonic() + 5
        while self.window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.environment.stop()
        self.temp.cleanup()

    def test_render_refresh_and_tool_changes_do_not_persist(self):
        with patch.object(self.window, "use_account") as select:
            self.window.loaded((self.snapshot, [], []))
            self.window.tools.setCurrentIndex(1)
            self.window.tools.setCurrentIndex(2)
            select.assert_not_called()
            self.assertEqual(self.window.account(), "personal")

    def test_user_dropdown_selection_persists_for_every_tool(self):
        for index, state in enumerate(self.snapshot):
            self.window.tools.setCurrentIndex(index)
            with patch.object(self.window, "use_account") as select:
                self.window.accounts.setCurrentIndex(1)
                select.assert_called_once()
                self.assertEqual(self.window.account(), "work")

    def test_worker_updates_default_only_after_successful_selection(self):
        import subprocess
        commands = []

        def select(argv, **kwargs):
            commands.append(argv)
            self.assertTrue(kwargs["capture_output"])
            self.snapshot[0].selected = "work"
            return subprocess.CompletedProcess(argv, 0, "", "")

        with patch("switcher_qt.native_run", side_effect=select), patch("switcher_qt.load", return_value=(self.snapshot, [], [])):
            self.window.accounts.setCurrentIndex(1)
            deadline = time.monotonic() + 5
            while self.window.busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
        self.assertFalse(self.window.busy)
        self.assertEqual(commands[0][-3:], ["codex", "use", "work"])
        self.assertEqual(self.window.state().selected, "work")
        self.assertIn("Switched Codex to work", self.window.notice.text())

    def test_failed_selection_is_reported_without_changing_default(self):
        import subprocess
        with patch("switcher_qt.native_run", return_value=subprocess.CompletedProcess([], 1, "", "Sign in again")):
            self.window.accounts.setCurrentIndex(1)
            deadline = time.monotonic() + 5
            while self.window.busy and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.01)
        self.assertEqual(self.window.state().selected, "personal")
        self.assertEqual(self.window.notice.text(), "Sign in again")

    def test_conversation_switches_tool_and_enforces_history_ownership(self):
        record = Conversation("claude", "synthetic-id", "A saved task", str(self.base), 0, ["personal"])
        self.window.loaded((self.snapshot, [record], []))
        self.window.table.selectRow(0)
        self.assertEqual(self.window.tool, "claude")
        self.assertTrue(self.window.resume_button.isEnabled())
        with patch.object(self.window, "use_account"):
            self.window.accounts.setCurrentIndex(1)
        self.assertFalse(self.window.resume_button.isEnabled())
        self.assertIn("Share this tool's history", self.window.details.text())


if __name__ == "__main__":
    unittest.main()
