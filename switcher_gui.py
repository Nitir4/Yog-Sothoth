"""Optional native GTK frontend; importing the CLI never requires GTK."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from codex_switcher import SwitcherError, account_name
from switcher_manager import (
    PROJECT, TOOLS, Conversation, conversations, launch_arguments,
    manager_environment, open_terminal, states,
)

CSS = b"""
.shell { background: #f4f6f2; color: #20332b; }
label { color: inherit; }
.sidebar { background: #172b24; color: #e3ede6; padding: 28px 18px; }
.sidebar label { color: #e3ede6; }
.sidebar .muted { color: #9cb4a6; }
.brand { font-size: 25px; font-weight: 800; letter-spacing: -1px; }
.eyebrow { font-size: 10px; font-weight: 700; letter-spacing: 2px; }
.nav { background: transparent; color: #bacdc0; border: none; box-shadow: none; padding: 13px 15px; border-radius: 9px; }
.nav:hover { background: #254437; }
.nav.active { background: #355f49; color: white; }
.nav label { font-weight: 600; }
.page { padding: 28px 30px 18px; }
.title { font-size: 29px; font-weight: 800; letter-spacing: -0.8px; }
.subtitle { font-size: 13px; color: #64796b; }
.muted { color: #738478; font-size: 12px; }
.section-title { font-size: 17px; font-weight: 700; }
.card { background: white; border: 1px solid #dee6db; border-radius: 12px; padding: 18px; }
.badge { background: #e9f1e5; color: #41623b; border-radius: 6px; padding: 5px 9px; font-size: 11px; font-weight: 600; }
.badge.warning { background: #fff0d6; color: #90651c; }
button { background: #ffffff; color: #284532; border: 1px solid #d5dfd1; border-radius: 7px; padding: 8px 12px; box-shadow: none; }
button:hover { background: #edf3e9; }
button.primary { background: #396148; color: #ffffff; border-color: #396148; font-weight: 600; }
button.primary:hover { background: #294e37; }
button:disabled { opacity: 0.45; }
entry, dropdown > button { background: white; color: #20332b; border: 1px solid #d5dfd1; border-radius: 7px; padding: 7px 10px; }
.history { background: white; border: 1px solid #dee6db; border-radius: 12px; }
.history list { background: transparent; color: #20332b; }
.history row { padding: 14px 18px; border-bottom: 1px solid #edf1e9; }
.history row:selected { background: #e6eedf; color: #20332b; }
.history row:hover { background: #f0f4ec; }
.conversation-title { font-size: 14px; font-weight: 600; }
.details { padding: 13px 16px; background: #edf2e8; border-radius: 8px; }
.status { font-size: 12px; color: #637c68; }
.status.error { color: #a34335; }
.dialog-body { padding: 24px; }
"""


def label(text="", css="", *, wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    if css:
        widget.add_css_class(css)
    widget.set_wrap(wrap)
    return widget


def box(*, vertical=False, spacing=10, css=""):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    if css:
        widget.add_css_class(css)
    return widget


def button(text, callback, css=""):
    widget = Gtk.Button(label=text)
    if css:
        widget.add_css_class(css)
    widget.connect("clicked", lambda _: callback())
    return widget


def css_provider():
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):
        provider.load_from_string(CSS.decode())
    else:
        provider.load_from_data(CSS)  # GTK 4.10 compatibility.
    return provider


class ManagerWindow(Gtk.ApplicationWindow):
    def __init__(self, application, *, loader=None):
        super().__init__(application=application, title="Yog-Sothoth — Accounts & history")
        self.set_default_size(1160, 800)
        self.set_size_request(920, 650)
        self.snapshot = []
        self.records: list[Conversation] = []
        self.tool = "codex"
        self.chosen = {}
        self.updating_accounts = False
        self.record = None
        self.busy = False
        self.closed = False
        self.loader = loader or self.load_data
        self.connect("close-request", self.on_close)
        self.build()
        self.refresh()

    @staticmethod
    def load_data():
        snapshot = states()
        records, errors = conversations(snapshot, limit=10000)
        return snapshot, records, errors

    def build(self):
        shell = box(spacing=0, css="shell")
        self.set_child(shell)
        sidebar = box(vertical=True, spacing=10, css="sidebar")
        sidebar.set_size_request(210, -1)
        sidebar.append(label("Y / YOG-SOTHOTH", "eyebrow"))
        brand = label("Your tools.\nOne place.", "brand")
        brand.set_margin_top(16)
        brand.set_margin_bottom(22)
        sidebar.append(brand)
        sidebar.append(label("CODING TOOLS", "eyebrow"))
        self.nav = {}
        for tool, name in TOOLS.items():
            item = button(name, lambda t=tool: self.select_tool(t), "nav")
            self.nav[tool] = item
            sidebar.append(item)
        spacer = box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)
        sidebar.append(label("Local accounts.\nNative conversations.", "muted", wrap=True))
        sidebar.append(label("Refresh after a terminal\nsession or account login.", "muted", wrap=True))
        shell.append(sidebar)

        page = box(vertical=True, spacing=14, css="page")
        page.set_hexpand(True)
        page.set_vexpand(True)
        page_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        page_scroll.set_hexpand(True)
        page_scroll.set_vexpand(True)
        page_scroll.set_child(page)
        shell.append(page_scroll)
        heading = box()
        titles = box(vertical=True, spacing=5)
        titles.set_hexpand(True)
        titles.append(label("Accounts & history", "title"))
        titles.append(label("Pick an account. Continue where you left off.", "subtitle"))
        heading.append(titles)
        self.refresh_button = button("Refresh", self.refresh)
        heading.append(self.refresh_button)
        page.append(heading)

        self.tool_title = label("Codex", "section-title")
        tool_heading = box()
        self.tool_title.set_hexpand(True)
        tool_heading.append(self.tool_title)
        self.installed = label("Checking…", "badge")
        tool_heading.append(self.installed)
        page.append(tool_heading)

        cards = box(spacing=14)
        account_card = box(vertical=True, spacing=10, css="card")
        account_card.set_hexpand(True)
        account_card.append(label("ACCOUNT", "eyebrow"))
        self.accounts = Gtk.DropDown.new_from_strings(["No saved accounts"])
        self.accounts.set_enable_search(True)
        self.accounts.connect("notify::selected", self.on_account)
        account_card.append(self.accounts)
        self.account_info = label("Add an account to get started.", "muted", wrap=True)
        account_card.append(self.account_info)
        actions = box(spacing=7)
        self.use_button = button("Switch account", self.use_account)
        self.login_button = button("Sign in", self.login_account)
        self.add_button = button("+ Add", self.add_account)
        for action in (self.use_button, self.login_button, self.add_button):
            actions.append(action)
        account_card.append(actions)
        cards.append(account_card)
        history_card = box(vertical=True, spacing=10, css="card")
        history_card.set_hexpand(True)
        history_card.append(label("HISTORY STORAGE", "eyebrow"))
        self.history_mode = label("Per account", "section-title")
        history_card.append(self.history_mode)
        self.history_info = label("Share conversations across this tool's accounts.", "muted", wrap=True)
        history_card.append(self.history_info)
        self.share_button = button("Share history…", self.share_history)
        history_card.append(self.share_button)
        cards.append(history_card)
        page.append(cards)

        work = box(spacing=8)
        work.append(label("Project", "muted"))
        self.directory = Gtk.Entry(text=str(Path.cwd()))
        self.directory.set_hexpand(True)
        self.directory.set_tooltip_text("Working directory for new sessions; resume uses the saved project when available.")
        work.append(self.directory)
        work.append(button("Browse…", lambda: self.choose_folder(self.directory)))
        self.launch_button = button("Launch in terminal ↗", self.launch, "primary")
        work.append(self.launch_button)
        page.append(work)
        self.resume_here = Gtk.CheckButton(label="Resume in the chosen project directory")
        self.resume_here.set_tooltip_text("Use this when a conversation's saved project was moved or is unavailable.")
        page.append(self.resume_here)

        history_heading = box()
        history_heading.append(label("Conversations", "section-title"))
        self.count = label("", "muted")
        self.count.set_hexpand(True)
        history_heading.append(self.count)
        self.filter_tool = Gtk.DropDown.new_from_strings(["All tools", *TOOLS.values()])
        self.filter_tool.connect("notify::selected", lambda *_: self.render_history())
        history_heading.append(self.filter_tool)
        page.append(history_heading)
        filters = box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text="Search titles or conversation IDs")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_: self.render_history())
        filters.append(self.search)
        self.project_filter = Gtk.Entry(placeholder_text="Filter by project path")
        self.project_filter.set_hexpand(True)
        self.project_filter.connect("changed", lambda *_: self.render_history())
        filters.append(self.project_filter)
        page.append(filters)

        self.history_stack = Gtk.Stack()
        self.history_stack.add_css_class("history")
        self.history_stack.set_vexpand(True)
        self.history_stack.set_size_request(-1, 150)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.connect("row-selected", self.on_conversation)
        self.list.connect("row-activated", lambda *_: self.resume())
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.list)
        self.history_stack.add_named(scroll, "list")
        empty = box(vertical=True, spacing=8)
        empty.set_valign(Gtk.Align.CENTER)
        empty.set_halign(Gtk.Align.CENTER)
        self.empty_title = label("No saved conversations yet", "section-title")
        self.empty_info = label("Add an account or share history from your original tool home.", "muted", wrap=True)
        empty.append(self.empty_title)
        empty.append(self.empty_info)
        self.history_stack.add_named(empty, "empty")
        page.append(self.history_stack)

        details = box(spacing=10, css="details")
        detail_text = box(vertical=True, spacing=4)
        detail_text.set_hexpand(True)
        self.detail_title = label("Select a conversation", "conversation-title")
        self.detail_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail_title.set_max_width_chars(55)
        self.detail_info = label("Resume opens the native CLI with the chosen account.", "muted", wrap=True)
        detail_text.append(self.detail_title)
        detail_text.append(self.detail_info)
        details.append(detail_text)
        self.resume_button = button("Resume ↗", self.resume, "primary")
        self.resume_button.set_sensitive(False)
        details.append(self.resume_button)
        page.append(details)
        self.status = label("Loading saved accounts and conversations…", "status", wrap=True)
        page.append(self.status)
        self.select_tool(self.tool, keep_filter=True)

    def on_close(self, *_):
        self.closed = True
        return False

    def message(self, text, *, error=False):
        self.status.set_text(text)
        self.status.remove_css_class("error")
        if error:
            self.status.add_css_class("error")

    def worker(self, job, done):
        if self.busy:
            self.message("An operation is still running. Wait for it to finish.")
            return
        self.busy = True
        self.refresh_button.set_sensitive(False)
        self.update_actions()

        def perform():
            try:
                result, error = job(), None
            except Exception as exc:
                result, error = None, str(exc)
            GLib.idle_add(complete, result, error)

        def complete(result, error):
            if self.closed:
                return False
            self.busy = False
            self.refresh_button.set_sensitive(True)
            self.update_actions()
            if error:
                self.message(error, error=True)
            else:
                done(result)
            return False

        threading.Thread(target=perform, daemon=True).start()

    def refresh(self):
        self.message("Reading local accounts and history…")
        self.worker(self.loader, self.loaded)

    def loaded(self, data):
        self.snapshot, self.records, errors = data
        self.select_tool(self.tool, keep_filter=True)
        self.render_history()
        self.message(errors[0] if errors else "Ready · Login caches are checked locally; each CLI validates sign-in when launched.", error=bool(errors))

    def state(self):
        return next((s for s in self.snapshot if s.tool == self.tool), None)

    def account(self):
        state = self.state()
        index = self.accounts.get_selected()
        return state.accounts[index].name if state and index < len(state.accounts) else None

    def select_tool(self, tool, *, keep_filter=False):
        self.tool = tool
        for name, item in self.nav.items():
            item.remove_css_class("active")
            if name == tool:
                item.add_css_class("active")
        state = self.state()
        self.tool_title.set_text(TOOLS[tool])
        self.installed.set_text("CLI ready" if state and state.available else "Unavailable")
        self.installed.remove_css_class("warning")
        if not state or not state.available:
            self.installed.add_css_class("warning")
        self.installed.set_tooltip_text(state.error if state else "Loading…")
        names = [a.name for a in state.accounts] if state else []
        chosen = self.chosen.get(tool) or (state.selected if state else None)
        # GTK emits selection notifications while replacing the model. Only a
        # user selection should save a new default, never rendering or refresh.
        self.updating_accounts = True
        try:
            self.accounts.set_model(Gtk.StringList.new(names or ["No saved accounts"]))
            self.accounts.set_selected(names.index(chosen) if chosen in names else 0)
        finally:
            self.updating_accounts = False
        self.accounts.set_sensitive(bool(names) and not self.busy)
        self.history_mode.set_text("Shared across accounts" if state and state.shared else "Per account")
        self.history_info.set_text("All saved accounts use this tool's shared conversations." if state and state.shared
                                   else "Enable sharing to continue conversations with any saved account.")
        self.share_button.set_label("History setup…" if state and state.shared else "Share history…")
        self.history_info.set_tooltip_text(state.root if state else "")
        self.on_account(persist=False)
        if not keep_filter:
            self.filter_tool.set_selected(list(TOOLS).index(tool) + 1)
            self.render_history()

    def on_account(self, *_, persist=True):
        if self.updating_accounts:
            return
        name = self.account()
        if name:
            self.chosen[self.tool] = name
        state = self.state()
        selected = next((a for a in state.accounts if a.name == name), None) if state else None
        info = (f"{selected.login} · Default: {state.selected or 'none'} · "
                f"{'Active for new sessions' if name == state.selected else 'Click Switch account to retry'}"
                if selected else "Add an account to get started.")
        self.account_info.set_text(info)
        self.update_actions()
        if persist and name and state and name != state.selected and not self.busy:
            self.use_account()

    def update_actions(self):
        if not hasattr(self, "resume_button"):
            return
        state, name = self.state(), self.account()
        available = bool(state and state.available and not self.busy)
        self.accounts.set_sensitive(bool(state and state.accounts and not self.busy))
        self.launch_button.set_sensitive(bool(name and available))
        self.use_button.set_sensitive(bool(name and available))
        self.login_button.set_sensitive(bool(name and available))
        self.add_button.set_sensitive(available)
        self.share_button.set_sensitive(bool(state and not self.busy))
        self.resume_button.set_sensitive(False)
        if self.record:
            self.detail_title.set_text(self.record.title)
            self.detail_info.set_text(f"{TOOLS[self.record.tool]} · {self.record.project or 'Choose a project above'}")
            if name and available:
                try:
                    launch_arguments(self.tool, name, self.record)
                    self.resume_button.set_sensitive(True)
                    self.resume_button.set_tooltip_text("Resume using " + name)
                except (SwitcherError, ValueError) as exc:
                    self.detail_info.set_text(str(exc))
                    self.resume_button.set_tooltip_text(str(exc))
        else:
            self.detail_title.set_text("Select a conversation")
            self.detail_info.set_text("Resume opens the native CLI with the chosen account.")

    def render_history(self):
        if not hasattr(self, "list"):
            return
        previous = (self.record.tool, self.record.id) if self.record else None
        while child := self.list.get_first_child():
            self.list.remove(child)
        self.record = None
        tool_index = self.filter_tool.get_selected()
        tool = list(TOOLS)[tool_index - 1] if 1 <= tool_index <= len(TOOLS) else None
        query, project = self.search.get_text().casefold(), self.project_filter.get_text().casefold()
        records = [r for r in self.records if (not tool or r.tool == tool)
                   and query in f"{r.title} {r.id} {r.project}".casefold() and project in r.project.casefold()]
        self.count.set_text(f"{len(records):,} saved" + (" · showing latest 500" if len(records) > 500 else ""))
        for record in records[:500]:
            row = Gtk.ListBoxRow()
            row.record = record
            content = box(spacing=16)
            text = box(vertical=True, spacing=5)
            text.set_hexpand(True)
            title = label(record.title, "conversation-title")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_max_width_chars(68)
            title.set_tooltip_text(record.title + "\n" + record.id)
            text.append(title)
            subtitle = label(f"{TOOLS[record.tool]}  ·  {record.project or 'Project unknown'}", "muted")
            subtitle.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            subtitle.set_max_width_chars(70)
            text.append(subtitle)
            content.append(text)
            right = box(vertical=True, spacing=5)
            right.set_halign(Gtk.Align.END)
            try:
                date = datetime.fromtimestamp(record.updated).strftime("%b %d, %H:%M") if record.updated else ""
            except (ValueError, OverflowError, OSError):
                date = ""
            right.append(label(date, "muted"))
            right.append(label("Shared" if record.shared else ", ".join(record.accounts), "badge"))
            content.append(right)
            row.set_child(content)
            self.list.append(row)
            if previous == (record.tool, record.id):
                self.list.select_row(row)
        self.history_stack.set_visible_child_name("list" if records else "empty")
        self.empty_title.set_text("No matching conversations" if self.records else "No saved conversations yet")
        self.empty_info.set_text("Try another tool, project, or search." if self.records else "Add an account or share history from your original tool home.")
        self.update_actions()

    def on_conversation(self, _, row):
        self.record = row.record if row else None
        if self.record and self.record.tool != self.tool:
            self.select_tool(self.record.tool, keep_filter=True)
        self.update_actions()

    def choose_folder(self, entry):
        dialog = Gtk.FileDialog(title="Choose a directory")
        path = Path(entry.get_text()).expanduser()
        if path.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(path)))

        def chosen(dialog, result):
            try:
                file = dialog.select_folder_finish(result)
                if file and file.get_path():
                    entry.set_text(file.get_path())
            except GLib.Error:
                pass  # The user dismissed the native chooser.
        dialog.select_folder(self, None, chosen)

    def start_terminal(self, arguments, directory=None):
        try:
            chosen = directory if directory is not None else self.directory.get_text().strip()
            if not chosen:
                raise SwitcherError("Choose a project directory first.")
            open_terminal(arguments, Path(chosen).expanduser())
            self.message("Terminal opened. Refresh after the command finishes to see changes.")
            return True
        except (SwitcherError, OSError) as exc:
            self.message(str(exc), error=True)
            return False

    def launch(self):
        name = self.account()
        if name:
            try:
                self.start_terminal(launch_arguments(self.tool, name))
            except (SwitcherError, ValueError) as exc:
                self.message(str(exc), error=True)

    def resume(self):
        name = self.account()
        if self.record and name and self.resume_button.get_sensitive():
            try:
                saved = None if self.resume_here.get_active() else self.record.project or None
                self.start_terminal(launch_arguments(self.tool, name, self.record), saved)
            except (SwitcherError, ValueError) as exc:
                self.message(str(exc), error=True)

    def login_account(self):
        if name := self.account():
            self.start_terminal([self.tool, "login", name])

    def use_account(self):
        name, tool = self.account(), self.tool
        if not name:
            return

        def select():
            try:
                result = subprocess.run([sys.executable, str(PROJECT / "switcher"), tool, "use", name],
                                        env=manager_environment(), capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                raise SwitcherError("Login check timed out. Retry or sign in from the terminal.") from None
            if result.returncode:
                raise SwitcherError(result.stderr.strip() or "Could not select the account. Try signing in again.")
            return self.loader()
        self.message(f"Selecting {name} for {TOOLS[tool]}…")
        def selected(data):
            self.loaded(data)
            self.message(f"Switched {TOOLS[tool]} to {name}. New terminal launches use this account; running sessions keep their account.")

        self.worker(select, selected)

    def form(self, title):
        dialog = Gtk.Window(title=title, transient_for=self, modal=True, default_width=470)
        dialog.add_css_class("shell")
        body = box(vertical=True, spacing=14, css="dialog-body")
        body.append(label(title, "section-title"))
        dialog.set_child(body)
        return dialog, body

    def add_account(self):
        tool = self.tool
        dialog, body = self.form("Add a " + TOOLS[tool] + " account")
        body.append(label("Save a name, then complete the tool's normal browser sign-in in a terminal.", "muted", wrap=True))
        entry = Gtk.Entry(placeholder_text="Account name, e.g. work")
        body.append(entry)
        error = label("", "status", wrap=True)
        body.append(error)

        def submit():
            try:
                name = account_name(entry.get_text().strip())
                if self.start_terminal([tool, "add", name]):
                    dialog.close()
            except argparse.ArgumentTypeError as exc:
                error.set_text(str(exc))
        actions = box()
        actions.append(button("Cancel", dialog.close))
        actions.append(button("Sign in in terminal ↗", submit, "primary"))
        body.append(actions)
        dialog.present()

    def share_history(self):
        tool = self.tool
        dialog, body = self.form("Share " + TOOLS[tool] + " history")
        body.append(label("Close all sessions for this tool first. This merges saved account history and the original home, keeps backups, and enables sharing for future launches.", "muted", wrap=True))
        body.append(label("Original tool home", "eyebrow"))
        source = Gtk.Entry(text=str(Path.home() / {"codex": ".codex", "claude": ".claude", "agy": ".gemini"}[tool]))
        source_row = box()
        source.set_hexpand(True)
        source_row.append(source)
        source_row.append(button("Browse…", lambda: self.choose_folder(source)))
        body.append(source_row)
        body.append(label("If sharing is already enabled, this command reports the existing setup; it does not import later changes from the original home.", "muted", wrap=True))
        closed = Gtk.CheckButton(label="All sessions for this tool are closed")
        body.append(closed)
        actions = box()
        actions.append(button("Cancel", dialog.close))

        def submit():
            if self.start_terminal([tool, "history", "share", "--source-home", str(Path(source.get_text()).expanduser())]):
                dialog.close()
        share = button("Share in terminal ↗", submit, "primary")
        share.set_sensitive(False)
        closed.connect("toggled", lambda item: share.set_sensitive(item.get_active()))
        actions.append(share)
        body.append(actions)
        dialog.present()


def run() -> int:
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 10):
        raise SwitcherError("The desktop manager requires GTK 4.10 or newer.")
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        raise SwitcherError("No graphical display is available. Use 'switcher status' and 'switcher history' from this terminal.")
    app = Gtk.Application(application_id="io.github.cli_switcher.Manager", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(application):
        provider = css_provider()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        ManagerWindow(application).present()
    app.connect("activate", activate)
    return app.run([])
