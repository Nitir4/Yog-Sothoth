"""GTK API checks and an optional real-window smoke test on a desktop session."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
try:
    import switcher_gui as gui
    from gi.repository import GObject
except (ImportError, ValueError):
    gui = None
GUI_AVAILABLE = bool(gui and (gui.Gtk.get_major_version(), gui.Gtk.get_minor_version()) >= (4, 10))


@unittest.skipUnless(GUI_AVAILABLE, "Optional GTK 4.10+ dependency is not installed")
class AccountSelectionTest(unittest.TestCase):
    """Exercise controller callbacks without requiring a desktop display."""

    def setUp(self):
        self.make_window("codex")

    def make_window(self, tool):
        from switcher_manager import Account, ToolState
        self.state = ToolState(tool, gui.TOOLS[tool], "/unused", True, True, False, "personal",
                               [Account("work", "Login cached"), Account("personal", "Login cached", True)])
        self.jobs = []

        class Dropdown:
            index = 0

            def get_selected(dropdown):
                return dropdown.index

            def set_model(dropdown, model):
                # Simulate GTK's intermediate selection during model replacement.
                dropdown.index = 0
                self.window.on_account()

            def set_selected(dropdown, index):
                dropdown.index = index
                self.window.on_account()

            def set_sensitive(dropdown, value):
                pass

        class Controller:
            account = gui.ManagerWindow.account
            state = gui.ManagerWindow.state
            on_account = gui.ManagerWindow.on_account
            select_tool = gui.ManagerWindow.select_tool
            use_account = gui.ManagerWindow.use_account

            def loaded(controller, data):
                controller.snapshot, _, _ = data
                controller.select_tool(controller.tool, keep_filter=True)

        self.window = Controller()
        self.window.snapshot = [self.state]
        self.window.tool = tool
        self.window.chosen = {}
        self.window.updating_accounts = False
        self.window.busy = False
        self.window.nav = {tool: Mock()}
        self.window.accounts = Dropdown()
        for name in ("tool_title", "installed", "history_mode", "history_info", "share_button", "account_info"):
            setattr(self.window, name, Mock())
        self.window.update_actions = Mock()
        self.window.message = Mock()
        self.window.loader = Mock(return_value=([self.state], [], []))
        self.window.worker = lambda job, done: self.jobs.append((job, done))

    def test_open_refresh_and_tool_render_do_not_switch_accounts(self):
        for tool in gui.TOOLS:
            with self.subTest(tool=tool):
                self.make_window(tool)
                for _ in range(3):
                    self.window.select_tool(tool, keep_filter=True)
                    self.assertEqual(self.window.account(), "personal")
                    self.assertEqual(self.window.chosen[tool], "personal")
                self.assertEqual(self.jobs, [])

    def test_dropdown_routes_switch_and_confirmation_for_every_tool(self):
        for tool, label in gui.TOOLS.items():
            with self.subTest(tool=tool):
                self.make_window(tool)
                self.window.select_tool(tool, keep_filter=True)
                self.window.accounts.set_selected(0)
                self.assertEqual(len(self.jobs), 1)
                job, done = self.jobs.pop()
                with patch.object(gui.subprocess, "run", return_value=SimpleNamespace(
                        returncode=0, stderr="")) as switched:
                    data = job()
                self.assertEqual(switched.call_args.args[0],
                                 [sys.executable, str(PROJECT / "switcher"), tool, "use", "work"])
                self.assertIn(tool.upper() + "_SWITCHER_HOME", switched.call_args.kwargs["env"])
                self.state.selected = "work"
                done(data)
                self.assertEqual(self.window.account(), "work")
                self.assertEqual(self.jobs, [])
                self.assertIn(f"Switched {label} to work", self.window.message.call_args.args[0])

    def test_dropdown_saves_default_and_confirms_success(self):
        import json
        from codex_switcher import Store
        self.window.select_tool("codex", keep_filter=True)
        self.window.accounts.set_selected(0)
        self.assertEqual(self.window.account(), "work")
        self.assertEqual(len(self.jobs), 1)
        job, done = self.jobs.pop()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for account in self.state.accounts:
                home = root / "accounts" / account.name
                home.mkdir(mode=0o700, parents=True)
                (home / "auth.json").write_text(json.dumps({"tokens": {
                    "access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}}))
            root.chmod(0o700)
            (root / "accounts").chmod(0o700)
            executable = root / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            with patch.dict(os.environ, {"CODEX_SWITCHER_HOME": str(root),
                                         "CODEX_SWITCHER_CODEX": str(executable)}):
                Store().select("personal")
                data = job()
                self.assertEqual(Store().current(), "work")
        self.state.selected = "work"
        done(data)
        self.assertEqual(self.window.account(), "work")
        self.assertEqual(self.jobs, [])
        self.assertIn("Switched Codex to work", self.window.message.call_args.args[0])

    def test_failed_login_keeps_saved_default_and_reports_error(self):
        from codex_switcher import SwitcherError
        for tool in gui.TOOLS:
            with self.subTest(tool=tool):
                self.make_window(tool)
                self.window.select_tool(tool, keep_filter=True)
                self.window.accounts.set_selected(0)
                job, _ = self.jobs.pop()
                with patch.object(gui.subprocess, "run", return_value=SimpleNamespace(
                        returncode=1, stderr="No cached login for 'work'.")):
                    with self.assertRaisesRegex(SwitcherError, "No cached login"):
                        job()
                self.assertEqual(self.state.selected, "personal")
                self.window.loader.assert_not_called()

    def test_selecting_current_default_does_not_start_a_switch(self):
        for tool in gui.TOOLS:
            with self.subTest(tool=tool):
                self.make_window(tool)
                self.window.select_tool(tool, keep_filter=True)
                self.window.on_account()
                self.assertEqual(self.jobs, [])


class HeadlessGuiTest(unittest.TestCase):
    @unittest.skipUnless(GUI_AVAILABLE, "Optional GTK 4.10+ dependency is not installed")
    def test_missing_display_is_reported_without_traceback(self):
        env = dict(os.environ, GDK_BACKEND="x11", DISPLAY="", WAYLAND_DISPLAY="")
        result = subprocess.run([sys.executable, str(PROJECT / "switcher"), "gui"],
                                env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("No graphical display", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("CRITICAL", result.stderr)

    @unittest.skipUnless(GUI_AVAILABLE, "Optional GTK 4.10+ dependency is not installed")
    def test_css_and_widget_properties_match_installed_gtk(self):
        errors = []
        css = gui.Gtk.CssProvider()
        css.connect("parsing-error", lambda *args: errors.append(str(args[-1])))
        if hasattr(css, "load_from_string"):
            css.load_from_string(gui.CSS.decode())
        else:
            css.load_from_data(gui.CSS)
        self.assertEqual(errors, [])
        for cls, properties in [(gui.Gtk.SearchEntry, ["placeholder-text"]),
                                (gui.Gtk.FileDialog, ["title"]),
                                (gui.Gtk.ApplicationWindow, ["title", "default-width"]),
                                (gui.Gtk.ScrolledWindow, ["hscrollbar-policy"])]:
            self.assertTrue(set(properties) <= {p.name for p in GObject.list_properties(cls)})

    @unittest.skipUnless(GUI_AVAILABLE, "Optional GTK 4.10+ dependency is not installed")
    def test_real_window_filters_selection_and_launch_plans(self):
        gui.Gtk.init_check()
        if gui.Gdk.Display.get_default() is None:
            self.skipTest("No accessible desktop display (sandbox may block display sockets)")
        from switcher_manager import Account, Conversation, ToolState
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for account in ("work", "personal"):
                (root / "accounts" / account).mkdir(parents=True)
            state = ToolState("codex", "Codex", str(root), True, True, False, "work",
                              [Account("work", "Login cached", True), Account("personal", "Login cached")])
            record = Conversation("codex", "demo-session", "Fix auth", str(root), time.time(), ["work"])
            app = gui.Gtk.Application(application_id="io.github.cli_switcher.Test", flags=gui.Gio.ApplicationFlags.NON_UNIQUE)
            app.register(None)
            with patch.dict(os.environ, {"CODEX_SWITCHER_HOME": str(root)}):
                win = gui.ManagerWindow(app, loader=lambda: ([state], [record], []))
                try:
                    win.present()
                    context = gui.GLib.MainContext.default()
                    deadline = time.monotonic() + 3
                    while win.busy and time.monotonic() < deadline:
                        context.iteration(False)
                        time.sleep(0.005)
                    self.assertFalse(win.busy)
                    self.assertEqual(win.account(), "work")
                    win.list.select_row(win.list.get_row_at_index(0))
                    self.assertTrue(win.resume_button.get_sensitive())
                    with patch.object(win, "use_account") as switch:
                        win.accounts.set_selected(1)
                        switch.assert_called_once()
                    self.assertFalse(win.resume_button.get_sensitive())
                    record.shared = True
                    win.update_actions()
                    with patch.object(gui, "open_terminal") as opened:
                        win.resume()
                        self.assertEqual(opened.call_args.args[0], ["codex", "run", "--account", "personal", "--", "resume", "demo-session"])
                        self.assertEqual(opened.call_args.args[1], root)
                    win.project_filter.set_text("does-not-match")
                    self.assertEqual(win.history_stack.get_visible_child_name(), "empty")
                finally:
                    win.close()
                    app.quit()


if __name__ == "__main__":
    unittest.main()
