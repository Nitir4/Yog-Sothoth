"""Opt-in shared local conversation storage; credentials stay in account homes."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import tempfile

from codex_switcher import Store, SwitcherError, atomic_write, private_directory, source_home


DIRECTORIES = ("sessions", "archived_sessions", "thread-writer-locks")
FILES = ("history.jsonl", "session_index.jsonl")
# Modern Codex stores conversation items separately from the thread index.
DATABASE_PREFIXES = ("state_", "thread_history_", "goals_", "queue_")


def shared_home(store: Store) -> Path | None:
    marker = store.root / "history.json"
    if marker.is_symlink():
        raise SwitcherError("Refusing a symlink history settings file.")
    if not marker.exists():
        return None
    try:
        if json.loads(marker.read_text()) != {"mode": "shared"}:
            raise ValueError
    except (ValueError, OSError):
        raise SwitcherError("Invalid shared history settings.") from None
    home = store.root / "history"
    private_directory(home)
    return home


def link_history(home: Path, shared: Path, *, directories: tuple[str, ...] = DIRECTORIES,
                 files: tuple[str, ...] = FILES) -> None:
    """Keep backups so interrupted linking can safely be retried."""
    for name in (*directories, *files):
        path, target = home / name, shared / name
        if path.is_symlink():
            if path.resolve() != target.resolve():
                raise SwitcherError(f"Unexpected history symlink: {path}")
            continue
        if path.exists():
            backup = home / ".history-backup"
            private_directory(backup)
            if (backup / name).exists():
                raise SwitcherError(f"History backup already exists for {path}; refusing to overwrite it.")
            path.rename(backup / name)
        path.symlink_to(target, target_is_directory=name in directories)


def launch_history(store: Store, home: Path) -> Path:
    shared = shared_home(store)
    if shared is not None:
        link_history(home, shared)
        return shared
    return home


def copy_transcripts(source: Path, destination: Path) -> None:
    for name in ("sessions", "archived_sessions"):
        root = source / name
        if root.is_symlink():
            raise SwitcherError(f"Refusing to import a symlink history directory: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise SwitcherError(f"Refusing to import a symlink transcript: {path}")
            if not path.is_file():
                continue
            target = destination / name / path.relative_to(root)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = path.read_bytes()
            if target.exists() and target.read_bytes() != data:
                raise SwitcherError(f"Conflicting saved transcript: {path.name}. Original history is untouched.")
            if not target.exists():
                atomic_write(target, data)


def merge_lines(sources: list[Path], destination: Path, name: str) -> None:
    records: dict[bytes, tuple[bytes, int | float | str]] = {}
    for source in sources:
        path = source / name
        if path.is_symlink():
            raise SwitcherError(f"Refusing to import a symlink history file: {path}")
        if not path.exists():
            continue
        for line in path.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError
                key = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
                order = record.get("ts", 0) if name == "history.jsonl" else record.get("updated_at", "")
                if name == "history.jsonl" and not isinstance(order, (int, float)):
                    raise ValueError
                if name == "session_index.jsonl" and not isinstance(order, str):
                    raise ValueError
            except ValueError:
                raise SwitcherError(f"Invalid JSON in {path}; original history is untouched.") from None
            records[key] = (line, order)
    # Retain each distinct index entry: Codex resolves the latest name update.
    atomic_write(destination / name, b"".join(line + b"\n" for line, _ in sorted(records.values(), key=lambda item: item[1])))


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def merge_database(source: Path, destination: Path, source_home_path: Path, final: Path) -> None:
    """Merge equal-schema databases, including WAL contents, without touching sources."""
    if source.is_symlink():
        raise SwitcherError(f"Refusing to import a symlink database: {source}")
    with tempfile.TemporaryDirectory(prefix="codex-history-db-") as temporary:
        snapshot = Path(temporary) / source.name
        shutil.copyfile(source, snapshot)
        wal = Path(str(source) + "-wal")
        if wal.exists():
            if wal.is_symlink():
                raise SwitcherError(f"Refusing a symlink database journal: {wal}")
            shutil.copyfile(wal, Path(str(snapshot) + "-wal"))
        incoming = sqlite3.connect(snapshot)
        output = sqlite3.connect(destination)
        try:
            if not output.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                incoming.backup(output)
                if source.name.startswith("state_"):
                    rewrite_rollout_paths(output, source_home_path, final)
                output.commit()
                return
            incoming_tables = dict(incoming.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            existing_tables = dict(output.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            if incoming_tables != existing_tables:
                raise SwitcherError(f"Different Codex database schemas in {source}; run the same Codex version on each home first.")
            for table in incoming_tables:
                if table == "_sqlx_migrations":
                    if incoming.execute('SELECT version, checksum, success FROM _sqlx_migrations ORDER BY version').fetchall() != output.execute('SELECT version, checksum, success FROM _sqlx_migrations ORDER BY version').fetchall():
                        raise SwitcherError(f"Different Codex database migrations in {source}.")
                    continue
                quoted = quote_identifier(table)
                columns = [row[1] for row in incoming.execute(f"PRAGMA table_info({quoted})")]
                names = ", ".join(map(quote_identifier, columns))
                placeholders = ", ".join("?" for _ in columns)
                for row in incoming.execute(f"SELECT {names} FROM {quoted}"):
                    values = list(row)
                    if table == "threads" and "rollout_path" in columns:
                        index = columns.index("rollout_path")
                        values[index] = remap_path(values[index], source_home_path, final)
                    output.execute(f"INSERT INTO {quoted} ({names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING", values)
            output.commit()
        finally:
            incoming.close()
            output.close()


def remap_path(value: str, source: Path, final: Path) -> str:
    try:
        relative = Path(value).relative_to(source)
    except ValueError:
        return value
    if relative.parts and relative.parts[0] in ("sessions", "archived_sessions"):
        return str(final / relative)
    return value


def rewrite_rollout_paths(connection: sqlite3.Connection, source: Path, final: Path) -> None:
    for thread_id, path in connection.execute("SELECT id, rollout_path FROM threads").fetchall():
        connection.execute("UPDATE threads SET rollout_path=? WHERE id=?", (remap_path(path, source, final), thread_id))


def share(store: Store, source: str | None) -> int:
    store.initialize()
    homes = [store.require(p.name) for p in sorted(store.accounts.iterdir()) if p.is_dir() and not p.is_symlink()]
    enabled = shared_home(store)
    if enabled is not None:
        for home in homes:
            link_history(home, enabled)
        print(f"Shared history is already enabled: {enabled}")
        return 0
    original = source_home(source)
    sources = list(dict.fromkeys([original, *homes]))
    final = store.root / "history"
    if final.exists() or final.is_symlink():
        raise SwitcherError(f"Unfinished history migration at {final}; refusing to overwrite it.")
    # Validate destinations before publishing anything.
    for home in homes:
        if (home / ".history-backup").exists():
            raise SwitcherError(f"History backup already exists in {home}.")
        for name in (*DIRECTORIES, *FILES):
            if (home / name).is_symlink():
                raise SwitcherError(f"Unexpected history symlink: {home / name}")
    with tempfile.TemporaryDirectory(prefix=".history-import-", dir=store.root) as temporary:
        staging = Path(temporary) / "history"
        private_directory(staging)
        for name in DIRECTORIES:
            private_directory(staging / name)
        for home in sources:
            if home.is_symlink():
                raise SwitcherError(f"Refusing a symlink source home: {home}")
            copy_transcripts(home, staging)
            for database in sorted(home.glob("*.sqlite")):
                if database.name.startswith(DATABASE_PREFIXES):
                    merge_database(database, staging / database.name, home, final)
        for name in FILES:
            merge_lines(sources, staging, name)
        staging.rename(final)
    # Publishing the marker first makes interruption during linking retryable.
    atomic_write(store.root / "history.json", b'{"mode":"shared"}\n')
    for home in homes:
        link_history(home, final)
    print(f"Shared history enabled: {final}")
    print("Existing account history is backed up in each account's .history-backup directory.")
    print("Future codex-switch launches share conversations while keeping their own login.")
    return 0


def status(store: Store) -> int:
    shared = shared_home(store)
    print("History: shared" if shared is not None else "History: per account")
    homes = [shared] if shared else list(dict.fromkeys([Path.home() / ".codex", source_home(None), *sorted(store.accounts.glob("*"))]))
    for home in homes:
        if not home.is_dir() or home.is_symlink():
            continue
        count = sum(1 for name in ("sessions", "archived_sessions") for _ in (home / name).rglob("*.jsonl"))
        print(f"{home}: {count} saved conversation files")
    return 0
