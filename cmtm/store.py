"""Thread-safe application-owned storage; never a terminal lifecycle authority.

Writes are explicit. Drafts, completion and vocabulary reads have no side effects.
Project/session renames require a separate, successful daemon orchestration step.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


_APPLICATION_ID = 0x434D544D
_SCHEMA_VERSION = 3
_RESERVED_PROJECTS = {"no-project", "mtm-only"}
_AUTHORITATIVE_FIELDS = {
    "state", "freshness", "harness", "model", "effort", "speed", "server",
    "host", "session", "window", "window_id", "kind",
}
_PROVENANCE_FIELDS = {
    "source", "source_server", "source_host", "source_session", "source_window",
    "source_window_id", "server", "host", "session", "window", "window_id",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _name(value, what):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be nonempty text")
    return value.strip()


class Store:
    """One serialized SQLite connection, safe to share with UI I/O workers.

    ``save`` merges supplied fields on update, preserving omitted provenance.
    An explicit null field value clears that field (except immutable provenance).
    Live records are optional metadata associations, not proof of a live process.
    """

    def __init__(self, path):
        self.path = Path(path).expanduser()
        if str(path) == ":memory:":
            raise ValueError("Use an explicitly isolated database file, not ephemeral storage")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            application_id = self._db.execute("PRAGMA application_id").fetchone()[0]
            tables = self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if application_id not in (0, _APPLICATION_ID) or (application_id == 0 and tables):
                raise ValueError("Refusing database not identified as clean-room cmtm storage")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._migrate()
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            if not existed:
                os.chmod(self.path, 0o600)
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _migrate(self):
        with self._transaction():
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise ValueError(f"Database schema {version} is newer than supported {_SCHEMA_VERSION}")
            if version < 1:
                self._db.execute("""CREATE TABLE records (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
                    label_key TEXT NOT NULL, fields_json TEXT NOT NULL DEFAULT '{}',
                    project_id TEXT REFERENCES records(id) ON DELETE RESTRICT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    CHECK(kind != 'project' OR project_id IS NULL))""")
                self._db.execute("CREATE UNIQUE INDEX project_names ON records(label_key) WHERE kind='project'")
                self._db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)")
                self._db.execute("PRAGMA user_version=1")
            if version < 2:
                self._db.execute("""CREATE TABLE extensible_fields (
                    kind TEXT NOT NULL, field TEXT NOT NULL, PRIMARY KEY(kind,field))""")
                self._db.execute("""CREATE TABLE vocabulary (
                    kind TEXT NOT NULL, field TEXT NOT NULL, value_key TEXT NOT NULL,
                    value TEXT NOT NULL, PRIMARY KEY(kind,field,value_key))""")
                self._db.executemany("INSERT INTO extensible_fields(kind,field) VALUES('*',?)",
                                     [(name,) for name in ("tags", "type", "status")])
                for row in self._db.execute("SELECT kind,fields_json FROM records").fetchall():
                    self._persist_vocabulary(row["kind"], json.loads(row["fields_json"]))
                self._db.execute("PRAGMA user_version=2")
            if version < 3:
                self._db.execute("""CREATE TABLE flags (
                    id TEXT PRIMARY KEY, starred INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    CHECK(starred IN (0,1)), CHECK(pinned IN (0,1)))""")
                self._db.execute("CREATE INDEX record_kind_label ON records(kind,label_key,id)")
                self._db.execute("CREATE INDEX record_project_kind ON records(project_id,kind,label_key,id)")
                self._db.execute("CREATE INDEX vocabulary_field ON vocabulary(field,value_key,kind)")
                self._db.execute("PRAGMA user_version=3")
            self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _record(row):
        if row is None:
            return None
        return {"id": row["id"], "kind": row["kind"], "label": row["label"],
                "project_id": row["project_id"], "fields": json.loads(row["fields_json"]),
                "starred": bool(row["starred"]), "pinned": bool(row["pinned"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    _SELECT = """SELECT r.*, COALESCE(f.starred,0) AS starred,
        COALESCE(f.pinned,0) AS pinned FROM records r LEFT JOIN flags f ON f.id=r.id"""

    def list(self, kind=None, project_id=None, all_projects=True):
        conditions, values = [], []
        if kind is not None:
            conditions.append("r.kind=?")
            values.append(kind)
        if not all_projects:
            conditions.append("r.project_id IS ?")
            values.append(project_id)
        sql = self._SELECT
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY r.label_key,r.id"
        with self._lock:
            return [self._record(row) for row in self._db.execute(sql, values)]

    def get(self, id):
        with self._lock:
            return self._record(self._db.execute(self._SELECT + " WHERE r.id=?", (id,)).fetchone())

    def _check_project(self, project_id):
        if project_id is not None:
            row = self._db.execute("SELECT kind FROM records WHERE id=?", (project_id,)).fetchone()
            if row is None or row[0] != "project":
                raise ValueError("Project association must identify an existing project or be None")

    def _persist_vocabulary(self, kind, fields):
        definitions = self._db.execute(
            "SELECT DISTINCT field FROM extensible_fields WHERE kind IN ('*',?)", (kind,)
        )
        for definition in definitions:
            field = definition[0]
            raw = fields.get(field)
            if raw is None:
                continue
            values = raw if isinstance(raw, (list, tuple)) else [raw]
            for value in values:
                if not isinstance(value, str):
                    raise ValueError(f"Extensible field {field!r} requires text values")
                value = value.strip()
                if value:
                    self._db.execute("""INSERT INTO vocabulary(kind,field,value_key,value)
                        VALUES(?,?,?,?) ON CONFLICT(kind,field,value_key) DO NOTHING""",
                        (kind, field, value.casefold(), value))

    def _save(self, kind, label, fields=None, project_id=None, id=None,
              starred=None, pinned=None, allow_rename=False):
        kind, label = _name(kind, "Kind"), _name(label, "Label")
        if fields is not None and not isinstance(fields, dict):
            raise ValueError("Fields must be a dictionary")
        id = _name(id, "Identity") if id is not None else uuid.uuid4().hex
        previous = self.get(id)
        if previous is not None and previous["kind"] != kind:
            raise ValueError("An existing record's kind cannot change")
        if kind == "project":
            if label.casefold() in _RESERVED_PROJECTS:
                raise ValueError(f"{label!r} is a reserved session, not a project")
            if project_id is not None:
                raise ValueError("Projects cannot belong to other projects")
            if previous and previous["label"] != label and not allow_rename:
                raise ValueError("Project rename refused: reconcile its daemon session placement first")
        self._check_project(project_id)
        merged = dict(previous["fields"]) if previous else {}
        if previous and kind == "context" and fields:
            for key in _PROVENANCE_FIELDS.intersection(fields):
                if key in merged and merged[key] != fields[key]:
                    raise ValueError(f"Saved source provenance {key!r} is immutable")
        merged.update(fields or {})
        encoded = _json(merged)
        now = time.time()
        self._db.execute("""INSERT INTO records
            (id,kind,label,label_key,fields_json,project_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            label=excluded.label,label_key=excluded.label_key,fields_json=excluded.fields_json,
            project_id=excluded.project_id,updated_at=excluded.updated_at""",
            (id, kind, label, label.casefold(), encoded, project_id, now, now))
        self._persist_vocabulary(kind, merged)
        for flag, value in (("starred", starred), ("pinned", pinned)):
            if value is not None:
                self._set_flag(id, flag, value)
        return self.get(id)

    def save(self, kind, label, fields=None, project_id=None, id=None, starred=None, pinned=None):
        with self._transaction():
            return self._save(kind, label, fields, project_id, id, starred, pinned)

    def _resolve_project(self, text):
        if text is None or (isinstance(text, str) and not text.strip()):
            return None
        text = _name(text, "Project")
        if text.casefold() == "no-project":
            return None
        row = self._db.execute("SELECT id FROM records WHERE kind='project' AND (id=? OR label_key=?)",
                               (text, text.casefold())).fetchone()
        if row:
            return row[0]
        return self._save("project", text)["id"]

    def resolve_project(self, text):
        """Explicitly resolve/create a project; never use this for completion."""
        with self._transaction():
            return self._resolve_project(text)

    def save_with_project(self, kind, label, fields=None, project="no-project", id=None):
        with self._transaction():
            project_id = self._resolve_project(project)
            return self._save(kind, label, fields, project_id, id)

    def reassociate(self, id, project):
        """Explicitly change ownership only, retaining identity and source provenance."""
        with self._transaction():
            record = self.get(id)
            if record is None:
                raise KeyError(id)
            if record["kind"] == "project":
                raise ValueError("Projects cannot be reassociated")
            project_id = self._resolve_project(project)
            return self._save(record["kind"], record["label"], project_id=project_id, id=id)

    def rename_project(self, id, label, *, placement_resolved=False):
        """Call only after daemon orchestration has reconciled every owned window.

        No daemon action is performed here. Without explicit reconciliation proof,
        the safe policy is to refuse rather than merge or displace sessions.
        """
        if placement_resolved is not True:
            raise ValueError("Project rename requires explicit daemon placement reconciliation")
        with self._transaction():
            record = self.get(id)
            if record is None or record["kind"] != "project":
                raise ValueError("Unknown project")
            return self._save("project", label, id=id, allow_rename=True)

    def delete(self, id):
        with self._transaction():
            if self._db.execute("SELECT 1 FROM records WHERE project_id=? LIMIT 1", (id,)).fetchone():
                raise ValueError("Project still owns records; explicitly reassociate them before deletion")
            self._db.execute("DELETE FROM records WHERE id=?", (id,))
            self._db.execute("DELETE FROM flags WHERE id=?", (id,))

    def _set_flag(self, id, flag, value):
        if flag not in {"starred", "pinned"}:
            raise ValueError("Flag must be starred or pinned")
        if not isinstance(value, bool):
            raise ValueError("Flag value must be boolean")
        id = _name(id, "Identity")
        self._db.execute(f"""INSERT INTO flags(id,{flag}) VALUES(?,?)
            ON CONFLICT(id) DO UPDATE SET {flag}=excluded.{flag}""", (id, int(value)))

    def set_flag(self, id, flag, value):
        with self._transaction():
            self._set_flag(id, flag, value)

    def decorate(self, records):
        """Overlay durable presentation/ownership metadata without rewriting live state."""
        records = list(records)
        identities = list(dict.fromkeys(record["id"] for record in records))
        flags, associations = {}, {}
        with self._lock:
            for offset in range(0, len(identities), 500):
                batch = identities[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                flags.update((row["id"], row) for row in self._db.execute(
                    f"SELECT * FROM flags WHERE id IN ({placeholders})", batch))
                associations.update((row["id"], row["project_id"]) for row in self._db.execute(
                    f"SELECT id,project_id FROM records WHERE id IN ({placeholders}) "
                    "AND kind IN ('window','session','host')", batch))
        result = []
        for record in records:
            item = dict(record)
            flag = flags.get(item["id"])
            if flag is not None:
                item["starred"], item["pinned"] = bool(flag["starred"]), bool(flag["pinned"])
            else:
                item.setdefault("starred", False)
                item.setdefault("pinned", False)
            if item["id"] in associations:
                item["project_id"] = associations[item["id"]]
            result.append(item)
        return result

    def register_field(self, name, kind=None):
        """Register a user-owned extensible field, not an authoritative daemon enum."""
        name = _name(name, "Field")
        if name in _AUTHORITATIVE_FIELDS:
            raise ValueError(f"{name!r} is authoritative and cannot be user-extensible")
        kind = "*" if kind is None else _name(kind, "Kind")
        with self._transaction():
            self._db.execute("INSERT OR IGNORE INTO extensible_fields(kind,field) VALUES(?,?)", (kind, name))
            sql, args = "SELECT kind,fields_json FROM records", ()
            if kind != "*":
                sql += " WHERE kind=?"
                args = (kind,)
            for row in self._db.execute(sql, args).fetchall():
                self._persist_vocabulary(row["kind"], json.loads(row["fields_json"]))

    def vocabulary(self, field, kind=None):
        sql, args = "SELECT value,value_key FROM vocabulary WHERE field=?", [field]
        if kind is not None:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY value_key,kind"
        with self._lock:
            values = {}
            for row in self._db.execute(sql, args):
                values.setdefault(row["value_key"], row["value"])
            return list(values.values())

    def settings(self):
        with self._lock:
            return {row["key"]: json.loads(row["value_json"])
                    for row in self._db.execute("SELECT key,value_json FROM settings ORDER BY key")}

    def set_settings(self, changes, reset=()):
        """Atomically save changed settings and remove explicit default resets."""
        with self._transaction():
            for key in reset:
                self._db.execute("DELETE FROM settings WHERE key=?", (_name(key, "Setting"),))
            for key, value in changes.items():
                self._db.execute("""INSERT INTO settings(key,value_json) VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json""",
                    (_name(key, "Setting"), _json(value)))

    def set_setting(self, key, value):
        self.set_settings({key: value})

    def unset_setting(self, key):
        self.set_settings({}, reset=(key,))
