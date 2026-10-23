"""The Files journal: `<root>\\.premiere-deck\\journal.jsonl` (docs/FILES.md 5.2, 3.6).

Append-only (the one exception is `drop`, the clean-up of docs/FILES.md 3.2). The first line of an entry carries all its fields; later lines with the same `id` carry only the
fields that changed (usually `status`) and are folded over the entry when read, so a crash never leaves a
half-rewritten file. A line that is not a JSON object (hand edit, torn write) is skipped.

An undo is a new entry with `status: "undone"` and `undo_of` = the id of the entry it reverses; `from` / `to` and
`bin_from` / `bin_to` are swapped relative to the original.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Iterable, Optional

PENDING = "pending"
DONE = "done"
FAILED = "failed"
UNDONE = "undone"
STATUSES = (PENDING, DONE, FAILED, UNDONE)

# Operations (docs/FILES.md 3). `removed`, `setup` and `zipped` cannot be undone; `collected` and `packed` are 1.x
# ops that stay readable (a `collected` entry is undone like `filed`).
OP_FILED = "filed"
OP_SORTED = "sorted"
OP_SYNCED = "synced"
OP_ZIPPED = "zipped"
OP_SETUP = "setup"
OP_COLLECTED = "collected"
OP_BINNED = "binned"
OP_IMPORTED = "imported"
OP_RENAMED = "renamed"
OP_MOVED_DISK = "moved_disk"
OP_MOVED_BIN = "moved_bin"
OP_REMOVED = "removed"
UNDOABLE = (OP_FILED, OP_SORTED, OP_SYNCED, OP_COLLECTED, OP_BINNED, OP_IMPORTED, OP_RENAMED, OP_MOVED_DISK,
            OP_MOVED_BIN)
# Ops whose pending entry is a copy or a move of one file (recovery on project open, docs/FILES.md 5.2).
FILE_OPS = (OP_FILED, OP_SORTED, OP_SYNCED, OP_COLLECTED, OP_MOVED_BIN)

FIELDS = ("id", "at", "op", "node_id", "name", "from", "to", "bin_from", "bin_to", "hash", "copied", "status",
          "undo_of")


def new_id() -> str:
    return time.strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._order: list[str] = []
        self._loaded_size = -1
        self.skipped_lines = 0

    # -- reading
    def _load(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size == self._loaded_size:
            return
        self._entries, self._order, self.skipped_lines = {}, [], 0
        if size:
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    self._fold_line(line)
        self._loaded_size = size

    def _fold_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            self.skipped_lines += 1
            return
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            self.skipped_lines += 1
            return
        entry_id = record["id"]
        if entry_id in self._entries:
            self._entries[entry_id].update(record)
        else:
            self._entries[entry_id] = dict(record)
            self._order.append(entry_id)

    def entries(self) -> list[dict]:
        """Every entry, oldest first, folded."""
        with self._lock:
            self._load()
            return [dict(self._entries[i]) for i in self._order]

    def get(self, entry_id: str) -> Optional[dict]:
        with self._lock:
            self._load()
            entry = self._entries.get(entry_id)
            return dict(entry) if entry else None

    def pending(self) -> list[dict]:
        return [e for e in self.entries() if e.get("status") == PENDING]

    def undone_ids(self) -> set[str]:
        return {e["undo_of"] for e in self.entries() if e.get("undo_of")}

    def recent(self, count: int) -> list[dict]:
        """The newest `count` entries, newest first."""
        return list(reversed(self.entries()[-count:])) if count > 0 else []

    def undoable(self, limit: int = 50) -> list[dict]:
        """Entries that "Undo" may reverse, newest first: done, of an undoable op, not yet undone, within the last
        `limit` entries of the journal (docs/FILES.md 3.6)."""
        entries = self.entries()
        undone = {e["undo_of"] for e in entries if e.get("undo_of")}
        window = entries[-limit:] if limit > 0 else []
        return [e for e in reversed(window)
                if e.get("status") == DONE and e.get("op") in UNDOABLE and not e.get("undo_of")
                and e["id"] not in undone]

    # -- writing
    def _write(self, record: dict, sync: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        before = self._loaded_size
        with open(self.path, "ab") as handle:
            start = handle.tell()
            handle.write(data)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        if before == start:  # nobody else appended: fold our own line instead of re-reading the file
            self._fold_line(line)
            self._loaded_size = start + len(data)
        else:
            self._load()

    def append(self, op: str, *, status: str = PENDING, **fields) -> dict:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        record = {"id": new_id(), "at": round(time.time(), 3), "op": op}
        for key in FIELDS:
            if key in fields:
                record[key] = fields.pop(key)
        record.update(fields)
        record["status"] = status
        with self._lock:
            self._write(record, sync=status != PENDING)
        return dict(record)

    def update(self, entry_id: str, **changes) -> dict:
        status = changes.get("status")
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            self._load()
            if entry_id not in self._entries:
                raise KeyError(entry_id)
            self._write({"id": entry_id, **changes}, sync=status in (DONE, FAILED, UNDONE))
            return dict(self._entries[entry_id])

    def mark_done(self, entry_id: str, **changes) -> dict:
        return self.update(entry_id, status=DONE, **changes)

    def mark_failed(self, entry_id: str, error: str, **changes) -> dict:
        return self.update(entry_id, status=FAILED, error=error, **changes)

    def drop(self, entry_ids: Iterable[str]) -> int:
        """Rewrite the file once without every line of the given entries (atomic replace through core/paths).
        Other lines are kept verbatim. Returns the number of entries dropped; nothing is written when none match."""
        from premiere_deck.core.paths import atomic_write_text

        ids = set(entry_ids)
        if not ids:
            return 0
        with self._lock:
            self._load()
            present = ids & set(self._entries)
            if not present:
                return 0
            kept: list[str] = []
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    text = line.rstrip("\r\n")
                    if not text.strip():
                        continue
                    try:
                        record = json.loads(text)
                    except ValueError:
                        kept.append(text)
                        continue
                    if isinstance(record, dict) and record.get("id") in present:
                        continue
                    kept.append(text)
            atomic_write_text(self.path, "".join(line + "\n" for line in kept))
            self._loaded_size = -1
            self._load()
        return len(present)

    def module_copy_hash(self, path: str, norm) -> Optional[str]:
        """The content hash recorded when the module itself created the file at `path` (a copy it made), or None
        when no entry says the module made it. Callers compare it with the file's current hash before deleting."""
        target = norm(path)
        found: Optional[str] = None
        for entry in self.entries():
            made = ((entry.get("status") == DONE and entry.get("copied"))
                    or (entry.get("status") == UNDONE and entry.get("recreated")))
            if made and entry.get("to") and norm(entry["to"]) == target and entry.get("hash"):
                found = str(entry["hash"])
        return found


def filter_entries(entries: Iterable[dict], op: str = "", query: str = "") -> list[dict]:
    q = query.casefold().strip()
    out = []
    for entry in entries:
        if op and entry.get("op") != op and not (op == "undone" and entry.get("status") == UNDONE):
            continue
        if q and q not in str(entry.get("name", "")).casefold() and q not in str(entry.get("to", "")).casefold() \
                and q not in str(entry.get("from", "")).casefold():
            continue
        out.append(entry)
    return out
