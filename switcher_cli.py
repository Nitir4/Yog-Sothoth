"""One command for the independent account adapters and desktop manager."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from codex_switcher import SwitcherError
from switcher_manager import TOOLS, adapter, as_data, conversations, launch_arguments, states, store_for


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage Codex, Claude Code, and Antigravity accounts and history.")
    commands = result.add_subparsers(dest="action", required=True)
    for tool, label in TOOLS.items():
        commands.add_parser(tool, help=f"Forward commands to the {label} adapter", add_help=False)
    commands.add_parser("gui", help="Open the native GTK 4 desktop manager")
    status = commands.add_parser("status", help="Show all stores, accounts, and cached login status")
    status.add_argument("--json", action="store_true")
    history = commands.add_parser("history", help="Browse saved conversations across tools")
    history.add_argument("--tool", choices=TOOLS)
    history.add_argument("--project", default="", help="Filter project paths by substring")
    history.add_argument("--query", default="", help="Filter titles, IDs, or paths by substring")
    history.add_argument("--limit", type=int, default=500)
    history.add_argument("--json", action="store_true")
    resume = commands.add_parser("resume", help="Resume a saved conversation in this terminal")
    resume.add_argument("tool", choices=TOOLS)
    resume.add_argument("id")
    resume.add_argument("--account", required=True)
    resume.add_argument("--project", help="Working directory override (defaults to the saved project)")
    return result


def dispatch(tool: str, arguments: list[str]) -> int:
    # Resolve repo-local fallback once so CLI, GUI, and spawned terminals agree.
    os.environ.setdefault(f"{tool.upper()}_SWITCHER_HOME", str(store_for(tool).root))
    return adapter(tool).main(arguments)


def terminal(arguments: list[str]) -> int:
    inner = argparse.ArgumentParser()
    inner.add_argument("--cwd", required=True)
    inner.add_argument("command", nargs=argparse.REMAINDER)
    args = inner.parse_args(arguments)
    cwd = Path(args.cwd).expanduser()
    if not cwd.is_dir() or not args.command:
        raise SwitcherError("Choose an existing project directory and a command.")
    print("$ " + shlex.join(["switcher", *args.command]), flush=True)
    code = subprocess.call([sys.executable, str(Path(__file__).with_name("switcher")), *args.command], cwd=cwd)
    print(f"\nCommand finished (exit {code}). Refresh the manager to see updates.", flush=True)
    if sys.stdin.isatty():
        try:
            input("Press Enter to close this terminal… ")
        except EOFError:
            pass
    return code


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] in TOOLS:
            return dispatch(argv[0], argv[1:])
        if argv and argv[0] == "_terminal":
            return terminal(argv[1:])
        args = parser().parse_args(argv)
        if args.action == "gui":
            try:
                from switcher_gui import run
            except (ImportError, ValueError):
                raise SwitcherError("The desktop manager requires PyGObject and GTK 4.10+. See README's desktop setup.") from None
            return run()
        snapshot = states()
        if args.action == "status":
            if args.json:
                print(json.dumps([as_data(state) for state in snapshot], indent=2))
            else:
                for state in snapshot:
                    print(f"{state.label}: {'shared' if state.shared else 'per-account'} history · {state.root}")
                    for account in state.accounts:
                        print(f"  {'*' if account.selected else ' '} {account.name}: {account.login}")
                    if state.error:
                        print("  " + state.error)
            return 0
        if args.action == "history":
            if not 1 <= args.limit <= 10000:
                raise SwitcherError("--limit must be between 1 and 10000.")
            records, errors = conversations(snapshot, tool=args.tool, project=args.project, query=args.query, limit=args.limit)
            if args.json:
                print(json.dumps({"conversations": [as_data(r) for r in records], "errors": errors}, indent=2))
            else:
                for r in records:
                    print(f"{r.tool}\t{r.id}\t{r.title}\t{r.project}\t{'shared' if r.shared else ', '.join(r.accounts)}")
                if not records:
                    print("No saved conversations match. Add accounts or import history with '<tool> history share'.")
                for error in errors:
                    print(error, file=sys.stderr)
            return 1 if errors else 0
        if args.action == "resume":
            records, errors = conversations(snapshot, tool=args.tool, query=args.id, limit=10000)
            record = next((r for r in records if r.id == args.id), None)
            if not record:
                raise SwitcherError("Conversation not found in saved account history. Import the original home using 'history share' first.")
            arguments = launch_arguments(args.tool, args.account, record)
            directory = args.project or record.project
            if not directory:
                raise SwitcherError("This conversation has no saved project path; pass --project PATH.")
            path = Path(directory).expanduser()
            if not path.is_dir():
                raise SwitcherError("The saved project directory is unavailable; pass --project PATH.")
            os.chdir(path)
            return dispatch(args.tool, arguments[1:])
    except (SwitcherError, OSError) as exc:
        print(f"switcher: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
