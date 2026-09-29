"""Saved Google Antigravity CLI accounts using separate native data directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
from switcher_runtime import native_call, native_run
import sys

from codex_switcher import Store, SwitcherError, account_name, atomic_write, find_executable, private_directory


# The directory flags are currently hidden from AGY's public help. Only launch
# a build whose native token-path and SSH/file-storage behavior was inspected.
# An upstream update must be checked before adding its fingerprint here.
SUPPORTED_SHA256 = "5f9c16b286895f8f7fdecd423883ca256a85077b8acf9a6bc1111761d34df164"
SUPPORTED_SHA256S = frozenset({
    SUPPORTED_SHA256,
    # Linux 1.2.16: native directory flags and SSH/file storage inspected.
    "a759ce7c7a235d9b6c281a25ead97cbbf2e92314a3ffd224e2f9144f3fae7a86",
    # Installed Linux build: native directories, SSH/file OAuth, and history
    # writes inspected; two temporary native MCP profiles verified isolation.
    "c54ef90651a8646ae67334d39212c81f5946feec373ad6aa335f9ef401662bc5",
    # Linux 1.3.1: native CLI token paths and SSH keyring bypass inspected.
    "ce1bdaed3201bb84f35d69d2773caec4f18af52af00e8c25f6cace07e4359615",
})
# 1.3.1 binaries from Google's installer manifest. Both CPU builds retain the
# account-relative CLI token path, persistent SSH/file storage selection, and
# the native directory flags. Unknown updates remain disabled.
SUPPORTED_BUILDS = {
    "linux": SUPPORTED_SHA256S,
    "darwin": frozenset({
        "4007928d5cac45ed392fb40ee623779ef8eba7f6402a6f16f7e03611e0c35b29",
        "88db8b4d21ece4999fa58e0b54cea77154e47b319ee178d086c446262317f3fa",
    }),
    "win32": frozenset({
        "38f30c7dd1ed808f5cf98fe2014de3d30903035a4f0df02d3eb72a9ff8993741",
        "4468da63643d8c4d05110027c8c7f528ccfda4f78f2523e5adcefd3dfa8a0107",
    }),
}
APP_DIRECTORY = "antigravity-cli"
TOKEN_FILENAME = "antigravity-oauth-token"
AUTH_OVERRIDES = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_ENTERPRISE", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GEMINI_BASE_URL",
    "AGY_LLM_GATEWAY_URL", "CLOUD_CODE_URL", "AGY_CLI_CDE_AUTH_ACTION",
    "ANTIGRAVITY_VSCODE_HOST", "ANTIGRAVITY_CDE", "ANTIGRAVITY_LS_ADDRESS",
    "ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_SIDECAR_UI_TOKEN",
    "ANTIGRAVITY_SIDECAR_WEB_PORT", "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_PROJECT_ID",
)


def binary() -> str:
    found = find_executable("agy", "agy")
    if not found:
        raise SwitcherError("Antigravity CLI was not found. Install it, or set AGY_SWITCHER_AGY to its executable.")
    if Path(found).resolve() == Path(__file__).with_name("agy-switch").resolve():
        raise SwitcherError("AGY_SWITCHER_AGY must point to the real AGY executable.")
    return os.path.abspath(found)


def fingerprint(executable: str) -> str:
    digest = hashlib.sha256()
    with open(executable, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_compatible(executable: str) -> None:
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    if fingerprint(executable) not in SUPPORTED_BUILDS.get(platform, ()):
        raise SwitcherError(
            "This AGY build has not been verified for account isolation. Run 'agy-switch doctor'. "
            "Its data-directory flags and SSH token storage must be checked before enabling it."
        )


def token_path(home: Path) -> Path:
    return home / APP_DIRECTORY / TOKEN_FILENAME


def read_token(path: Path) -> bytes:
    if path.is_symlink():
        raise SwitcherError("Refusing a symlink credential cache.")
    try:
        data = path.read_bytes()
        value = json.loads(data)
    except (OSError, ValueError):
        raise SwitcherError("No readable, valid AGY file-based login cache found.") from None
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, dict) or not all(
        isinstance(token.get(key), str) and token[key]
        for key in ("access_token", "refresh_token")
    ):
        raise SwitcherError("The AGY cache does not contain a saved OAuth login.")
    return data


def cached(home: Path) -> bool:
    try:
        if not token_path(home).parent.is_dir():
            return False
        private_directory(token_path(home).parent)
        read_token(token_path(home))
        return True
    except SwitcherError:
        return False


def environment() -> dict[str, str]:
    from switcher_runtime import external_environment
    env = external_environment()
    for key in AUTH_OVERRIDES:
        env.pop(key, None)
    # AGY's native SSH detector selects file token storage for the lifetime of
    # each token-storage object. This also uses its manual URL/code sign-in
    # flow; it doesn't rewrite this shell's SSH variables or HOME.
    env["SSH_CONNECTION"] = "127.0.0.1 0 127.0.0.1 0"
    env["AGY_ADC_AUTH"] = "false"
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return env


def command(executable: str, home: Path, args: list[str]) -> list[str]:
    return [executable, f"--gemini_dir={home}", f"--app_data_dir={APP_DIRECTORY}",
            "--use_host_auth=false", *args]


def validate_forwarded(args: list[str]) -> None:
    restricted = {
        "gemini_dir", "app_data_dir", "use_host_auth", "host_bridge_url",
        "read_host_bridge_token_from_stdin", "bg-updater", "remote-control",
    }
    value_flags = {
        "add-dir", "agent", "conversation", "effort", "input-format",
        "json-schema", "log-file", "mode", "model", "output-format", "p",
        "print", "print-timeout", "project", "prompt", "i", "prompt-interactive",
    }
    skip_value = False
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and arg.lstrip("-").split("=", 1)[0] in restricted:
            raise SwitcherError("The AGY switcher manages local data paths and authentication; this flag would override them.")
        if skip_value:
            skip_value = False
            continue
        if arg.startswith("-"):
            skip_value = "=" not in arg and arg.lstrip("-") in value_flags
        elif arg in ("remote-control", "update", "install"):
            # These subcommands can reuse a global daemon or replace the
            # inspected binary, including when preceded by ordinary flags.
            raise SwitcherError("Use the native AGY command directly for installation, updates, or daemon management.")


def login(store: Store, name: str) -> int:
    from local_history import AGY, launch_history

    executable = binary()
    require_compatible(executable)
    home = store.require(name)
    private_directory(token_path(home).parent)
    if token_path(home).is_symlink():
        raise SwitcherError("Refusing a symlink credential cache.")
    launch_history(store, home, AGY)
    print(
        f"Opening AGY for '{name}'. Complete its URL/code sign-in if prompted, "
        "then type /exit to return to the switcher.", flush=True,
    )
    result = native_call(command(executable, home, []), env=environment())
    if result:
        print(f"AGY exited with an error. Retry with: agy-switch login {name}", file=sys.stderr)
        return result if result > 0 else 1
    if not cached(home):
        raise SwitcherError(f"No login was saved. Retry with: agy-switch login {name}")
    if store.current() is None:
        store.select(name)
        print(f"Saved and selected '{name}'.")
    else:
        print(f"Saved '{name}'. Select it with: agy-switch use {name}")
    return 0


def add(store: Store, args: argparse.Namespace) -> int:
    if args.source_home and not args.import_current:
        raise SwitcherError("--source-home requires --import-current for AGY.")
    executable = binary()
    require_compatible(executable)
    store.initialize()
    home = store.home(args.name)
    if home.exists() or home.is_symlink():
        raise SwitcherError(f"Account '{args.name}' already exists. Use 'login {args.name}' to open its sign-in flow.")
    source = Path(args.source_home or str(Path.home() / ".gemini")).expanduser().absolute()
    data = read_token(token_path(source)) if args.import_current else None
    home.mkdir(mode=0o700)
    if data is None:
        return login(store, args.name)
    token_path(home).parent.mkdir(mode=0o700)
    atomic_write(token_path(home), data)
    if store.current() is None:
        store.select(args.name)
        print(f"Imported and selected '{args.name}'.")
    else:
        print(f"Imported '{args.name}'. Select it with: agy-switch use {args.name}")
    return 0


def use(store: Store, name: str) -> int:
    require_compatible(binary())
    home = store.require(name)
    if not cached(home):
        raise SwitcherError(f"No saved OAuth login for '{name}'. Run: agy-switch login {name}")
    store.select(name)
    print(f"Selected '{name}' for future launches. Running sessions keep their account.")
    return 0


def list_accounts(store: Store) -> int:
    store.initialize()
    selected = store.current()
    names = sorted(path.name for path in store.accounts.iterdir() if path.is_dir() and not path.is_symlink())
    if not names:
        print("No saved accounts. Run: agy-switch add personal")
    for name in names:
        status = "OAuth login cached" if cached(store.require(name)) else "login needed"
        print(f"{'*' if name == selected else ' '} {name}  ({status})")
    return 0


def run(store: Store, args: argparse.Namespace) -> int:
    from local_history import AGY, launch_history

    name = args.account or store.current()
    if not name:
        raise SwitcherError("No account selected. Run: agy-switch add personal")
    executable = binary()
    require_compatible(executable)
    home = store.require(name)
    forwarded = args.agy_args
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    validate_forwarded(forwarded)
    if forwarded[:1] not in (["--help"], ["-h"], ["help"]) and not cached(home):
        raise SwitcherError(f"No saved OAuth login for '{name}'. Run: agy-switch login {name}")
    launch_history(store, home, AGY)
    print(f"AGY account: {name}", file=sys.stderr, flush=True)
    argv = command(executable, home, forwarded)
    if os.name != "nt":
        os.execve(executable, argv, environment())
    return native_call(argv, env=environment())


def shell_init(shell: str) -> int:
    executable = binary()
    require_compatible(executable)
    from switcher_runtime import shell_function
    print(shell_function("agy", executable, shell))
    return 0


def doctor() -> int:
    executable = binary()
    digest = fingerprint(executable)
    platform = "linux" if sys.platform.startswith("linux") else sys.platform
    supported = digest in SUPPORTED_BUILDS.get(platform, ())
    print(f"AGY executable: {executable}")
    print(f"SHA-256: {digest}")
    print("Compatibility: " + (f"inspected {platform} build" if supported else "unverified; launches disabled"))
    print("Profile mode: native data-directory flags + SSH/file-backed OAuth storage")
    return 0 if supported else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="action", required=True)
    add_parser = commands.add_parser("add", help="Save an account through AGY's normal sign-in flow")
    add_parser.add_argument("name", type=account_name)
    add_parser.add_argument("--import-current", action="store_true", help="Copy an existing file-backed OAuth login")
    add_parser.add_argument("--source-home", help="Import source Gemini directory (default: ~/.gemini)")
    for action, description in (("login", "Open a saved account to finish or renew sign-in"),
                                ("use", "Select an account for future launches")):
        subparser = commands.add_parser(action, help=description)
        subparser.add_argument("name", type=account_name)
    commands.add_parser("list", help="List accounts and locally cached login status")
    commands.add_parser("current", help="Print the selected account name")
    commands.add_parser("doctor", help="Check whether the installed AGY build is supported")
    run_parser = commands.add_parser("run", help="Launch AGY; pass its arguments after --")
    run_parser.add_argument("--account", type=account_name)
    run_parser.add_argument("agy_args", nargs=argparse.REMAINDER)
    shell_parser = commands.add_parser("shell-init", help="Print an optional agy shell function")
    shell_parser.add_argument("shell", choices=("zsh", "bash", "powershell"))
    from local_history import add_parser

    add_parser(commands)
    return result


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = Store("agy")
    try:
        if args.action == "add":
            return add(store, args)
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
        if args.action == "doctor":
            return doctor()
        if args.action == "run":
            return run(store, args)
        if args.action == "shell-init":
            return shell_init(args.shell)
        if args.action == "history":
            from local_history import AGY, share, status

            return share(store, args.source_home, AGY) if args.history_action == "share" else status(store, AGY)
    except (SwitcherError, OSError, sqlite3.Error) as error:
        print(f"agy-switch: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
