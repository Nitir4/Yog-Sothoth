"""Read-only manager data and argv-based launch plans for the native adapters."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import lru_cache
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlparse

from codex_switcher import Store, SwitcherError, account_name, read_auth
from switcher_runtime import cli_command

TOOLS = {"codex": "Codex", "claude": "Claude Code", "agy": "Antigravity"}
PROJECT = Path(__file__).resolve().parent


def adapter(tool: str):
    if tool not in TOOLS:
        raise SwitcherError(f"Unknown tool: {tool}")
    return importlib.import_module(f"{tool}_switcher")


def store_for(tool: str) -> Store:
    """Honor explicit stores; recover the existing repo-local AGY setup."""
    adapter(tool)
    store = Store(tool)
    local = PROJECT / ".agy-switcher"
    if (tool == "agy" and "AGY_SWITCHER_HOME" not in os.environ
            and not any(store.accounts.glob("*")) and any((local / "accounts").glob("*"))):
        store.root = local
        store.accounts = local / "accounts"
        store.selection = local / "selected.json"
    return store


def manager_environment(*, external: bool = False) -> dict[str, str]:
    from switcher_runtime import external_environment
    env = external_environment() if external else os.environ.copy()
    for tool in TOOLS:
        env[f"{tool.upper()}_SWITCHER_HOME"] = str(store_for(tool).root)
    return env


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def clean(value, limit=240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())[:limit]


def timestamp(value) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value) / (1000 if value > 10**11 else 1)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, TypeError):
        return 0


@dataclass
class Account:
    name: str
    login: str
    selected: bool = False


@dataclass
class ToolState:
    tool: str
    label: str
    root: str
    installed: bool = False
    available: bool = False
    shared: bool = False
    selected: str | None = None
    accounts: list[Account] = field(default_factory=list)
    error: str = ""


@lru_cache(maxsize=8)
def agy_compatible(executable: str, size: int, modified: int) -> bool:
    # Cache only against the actual executable identity; a new build is checked again.
    adapter("agy").require_compatible(executable)
    return True


def tool_state(tool: str) -> ToolState:
    store = store_for(tool)
    result = ToolState(tool, TOOLS[tool], str(store.root))
    try:
        if store.root.is_symlink() or store.accounts.is_symlink():
            raise SwitcherError("Refusing a symlink account store.")
        result.selected = store.current()
        marker = store.root / "history.json"
        if marker.is_symlink():
            raise SwitcherError("Refusing a symlink history marker.")
        if marker.exists():
            if (read_json(marker) != {"mode": "shared"} or not (store.root / "history").is_dir()
                    or (store.root / "history").is_symlink()):
                raise SwitcherError("Invalid shared history store. Check the tool's history command.")
            result.shared = True
        for home in sorted(store.accounts.glob("*")):
            if not home.is_dir() or home.is_symlink():
                continue
            try:
                account_name(home.name)
            except argparse.ArgumentTypeError:
                continue
            login = "Sign in needed"
            try:
                if tool == "codex":
                    read_auth(home / "auth.json")
                    login = "Login cached"
                elif tool == "agy":
                    adapter(tool).read_token(adapter(tool).token_path(home))
                    login = "Login cached"
                else:
                    # macOS may keep Claude credentials in a keychain. Checking the
                    # native CLI is deferred to launch; never guess that it is logged out.
                    credentials = home / ".credentials.json"
                    value = read_json(credentials) if not credentials.is_symlink() else None
                    oauth = value.get("claudeAiOauth") if isinstance(value, dict) else None
                    login = "Login cached" if isinstance(oauth, dict) and oauth.get("accessToken") else "Checked on launch"
            except SwitcherError:
                pass
            result.accounts.append(Account(home.name, login, home.name == result.selected))
        executable = adapter(tool).binary()
        result.installed = True
        if tool == "agy":
            info = Path(executable).stat()
            agy_compatible(executable, info.st_size, info.st_mtime_ns)
        result.available = True
    except (SwitcherError, OSError) as exc:
        result.error = str(exc)
    return result


def states() -> list[ToolState]:
    return [tool_state(tool) for tool in TOOLS]


@dataclass
class Conversation:
    tool: str
    id: str
    title: str
    project: str
    updated: float
    accounts: list[str] = field(default_factory=list)
    shared: bool = False


def json_lines(path: Path, *, max_bytes: int | None = None):
    """Stream bounded lines, tolerating partial native writes."""
    try:
        with path.open("rb") as handle:
            remaining = max_bytes
            while remaining is None or remaining > 0:
                line = handle.readline(min(1024 * 1024, remaining) if remaining else 1024 * 1024)
                if not line:
                    break
                if remaining is not None:
                    remaining -= len(line)
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except (ValueError, UnicodeError):
                    continue
    except OSError:
        return


def database_rows(path: Path, table: str) -> list[dict]:
    """Read a private copy, including WAL; never open the user's database writable."""
    if not path.is_file() or path.is_symlink():
        return []
    with tempfile.TemporaryDirectory(prefix="switcher-read-") as temporary:
        copy = Path(temporary) / path.name
        shutil.copyfile(path, copy)
        wal = Path(str(path) + "-wal")
        if wal.is_symlink():
            raise SwitcherError("Refusing a symlink database journal.")
        if wal.is_file():
            shutil.copyfile(wal, str(copy) + "-wal")
        with closing(sqlite3.connect(copy, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                return []
            # Table is a constant supplied by the readers below.
            return [dict(row) for row in db.execute(f'SELECT * FROM "{table}"')]


def codex_records(root: Path):
    found = {}
    names = {r.get("id"): r for r in json_lines(root / "session_index.jsonl")}
    for database in sorted(root.glob("state_*.sqlite"), reverse=True):
        for row in database_rows(database, "threads"):
            key = row.get("id")
            if not isinstance(key, str) or key in found:
                continue
            index = names.get(key, {})
            found[key] = (key, clean(index.get("thread_name") or row.get("title") or row.get("first_user_message")) or key,
                          str(row.get("cwd") or ""), timestamp(row.get("updated_at_ms") or row.get("updated_at")))
    for folder in ("sessions", "archived_sessions"):
        for path in (root / folder).rglob("*.jsonl"):
            rows = list(json_lines(path, max_bytes=65536))
            meta = next((r.get("payload", {}) for r in rows if r.get("type") == "session_meta"), {})
            key = meta.get("id") or (rows[0].get("id") if rows else None)
            if not isinstance(key, str) or key in found:
                continue
            first = next((r.get("payload", {}).get("message") for r in rows
                          if r.get("type") == "event_msg" and r.get("payload", {}).get("type") == "user_message"), "")
            index = names.get(key, {})
            found[key] = (key, clean(index.get("thread_name") or first) or key,
                          str(meta.get("cwd") or ""), timestamp(index.get("updated_at")) or path.stat().st_mtime)
    return list(found.values())


def claude_records(root: Path):
    found = {}
    for index in (root / "projects").glob("*/sessions-index.json"):
        value = read_json(index)
        entries = value.get("entries", []) if isinstance(value, dict) else []
        if not isinstance(entries, list):
            continue
        for row in entries:
            if not isinstance(row, dict) or not isinstance(row.get("sessionId"), str):
                continue
            key = row["sessionId"]
            found[key] = (key, clean(row.get("summary") or row.get("firstPrompt")) or key,
                          str(row.get("projectPath") or ""), timestamp(row.get("modified")))
    # Recent Claude versions don't always write sessions-index.json. Avoid
    # subagent transcripts: only top-level project/*.jsonl is resumable here.
    for path in (root / "projects").glob("*/*.jsonl"):
        key = path.stem
        if key in found:
            continue
        rows = list(json_lines(path, max_bytes=65536))
        first = next((r for r in rows if r.get("type") == "user" and not r.get("isMeta")), {})
        content = first.get("message", {})
        content = content.get("content", "") if isinstance(content, dict) else ""
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        project = next((r["cwd"] for r in rows if isinstance(r.get("cwd"), str)), "")
        found[key] = (key, clean(content) or key, project, path.stat().st_mtime)
    return list(found.values())


def workspace(value) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [value]
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        return ""
    uri = urlparse(value[0])
    return unquote(uri.path) if uri.scheme == "file" and uri.netloc in ("", "localhost") else value[0]


def agy_records(root: Path):
    found = {}
    metadata = read_json(root / "cache" / "conversation_metadata.json") or {}
    metadata = metadata.get("conversations", {}) if isinstance(metadata, dict) else {}
    last = read_json(root / "cache" / "last_conversations.json") or {}
    projects = {v: k for k, v in last.items() if isinstance(v, str)} if isinstance(last, dict) else {}
    for row in database_rows(root / "conversation_summaries.db", "conversation_summaries"):
        key = row.get("conversation_id")
        if isinstance(key, str):
            found[key] = (key, clean(row.get("title") or row.get("preview")) or key,
                          workspace(row.get("workspace_uris")) or projects.get(key, ""), timestamp(row.get("last_modified_time")))
    for path in (root / "conversations").glob("*.db"):
        key = path.stem
        if key in found:
            continue
        value = metadata.get(key, {}) if isinstance(metadata, dict) else {}
        summary = value.get("summary", {}) if isinstance(value, dict) else {}
        summary = summary if isinstance(summary, dict) else {}
        found[key] = (key, clean(summary.get("title")) or key,
                      workspace(summary.get("workspace_uris")) or projects.get(key, ""), path.stat().st_mtime)
    return list(found.values())


def conversations(snapshot: list[ToolState] | None = None, *, tool: str | None = None,
                  project: str = "", query: str = "", limit: int = 500) -> tuple[list[Conversation], list[str]]:
    found: dict[tuple[str, str], Conversation] = {}
    errors = []
    for state in snapshot if snapshot is not None else states():
        if tool and tool != state.tool:
            continue
        store = store_for(state.tool)
        roots = [(store.root / "history", None, True)] if state.shared else []
        roots.extend(((store.home(a.name) / "antigravity-cli" if state.tool == "agy" else store.home(a.name)),
                      a.name, False) for a in state.accounts)
        reader = {"codex": codex_records, "claude": claude_records, "agy": agy_records}[state.tool]
        for root, owner, shared in roots:
            if state.shared and owner is not None:
                # Accounts are linked to shared storage on launch; skip duplicate
                # paths, but include a newly added account's unlinked local files.
                anchor = root / ("sessions" if state.tool == "codex" else "projects" if state.tool == "claude" else "conversations")
                if anchor.is_symlink() and anchor.resolve().parent == (store.root / "history").resolve():
                    continue
            try:
                for key, title, cwd, updated in reader(root):
                    index = (state.tool, key)
                    previous = found.get(index)
                    owners = [owner] if owner else []
                    if previous:
                        owners = sorted(set(owners + previous.accounts))
                        if previous.updated > updated:
                            title, cwd, updated = previous.title, previous.project, previous.updated
                    found[index] = Conversation(state.tool, key, title, cwd, updated, owners, shared or bool(previous and previous.shared))
            except (OSError, sqlite3.Error, SwitcherError, ValueError, TypeError) as exc:
                errors.append(f"{state.label}: could not read {root}: {exc}")
    records = [r for r in found.values() if project.casefold() in r.project.casefold()
               and query.casefold() in f"{r.title} {r.id} {r.project}".casefold()]
    return sorted(records, key=lambda r: (-r.updated, r.tool, r.id))[:limit], errors


def launch_arguments(tool: str, account: str, record: Conversation | None = None) -> list[str]:
    try:
        account_name(account)
    except argparse.ArgumentTypeError as exc:
        raise SwitcherError(str(exc)) from None
    adapter(tool)
    store = store_for(tool)
    if not store.home(account).is_dir() or store.home(account).is_symlink():
        raise SwitcherError(f"Unknown account '{account}'.")
    args = [tool, "run", "--account", account, "--"]
    if record:
        if record.tool != tool:
            raise SwitcherError("Select an account for the conversation's tool.")
        if not record.shared and account not in record.accounts:
            raise SwitcherError("Share this tool's history first to resume with a different account.")
        if not record.id or record.id.startswith("-"):
            raise SwitcherError("Invalid conversation ID.")
        args += {"codex": ["resume", record.id], "claude": ["--resume", record.id],
                 "agy": ["--conversation", record.id]}[tool]
    return args


def terminal_command(arguments: list[str], cwd: Path, *, lookup=shutil.which) -> list[str]:
    cwd = cwd.expanduser().absolute()
    if not cwd.is_dir():
        raise SwitcherError(f"Project directory does not exist: {cwd}")
    payload = cli_command(["_terminal", "--cwd", str(cwd), *arguments])
    override = os.environ.get("SWITCHER_TERMINAL")
    if override:
        try:
            prefix = shlex.split(override)
        except ValueError:
            raise SwitcherError("Invalid SWITCHER_TERMINAL quoting.") from None
        if not prefix or not lookup(prefix[0]):
            raise SwitcherError("SWITCHER_TERMINAL executable was not found.")
        return [*prefix, *payload]
    for name, flags in [("kitty", ["--title", "Switcher", "--"]), ("gnome-terminal", ["--"]),
                        ("konsole", ["-e"]), ("xfce4-terminal", ["--disable-server", "-x"]),
                        ("wezterm", ["start", "--"]), ("xterm", ["-e"])]:
        if executable := lookup(name):
            return [executable, *flags, *payload]
    raise SwitcherError("No supported terminal found. Set SWITCHER_TERMINAL to a command prefix, e.g. 'kitty --'.")


def open_terminal(arguments: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(terminal_command(arguments, cwd), env=manager_environment(external=True), start_new_session=True)


def as_data(value):
    return asdict(value)
