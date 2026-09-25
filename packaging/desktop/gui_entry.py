"""Windowed entry point and display diagnostic for native desktop packages."""

import argparse
import os
import sys

# Windows windowed executables have no standard streams.
for name in ("stdout", "stderr"):
    if getattr(sys, name) is None:
        setattr(sys, name, open(os.devnull, "w"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox
from switcher_qt import ManagerWindow, load
from switcher_runtime import VERSION


def main():
    parser = argparse.ArgumentParser(description="Yog-Sothoth desktop manager")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--check", action="store_true", help="Open briefly to verify packaged desktop dependencies")
    args = parser.parse_args()
    app = QApplication([])
    app.setApplicationName("Yog-Sothoth")
    app.setApplicationVersion(VERSION)
    try:
        window = ManagerWindow(auto_refresh=not args.check)
        if args.check:
            window.setup_offered = True
            window.loaded(load())
            QTimer.singleShot(500, app.quit)
        window.show()
        return app.exec()
    except Exception as exc:
        if not args.check:
            QMessageBox.critical(None, "Yog-Sothoth", str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
