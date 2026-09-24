"""Qt desktop manager for Windows and macOS, using the shared CLI adapters."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from codex_switcher import SwitcherError, account_name
from switcher_manager import (
    TOOLS, conversations, launch_arguments, manager_environment, open_terminal, states,
)
from switcher_runtime import VERSION, cli_command, native_run
from switcher_setup import INSTALL_GUIDES, setup_hint


class Worker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.completed.emit(self.operation())
        except Exception as exc:
            self.failed.emit(str(exc))


def load():
    snapshot = states()
    records, errors = conversations(snapshot, limit=10000)
    return snapshot, records, errors


class ManagerWindow(QMainWindow):
    def __init__(self, *, auto_refresh=True):
        super().__init__()
        self.setWindowTitle("Yog-Sothoth")
        self.resize(1060, 720)
        self.snapshot, self.records, self.visible = [], [], []
        self.tool, self.record = "codex", None
        self.busy, self.updating_accounts = False, False
        self.worker_thread = None
        self.setup_offered = False
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        self.setCentralWidget(page)
        heading = QHBoxLayout()
        title = QLabel("Yog-Sothoth")
        title.setStyleSheet("font-size: 25px; font-weight: 600;")
        heading.addWidget(title)
        heading.addStretch()
        self.setup_button = self.button("Setup…", self.setup, heading)
        self.refresh_button = self.button("Refresh", self.refresh, heading)
        layout.addLayout(heading)
        subtitle = QLabel("Accounts and conversations for your coding tools")
        layout.addWidget(subtitle)

        accounts = QHBoxLayout()
        self.tools = QComboBox()
        for tool, label in TOOLS.items():
            self.tools.addItem(label, tool)
        self.tools.currentIndexChanged.connect(self.select_tool)
        accounts.addWidget(self.tools)
        self.accounts = QComboBox()
        self.accounts.setMinimumWidth(170)
        self.accounts.currentIndexChanged.connect(self.on_account)
        accounts.addWidget(self.accounts, 1)
        self.use_button = self.button("Switch account", self.use_account, accounts)
        self.login_button = self.button("Sign in…", self.login_account, accounts)
        self.add_button = self.button("Add account…", self.add_account, accounts)
        layout.addLayout(accounts)
        self.account_info = QLabel("Loading accounts…")
        self.account_info.setWordWrap(True)
        layout.addWidget(self.account_info)
        history_actions = QHBoxLayout()
        self.history_info = QLabel()
        history_actions.addWidget(self.history_info, 1)
        self.share_button = self.button("Share history…", self.share_history, history_actions)
        layout.addLayout(history_actions)

        project = QHBoxLayout()
        project.addWidget(QLabel("Project"))
        self.directory = QLineEdit(str(Path.home()))
        project.addWidget(self.directory, 1)
        self.button("Browse…", lambda: self.choose_folder(self.directory), project)
        self.launch_button = self.button("Launch tool", self.launch, project)
        layout.addLayout(project)
        self.resume_here = QCheckBox("Resume in the chosen project directory")
        layout.addWidget(self.resume_here)

        filters = QHBoxLayout()
        self.filter_tool = QComboBox()
        self.filter_tool.addItem("All tools", None)
        for tool, label in TOOLS.items():
            self.filter_tool.addItem(label, tool)
        self.filter_tool.currentIndexChanged.connect(self.render_history)
        filters.addWidget(self.filter_tool)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search titles or conversation IDs")
        self.search.textChanged.connect(self.render_history)
        filters.addWidget(self.search, 1)
        self.project_filter = QLineEdit()
        self.project_filter.setPlaceholderText("Filter by project path")
        self.project_filter.textChanged.connect(self.render_history)
        filters.addWidget(self.project_filter, 1)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Conversation", "Tool", "Project", "Account", "Updated"])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.on_conversation)
        self.table.itemDoubleClicked.connect(lambda *_: self.resume())
        layout.addWidget(self.table, 1)
        self.count = QLabel()
        layout.addWidget(self.count)
        details = QHBoxLayout()
        self.details = QLabel("Select a conversation to resume.")
        self.details.setWordWrap(True)
        details.addWidget(self.details, 1)
        self.resume_button = self.button("Resume conversation", self.resume, details)
        layout.addLayout(details)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.notice)
        self.update_actions()
        if auto_refresh:
            QTimer.singleShot(0, self.refresh)

    @staticmethod
    def button(text, callback, layout):
        item = QPushButton(text)
        item.clicked.connect(lambda *_: callback())
        layout.addWidget(item)
        return item

    def message(self, text, *, error=False):
        self.notice.setText(text)
        self.notice.setStyleSheet("color: #c74b3d;" if error else "")

    def work(self, operation, callback):
        if self.busy:
            return
        self.busy = True
        self.update_actions()
        worker = Worker(operation, self)
        self.worker_thread = worker
        worker.completed.connect(callback)
        worker.failed.connect(lambda error: self.message(error, error=True))
        worker.finished.connect(self.work_finished)
        worker.start()

    def work_finished(self):
        worker = self.worker_thread
        self.worker_thread = None
        self.busy = False
        if worker:
            worker.deleteLater()
        self.update_actions()

    def closeEvent(self, event):
        if self.busy:
            self.message("Wait for the account check to finish before closing.")
            event.ignore()
        else:
            event.accept()

    def refresh(self):
        self.message("Reading saved accounts and conversations…")
        self.work(load, self.loaded)

    def loaded(self, data):
        self.snapshot, self.records, errors = data
        self.select_tool()
        self.render_history()
        self.message("\n".join(errors) if errors else "Ready. Account changes apply to new launches.", error=bool(errors))
        if not self.setup_offered:
            self.setup_offered = True
            if not any(state.accounts for state in self.snapshot):
                # Open after the worker finished signal re-enables setup actions.
                QTimer.singleShot(0, self.setup)

    def state(self):
        return next((s for s in self.snapshot if s.tool == self.tool), None)

    def account(self):
        return self.accounts.currentData()

    def select_tool(self, *_):
        self.tool = self.tools.currentData()
        state = self.state()
        self.updating_accounts = True
        try:
            self.accounts.clear()
            for account in state.accounts if state else []:
                self.accounts.addItem(account.name, account.name)
            if state:
                index = self.accounts.findData(state.selected)
                self.accounts.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self.updating_accounts = False
        self.on_account(persist=False)
        self.history_info.setText("History shared across accounts" if state and state.shared else "History saved per account")
        self.update_actions()

    def on_account(self, *_, persist=True):
        if self.updating_accounts:
            return
        state, name = self.state(), self.account()
        selected = next((a for a in state.accounts if a.name == name), None) if state else None
        self.account_info.setText(
            f"{selected.login} · Default: {state.selected or 'none'}" if selected
            else setup_hint(state) if state else "Loading accounts…")
        self.update_actions()
        if persist and name and state and name != state.selected and not self.busy:
            self.use_account()

    def update_actions(self):
        state, name = self.state(), self.account()
        available = bool(state and state.available and not self.busy)
        self.tools.setEnabled(not self.busy)
        self.accounts.setEnabled(bool(state and state.accounts and not self.busy))
        for item in (self.launch_button, self.use_button, self.login_button):
            item.setEnabled(bool(name and available))
        self.add_button.setEnabled(available)
        self.share_button.setEnabled(bool(state and not self.busy))
        self.refresh_button.setEnabled(not self.busy)
        self.setup_button.setEnabled(not self.busy)
        self.resume_button.setEnabled(False)
        if self.record:
            self.details.setText(f"{self.record.title}\n{self.record.project or 'Choose a project above'}")
            if name and available:
                try:
                    launch_arguments(self.tool, name, self.record)
                    self.resume_button.setEnabled(True)
                except SwitcherError as exc:
                    self.details.setText(str(exc))
        else:
            self.details.setText("Select a conversation to resume.")

    def render_history(self, *_):
        previous = (self.record.tool, self.record.id) if self.record else None
        tool = self.filter_tool.currentData()
        query, project = self.search.text().casefold(), self.project_filter.text().casefold()
        records = [r for r in self.records if (not tool or r.tool == tool)
                   and query in f"{r.title} {r.id} {r.project}".casefold() and project in r.project.casefold()]
        self.visible = records[:500]
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.setRowCount(len(self.visible))
        self.record = None
        for row, record in enumerate(self.visible):
            try:
                date = datetime.fromtimestamp(record.updated).strftime("%b %d, %H:%M") if record.updated else ""
            except (ValueError, OverflowError, OSError):
                date = ""
            values = [record.title, TOOLS[record.tool], record.project,
                      "Shared" if record.shared else ", ".join(record.accounts), date]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value + ("\n" + record.id if column == 0 else ""))
                self.table.setItem(row, column, item)
            if previous == (record.tool, record.id):
                self.table.selectRow(row)
                self.record = record
        self.table.blockSignals(False)
        self.count.setText(f"{len(records):,} conversations" + (" · showing latest 500" if len(records) > 500 else "")
                           if records else "No matching conversations. Add accounts or share existing history.")
        self.update_actions()

    def on_conversation(self):
        row = self.table.currentRow()
        self.record = self.visible[row] if 0 <= row < len(self.visible) else None
        if self.record and self.record.tool != self.tool:
            self.tools.setCurrentIndex(self.tools.findData(self.record.tool))
        self.update_actions()

    def choose_folder(self, entry):
        folder = QFileDialog.getExistingDirectory(self, "Choose a directory", entry.text())
        if folder:
            entry.setText(folder)

    def start_terminal(self, arguments, directory=None):
        try:
            chosen = directory if directory is not None else self.directory.text().strip()
            if not chosen:
                raise SwitcherError("Choose a project directory first.")
            open_terminal(arguments, Path(chosen).expanduser())
            self.message("Terminal opened. Refresh after the command finishes to see changes.")
            return True
        except (SwitcherError, OSError) as exc:
            self.message(str(exc), error=True)
            return False

    def launch(self):
        if name := self.account():
            try:
                self.start_terminal(launch_arguments(self.tool, name))
            except SwitcherError as exc:
                self.message(str(exc), error=True)

    def resume(self):
        if self.record and self.account() and self.resume_button.isEnabled():
            try:
                saved = None if self.resume_here.isChecked() else self.record.project or None
                self.start_terminal(launch_arguments(self.tool, self.account(), self.record), saved)
            except SwitcherError as exc:
                self.message(str(exc), error=True)

    def login_account(self):
        if name := self.account():
            self.start_terminal([self.tool, "login", name])

    def use_account(self):
        tool, name = self.tool, self.account()
        if not name:
            return

        def select():
            try:
                result = native_run(cli_command([tool, "use", name], standalone=False),
                                    env=manager_environment(external=True), capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                raise SwitcherError("Login check timed out. Retry or sign in from the terminal.") from None
            if result.returncode:
                raise SwitcherError(result.stderr.strip() or "Could not select the account. Try signing in again.")
            return load()

        def selected(data):
            self.loaded(data)
            self.message(f"Switched {TOOLS[tool]} to {name}. Running sessions keep their account.")

        self.message(f"Selecting {name} for {TOOLS[tool]}…")
        self.work(select, selected)

    def add_account(self):
        name, accepted = QInputDialog.getText(self, "Add a " + TOOLS[self.tool] + " account", "Account name, e.g. work")
        if accepted:
            try:
                self.start_terminal([self.tool, "add", account_name(name.strip())])
            except argparse.ArgumentTypeError as exc:
                self.message(str(exc), error=True)

    def share_history(self):
        tool = self.tool
        dialog = QDialog(self)
        dialog.setWindowTitle("Share " + TOOLS[tool] + " history")
        dialog.resize(550, 240)
        layout = QVBoxLayout(dialog)
        info = QLabel("Close all sessions for this tool first. Sharing merges account history and the original home, keeps backups, and applies to future launches. Existing shared stores are reported without importing later changes.")
        info.setWordWrap(True)
        layout.addWidget(info)
        source = QLineEdit(str(Path.home() / {"codex": ".codex", "claude": ".claude", "agy": ".gemini"}[tool]))
        form = QHBoxLayout()
        form.addWidget(source, 1)
        self.button("Browse…", lambda: self.choose_folder(source), form)
        layout.addLayout(form)
        closed = QCheckBox("All sessions for this tool are closed")
        layout.addWidget(closed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        share = buttons.addButton("Share in terminal", QDialogButtonBox.ButtonRole.AcceptRole)
        share.setEnabled(False)
        closed.toggled.connect(share.setEnabled)
        buttons.rejected.connect(dialog.reject)
        share.clicked.connect(lambda: dialog.accept() if self.start_terminal(
            [tool, "history", "share", "--source-home", str(Path(source.text()).expanduser())]) else None)
        layout.addWidget(buttons)
        dialog.exec()

    def setup(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Set up your coding tools")
        layout = QVBoxLayout(dialog)
        intro = QLabel("Install the coding CLIs you use, then add accounts through their normal sign-in flows.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        for state in self.snapshot:
            label = QLabel(state.label + "\n" + setup_hint(state))
            label.setWordWrap(True)
            layout.addWidget(label)
            actions = QHBoxLayout()
            self.button("Installation guide ↗", lambda tool=state.tool: QDesktopServices.openUrl(QUrl(INSTALL_GUIDES[tool])), actions)

            def add(tool=state.tool):
                dialog.accept()
                self.tools.setCurrentIndex(self.tools.findData(tool))
                self.add_account()

            action = self.button("Add account…", add, actions)
            action.setEnabled(state.available and not self.busy)
            layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()


def run() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Yog-Sothoth")
    app.setApplicationVersion(VERSION)
    window = ManagerWindow()
    window.show()
    return app.exec()
