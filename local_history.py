"""Shared Claude Code and Antigravity history, with account-local authentication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from codex_history import link_history, merge_database, shared_home
from codex_switcher import Store, SwitcherError, atomic_write, private_directory


@dataclass(frozen=True)
class Layout:
    tool: str
    directories: tuple[str, ...]
    files: tuple[str, ...]
    timestamp: str
    app_directory: str = ""

    def home(self, root: Path) -> Path:
        return root / self.app_directory if self.app_directory else root

    def source(self, value: str | None) -> Path:
        default = (os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
                   if self.tool == "claude" else str(Path.home() / ".gemini"))
        return Path(value or default).expanduser().absolute()


CLAUDE = Layout("claude", ("projects", "file-history", "plans", "todos"),
                ("history.jsonl",), "timestamp")
AGY = Layout("agy", ("conversations", "brain", "annotations"),
             ("history.jsonl", "conversation_summaries.db"), "timestamp", "antigravity-cli")
CACHE_FILES = ("last_conversations.json", "conversation_metadata.json")


def time_order(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return 0


def account_homes(store: Store, layout: Layout) -> list[Path]:
    return [layout.home(store.require(p.name)) for p in sorted(store.accounts.glob("*"))
            if p.is_dir() and not p.is_symlink()]


def database_snapshot(source: Path, destination: Path) -> None:
    """Include committed WAL data and produce a self-contained SQLite file."""
    with tempfile.TemporaryDirectory(prefix="history-snapshot-") as temporary:
        copy = Path(temporary) / source.name
        shutil.copyfile(source, copy)
        wal = Path(str(source) + "-wal")
        if wal.is_symlink():
            raise SwitcherError(f"Refusing a symlink database journal: {wal}")
        if wal.exists():
            shutil.copyfile(wal, Path(str(copy) + "-wal"))
        incoming = sqlite3.connect(copy)
        output = sqlite3.connect(destination)
        try:
            incoming.backup(output)
        finally:
            incoming.close()
            output.close()


def merge_claude_index(source: Path, destination: Path, home: Path, final: Path) -> None:
    incoming = read_map(source)
    existing = read_map(destination)
    if existing and incoming.get("version") != existing.get("version"):
        raise SwitcherError(f"Different Claude session index versions in {source}")
    entries: dict[str, dict] = {}
    for index in (existing, incoming):
        rows = index.get("entries", [])
        if not isinstance(rows, list):
            raise SwitcherError(f"Invalid Claude session index: {source}")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("sessionId"), str):
                raise SwitcherError(f"Invalid Claude session index: {source}")
            value = row.copy()
            path = value.get("fullPath")
            if isinstance(path, str):
                try:
                    value["fullPath"] = str(final / Path(path).relative_to(home))
                except ValueError:
                    pass
            previous = entries.get(value["sessionId"])
            if previous is None or str(value.get("modified", "")) >= str(previous.get("modified", "")):
                entries[value["sessionId"]] = value
    result = dict(incoming)
    result["entries"] = list(entries.values())
    atomic_write(destination, json.dumps(result).encode() + b"\n")


def copy_directory(source: Path, destination: Path, *, claude_home: Path | None = None,
                   final: Path | None = None, sqlite_files: bool = False) -> None:
    if source.is_symlink():
        raise SwitcherError(f"Refusing a symlink history directory: {source}")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise SwitcherError(f"Refusing a symlink history entry: {path}")
        target = destination / path.relative_to(source)
        if path.is_dir():
            private_directory(target)
            continue
        if not path.is_file() or (sqlite_files and path.name.endswith((".db-wal", ".db-shm"))):
            continue
        private_directory(target.parent)
        if path.name == "sessions-index.json" and claude_home is not None:
            merge_claude_index(path, target, claude_home, final)
        elif sqlite_files and path.suffix == ".db":
            with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
                snapshot = Path(temporary) / path.name
                database_snapshot(path, snapshot)
                copy_file(snapshot, target)
        else:
            copy_file(path, target)


def copy_file(source: Path, destination: Path) -> None:
    data = source.read_bytes()
    if destination.exists():
        if destination.read_bytes() != data:
            raise SwitcherError(f"Conflicting history file: {destination.name}. Original history is untouched.")
    else:
        atomic_write(destination, data)


def merge_prompt_history(sources: list[Path], destination: Path, timestamp: str) -> None:
    records: dict[str, dict] = {}
    for home in sources:
        path = home / "history.jsonl"
        if path.is_symlink():
            raise SwitcherError(f"Refusing a symlink history file: {path}")
        if not path.exists():
            continue
        try:
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get(timestamp, 0), (int, float)):
                    raise ValueError
                records[json.dumps(value, sort_keys=True)] = value
        except (ValueError, UnicodeError):
            raise SwitcherError(f"Invalid prompt history: {path}. Original history is untouched.") from None
    ordered = sorted(records.values(), key=lambda entry: entry.get(timestamp, 0))
    atomic_write(destination / "history.jsonl", b"".join(json.dumps(record).encode() + b"\n" for record in ordered))


def merge_agy_summaries(source: Path, destination: Path, home: Path, final: Path) -> None:
    if source.is_symlink():
        raise SwitcherError(f"Refusing a symlink summary database: {source}")
    with tempfile.TemporaryDirectory(prefix="history-summaries-") as temporary:
        snapshot = Path(temporary) / source.name
        database_snapshot(source, snapshot)
        with sqlite3.connect(snapshot) as connection:
            # Older, unused profiles can have an older empty GORM schema.
            # They have no history to import and must not block migration.
            exists = connection.execute("SELECT name FROM sqlite_master WHERE name='conversation_summaries'").fetchone()
            if exists is None or not connection.execute("SELECT 1 FROM conversation_summaries LIMIT 1").fetchone():
                return
        merge_database(snapshot, destination, home, final)


def read_map(path: Path) -> dict:
    if path.is_symlink():
        raise SwitcherError(f"Refusing a symlink resume cache: {path}")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise SwitcherError(f"Invalid resume cache: {path}") from None


def merge_agy_caches(sources: list[Path], destination: Path, final: Path) -> None:
    """Cache JSON is atomically replaced by AGY; synchronize regular files at launch."""
    metadata: dict[str, dict] = {}
    last_candidates: dict[str, list[tuple[str, int]]] = {}
    for home in sources:
        cache = home / "cache"
        if cache.is_symlink():
            raise SwitcherError(f"Refusing a symlink cache directory: {cache}")
        entries = read_map(cache / "conversation_metadata.json").get("conversations", {})
        if not isinstance(entries, dict):
            raise SwitcherError(f"Invalid conversation metadata in {cache}")
        for conversation_id, value in entries.items():
            if not isinstance(value, dict) or not isinstance(value.get("last_modified_time", ""), str):
                raise SwitcherError(f"Invalid conversation metadata in {cache}")
            previous = metadata.get(conversation_id)
            if previous is None or time_order(value.get("last_modified_time", "")) > time_order(previous.get("last_modified_time", "")):
                metadata[conversation_id] = value
        path = cache / "last_conversations.json"
        last = read_map(path)
        for workspace, conversation_id in last.items():
            if not isinstance(conversation_id, str):
                raise SwitcherError(f"Invalid last-conversation cache: {path}")
            last_candidates.setdefault(workspace, []).append((conversation_id, path.stat().st_mtime_ns))
    # The database is shared during use, and gives recency even if a CLI cache
    # has not yet saved its metadata for a newly created conversation.
    recency = {key: time_order(value.get("last_modified_time", "")) for key, value in metadata.items()}
    database = destination / "conversation_summaries.db"
    if database.exists():
        connection = sqlite3.connect(database)
        try:
            if connection.execute("SELECT name FROM sqlite_master WHERE name='conversation_summaries'").fetchone():
                columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation_summaries)")}
                if {"conversation_id", "last_modified_time"} <= columns:
                    for key, value in connection.execute("SELECT conversation_id, last_modified_time FROM conversation_summaries"):
                        recency[key] = max(recency.get(key, 0), time_order(str(value or "")))
        finally:
            connection.close()
    for value in metadata.values():
        summary = value.get("summary")
        if isinstance(summary, dict):
            for key in ("app_data_dir", "appDataDir"):
                if key in summary:
                    summary[key] = str(final)
    last = {workspace: max(candidates, key=lambda item: (recency.get(item[0], 0), item[1]))[0]
            for workspace, candidates in last_candidates.items()}
    private_directory(destination / "cache")
    atomic_write(destination / "cache" / "conversation_metadata.json", json.dumps({"conversations": metadata}).encode() + b"\n")
    atomic_write(destination / "cache" / "last_conversations.json", json.dumps(last).encode() + b"\n")


def launch_history(store: Store, root: Path, layout: Layout) -> None:
    shared = shared_home(store)
    if shared is None:
        return
    home = layout.home(root)
    private_directory(home)
    link_history(home, shared, directories=layout.directories, files=layout.files)
    if layout.tool == "agy":
        merge_agy_caches([shared, *account_homes(store, layout)], shared, shared)
        cache = home / "cache"
        private_directory(cache)
        for name in CACHE_FILES:
            atomic_write(cache / name, (shared / "cache" / name).read_bytes())


def share(store: Store, source: str | None, layout: Layout) -> int:
    store.initialize()
    from switcher_runtime import check_history_links
    check_history_links(store.root)
    homes = account_homes(store, layout)
    if shared_home(store) is not None:
        for path in sorted(store.accounts.glob("*")):
            if path.is_dir() and not path.is_symlink():
                launch_history(store, store.require(path.name), layout)
        print(f"Shared history is already enabled: {store.root / 'history'}")
        return 0
    original = layout.source(source)
    if original.is_symlink():
        raise SwitcherError(f"Refusing a symlink source home: {original}")
    sources = list(dict.fromkeys([layout.home(original), *homes]))
    final = store.root / "history"
    if final.exists() or final.is_symlink():
        raise SwitcherError(f"Unfinished migration at {final}; refusing to overwrite it.")
    for home in homes:
        private_directory(home)
        if (home / ".history-backup").exists() or (home / ".history-backup").is_symlink():
            raise SwitcherError(f"History backup already exists in {home}.")
        for name in (*layout.directories, *layout.files):
            if (home / name).is_symlink():
                raise SwitcherError(f"Unexpected history symlink: {home / name}")
    with tempfile.TemporaryDirectory(prefix=".history-import-", dir=store.root) as temporary:
        staging = Path(temporary) / "history"
        private_directory(staging)
        for name in layout.directories:
            private_directory(staging / name)
        for home in sources:
            if home.is_symlink():
                raise SwitcherError(f"Refusing a symlink history home: {home}")
            for name in layout.directories:
                copy_directory(home / name, staging / name,
                               claude_home=home if layout.tool == "claude" else None, final=final,
                               sqlite_files=layout.tool == "agy" and name == "conversations")
            if layout.tool == "agy":
                database = home / "conversation_summaries.db"
                if database.exists():
                    merge_agy_summaries(database, staging / database.name, home, final)
        merge_prompt_history(sources, staging, layout.timestamp)
        if layout.tool == "agy":
            database = staging / "conversation_summaries.db"
            if database.exists():
                with sqlite3.connect(database) as connection:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(conversation_summaries)")}
                    if "app_data_dir" in columns:
                        connection.execute("UPDATE conversation_summaries SET app_data_dir=?", (str(final),))
            else:
                sqlite3.connect(database).close()
            merge_agy_caches(sources, staging, final)
        staging.rename(final)
    atomic_write(store.root / "history.json", b'{"mode":"shared"}\n')
    if layout.tool == "agy":
        for home in homes:
            # These files stay regular: native AGY replaces them on each save.
            for name in CACHE_FILES:
                original_cache = home / "cache" / name
                if original_cache.exists():
                    backup = home / ".history-backup" / "cache"
                    private_directory(backup)
                    atomic_write(backup / name, original_cache.read_bytes())
    for path in sorted(store.accounts.glob("*")):
        if path.is_dir() and not path.is_symlink():
            launch_history(store, store.require(path.name), layout)
    print(f"Shared history enabled: {final}")
    print("Existing account history is backed up in each native home's .history-backup directory.")
    print(f"Future {store.program} launches share history while keeping their own login and settings.")
    return 0


def status(store: Store, layout: Layout) -> int:
    shared = shared_home(store)
    print("History: shared" if shared else "History: per account")
    default = Path.home() / (".claude" if layout.tool == "claude" else ".gemini")
    homes = [shared] if shared else list(dict.fromkeys([layout.home(default), layout.home(layout.source(None)),
                                                       *[layout.home(p) for p in sorted(store.accounts.glob("*")) if p.is_dir() and not p.is_symlink()]]))
    for home in homes:
        if not home.is_dir() or home.is_symlink():
            continue
        if layout.tool == "claude":
            count = sum(1 for _ in (home / "projects").rglob("*.jsonl"))
            unit = "saved transcript files"
        else:
            count = sum(1 for p in (home / "conversations").glob("*") if p.is_file() and p.suffix in (".db", ".pb"))
            unit = "saved conversation files"
        print(f"{home}: {count} {unit}")

    return 0


def add_parser(commands) -> None:
    parser = commands.add_parser("history", help="Inspect or share local conversation history")
    actions = parser.add_subparsers(dest="history_action")
    migration = actions.add_parser("share", help="Merge history; close all sessions for this tool first")
    migration.add_argument("--source-home", help="Original native home to also import (~/.claude or ~/.gemini by default)")
