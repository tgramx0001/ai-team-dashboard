"""Durable state for AI Team Dashboard.

SQLite is the SOURCE OF TRUTH for workspaces, agents, tasks, stages, messages
and events. `tasks.json` is only a temporary write-through backup kept during
the migration (see export_tasks_json / import_tasks_from_json and the
JSON_BACKUP markers in main.py). Removal point: delete export_tasks_json()
call sites + JSON_BACKUP_PATH usage and the JSON state is gone; the DB alone
still holds everything.

No framework, stdlib sqlite3 only.
"""
import json
import os
import re
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 3

_initialized_dbs = set()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_BACKUP_PATH = os.path.join(BASE_DIR, "tasks.json")  # JSON_BACKUP: temporary migration backup
DEFAULT_REGISTRY_DIR = os.path.join(BASE_DIR, "registry")

# Statuses that cannot survive a process restart (their coroutine died).
INTERRUPTABLE_STATUSES = ("queued", "running", "waiting_approval")
INTERRUPTED_STATUS = "interrupted"


def db_path() -> str:
    return os.environ.get("AI_TEAM_DB") or os.path.join(BASE_DIR, "ai_team.db")


def registry_dir() -> str:
    return os.environ.get("REGISTRY_DIR") or DEFAULT_REGISTRY_DIR


def agents_seed_path() -> str:
    return os.path.join(registry_dir(), "agents.json")


def presets_path() -> str:
    return os.path.join(registry_dir(), "presets.json")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    root           TEXT NOT NULL UNIQUE,
    agents         TEXT NOT NULL DEFAULT '[]',
    context_sources TEXT NOT NULL DEFAULT '[]',
    is_default     INTEGER NOT NULL DEFAULT 0,
    created_at     REAL,
    updated_at     REAL
);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL UNIQUE,
    name        TEXT,
    icon        TEXT,
    system      TEXT,
    temperature REAL DEFAULT 0.2,
    tools       TEXT NOT NULL DEFAULT '[]',
    permissions TEXT NOT NULL DEFAULT '{}',
    builtin     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT,
    prompt               TEXT,
    preset_id            TEXT,
    session_id           TEXT,
    working_directory    TEXT,
    project_context      TEXT,
    skills               TEXT NOT NULL DEFAULT '[]',
    auto_save_artifact   INTEGER NOT NULL DEFAULT 0,
    auto_apply_files     INTEGER NOT NULL DEFAULT 0,
    require_approval     INTEGER NOT NULL DEFAULT 0,
    auto_fix_loops       INTEGER NOT NULL DEFAULT 1,
    current_fix_loop     INTEGER NOT NULL DEFAULT 0,
    status               TEXT NOT NULL DEFAULT 'queued',
    error                TEXT,
    scope_matrix         TEXT,
    context_model        TEXT,
    stages_approved      TEXT NOT NULL DEFAULT '{}',
    latest_feedback      TEXT,
    cancelled            INTEGER NOT NULL DEFAULT 0,
    waiting_stage_index  INTEGER,
    waiting_stage_name   TEXT,
    final_output         TEXT,
    applied_files        TEXT,
    blocked_files        TEXT,
    saved_artifact_path  TEXT,
    saved_artifact_error TEXT,
    extra                TEXT,
    created_at           REAL,
    started_at           REAL,
    completed_at         REAL,
    updated_at           REAL
);

CREATE TABLE IF NOT EXISTS stages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    idx          INTEGER NOT NULL,
    role         TEXT,
    name         TEXT,
    icon         TEXT,
    system       TEXT,
    temperature  REAL,
    status       TEXT,
    output       TEXT,
    error        TEXT,
    started_at   REAL,
    completed_at REAL,
    UNIQUE (task_id, idx)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    stage_idx  INTEGER,
    role       TEXT,
    to_role    TEXT DEFAULT 'all',
    kind       TEXT,
    content    TEXT,
    meta       TEXT DEFAULT '{}',
    created_at REAL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT REFERENCES tasks(id) ON DELETE CASCADE,
    type       TEXT,
    payload    TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status   ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_stages_task    ON stages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_task  ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task    ON events(task_id);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    workspace_root    TEXT,
    model             TEXT,
    active_agents     TEXT DEFAULT '["Hermes"]',
    created_at        REAL,
    updated_at        REAL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    agent      TEXT DEFAULT 'Hermes',
    content    TEXT NOT NULL,
    model      TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
"""


def init_db(force: bool = False) -> int:
    """Create/upgrade schema. Returns schema version. Guarded per database path."""
    current_path = os.path.abspath(db_path())
    if not force and current_path in _initialized_dbs:
        return SCHEMA_VERSION
    os.makedirs(os.path.dirname(current_path) or ".", exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA_SQL)
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version < SCHEMA_VERSION:
            _migrate(conn, version, SCHEMA_VERSION)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('migrated_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
        conn.commit()
        _initialized_dbs.add(current_path)
        return SCHEMA_VERSION
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection, from_version: int, to_version: int) -> None:
    """Versioned upgrades.
    v0 -> v1 is CREATE TABLE IF NOT EXISTS (idempotent).
    v1 -> v2 adds chat_sessions + chat_messages (also CREATE IF NOT EXISTS, safe).
    v2 -> v3 adds structured messages columns (to_role, meta) and collaborative chat agents."""
    if from_version < 3:
        # Check messages table columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if "to_role" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN to_role TEXT DEFAULT 'all'")
        if "meta" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT DEFAULT '{}'")

        # Check chat_sessions table columns
        s_cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()]
        if "active_agents" not in s_cols:
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN active_agents TEXT DEFAULT '[\"Hermes\"]'")

        # Check chat_messages table columns
        m_cols = [r[1] for r in conn.execute("PRAGMA table_info(chat_messages)").fetchall()]
        if "agent" not in m_cols:
            conn.execute("ALTER TABLE chat_messages ADD COLUMN agent TEXT DEFAULT 'Hermes'")


def schema_info() -> Dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        counts = {}
        for table in ("workspaces", "agents", "tasks", "stages", "messages", "events"):
            try:
                counts[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            except sqlite3.Error:
                counts[table] = None
        return {"schema_version": version, "db_path": db_path(), "counts": counts}
    finally:
        conn.close()


# ---------------------------------------------------------------- tasks + stages

TASK_COLUMNS = (
    "id", "title", "prompt", "preset_id", "session_id", "working_directory",
    "project_context", "skills", "auto_save_artifact", "auto_apply_files",
    "require_approval", "auto_fix_loops", "current_fix_loop", "status", "error",
    "scope_matrix", "context_model", "stages_approved", "latest_feedback",
    "cancelled", "waiting_stage_index", "waiting_stage_name", "final_output",
    "applied_files", "blocked_files", "saved_artifact_path", "saved_artifact_error",
    "created_at", "started_at", "completed_at", "updated_at",
)

JSON_COLUMNS = {
    "skills", "scope_matrix", "context_model", "stages_approved",
    "applied_files", "blocked_files",
}
INT_COLUMNS = {
    "auto_save_artifact", "auto_apply_files", "require_approval",
    "auto_fix_loops", "current_fix_loop", "cancelled",
}

STAGE_COLUMNS = (
    "role", "name", "icon", "system", "temperature", "status", "output",
    "error", "started_at", "completed_at",
)


def _task_to_row(task: Dict[str, Any]) -> Dict[str, Any]:
    row = {}
    known = set(TASK_COLUMNS)
    for col in TASK_COLUMNS:
        val = task.get(col)
        if col in JSON_COLUMNS:
            if val is None:
                # NOT NULL columns need a sane default for legacy rows
                if col in ("skills", "applied_files", "blocked_files"):
                    val = "[]"
                elif col == "stages_approved":
                    val = "{}"
                else:
                    val = None
            else:
                val = json.dumps(val, ensure_ascii=False)
        elif col in INT_COLUMNS:
            val = 1 if val else 0
        row[col] = val
    extra = {k: v for k, v in task.items() if k not in known and k != "stages"}
    row["extra"] = json.dumps(extra, ensure_ascii=False) if extra else None
    return row


def _row_to_task(row: sqlite3.Row, stages: List[Dict[str, Any]]) -> Dict[str, Any]:
    task: Dict[str, Any] = {}
    for col in TASK_COLUMNS:
        val = row[col]
        if col in JSON_COLUMNS:
            try:
                val = json.loads(val) if val else None
            except Exception:
                val = None
            if val is None:
                if col == "stages_approved":
                    val = {}
                elif col in ("skills", "applied_files", "blocked_files"):
                    val = []
                # context_model / scope_matrix stay None when absent
        elif col in INT_COLUMNS:
            val = bool(val)
        task[col] = val
    if row["extra"]:
        try:
            task.update(json.loads(row["extra"]))
        except Exception:
            pass
    task["stages"] = stages
    return task


def load_tasks() -> Dict[str, Dict[str, Any]]:
    init_db()
    conn = connect()
    try:
        tasks: Dict[str, Dict[str, Any]] = {}
        for row in conn.execute("SELECT * FROM tasks ORDER BY created_at"):
            stage_rows = conn.execute(
                "SELECT * FROM stages WHERE task_id=? ORDER BY idx", (row["id"],)
            ).fetchall()
            stages = []
            for s in stage_rows:
                stages.append({
                    "role": s["role"], "name": s["name"], "icon": s["icon"],
                    "system": s["system"], "temperature": s["temperature"],
                    "status": s["status"], "output": s["output"] or "",
                    "error": s["error"],
                    "started_at": s["started_at"], "completed_at": s["completed_at"],
                })
            tasks[row["id"]] = _row_to_task(row, stages)
        return tasks
    finally:
        conn.close()


def save_tasks(tasks: Dict[str, Dict[str, Any]]) -> None:
    """Upsert every task (and its stages) in one transaction."""
    if not tasks:
        init_db()
        return
    init_db()
    conn = connect()
    try:
        with conn:
            for task_id, task in tasks.items():
                _upsert_task(conn, task_id, task)
    finally:
        conn.close()


def save_task(task: Dict[str, Any]) -> None:
    save_tasks({task["id"]: task})


def _upsert_task(conn: sqlite3.Connection, task_id: str, task: Dict[str, Any]) -> None:
    row = _task_to_row(task)
    row["id"] = task_id
    row["status"] = task.get("status") or "queued"
    row["updated_at"] = time.time()
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{c}" for c in row.keys())
    updates = ", ".join(f"{c}=excluded.{c}" for c in row.keys() if c != "id")
    conn.execute(
        f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        row,
    )
    conn.execute("DELETE FROM stages WHERE task_id=?", (task_id,))
    for idx, stage in enumerate(task.get("stages", [])):
        conn.execute(
            "INSERT INTO stages (task_id, idx, role, name, icon, system, temperature,"
            " status, output, error, started_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, idx, stage.get("role"), stage.get("name"), stage.get("icon"),
                stage.get("system"), stage.get("temperature"), stage.get("status"),
                stage.get("output") or "", stage.get("error"),
                stage.get("started_at"), stage.get("completed_at"),
            ),
        )


def reconcile_interrupted(tasks: Dict[str, Dict[str, Any]]) -> List[str]:
    """Mark tasks whose worker died on restart as interrupted (recoverable)."""
    changed: List[str] = []
    for task_id, task in tasks.items():
        if task.get("status") in INTERRUPTABLE_STATUSES:
            task["status"] = INTERRUPTED_STATUS
            task["error"] = "Terhenti karena restart server. Gunakan Retry untuk melanjutkan."
            task["waiting_stage_index"] = None
            task["waiting_stage_name"] = None
            for stage in task.get("stages", []):
                if stage.get("status") in ("running", "waiting"):
                    stage["status"] = INTERRUPTED_STATUS
            changed.append(task_id)
    if changed:
        save_tasks(tasks)
        for task_id in changed:
            add_event(task_id, "task.interrupted", {"reason": "server_restart"})
    return changed


# ---------------------------------------------------------------- JSON backup (temporary)

def export_tasks_json(tasks: Dict[str, Dict[str, Any]], path: Optional[str] = None) -> None:
    """JSON_BACKUP: write-through mirror of the DB. DELETE THIS to drop JSON state."""
    target = path or JSON_BACKUP_PATH
    tmp = f"{target}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2, ensure_ascii=False)
    os.replace(tmp, target)


def import_tasks_from_json(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """One-time migration: read legacy tasks.json into memory (caller persists to DB)."""
    source = path or JSON_BACKUP_PATH
    if not os.path.exists(source):
        return {}
    try:
        with open(source, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"tasks.json migration skipped: {e}")
        return {}


# ---------------------------------------------------------------- messages + events

def add_message(task_id: str, role: str, kind: str, content: str,
                stage_idx: Optional[int] = None, to_role: str = "all",
                meta: Optional[Dict[str, Any]] = None) -> int:
    init_db()
    conn = connect()
    try:
        with conn:
            meta_json = json.dumps(meta or {}, ensure_ascii=False) if isinstance(meta, dict) else (meta or "{}")
            cur = conn.execute(
                "INSERT INTO messages (task_id, stage_idx, role, to_role, kind, content, meta, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, stage_idx, role, to_role, kind, content, meta_json, time.time()),
            )
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def add_event(task_id: Optional[str], type_: str, payload: Optional[Dict[str, Any]] = None) -> int:
    init_db()
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO events (task_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, type_, json.dumps(payload or {}, ensure_ascii=False), time.time()),
            )
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_messages(task_id: str, kind: Optional[str] = None, to_role: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        sql = "SELECT * FROM messages WHERE task_id=?"
        params: List[Any] = [task_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if to_role:
            sql += " AND (to_role=? OR to_role='all')"
            params.append(to_role)
        sql += " ORDER BY id ASC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            if "meta" in r and isinstance(r["meta"], str):
                try:
                    r["meta"] = json.loads(r["meta"])
                except Exception:
                    pass
        return rows
    finally:
        conn.close()


def list_events(task_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except Exception:
                item["payload"] = {}
            out.append(item)
        return list(reversed(out))
    finally:
        conn.close()


def purge_task(task_id: str) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------- workspaces

def list_workspaces() -> List[Dict[str, Any]]:
    conn = connect()
    try:
        out = []
        for r in conn.execute("SELECT * FROM workspaces ORDER BY is_default DESC, name"):
            item = dict(r)
            item["agents"] = json.loads(item.get("agents") or "[]")
            item["context_sources"] = json.loads(item.get("context_sources") or "[]")
            item["is_default"] = bool(item.get("is_default"))
            out.append(item)
        return out
    finally:
        conn.close()


def get_workspace(ws_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
        if not r:
            return None
        item = dict(r)
        item["agents"] = json.loads(item.get("agents") or "[]")
        item["context_sources"] = json.loads(item.get("context_sources") or "[]")
        item["is_default"] = bool(item.get("is_default"))
        return item
    finally:
        conn.close()


def upsert_workspace(ws: Dict[str, Any]) -> Dict[str, Any]:
    init_db()
    now = time.time()
    ws_id = ws.get("id") or str(uuid.uuid4())[:8]
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO workspaces (id, name, root, agents, context_sources, is_default,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, root=excluded.root,"
                " agents=excluded.agents, context_sources=excluded.context_sources,"
                " is_default=excluded.is_default, updated_at=excluded.updated_at",
                (
                    ws_id, ws.get("name") or ws.get("root") or ws_id,
                    ws.get("root") or "",
                    json.dumps(ws.get("agents") or [], ensure_ascii=False),
                    json.dumps(ws.get("context_sources") or [], ensure_ascii=False),
                    1 if ws.get("is_default") else 0,
                    ws.get("created_at") or now, now,
                ),
            )
    finally:
        conn.close()
    return get_workspace(ws_id) or {}


def delete_workspace(ws_id: str) -> bool:
    conn = connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
        return cur.rowcount > 0
    finally:
        conn.close()


def seed_workspaces(presets: List[Dict[str, str]], default_root: str,
                    force: bool = False) -> int:
    """Seed workspaces from config presets when the table is empty."""
    init_db()
    existing = list_workspaces()
    if existing and not force:
        return 0
    known_roots = {w["root"] for w in existing}
    created = 0
    for item in presets:
        root = item.get("path") or item.get("root")
        if not root or root in known_roots:
            continue
        upsert_workspace({
            "name": item.get("label") or _get_path_name(root) or root,
            "root": root,
            "is_default": root == default_root,
        })
        created += 1
    if default_root and not any(w["is_default"] for w in list_workspaces()):
        if default_root not in known_roots:
            upsert_workspace({"name": _get_path_name(default_root) or "default",
                              "root": default_root, "is_default": True})
            created += 1
        else:
            conn = connect()
            try:
                with conn:
                    conn.execute("UPDATE workspaces SET is_default=1 WHERE root=?", (default_root,))
            finally:
                conn.close()
    return created


def _get_path_name(path_str: str) -> str:
    """Extract folder/file name handling both POSIX and Windows path separators."""
    clean = (path_str or "").rstrip("/\\")
    if not clean:
        return ""
    # Use PurePath or regex split
    return re.split(r'[\\/]', clean)[-1]

def _agent_row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    return {
        "role": r["role"], "name": r["name"], "icon": r["icon"],
        "system": r["system"], "temperature": r["temperature"],
        "tools": json.loads(r["tools"] or "[]"),
        "permissions": json.loads(r["permissions"] or "{}"),
    }


def load_agents() -> Dict[str, Dict[str, Any]]:
    """Agent registry from SQLite; seeds from registry/agents.json when empty."""
    init_db()
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM agents ORDER BY role").fetchall()
    finally:
        conn.close()
    if not rows:
        seed_agents_from_file()
        conn = connect()
        try:
            rows = conn.execute("SELECT * FROM agents ORDER BY role").fetchall()
        finally:
            conn.close()
    return {r["role"]: _agent_row_to_dict(r) for r in rows}


def upsert_agent(agent: Dict[str, Any]) -> Dict[str, Any]:
    init_db()
    role = (agent.get("role") or "").strip()
    if not role:
        raise ValueError("agent role is required")
    now = time.time()
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO agents (id, role, name, icon, system, temperature, tools,"
                " permissions, builtin, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(role) DO UPDATE SET name=excluded.name, icon=excluded.icon,"
                " system=excluded.system, temperature=excluded.temperature,"
                " tools=excluded.tools, permissions=excluded.permissions,"
                " updated_at=excluded.updated_at",
                (
                    str(uuid.uuid4())[:8], role, agent.get("name") or role,
                    agent.get("icon") or "🤖", agent.get("system") or "",
                    float(agent.get("temperature") if agent.get("temperature") is not None else 0.2),
                    json.dumps(agent.get("tools") or [], ensure_ascii=False),
                    json.dumps(agent.get("permissions") or {}, ensure_ascii=False),
                    1 if agent.get("builtin", True) else 0, now, now,
                ),
            )
    finally:
        conn.close()
    return _get_agent(role)


def _get_agent(role: str) -> Dict[str, Any]:
    conn = connect()
    try:
        r = conn.execute("SELECT * FROM agents WHERE role=?", (role,)).fetchone()
        return _agent_row_to_dict(r) if r else {}
    finally:
        conn.close()


def seed_agents_from_file(path: Optional[str] = None) -> int:
    """Import registry/agents.json into the agents table (only missing roles are added)."""
    source = path or agents_seed_path()
    if not os.path.exists(source):
        return 0
    try:
        with open(source, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"agents seed skipped: {e}")
        return 0
    init_db()
    created = 0
    conn = connect()
    try:
        with conn:
            for key, agent in (data or {}).items():
                agent = dict(agent)
                agent.setdefault("role", key)
                role = (agent.get("role") or key).strip()
                cur = conn.execute("SELECT 1 FROM agents WHERE role=?", (role,))
                if cur.fetchone():
                    # Update definition and permissions from file for builtin agents
                    conn.execute(
                        "UPDATE agents SET name=?, icon=?, system=?, temperature=?, tools=?, permissions=?, updated_at=? WHERE role=? AND builtin=1",
                        (
                            agent.get("name") or role,
                            agent.get("icon") or "🤖",
                            agent.get("system") or "",
                            float(agent.get("temperature") or 0.2),
                            json.dumps(agent.get("tools") or [], ensure_ascii=False),
                            json.dumps(agent.get("permissions") or {}, ensure_ascii=False),
                            time.time(),
                            role,
                        ),
                    )
                    continue
                now = time.time()
                conn.execute(
                    "INSERT INTO agents (id, role, name, icon, system, temperature, tools,"
                    " permissions, builtin, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        str(uuid.uuid4())[:8], role, agent.get("name") or role,
                        agent.get("icon") or "🤖", agent.get("system") or "",
                        float(agent.get("temperature") or 0.2),
                        json.dumps(agent.get("tools") or [], ensure_ascii=False),
                        json.dumps(agent.get("permissions") or {}, ensure_ascii=False),
                        now, now,
                    ),
                )
                created += 1
    finally:
        conn.close()
    return created


def load_presets(path: Optional[str] = None) -> Dict[str, Any]:
    source = path or presets_path()
    if not os.path.exists(source):
        return {}
    try:
        with open(source, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"presets registry unreadable: {e}")
        return {}


def save_presets(presets: Dict[str, Any], path: Optional[str] = None) -> str:
    target = path or presets_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return target


# ---------------------------------------------------------------- chat sessions + messages

def list_chat_sessions() -> List[Dict[str, Any]]:
    init_db()
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if "active_agents" in d and isinstance(d["active_agents"], str):
                try:
                    d["active_agents"] = json.loads(d["active_agents"])
                except Exception:
                    d["active_agents"] = ["Hermes"]
            else:
                d["active_agents"] = ["Hermes"]
            result.append(d)
        return result
    finally:
        conn.close()


def get_chat_session(session_id: str) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        if "active_agents" in d and isinstance(d["active_agents"], str):
            try:
                d["active_agents"] = json.loads(d["active_agents"])
            except Exception:
                d["active_agents"] = ["Hermes"]
        else:
            d["active_agents"] = ["Hermes"]
        return d
    finally:
        conn.close()


def create_chat_session(title: str, workspace_root: Optional[str] = None,
                        model: Optional[str] = None,
                        active_agents: Optional[List[str]] = None) -> str:
    init_db()
    sess_id = str(uuid.uuid4())[:12]
    now = time.time()
    agents_json = json.dumps(active_agents or ["Hermes"], ensure_ascii=False)
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO chat_sessions (id, title, workspace_root, model, active_agents, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sess_id, title, workspace_root, model, agents_json, now, now),
            )
    finally:
        conn.close()
    return sess_id


def update_chat_session_agents(session_id: str, active_agents: List[str]) -> None:
    init_db()
    conn = connect()
    try:
        with conn:
            conn.execute(
                "UPDATE chat_sessions SET active_agents=?, updated_at=? WHERE id=?",
                (json.dumps(active_agents, ensure_ascii=False), time.time(), session_id),
            )
    finally:
        conn.close()


def update_chat_session_ts(session_id: str) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (time.time(), session_id))
    finally:
        conn.close()


def delete_chat_session(session_id: str) -> bool:
    conn = connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        return cur.rowcount > 0
    finally:
        conn.close()


def add_chat_message(session_id: str, role: str, content: str,
                     model: Optional[str] = None, agent: str = "Hermes") -> int:
    now = time.time()
    conn = connect()
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO chat_messages (session_id, role, agent, content, model, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, agent, content, model, now),
            )
        update_chat_session_ts(session_id)
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_chat_messages(session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_agent_permission(role: str, capability: str, subaction: Optional[str] = None) -> bool:
    """Check if an agent has permission for a specific capability/tool.
    capability: 'filesystem', 'shell', 'git', 'web'
    subaction: for filesystem: 'read', 'write'
    """
    agents = load_agents()
    agent = agents.get(role)
    if not agent:
        # Fallback for Hermes / unspecified: unrestricted
        return True
    perms = agent.get("permissions") or {}
    if not perms:
        return True
    val = perms.get(capability)
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, dict) and subaction:
        return bool(val.get(subaction, False))
    return bool(val)


def get_recent_chat_context(session_id: str, n_turns: int = 20) -> List[Dict[str, str]]:
    """Return the last n_turns pairs as [{role, content}, …] for the LLM messages array."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, n_turns * 2),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    finally:
        conn.close()

