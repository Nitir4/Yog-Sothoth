"""Select saved Claude Code subscription accounts in isolated configuration directories."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

from codex_switcher import Store, SwitcherError, account_name, find_executable


# These select credentials or a provider ahead of the directory's browser login.
# Clear inherited values only; managed and project policies still apply.
AUTH_VARIABLES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_WORKSPACE_ID",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)


def binary() -> str:
    found = find_executable("claude", "claude")
    if not found:
        raise SwitcherError("Claude Code was not found. Install it, or set CLAUDE_SWITCHER_CLAUDE to its executable.")
    if Path(found).resolve() == Path(__file__).with_name("claude-switch").resolve():
        raise SwitcherError("CLAUDE_SWITCHER_CLAUDE must point to the real Claude Code executable.")
    return os.path.abspath(found)


def environment(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for variable in AUTH_VARIABLES:
        env.pop(variable, None)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    return env


def signed_in(executable: str, home: Path) -> bool:
    result = subprocess.run([executable, "auth", "status"], env=environment(home), capture_output=True)
    if result.returncode:
        return False
    try:
        status = json.loads(result.stdout)
        if not isinstance(status, dict) or status.get("authMethod") != "claude.ai":
            return False
        # New releases expose this field. Fail closed if another directory won.
        reported_home = status.get("configDirectory")
        return (status.get("loggedIn", True) is True and
                (reported_home is None or Path(reported_home).resolve() == home.resolve()))
    except (ValueError, TypeError, OSError):
        return False


def ensure_login(executable: str, home: Path, name: str) -> None:
    if not signed_in(executable, home):
        raise SwitcherError(
            f"No active Claude subscription login for '{name}'. Run: claude-switch login {name}. "
            "API keys, cloud-provider credentials, and settings that override the saved login are not supported."
        )


def login(store: Store, name: str) -> int:
    home = store.require(name)
    executable = binary()
    print(f"Sign in to the Claude subscription account to save as '{name}'.", flush=True)
    result = subprocess.call([executable, "auth", "login"], env=environment(home))
    if result:
        print(f"Login did not finish. Retry with: claude-switch login {name}", file=sys.stderr)
        return result if result > 0 else 1
    ensure_login(executable, home, name)
    if store.current() is None:
        store.select(name)
        print(f"Saved and selected '{name}'.")
    else:
        print(f"Saved '{name}'. Select it with: claude-switch use {name}")
    return 0


def add(store: Store, name: str) -> int:
    binary()  # Report a missing CLI before creating a profile.
    store.initialize()
    home = store.home(name)
    if home.exists() or home.is_symlink():
        raise SwitcherError(f"Account '{name}' already exists. Use 'login {name}' to sign in again.")
    home.mkdir(mode=0o700)
    # Start with fresh settings; copying apiKeyHelper or env from another home
    # could silently make the new profile use a different credential.
    return login(store, name)


def use(store: Store, name: str) -> int:
    home = store.require(name)
    ensure_login(binary(), home, name)
    store.select(name)
    print(f"Selected '{name}' for future launches. Running sessions keep their account.")
    return 0


def list_accounts(store: Store) -> int:
    store.initialize()
    selected = store.current()
    names = sorted(path.name for path in store.accounts.iterdir() if path.is_dir() and not path.is_symlink())
    if not names:
        print("No saved accounts. Run: claude-switch add personal")
        return 0
    executable = binary()
    for name in names:
        home = store.require(name)
        status = "subscription login cached" if signed_in(executable, home) else "subscription login needed"
        print(f"{'*' if name == selected else ' '} {name}  ({status})")
    return 0


def run(store: Store, args: argparse.Namespace) -> int:
    from local_history import CLAUDE, launch_history

    name = args.account or store.current()
    if not name:
        raise SwitcherError("No account selected. Run: claude-switch add personal")
    home = store.require(name)
    executable = binary()
    forwarded = args.claude_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    # Allow local login recovery and offline CLI information with an empty home.
    offline = forwarded[:1] in (["auth"], ["--help"], ["-h"], ["--version"], ["-v"])
    if not offline:
        ensure_login(executable, home, name)
    launch_history(store, home, CLAUDE)
    print(f"Claude account: {name}", file=sys.stderr, flush=True)
    argv = [executable, *forwarded]
    if os.name == "posix":
        os.execve(executable, argv, environment(home))
    return subprocess.call(argv, env=environment(home))


def shell_init(shell: str) -> int:
    executable = shlex.quote(binary())
    python = shlex.quote(sys.executable)
    script = shlex.quote(str(Path(__file__).with_name("claude-switch").resolve()))
    print(f"# claude-switcher integration for {shell}; affects this shell only")
    print("claude() {")
    print(f'  CLAUDE_SWITCHER_CLAUDE={executable} {python} {script} run -- "$@"')
    print("}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    for action, help_text in (("add", "Save an account through official browser login"),
                              ("login", "Sign in again for a saved account"),
                              ("use", "Select an account for future launches")):
        subparser = commands.add_parser(action, help=help_text)
        subparser.add_argument("name", type=account_name)
    commands.add_parser("list", help="List accounts and local subscription login status")
    commands.add_parser("current", help="Print the selected account name")
    run_parser = commands.add_parser("run", help="Launch Claude Code; pass its arguments after --")
    run_parser.add_argument("--account", type=account_name)
    run_parser.add_argument("claude_args", nargs=argparse.REMAINDER)
    shell_parser = commands.add_parser("shell-init", help="Print an optional claude shell function")
    shell_parser.add_argument("shell", choices=("zsh", "bash"))
    from local_history import add_parser

    add_parser(commands)
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = Store("claude")
    try:
        if args.action == "add":
            return add(store, args.name)
        if args.action == "login":
            return login(store, args.name)
        if args.action == "use":
            return use(store, args.name)
        if args.action == "list":
            return list_accounts(store)
        if args.action == "current":
            store.initialize()
            name = store.current()
            if not name:
                raise SwitcherError("No account selected.")
            print(name)
            return 0
        if args.action == "run":
            return run(store, args)
        if args.action == "shell-init":
            return shell_init(args.shell)
        if args.action == "history":
            from local_history import CLAUDE, share, status

            return share(store, args.source_home, CLAUDE) if args.history_action == "share" else status(store, CLAUDE)
    except (SwitcherError, OSError, sqlite3.Error) as error:
        print(f"claude-switch: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0
