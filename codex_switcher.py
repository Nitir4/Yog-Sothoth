"""Local account selection for Codex CLI, using isolated CODEX_HOME directories."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile


class SwitcherError(Exception):
    pass


def account_name(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", value):
        raise argparse.ArgumentTypeError(
            "Use 1–48 letters, digits, underscores, or hyphens; start with a letter or digit."
        )
    return value


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SwitcherError(f"Refusing a symlink directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise SwitcherError(f"Not a directory: {path}")
    if os.name == "posix":
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise SwitcherError(f"Storage must be owned by you and private (chmod 700): {path}")


def atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".switcher-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_auth(path: Path) -> bytes:
    # Never print the contents, even when parsing or validation fails.
    if path.is_symlink():
        raise SwitcherError("Refusing a symlink credential file.")
    try:
        data = path.read_bytes()
        auth = json.loads(data)
    except (OSError, ValueError):
        raise SwitcherError("No readable, valid auth.json found. Use the normal 'add' login flow.") from None
    if not isinstance(auth, dict):
        raise SwitcherError("Invalid credential file.")
    tokens = auth.get("tokens")
    if not (
        isinstance(tokens, dict) and tokens.get("access_token") and tokens.get("refresh_token")
    ):
        raise SwitcherError("This is not a saved ChatGPT login. Use the normal 'add' login flow.")
    return data


class Store:
    def __init__(self, tool: str = "codex") -> None:
        self.program = f"{tool}-switch"
        default = Path.home() / ".local" / "share" / f"{tool}-switcher"
        self.root = Path(os.environ.get(f"{tool.upper()}_SWITCHER_HOME", str(default))).expanduser().absolute()
        self.accounts = self.root / "accounts"
        self.selection = self.root / "selected.json"

    def initialize(self) -> None:
        private_directory(self.root)
        private_directory(self.accounts)

    def home(self, name: str) -> Path:
        account_name(name)
        return self.accounts / name

    def require(self, name: str) -> Path:
        self.initialize()
        path = self.home(name)
        if not path.is_dir():
            raise SwitcherError(f"Unknown account '{name}'. Run: {self.program} add {name}")
        private_directory(path)
        return path

    def current(self) -> str | None:
        if not self.selection.exists():
            return None
        if self.selection.is_symlink():
            raise SwitcherError("Refusing a symlink selection file.")
        try:
            value = json.loads(self.selection.read_text())["account"]
            return account_name(value)
        except (OSError, ValueError, KeyError, TypeError, argparse.ArgumentTypeError):
            raise SwitcherError("Invalid selection file. Select an account with 'use'.") from None

    def select(self, name: str) -> None:
        self.require(name)
        atomic_write(self.selection, json.dumps({"account": name}).encode() + b"\n")


def find_executable(tool: str, command_name: str) -> str | None:
    override = f"{tool.upper()}_SWITCHER_{command_name.upper()}"
    candidate = os.environ.get(override, command_name)
    found = shutil.which(candidate)
    # Desktop launchers often omit the directory used by user CLI installs.
    # An explicit executable override must still fail if it cannot be found.
    if not found and override not in os.environ:
        found = shutil.which(str(Path.home() / ".local" / "bin" / command_name))
    return found


def binary() -> str:
    found = find_executable("codex", "codex")
    if not found:
        raise SwitcherError("Codex CLI was not found. Install it, or set CODEX_SWITCHER_CODEX to its executable.")
    if Path(found).resolve() == Path(__file__).with_name("codex-switch").resolve():
        raise SwitcherError("CODEX_SWITCHER_CODEX must point to the real Codex executable.")
    return os.path.abspath(found)


def environment(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_SQLITE_HOME"):
        env.pop(key, None)
    env["CODEX_HOME"] = str(home)
    return env


def command(executable: str, home: Path, args: list[str], *, sqlite_home: Path | None = None) -> list[str]:
    return [
        executable,
        "-c", 'cli_auth_credentials_store="file"',
        "-c", "sqlite_home=" + json.dumps(str(sqlite_home or home)),
        *args,
    ]


def signed_in(executable: str, home: Path) -> bool:
    result = subprocess.run(
        command(executable, home, ["login", "status"]),
        env=environment(home), capture_output=True,
    )
    # Suppress all status output: the switcher never needs to display credentials.
    return result.returncode == 0


def source_home(value: str | None) -> Path:
    return Path(value or os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().absolute()


def copy_settings(source: Path, destination: Path) -> None:
    for path in [source / "config.toml", *sorted(source.glob("*.config.toml"))]:
        if path.is_file():
            atomic_write(destination / path.name, path.read_bytes())


def add(store: Store, args: argparse.Namespace) -> int:
    executable = binary()
    store.initialize()
    home = store.home(args.name)
    if home.exists() or home.is_symlink():
        raise SwitcherError(f"Account '{args.name}' already exists. Use 'login {args.name}' to sign in again.")
    source = source_home(args.source_home)
    auth = read_auth(source / "auth.json") if args.import_current else None
    # Exclusive creation prevents two simultaneous adds from sharing a login.
    home.mkdir(mode=0o700)
    if not args.no_copy_config:
        copy_settings(source, home)
    if auth is not None:
        atomic_write(home / "auth.json", auth)
    else:
        print(f"Sign in to the account you want to save as '{args.name}'.", flush=True)
        result = subprocess.call(
            command(executable, home, ["login", *(["--device-auth"] if args.device_auth else [])]),
            env=environment(home),
        )
        if result:
            print(f"Login did not finish. Retry with: codex-switch login {args.name}", file=sys.stderr)
            return result if result > 0 else 1
    if not signed_in(executable, home):
        raise SwitcherError(f"No cached login found. Retry with: codex-switch login {args.name}")
    if store.current() is None:
        store.select(args.name)
        print(f"Saved and selected '{args.name}'.")
    else:
        print(f"Saved '{args.name}'. Select it with: codex-switch use {args.name}")
    return 0


def login(store: Store, args: argparse.Namespace) -> int:
    home = store.require(args.name)
    executable = binary()
    result = subprocess.call(
        command(executable, home, ["login", *(["--device-auth"] if args.device_auth else [])]),
        env=environment(home),
    )
    if result:
        return result if result > 0 else 1
    if not signed_in(executable, home):
        raise SwitcherError(f"No cached login found for '{args.name}'.")
    if store.current() is None:
        store.select(args.name)
        print(f"Selected '{args.name}'.")
    return 0


def use(store: Store, name: str) -> int:
    home = store.require(name)
    if not signed_in(binary(), home):
        raise SwitcherError(f"No cached login for '{name}'. Run: codex-switch login {name}")
    store.select(name)
    print(f"Selected '{name}' for future launches. Running sessions keep their account.")
    return 0


def list_accounts(store: Store) -> int:
    store.initialize()
    selected = store.current()
    names = sorted(path.name for path in store.accounts.iterdir() if path.is_dir() and not path.is_symlink())
    if not names:
        print("No saved accounts. Run: codex-switch add personal")
        return 0
    executable = binary()
    for name in names:
        home = store.require(name)
        status = "login cached" if signed_in(executable, home) else "login needed"
        print(f"{'*' if name == selected else ' '} {name}  ({status})")
    return 0


def validate_forwarded(args: list[str]) -> None:
    for index, arg in enumerate(args):
        if arg == "--":
            break
        if arg in ("--remote", "--remote-auth-token-env") or arg.startswith(("--remote=", "--remote-auth-token-env=")):
            raise SwitcherError("Remote servers manage their own login; this switcher supports local Codex only.")
        value = None
        if arg in ("-c", "--config") and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--config="):
            value = arg[len("--config="):]
        elif arg.startswith("-c") and len(arg) > 2:
            value = arg[2:]
        if value and value.split("=", 1)[0].strip().strip("\"'") in ("cli_auth_credentials_store", "sqlite_home"):
            raise SwitcherError("The switcher manages credential storage and sqlite_home for account isolation.")


def run(store: Store, args: argparse.Namespace) -> int:
    from codex_history import launch_history

    name = args.account or store.current()
    if not name:
        raise SwitcherError("No account selected. Run: codex-switch add personal")
    home = store.require(name)
    forwarded = args.codex_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    validate_forwarded(forwarded)
    executable = binary()
    # Current Codex versions can reuse a shared daemon. Keep this launch local
    # so the selected home always determines authentication. Older versions
    # predate this flag and already ran locally.
    help_result = subprocess.run([executable, "--help"], capture_output=True)
    flags = ["--no-daemon"] if b"--no-daemon" in help_result.stdout else []
    print(f"Codex account: {name}", file=sys.stderr, flush=True)
    argv = command(executable, home, [*flags, *forwarded], sqlite_home=launch_history(store, home))
    if os.name == "posix":
        os.execve(executable, argv, environment(home))
    return subprocess.call(argv, env=environment(home))


def shell_init(shell: str) -> int:
    executable = shlex.quote(binary())
    python = shlex.quote(sys.executable)
    script = shlex.quote(str(Path(__file__).with_name("codex-switch").resolve()))
    print(f"# codex-switcher integration for {shell}; affects this shell only")
    print("codex() {")
    print(f'  CODEX_SWITCHER_CODEX={executable} {python} {script} run -- "$@"')
    print("}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    add_parser = commands.add_parser("add", help="Save an account using Codex's official sign-in flow")
    add_parser.add_argument("name", type=account_name)
    modes = add_parser.add_mutually_exclusive_group()
    modes.add_argument("--import-current", action="store_true", help="Copy an existing ChatGPT auth.json without logging out")
    modes.add_argument("--device-auth", action="store_true", help="Use Codex device-code login")
    add_parser.add_argument("--source-home", help="Source for settings/import (default: CODEX_HOME or ~/.codex)")
    add_parser.add_argument("--no-copy-config", action="store_true", help="Start with Codex's default settings")
    login_parser = commands.add_parser("login", help="Sign in again for a saved account")
    login_parser.add_argument("name", type=account_name)
    login_parser.add_argument("--device-auth", action="store_true")
    use_parser = commands.add_parser("use", help="Select the account for future launches")
    use_parser.add_argument("name", type=account_name)
    commands.add_parser("list", help="List accounts and locally cached login status")
    commands.add_parser("current", help="Print the selected account name")
    run_parser = commands.add_parser("run", help="Launch Codex; pass its arguments after --")
    run_parser.add_argument("--account", type=account_name, help="Use an account for this launch only")
    run_parser.add_argument("codex_args", nargs=argparse.REMAINDER)
    shell_parser = commands.add_parser("shell-init", help="Print an optional codex shell function")
    shell_parser.add_argument("shell", choices=("zsh", "bash"))
    history_parser = commands.add_parser("history", help="Inspect or share local conversation history")
    history_commands = history_parser.add_subparsers(dest="history_action")
    share_parser = history_commands.add_parser("share", help="Merge existing history; close all Codex sessions first")
    share_parser.add_argument("--source-home", help="Also import this original Codex home (default: CODEX_HOME or ~/.codex)")
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = Store()
    try:
        if args.action == "add":
            return add(store, args)
        if args.action == "login":
            return login(store, args)
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
            from codex_history import share, status

            return share(store, args.source_home) if args.history_action == "share" else status(store)
    except (SwitcherError, OSError, sqlite3.Error) as error:
        print(f"codex-switch: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0
