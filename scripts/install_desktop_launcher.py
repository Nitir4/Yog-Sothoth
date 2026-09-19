#!/usr/bin/env python3
"""Install a Linux application-menu launcher for this checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT = Path(__file__).resolve().parents[1]


def desktop_string(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def exec_argument(value: str) -> str:
    # Desktop Exec has its own quoting rules, followed by string escaping.
    quoted = "".join("\\" + c if c in '\\"`$' else c for c in value)
    return desktop_string('"' + quoted.replace("%", "%%") + '"')


def main() -> int:
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=data / "applications/coding-switcher.desktop",
                        help="Launcher destination (default: the current user's application menu)")
    args = parser.parse_args()
    try:
        python = str(Path(sys.executable).resolve())
        template = (PROJECT / "coding-switcher.desktop").read_text()
        content = template.replace("Exec=python3 @SWITCHER_SCRIPT@ gui",
                                   f"Exec={exec_argument(python)} {exec_argument(str(PROJECT / 'switcher'))} gui")
        content = content.replace("TryExec=python3", "TryExec=" + desktop_string(python))
        content = content.replace("@PROJECT_DIR@", desktop_string(str(PROJECT)))
        output = args.output.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content)
        output.chmod(0o644)
        print(f"Installed Yog-Sothoth launcher: {output}")
        return 0
    except OSError as exc:
        print(f"Could not install the desktop launcher: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
