import asyncio
import difflib
import fnmatch
import glob
import hmac
import json
import os
import py_compile
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
import yaml
from pathlib import Path

import store
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = store.JSON_BACKUP_PATH  # JSON_BACKUP: temporary mirror of SQLite state
IS_WINDOWS = sys.platform.startswith("win")

def _load_env_file(path: str) -> None:
    """Read simple KEY=VALUE lines from a local .env. Real env vars always win."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except Exception:
        pass

_load_env_file(os.path.join(BASE_DIR, ".env"))

def _split_paths(raw: str) -> List[str]:
    """Parse a path list from env (comma/semicolon separated), expand ~ and $VARS."""
    out: List[str] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(os.path.abspath(os.path.expandvars(os.path.expanduser(part))))
    return out

def _env_paths(key: str, default: str) -> List[str]:
    raw = os.environ[key] if key in os.environ else default
    return _split_paths(raw)

# Workspace boundary: primary root plus explicit extra roots (never the whole $HOME).
ALLOWED_ROOT = os.path.abspath(os.path.expandvars(os.path.expanduser(
    os.environ.get("WORKSPACE_ROOT", os.path.join(os.path.expanduser("~"), "projects"))
)))
EXTRA_ROOTS = _env_paths("WORKSPACE_EXTRA_ROOTS", os.path.join(os.path.expanduser("~"), "Documents"))
ALLOWED_ROOTS = [ALLOWED_ROOT] + [r for r in EXTRA_ROOTS if r != ALLOWED_ROOT]
DEFAULT_WORKSPACE = os.environ.get("DEFAULT_WORKSPACE", ALLOWED_ROOT)
AUTH_TOKEN = os.environ.get("AI_TEAM_AUTH_TOKEN", "").strip()
LAN_HOST = os.environ.get("LAN_HOST", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()] or [
    "http://localhost:8090", "http://127.0.0.1:8090"
]

# Hermes Integration Constants
HERMES_DIR = os.environ.get("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))
HERMES_SKILLS_DIR = os.path.join(HERMES_DIR, "skills")
LOCAL_SKILLS_DIR = os.path.join(BASE_DIR, "skills")
HERMES_STATE_DB = os.path.join(HERMES_DIR, "state.db")
HERMES_CONFIG_FILE = os.path.join(HERMES_DIR, "config.yaml")

app = FastAPI(title="AI Team Dashboard - Autonomous Workstation")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Bearer token on every /api/* route. Fail closed when no token is configured:
    API stays reachable from loopback only, never from the network."""
    if request.url.path.startswith("/api"):
        if AUTH_TOKEN:
            header = request.headers.get("authorization", "")
            expected = f"Bearer {AUTH_TOKEN}"
            if not hmac.compare_digest(header.encode(), expected.encode()):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized: token tidak valid."})
        else:
            host = (request.client.host if request.client else "") or ""
            if host not in ("127.0.0.1", "::1", "testclient"):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "AI_TEAM_AUTH_TOKEN belum diatur; API hanya bisa diakses dari localhost."},
                )
    return await call_next(request)


# In-memory approval events
APPROVAL_EVENTS: Dict[str, asyncio.Event] = {}

# Dashboard-scoped model override: keeps model switching local to this app so
# changing it from the UI does not touch ~/.hermes/config.yaml.
MODEL_OVERRIDE_FILE = os.path.join(BASE_DIR, ".model_override")

def _read_hermes_model() -> Optional[str]:
    if os.path.exists(HERMES_CONFIG_FILE):
        try:
            with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
                hcfg = yaml.safe_load(f) or {}
                return hcfg.get("model", {}).get("default") or None
        except Exception:
            return None
    return None

def _read_model_override() -> Optional[str]:
    try:
        if os.path.exists(MODEL_OVERRIDE_FILE):
            with open(MODEL_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                val = f.read().strip()
                return val or None
    except Exception:
        return None
    return None

def _write_model_override(model_name: str) -> None:
    tmp = f"{MODEL_OVERRIDE_FILE}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(model_name.strip())
    os.replace(tmp, MODEL_OVERRIDE_FILE)

def get_llm_config() -> Tuple[str, str, str]:
    # Precedence: LLM_MODEL env > dashboard override > Hermes config > "bai"
    hermes_model = _read_hermes_model()

    base_url = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or _read_model_override() or hermes_model or "bai"
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    # Auto-detect local 9Router sqlite if key not passed in env
    if not key:
        default_9r = os.path.join(os.path.expanduser("~"), ".9router", "db", "data.sqlite")
        db_path = os.environ.get("NINEROUTER_DB", default_9r)
        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT key FROM apiKeys WHERE isActive=1 ORDER BY id DESC LIMIT 1;")
                row = cursor.fetchone()
                conn.close()
                if row:
                    key = row[0]
            except Exception:
                pass

    if not key:
        key = "sk-local-key"  # Default fallback for local Ollama / LM Studio without auth
    return base_url, model, key

def get_9router_key() -> str:
    _, _, key = get_llm_config()
    return key

# ==============================================================================
# HERMES CORE BRIDGE FUNCTIONS
# ==============================================================================

def get_hermes_skills() -> List[Dict[str, Any]]:
    skills = []
    seen = set()
    dirs_to_scan = []
    if os.path.isdir(HERMES_SKILLS_DIR):
        dirs_to_scan.append(HERMES_SKILLS_DIR)
    if os.path.isdir(LOCAL_SKILLS_DIR):
        dirs_to_scan.append(LOCAL_SKILLS_DIR)

    for base_p in dirs_to_scan:
        for root, _, files in os.walk(base_p):
            if "SKILL.md" in files:
                p = os.path.join(root, "SKILL.md")
                rel = os.path.relpath(root, base_p)
                parts = rel.split(os.sep)
                category = parts[0] if len(parts) > 1 else "general"
                name = parts[-1]
                if name.startswith("."):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                desc = ""
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        snippet = f.read(1500)
                        m = re.search(r'description:\s*["\']?(.*?)["\']?\n', snippet)
                        if m:
                            desc = m.group(1).strip()
                except Exception:
                    pass
                skills.append({
                    "name": name,
                    "category": category,
                    "description": desc or f"SOP and rules for {name}",
                    "path": p
                })
    skills.sort(key=lambda s: (s["category"], s["name"]))
    return skills

def read_skill_content(skill_name: str) -> str:
    skills = get_hermes_skills()
    for s in skills:
        if s["name"].lower() == skill_name.lower():
            try:
                with open(s["path"], "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
            except Exception:
                pass
    return ""

def get_hermes_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    if not os.path.exists(HERMES_STATE_DB):
        return []
    try:
        conn = sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True)
        c = conn.cursor()
        query = """
            SELECT id, source, title, model, message_count,
                   datetime(last_activity_at, 'unixepoch', 'localtime') as last_active,
                   datetime(started_at, 'unixepoch', 'localtime') as started
            FROM sessions
            ORDER BY last_activity_at DESC
            LIMIT ?
        """
        rows = c.execute(query, (limit,)).fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "source": r[1] or "web",
                "title": r[2] or f"Session {r[0][:12]}",
                "model": r[3] or "default",
                "message_count": r[4] or 0,
                "last_active": r[5] or "",
                "started": r[6] or ""
            }
            for r in rows
        ]
    except Exception as e:
        return []

def get_session_messages(session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    if not os.path.exists(HERMES_STATE_DB):
        return []
    try:
        conn = sqlite3.connect(f"file:{HERMES_STATE_DB}?mode=ro", uri=True)
        c = conn.cursor()
        query = """
            SELECT id, role, content, tool_name,
                   datetime(timestamp, 'unixepoch', 'localtime') as ts
            FROM messages
            WHERE session_id = ?
            ORDER BY id ASC
            LIMIT ?
        """
        rows = c.execute(query, (session_id, limit)).fetchall()
        conn.close()
        return [
            {
                "id": r[0],
                "role": r[1],
                "content": r[2],
                "tool_name": r[3],
                "timestamp": r[4]
            }
            for r in rows
        ]
    except Exception as e:
        return []

def get_hermes_model_info() -> Dict[str, Any]:
    """Effective model (what this dashboard actually calls) + where it comes from."""
    provider = "custom"
    base_url = "http://127.0.0.1:20128/v1"
    hermes_default = _read_hermes_model()

    if os.path.exists(HERMES_CONFIG_FILE):
        try:
            with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            m = cfg.get("model", {})
            provider = m.get("provider", provider)
            base_url = m.get("base_url", base_url)
        except Exception:
            pass

    _, active_model, _ = get_llm_config()
    override = _read_model_override()
    if os.environ.get("LLM_MODEL"):
        scope = "env"
    elif override:
        scope = "dashboard"
    elif hermes_default:
        scope = "hermes"
    else:
        scope = "default"

    available = []
    try:
        r = httpx.get(f"{base_url}/models", timeout=3.0)
        if r.status_code == 200:
            for item in r.json().get("data", []):
                mid = item.get("id")
                if mid:
                    available.append(mid)
    except Exception:
        pass

    if not available:
        available = [active_model, "bai", "claude_code", "deepseek-coder", "gpt-4o-mini"]

    return {
        "active_model": active_model,
        "provider": provider,
        "base_url": base_url,
        "scope": scope,
        "hermes_default": hermes_default,
        "dashboard_override": override,
        "env_override": os.environ.get("LLM_MODEL") or None,
        "available_models": list(dict.fromkeys(available))
    }

def update_hermes_model(model_name: str, apply_globally: bool = False) -> Optional[str]:
    """Switch the model used by this dashboard.

    Default is dashboard-scoped (a local override file, no side effects on
    ~/.hermes/config.yaml). apply_globally=True is the explicit, intentional
    path that rewrites the Hermes config; returns the applied scope or None.
    """
    name = (model_name or "").strip()
    if not name:
        return None
    if apply_globally:
        if not os.path.exists(HERMES_CONFIG_FILE):
            return None
        try:
            with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if "model" not in cfg:
                cfg["model"] = {}
            cfg["model"]["default"] = name
            with open(HERMES_CONFIG_FILE, "w", encoding="utf-8") as f:
                yaml.dump(cfg, f, default_flow_style=False)
            # drop the local override so the global choice takes effect
            try:
                if os.path.exists(MODEL_OVERRIDE_FILE):
                    os.remove(MODEL_OVERRIDE_FILE)
            except OSError:
                pass
            return "hermes"
        except Exception:
            return None
    try:
        _write_model_override(name)
        return "dashboard"
    except Exception:
        return None

# Agent registry lives in SQLite (seeded from registry/agents.json) and presets in
# registry/presets.json -- configurable data, not hardcoded Python structures.
SPECIALIST_CATALOG: Dict[str, Dict[str, Any]] = store.load_agents()
PRESETS: Dict[str, Dict[str, Any]] = store.load_presets()

def parse_orchestrator_plan(text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    catalog = store.load_agents() or SPECIALIST_CATALOG
    m = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
    data = None
    if m:
        try:
            data = json.loads(m.group(1))
        except Exception:
            pass
    if not data:
        m_bare = re.search(r'(\{[\s\S]*"selected_roles"[\s\S]*\})', text)
        if m_bare:
            try:
                data = json.loads(m_bare.group(1))
            except Exception:
                pass

    scope_matrix = {}
    if data and isinstance(data, dict):
        scope_matrix = {
            "task_type": data.get("task_type", "GENERAL"),
            "target_scope": data.get("target_scope") or data.get("scope", "General targeted changes"),
            "allowed_files": data.get("allowed_files", []),
            "forbidden_files": data.get("forbidden_files", []),
            "selected_roles": data.get("selected_roles", []),
            "bypassed_roles": data.get("bypassed_roles", []),
            "role_boundaries": data.get("role_boundaries", {}),
            "reasoning": data.get("reasoning", "")
        }

    stages = []
    if data and isinstance(data, dict):
        roles = data.get("selected_roles") or data.get("stages") or []
        for r in roles:
            role_key = None
            for k in catalog.keys():
                if k.lower() == str(r).lower() or k.lower() in str(r).lower() or str(r).lower() in k.lower():
                    role_key = k
                    break
            if role_key and role_key in catalog:
                spec = catalog[role_key]
                stages.append({
                    "role": spec["role"],
                    "name": spec["name"],
                    "icon": spec.get("icon", "🤖"),
                    "system": spec["system"],
                    "temperature": spec.get("temperature", 0.1),
                    "status": "waiting",
                    "output": "",
                    "error": None
                })

    if not stages:
        text_lower = text.lower()
        if any(w in text_lower for w in ["security", "audit", "vulnerability", "celah", "injection", "token"]):
            fallback_roles = ["Security", "Coder", "QA"]
            scope_matrix = {
                "task_type": "SECURITY_AUDIT",
                "target_scope": "Security Audit & Vulnerability Mitigation",
                "allowed_files": [],
                "forbidden_files": [],
                "selected_roles": ["Security", "Coder", "QA"],
                "bypassed_roles": ["UI/UX", "Architect", "Researcher", "Writer"],
                "role_boundaries": {"Security": "Audit celah", "Coder": "Patch keamanan saja", "QA": "Verifikasi patch"}
            }
        elif any(w in text_lower for w in ["kuliah", "makalah", "riset", "paper", "jurnal"]):
            fallback_roles = ["Researcher", "Writer", "Reviewer"]
            scope_matrix = {
                "task_type": "ACADEMIC",
                "target_scope": "Karya Ilmiah / Riset Akademik",
                "allowed_files": ["*.md"],
                "forbidden_files": [],
                "selected_roles": ["Researcher", "Writer", "Reviewer"],
                "bypassed_roles": ["UI/UX", "Coder", "Security"],
                "role_boundaries": {}
            }
        elif any(w in text_lower for w in ["ui", "ux", "tampilan", "layar", "screen", "frontend", "flutter"]):
            fallback_roles = ["Architect", "UI/UX", "Coder", "QA"]
            scope_matrix = {
                "task_type": "FRONTEND_UI",
                "target_scope": "Pengembangan antarmuka & styling UI/UX",
                "allowed_files": ["index.html"],
                "forbidden_files": ["main.py", "test_security.py"],
                "selected_roles": ["Architect", "UI/UX", "Coder", "QA"],
                "bypassed_roles": ["Security", "Researcher"],
                "role_boundaries": {"Coder": "Hanya modifikasi frontend"}
            }
        else:
            fallback_roles = ["Architect", "Coder", "QA"]
            scope_matrix = {
                "task_type": "FEATURE_DEV",
                "target_scope": "Pengembangan fitur",
                "allowed_files": [],
                "forbidden_files": [],
                "selected_roles": ["Architect", "Coder", "QA"],
                "bypassed_roles": ["UI/UX", "Researcher"],
                "role_boundaries": {}
            }
        for r in fallback_roles:
            if r in catalog:
                spec = catalog[r]
                stages.append({
                    "role": spec["role"],
                    "name": spec["name"],
                    "icon": spec.get("icon", "🤖"),
                    "system": spec["system"],
                    "temperature": spec.get("temperature", 0.1),
                    "status": "waiting",
                    "output": "",
                    "error": None
                })

    return stages, scope_matrix

tasks_store: Dict[str, Dict[str, Any]] = {}
WORKSTATION_SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
workstation_sessions_store: Dict[str, Dict[str, Any]] = {}
SNAPSHOTS_DIR = os.path.join(BASE_DIR, ".snapshots")
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

def load_tasks():
    """SQLite is the source of truth. tasks.json is read once, to migrate legacy state."""
    global tasks_store
    store.init_db()
    tasks_store = store.load_tasks()
    if not tasks_store and os.path.exists(TASKS_FILE):
        migrated = store.import_tasks_from_json(TASKS_FILE)
        if migrated:
            tasks_store = migrated
            store.save_tasks(tasks_store)
            print(f"Migration: {len(tasks_store)} tasks imported from tasks.json -> SQLite")
    # restart reconciliation: tasks whose worker died become 'interrupted'
    recovered = store.reconcile_interrupted(tasks_store)
    if recovered:
        print(f"Reconciliation: {len(recovered)} task(s) marked interrupted: {', '.join(recovered)}")
    return recovered

def _atomic_write_json(file_path: str, data: Any):
    tmp_path = f"{file_path}.{uuid.uuid4().hex}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, file_path)

def save_tasks():
    try:
        store.save_tasks(tasks_store)          # source of truth
        store.export_tasks_json(tasks_store)   # JSON_BACKUP: delete this line to drop JSON state
    except Exception as e:
        print(f"Error saving tasks: {e}")

def save_single_task(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        return
    try:
        store.save_task(task)                  # single-row upsert, avoids O(N*S) write churn
        store.export_tasks_json(tasks_store)   # JSON_BACKUP: delete this line to drop JSON state
    except Exception as e:
        print(f"Error saving task {task_id}: {e}")

load_tasks()

def load_workstation_sessions():
    global workstation_sessions_store
    if os.path.exists(WORKSTATION_SESSIONS_FILE):
        try:
            with open(WORKSTATION_SESSIONS_FILE, "r", encoding="utf-8") as f:
                workstation_sessions_store = json.load(f)
        except Exception as e:
            print(f"Error loading sessions: {e}")
            workstation_sessions_store = {}
    if not workstation_sessions_store:
        s_id = "default"
        workstation_sessions_store[s_id] = {
            "id": s_id,
            "title": "Default Project Session",
            "created_at": time.time(),
            "updated_at": time.time(),
            "pinned": True,
            "working_directory": DEFAULT_WORKSPACE,
            "task_ids": []
        }
        save_workstation_sessions()

def save_workstation_sessions():
    try:
        _atomic_write_json(WORKSTATION_SESSIONS_FILE, workstation_sessions_store)
    except Exception as e:
        print(f"Error saving sessions: {e}")

load_workstation_sessions()

def save_apply_snapshot(wdir: str, task_id: str, records: List[Dict[str, Any]], session_id: Optional[str] = None):
    try:
        data = {
            "timestamp": time.time(),
            "working_directory": wdir,
            "task_id": task_id,
            "session_id": session_id or "default",
            "records": records
        }
        # Save both global latest and session-scoped snapshot to avoid cross-session overwrites
        _atomic_write_json(os.path.join(SNAPSHOTS_DIR, "last_apply.json"), data)
        if session_id:
            safe_sid = re.sub(r'[^a-zA-Z0-9_\-]', '_', session_id)
            _atomic_write_json(os.path.join(SNAPSHOTS_DIR, f"last_apply_{safe_sid}.json"), data)
    except Exception as e:
        print(f"Error saving snapshot: {e}")

def sanitize_path(path: str, base_dir: Optional[str] = None) -> str:
    """Resolve `path` (symlinks included) and require it to stay inside the boundary.

    Returns the fully resolved absolute path so callers never write through an
    in-root symlink that points outside the workspace. With no base_dir the path
    must stay inside any configured workspace root (primary or extra).
    """
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="Akses direktori di luar batas diizinkan.")
    try:
        resolved_path = Path(os.path.abspath(path.strip())).resolve()
        roots = [Path(os.path.abspath(base_dir)).resolve()] if base_dir else [Path(r).resolve() for r in ALLOWED_ROOTS]
        if not any(resolved_path == r or resolved_path.is_relative_to(r) for r in roots):
            raise HTTPException(status_code=400, detail="Akses direktori di luar batas diizinkan.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Akses direktori di luar batas diizinkan.")
    return str(resolved_path)

def generate_repo_map(target_dir: str, max_files: int = 35) -> str:
    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        return ""
    
    ignore_dirs = {'.git', 'node_modules', '.dart_tool', 'build', 'vendor', '__pycache__', 'venv', '.idea', '.vscode', 'dist'}
    relevant_exts = {'.dart', '.php', '.py', '.js', '.ts', '.html', '.sql'}
    
    lines = []
    scanned_count = 0
    
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in relevant_exts:
                scanned_count += 1
                if scanned_count > max_files:
                    return "\n".join(lines)
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, target_dir)
                try:
                    with open(full_path, "r", encoding="utf-8", errors="ignore") as fp:
                        content = fp.read(6000)
                    signatures = []
                    for line in content.splitlines()[:120]:
                        line_s = line.strip()
                        if line_s.startswith(('class ', 'abstract class ', 'enum ', 'interface ', 'Route::')):
                            signatures.append(line_s.split('{')[0].strip())
                    if signatures:
                        lines.append(f"- `{rel_path}`:")
                        for sig in signatures[:4]:
                            lines.append(f"    * {sig}")
                    else:
                        lines.append(f"- `{rel_path}`")
                except Exception:
                    lines.append(f"- `{rel_path}`")
    
    return "\n".join(lines)

def extract_code_files(text: str, scope_matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    file_header_re = re.compile(r'(?:###\s*FILE:|(?:\*\*|#)?FILE:(?:\*\*)?)\s*[`"]?([a-zA-Z0-9_\-\.\/\\]+)[`"]?', re.IGNORECASE)
    matches = list(file_header_re.finditer(text))
    files_map: Dict[str, Dict[str, Any]] = {}

    forbidden_list = []
    allowed_list = []
    if scope_matrix:
        forbidden_list = [f.lower().strip() for f in scope_matrix.get("forbidden_files", []) if f.strip()]
        allowed_list = [f.lower().strip() for f in scope_matrix.get("allowed_files", []) if f.strip()]

    for i, m in enumerate(matches):
        raw_fpath = m.group(1)
        fpath = raw_fpath.strip().replace('\\', '/').strip('/')
        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start_pos:end_pos]

        fence_start_m = re.search(r"^\s*```([a-zA-Z0-9_\-]+)?\s*\n", chunk, re.MULTILINE)
        if fence_start_m:
            lang = (fence_start_m.group(1) or "text").strip()
            code_start = fence_start_m.end()
            fence_end_matches = list(re.finditer(r"\n```\s*$", chunk, re.MULTILINE))
            if fence_end_matches:
                last_fence = fence_end_matches[-1]
                code = chunk[code_start:last_fence.start()]
            else:
                code = chunk[code_start:]
        else:
            lang = "text"
            code = chunk.strip()
        if fpath:
            is_blocked = False
            blocked_reason = None
            fpath_norm = os.path.normpath(fpath.lower().strip())
            fname_only = os.path.basename(fpath_norm)

            # Check forbidden list with path and glob support
            for fb in forbidden_list:
                fb_norm = os.path.normpath(fb)
                fb_base = os.path.basename(fb_norm)
                if (fb_norm == fpath_norm or 
                    fpath_norm.endswith(fb_norm) or 
                    fnmatch.fnmatch(fpath_norm, fb_norm) or 
                    fnmatch.fnmatch(fname_only, fb_norm) or 
                    (fb_base == fname_only and fb_base)):
                    is_blocked = True
                    blocked_reason = "File dilarang diubah oleh Scope Matrix (forbidden_files)"
                    break

            # Check allowed list with path and glob support
            if not is_blocked and allowed_list:
                allowed_match = False
                for al in allowed_list:
                    al_norm = os.path.normpath(al)
                    if al_norm in ("*", "*.*", "."):
                        allowed_match = True
                        break
                    if (al_norm == fpath_norm or 
                        fpath_norm.endswith(al_norm) or 
                        fnmatch.fnmatch(fpath_norm, al_norm) or 
                        fnmatch.fnmatch(fname_only, al_norm)):
                        allowed_match = True
                        break
                    if al_norm.endswith("/*") and fpath_norm.startswith(al_norm[:-2] + "/"):
                        allowed_match = True
                        break
                    al_base = os.path.basename(al_norm)
                    if al_base and "*" not in al_norm and al_base == fname_only:
                        allowed_match = True
                        break
                if not allowed_match:
                    is_blocked = True
                    blocked_reason = "File di luar whitelist izin Scope Matrix (allowed_files)"

            # Match paling akhir menimpa match sebelumnya (hasil revisi auto-fix menang)
            files_map[fpath] = {
                "path": fpath,
                "language": lang,
                "content": code,
                "lines": len(code.strip().splitlines()),
                "blocked": is_blocked,
                "blocked_reason": blocked_reason
            }
    return list(files_map.values())

def get_git_info(target_dir: str) -> Dict[str, Any]:
    git_dir = os.path.join(target_dir, ".git")
    if not os.path.exists(git_dir):
        return {"is_git": False, "branch": None, "clean": True, "status_lines": []}
    try:
        branch = subprocess.check_output(
            ["git", "-C", target_dir, "branch", "--show-current"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        status_raw = subprocess.check_output(
            ["git", "-C", target_dir, "status", "--porcelain"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        status_lines = [s.strip() for s in status_raw.splitlines() if s.strip()]
        return {"is_git": True, "branch": branch or "HEAD", "clean": len(status_lines) == 0, "status_lines": status_lines}
    except Exception:
        return {"is_git": False, "branch": None, "clean": True, "status_lines": []}

class StageConfig(BaseModel):
    role: str
    name: str
    icon: Optional[str] = "🤖"
    system: str
    temperature: Optional[float] = 0.2
    enabled: Optional[bool] = True

class TaskCreateRequest(BaseModel):
    title: str
    prompt: str
    preset_id: Optional[str] = "auto"
    session_id: Optional[str] = "default"
    stages: Optional[List[StageConfig]] = None
    working_directory: Optional[str] = None
    project_context: Optional[str] = None
    skills: Optional[List[str]] = None
    auto_save_artifact: Optional[bool] = True
    auto_apply_files: Optional[bool] = False
    require_approval: Optional[bool] = False
    auto_fix_loops: Optional[int] = 1

class ModelUpdateRequest(BaseModel):
    model: str
    # False = dashboard-scoped override (default, no ~/.hermes/config.yaml write)
    apply_globally: Optional[bool] = False

class TaskApproveRequest(BaseModel):
    action: str = "approve"  # "approve" or "reject"
    feedback: Optional[str] = None

class WorkspaceSaveContextRequest(BaseModel):
    path: str
    content: str

class WorkspaceSaveArtifactRequest(BaseModel):
    path: str
    filename: str
    content: str

class WorkspaceSaveFileRequest(BaseModel):
    path: str
    rel_path: str
    content: str

class WorkspaceCreateItemRequest(BaseModel):
    path: str
    rel_path: str
    is_dir: bool = False

class WorkspaceDeleteItemRequest(BaseModel):
    path: str
    rel_path: str

class WorkspaceRenameItemRequest(BaseModel):
    path: str
    old_rel_path: str
    new_rel_path: str

async def call_llm(system_prompt: str, user_content: str, temperature: float = 0.2) -> str:
    base_url, model, key = get_llm_config()
    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}"
    }

    anti_tool_call_guard = (
        "\n\n[ATURAN TEKNIS MUTLAK]:\n"
        "1. DILARANG keras memanggil tool calling, format XML <｜｜DSML｜｜ calls>, atau sintaks perintah bash seperti 'cd /... && ls'.\n"
        "2. Kamu BUKAN terminal bash dan tidak memiliki console eksekusi langsung. Semua analisis, arsitektur, laporan, dan kode harus ditulis langsung dalam bentuk teks dan blok markdown murni."
    )
    final_system = f"{system_prompt}{anti_tool_call_guard}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": final_system},
            {"role": "user", "content": user_content}
        ],
        "temperature": max(0.0, min(1.0, float(temperature))),
        "stream": True
    }

    content_chunks = []
    reasoning_chunks = []

    timeout = httpx.Timeout(180.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as res:
            if res.status_code != 200:
                body = await res.aread()
                raise RuntimeError(f"HTTP {res.status_code} dari {base_url}: {body.decode(errors='ignore')}")
            
            async for line in res.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0]["delta"]
                    c = delta.get("content")
                    if c:
                        content_chunks.append(c)
                    r = delta.get("reasoning_content")
                    if r:
                        reasoning_chunks.append(r)
                except Exception:
                    pass

    full_content = "".join(content_chunks).strip()
    if not full_content and reasoning_chunks:
        full_content = "".join(reasoning_chunks).strip()

    if not full_content:
        raise RuntimeError(f"LLM Gateway ({base_url}) mengembalikan respons kosong (0 tokens). Periksa apakah model '{model}' memiliki kuota atau sedang dibatasi.")

    return full_content


class ContextLog:
    """Structured context model: ordered typed entries instead of one growing string.

    Static context (working dir, repo map, rules, skills, goal, notes) is stored
    inline; stage outputs are stored as references (no duplication) and resolved
    at render time. Entries are mirrored into task["context_model"] so the
    structure survives a restart. render() reproduces the legacy prompt string
    byte for byte.
    """

    def __init__(self, task: Dict[str, Any], entries: Optional[List[Dict[str, Any]]] = None):
        self.task = task
        self.entries: List[Dict[str, Any]] = list(entries or [])
        task["context_model"] = self.entries

    def _commit(self) -> None:
        self.task["context_model"] = self.entries

    def add(self, kind: str, content: str) -> None:
        self.entries.append({"kind": kind, "content": content})
        self._commit()

    def add_stage_output(self, stage_idx: int) -> None:
        self.entries.append({"kind": "stage_output", "stage_idx": stage_idx})
        self._commit()

    def add_stage_bypass(self, stage_idx: int, reason: str) -> None:
        self.entries.append({"kind": "stage_bypassed", "stage_idx": stage_idx, "reason": reason})
        self._commit()

    @staticmethod
    def _stage_ref_text(stage: Dict[str, Any], entry: Dict[str, Any]) -> str:
        role, name = stage.get("role", ""), stage.get("name", "")
        if entry["kind"] == "stage_output":
            return f"=== HASIL DARI [{role} - {name}] ===\n{stage.get('output') or ''}\n\n"
        return (f"=== [{role} - {name}] DILEWATI (OUT OF SCOPE) ===\n"
                f"{entry.get('reason') or 'Tugas di luar domain peran ini.'}\n\n")

    def render(self) -> str:
        parts: List[str] = []
        stages = self.task.get("stages") or []
        for entry in self.entries:
            if "content" in entry:
                parts.append(entry["content"])
                continue
            idx = entry.get("stage_idx", -1)
            if 0 <= idx < len(stages):
                parts.append(self._stage_ref_text(stages[idx], entry))
        return "".join(parts)


async def execute_pipeline(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        return
    task["status"] = "running"
    task["started_at"] = time.time()
    save_single_task(task_id)
    store.add_event(task_id, "task.started", {"preset": task.get("preset_id")})

    ctx = ContextLog(task)
    wdir = None
    if task.get("working_directory"):
        try:
            wdir = sanitize_path(task["working_directory"])
            ctx.add("working_directory", f"=== WORKING DIRECTORY: {wdir} ===\n")
            
            # Auto-inject codebase skeleton / repo map
            repo_map = generate_repo_map(wdir)
            if repo_map:
                ctx.add("repo_map", f"=== EXISTING REPOSITORY SKELETON (CODEBASE) ===\n{repo_map}\n\n")
        except Exception:
            pass

    if task.get("project_context"):
        ctx.add("project_rules", f"=== PROJECT RULES & CONTEXT (AGENTS.md) ===\n{task['project_context']}\n\n")
    
    # Auto-inject Hermes Skills SOPs
    if task.get("skills"):
        skills_context = ""
        for sk in task["skills"]:
            content = read_skill_content(sk)
            if content:
                skills_context += f"--- HERMES SKILL SOP: {sk} ---\n{content}\n\n"
        if skills_context:
            ctx.add("skills", f"=== ACTIVE HERMES SKILLS / GUIDELINES ===\n{skills_context}\n")

    ctx.add("goal", f"=== TARGET TUGAS / GOAL ===\n{task['prompt']}\n\n")

    # Auto-inject previous session turn context if session exists
    s_id = task.get("session_id")
    if s_id and s_id in workstation_sessions_store:
        prev_ids = [tid for tid in workstation_sessions_store[s_id].get("task_ids", []) if tid != task_id and tid in tasks_store]
        if prev_ids:
            for tid in reversed(prev_ids):
                prev_t = tasks_store[tid]
                if prev_t.get("status") == "completed" and prev_t.get("final_output"):
                    prev_files = [f["path"] for f in prev_t.get("extracted_files", [])]
                    ctx.add("previous_turn", (
                        f"=== PREVIOUS SESSION TURN CONTEXT (MULTI-TURN ITERATION) ===\n"
                        f"- Sesi Proyek: {workstation_sessions_store[s_id].get('title', 'Project Session')}\n"
                        f"- Tugas Sebelumnya: {prev_t.get('title', '')}\n"
                        f"- Ringkasan Deliverable Sebelumnya:\n{prev_t['final_output'][:1500]}\n"
                        f"- File yang Dihasilkan Sebelumnya: {', '.join(prev_files) or 'None'}\n"
                        f"=== END OF PREVIOUS TURN ===\n\n"
                    ))
                    break

    try:
        idx = 0
        while idx < len(task["stages"]):
            stage = task["stages"][idx]
            if task.get("cancelled", False):
                stage["status"] = "cancelled"
                break

            # Fast-bypass check (Orchestrator Stage 0 bypass)
            if stage.get("status") == "bypassed":
                idx += 1
                continue

            stage["status"] = "running"
            stage["started_at"] = time.time()
            save_single_task(task_id)
            store.add_event(task_id, "stage.started", {"index": idx, "role": stage.get("role")})

            boundary_note = ""
            jobdesk_note = ""
            if stage["role"] != "Orchestrator":
                if task.get("scope_matrix"):
                    sm = task["scope_matrix"]
                    r_bound = sm.get("role_boundaries", {}).get(stage["role"], "")
                    allowed = sm.get("allowed_files", [])
                    forbidden = sm.get("forbidden_files", [])
                    boundary_note = (
                        f"\n=== SCOPE MATRIX ENFORCEMENT ===\n"
                        f"- Target Scope: {sm.get('target_scope', 'Sesuai prompt')}\n"
                        f"- File yang BOLEH diubah/dibuat: {', '.join(allowed) or 'File relevan'}\n"
                        f"- File TERLARANG disentuh: {', '.join(forbidden) or 'None'}\n"
                    )
                    if r_bound:
                        boundary_note += f"- Batasan Khusus Peranmu: {r_bound}\n"

                jobdesk_note = (
                    f"\n=== UNIVERSAL JOBDESK CONTRACT & RELEVANCE EVALUATION ===\n"
                    f"Peranmu: [{stage['role']} - {stage['name']}].\n"
                    f"Evaluasi sebelum bekerja:\n"
                    f"1. Jika target tugas SEPENUHNYA DI LUAR DOMAINMU (contoh: peranmu UI/UX tapi tugas backend/API, atau peranmu Architect tapi tugas styling):\n"
                    f"   DILARANG mengarang fitur atau file baru. Kamu WAJIB menjawab HANYA dengan 2 baris:\n"
                    f"   STATUS: OUT_OF_SCOPE\n"
                    f"   Alasan: [Jelaskan singkat 1 kalimat mengapa peranmu tidak dibutuhkan di tugas ini]\n"
                    f"2. Jika tugas RELEVAN dengan peranmu:\n"
                    f"   Lakukan tugasmu sesuai spesialisasi. Jangan melanggar batasan Scope Matrix di atas.\n"
                )

            # Pull inbox of structured messages directed to this role (exclude raw stage outputs already in ctx.render())
            pending_msgs = store.list_messages(task_id, to_role=stage["role"])
            structured_kinds = {"QUESTION", "ANSWER", "FINDING", "REQUEST_CHANGE", "ARCHITECTURE_CONCERN", "TEST_FAILED", "TEST_PASSED", "APPROVAL_REQUIRED", "APPROVED", "REJECTED", "BLOCKED"}
            collab_msgs = [m for m in pending_msgs if m.get("kind") in structured_kinds]
            inbox_note = ""
            if collab_msgs:
                inbox_lines = [f"\n=== PESAN & DIRECTIVE DARI REKAN TIM UNTUK [{stage['role']}] ==="]
                for pm in collab_msgs[-4:]:
                    snippet = (pm.get('content') or '').strip()
                    if len(snippet) > 400:
                        snippet = snippet[:400] + "..."
                    inbox_lines.append(f"- Dari {pm.get('role')} [{pm.get('kind')}]: {snippet}")
                inbox_note = "\n".join(inbox_lines) + "\n"

            user_msg = (
                f"{ctx.render()}\n"
                f"{boundary_note}\n"
                f"{jobdesk_note}\n"
                f"{inbox_note}"
                f"Tugas kamu sekarang sebagai [{stage['role']} - {stage['name']}]:\n"
                f"Lakukan tugas sesuai peran dan panduan spesialisasi yang diberikan."
            )

            store.add_event(task_id, "agent.started", {"role": stage["role"], "name": stage["name"], "index": idx})

            try:
                temp = stage.get("temperature", 0.2)
                output = await call_llm(stage["system"], user_msg, temperature=temp)
                stage["output"] = output

                output_clean = output.replace(" ", "").upper()
                if stage["role"] != "Orchestrator" and ("STATUS:OUT_OF_SCOPE" in output_clean or "STATUS: OUT_OF_SCOPE" in output.upper()):
                    stage["status"] = "bypassed"
                    stage["completed_at"] = time.time()
                    reason_line = ""
                    for line in output.splitlines():
                        if any(k in line.lower() for k in ["alasan", "reason", "out_of_scope"]):
                            reason_line = line.strip()
                            break
                    # Context isolation: isolate from subsequent stages
                    ctx.add_stage_bypass(idx, reason_line or 'Tugas di luar domain peran ini.')
                    store.add_event(task_id, "stage.bypassed", {"index": idx, "role": stage.get("role")})
                    store.add_message(task_id, stage.get("role") or "agent", "stage_bypassed",
                                      reason_line or 'Tugas di luar domain peran ini.', stage_idx=idx)
                else:
                    stage["status"] = "completed"
                    stage["completed_at"] = time.time()
                    ctx.add_stage_output(idx)
                    store.add_event(task_id, "agent.completed", {"index": idx, "role": stage.get("role")})
                    store.add_event(task_id, "stage.completed", {"index": idx, "role": stage.get("role")})
                    store.add_message(task_id, stage.get("role") or "agent", "stage_output",
                                      output, stage_idx=idx)

                    # Extract structured collaborative signals: FINDING, QUESTION, ARCHITECTURE_CONCERN
                    if re.search(r'(?:FINDING|TEMUAN):', output, re.IGNORECASE):
                        fm = re.search(r'(?:FINDING|TEMUAN):\s*(.*?)(?=\n\s*(?:###|[A-Z_]{3,}:)|$)', output, re.IGNORECASE | re.DOTALL)
                        ftxt = fm.group(1).strip() if fm else output[:120]
                        store.add_message(task_id, stage["role"], "FINDING", ftxt, stage_idx=idx, to_role="all")
                        store.add_event(task_id, "agent.finding", {"role": stage["role"], "finding": ftxt})

                    if re.search(r'(?:QUESTION|TANYA)', output, re.IGNORECASE):
                        qm = re.search(r'(?:QUESTION|TANYA)(?:\s+(?:KE|TO))?\s*([a-zA-Z0-9_\-]+)?:\s*(.*?)(?=\n\s*(?:###|[A-Z_]{3,}:)|$)', output, re.IGNORECASE | re.DOTALL)
                        if qm:
                            q_target = qm.group(1) or "all"
                            q_txt = qm.group(2).strip()
                            store.add_message(task_id, stage["role"], "QUESTION", q_txt, stage_idx=idx, to_role=q_target)
                            store.add_event(task_id, "agent.question", {"from": stage["role"], "to": q_target, "question": q_txt})

                    if "ARCHITECTURE_CONCERN:" in output or "ARCHITECTURE CONCERN:" in output:
                        acm = re.search(r'ARCHITECTURE_?CONCERN:\s*(.*?)(?=\n\s*(?:###|[A-Z_]{3,}:)|$)', output, re.IGNORECASE | re.DOTALL)
                        actxt = acm.group(1).strip() if acm else "Perhatian terhadap konsistensi arsitektur."
                        store.add_message(task_id, stage["role"], "ARCHITECTURE_CONCERN", actxt, stage_idx=idx, to_role="Architect")
                        store.add_event(task_id, "agent.message", {"type": "ARCHITECTURE_CONCERN", "from": stage["role"], "to": "Architect", "summary": actxt})
            except Exception as e:
                err_type = type(e).__name__
                raw_err = str(e).strip()
                base_url, model, _ = get_llm_config()

                if isinstance(e, httpx.ReadTimeout) or "ReadTimeout" in err_type or "Timeout" in err_type:
                    err_detail = f"LLM Gateway Timeout ({base_url}): Server LLM tidak merespons dalam 180s. Kemungkinan model '{model}' kehabisan slot antrean atau lambat memproses prompt panjang."
                elif isinstance(e, httpx.ConnectError) or "ConnectError" in err_type:
                    err_detail = f"LLM Gateway Unreachable ({base_url}): Gagal membuka koneksi ke port LLM. Pastikan server LLM lokal (Ollama / LM Studio / 9Router) sudah aktif di port tersebut."
                elif "401" in raw_err or "Unauthorized" in raw_err:
                    err_detail = f"Autentikasi Gagal (HTTP 401): API key tidak sah atau sudah kedaluwarsa di gateway {base_url}."
                elif "404" in raw_err:
                    err_detail = f"Model Tidak Ditemukan (HTTP 404): Model '{model}' tidak terdaftar pada endpoint {base_url}."
                elif "429" in raw_err:
                    err_detail = f"Rate Limit / Quota Exceeded (HTTP 429): Terlalu banyak request atau kuota model '{model}' habis di provider."
                else:
                    err_detail = f"{err_type}: {raw_err or 'Koneksi terputus atau respon tidak valid dari LLM gateway'}"

                stage["status"] = "error"
                stage["error"] = err_detail
                stage["completed_at"] = time.time()
                task["status"] = "failed"
                task["error"] = f"Gagal pada tahap [{stage['name']}]: {err_detail}"
                task["completed_at"] = time.time()
                save_single_task(task_id)
                store.add_event(task_id, "stage.failed", {"index": idx, "role": stage.get("role"),
                                                          "error": err_detail})
                store.add_event(task_id, "task.failed", {"error": task.get("error")})
                return

            save_single_task(task_id)

            # --- DYNAMIC ORCHESTRATION & FAST-BYPASS ---
            if stage["role"] == "Orchestrator":
                dynamic_stages, scope_matrix = parse_orchestrator_plan(output)
                if scope_matrix:
                    task["scope_matrix"] = scope_matrix
                    ctx.add("scope_matrix", (
                        f"=== SCOPE MATRIX & EXECUTION BOUNDARIES (ENFORCED BY ORCHESTRATOR) ===\n"
                        f"- Target Scope: {scope_matrix.get('target_scope', 'Sesuai prompt')}\n"
                        f"- Allowed Files: {', '.join(scope_matrix.get('allowed_files', [])) or 'File relevan'}\n"
                        f"- Forbidden Files: {', '.join(scope_matrix.get('forbidden_files', [])) or 'None'}\n"
                    ))
                    if scope_matrix.get("role_boundaries"):
                        ctx.add("scope_matrix", "- Role Boundaries:\n")
                        for rb_k, rb_v in scope_matrix["role_boundaries"].items():
                            ctx.add("scope_matrix", f"  * {rb_k}: {rb_v}\n")
                    ctx.add("scope_matrix", "\n")

                    bypassed_roles = [r.lower().strip() for r in scope_matrix.get("bypassed_roles", [])]
                    selected_roles = [r.lower().strip() for r in scope_matrix.get("selected_roles", [])]

                    # Fast-bypass existing pending stages (Stage 0 instant bypass 0s)
                    for stg in task["stages"]:
                        if stg["role"] != "Orchestrator" and stg.get("status") == "waiting":
                            r_low = stg["role"].lower().strip()
                            n_low = stg["name"].lower().strip()
                            if any(b in r_low or b in n_low for b in bypassed_roles):
                                stg["status"] = "bypassed"
                                stg["output"] = f"STATUS: OUT_OF_SCOPE\nDilewati otomatis oleh Orchestrator (di luar target scope: {scope_matrix.get('target_scope', '')})."
                                stg["completed_at"] = time.time()
                            elif selected_roles and not any(sel in r_low or sel in n_low for sel in selected_roles):
                                stg["status"] = "bypassed"
                                stg["output"] = f"STATUS: OUT_OF_SCOPE\nDilewati otomatis oleh Orchestrator (peran tidak terpilih)."
                                stg["completed_at"] = time.time()

                if dynamic_stages:
                    for ds in dynamic_stages:
                        task["stages"].append(ds)

                # In auto mode, append bypassed roles as visual timeline stubs at the end of pipeline
                existing_roles = [s["role"].lower() for s in task["stages"]]
                for bp_name in scope_matrix.get("bypassed_roles", []):
                    bp_clean = bp_name.strip()
                    if not bp_clean:
                        continue
                    matched_key = None
                    for k in SPECIALIST_CATALOG.keys():
                        if k.lower() == bp_clean.lower() or bp_clean.lower() in k.lower():
                            matched_key = k
                            break
                    spec = SPECIALIST_CATALOG.get(matched_key, {
                        "role": bp_clean,
                        "name": bp_clean,
                        "icon": "⊘",
                        "system": f"Peran {bp_clean}"
                    })
                    if spec["role"].lower() not in existing_roles:
                        task["stages"].append({
                            "role": spec["role"],
                            "name": spec["name"],
                            "icon": spec.get("icon", "⊘"),
                            "system": spec.get("system", ""),
                            "temperature": 0.1,
                            "status": "bypassed",
                            "output": f"STATUS: OUT_OF_SCOPE\nDilewati otomatis oleh Orchestrator pada Tahap 0 (di luar target scope: '{scope_matrix.get('target_scope', '')}').",
                            "completed_at": time.time(),
                            "error": None
                        })
                        existing_roles.append(spec["role"].lower())

                save_single_task(task_id)

            # --- HUMAN APPROVAL GATE ---
            # If enabled and current stage is Orchestrator, Architect or Planner, pause for human steering
            if task.get("require_approval") and stage["role"] in ["Orchestrator", "Architect", "Planner"] and not task.get("stages_approved", {}).get(str(idx)):
                task["status"] = "waiting_approval"
                task["waiting_stage_index"] = idx
                task["waiting_stage_name"] = stage["name"]
                save_single_task(task_id)
                store.add_event(task_id, "approval.requested",
                                {"index": idx, "role": stage.get("role"), "name": stage.get("name")})

                event = asyncio.Event()
                APPROVAL_EVENTS[task_id] = event
                await event.wait()
                APPROVAL_EVENTS.pop(task_id, None)

                if task.get("cancelled"):
                    break

                task["status"] = "running"
                task["waiting_stage_index"] = None
                task["waiting_stage_name"] = None
                
                # If human gave feedback during approval, inject into context
                if task.get("latest_feedback"):
                    ctx.add("human_feedback", f"=== HUMAN SUPERVISOR FEEDBACK & DIRECTIVES ===\n{task['latest_feedback']}\n\n")
                    task["latest_feedback"] = None
                store.add_event(task_id, "approval.granted", {"index": idx})
                save_single_task(task_id)

            # --- AUTO-FIX / VERIFICATION LOOP & COLLABORATIVE GATE ---
            if (stage["role"] in ["QA", "Reviewer"] or "QA" in stage.get("name", "")) and stage.get("status") == "completed":
                if "VERDICT: NEEDS_REVISION" in output or "TEST_FAILED" in output or "STATUS: NEEDS_REVISION" in output:
                    store.add_message(
                        task_id, stage["role"], "REQUEST_CHANGE", output,
                        stage_idx=idx, to_role="Coder", meta={"verdict": "NEEDS_REVISION"}
                    )
                    store.add_event(task_id, "test.failed", {"role": stage["role"], "index": idx})

                    curr_loop = task.get("current_fix_loop", 0)
                    max_loops = task.get("auto_fix_loops", 1)
                    if curr_loop < max_loops:
                        task["current_fix_loop"] = curr_loop + 1
                        
                        fix_stage = {
                            "role": "Coder",
                            "name": f"Lead Developer (Auto-Fix Cycle #{task['current_fix_loop']})",
                            "icon": "🔧",
                            "temperature": 0.1,
                            "system": "Kamu adalah Lead Full-Stack Developer. QA menemukan catatan perbaikan/bug pada kode sebelumnya. Analisis kritik QA, perbaiki implementasi secara presisi dan patuhi Scope Matrix. Setiap file wajib ditulis dengan format `### FILE: path/to/file.ext`.",
                            "status": "waiting",
                            "output": "",
                            "error": None
                        }
                        qa_re_stage = {
                            "role": "QA",
                            "name": f"QA & Security Re-Verification #{task['current_fix_loop']}",
                            "icon": "🛡️",
                            "temperature": 0.1,
                            "system": stage["system"],
                            "status": "waiting",
                            "output": "",
                            "error": None
                        }
                        task["stages"].append(fix_stage)
                        task["stages"].append(qa_re_stage)
                        save_single_task(task_id)
                        store.add_event(task_id, "task.auto_fix", {"cycle": task["current_fix_loop"]})
                    else:
                        # Max loops reached: pause and wait for human supervisor decision
                        task["status"] = "waiting_approval"
                        task["waiting_stage_index"] = idx
                        task["waiting_stage_name"] = f"QA Review Gate (Maks. Perbaikan #{max_loops}x)"
                        save_single_task(task_id)
                        store.add_message(
                            task_id, stage["role"], "APPROVAL_REQUIRED",
                            "Batas siklus perbaikan otomatis tercapai namun masih ditemukan catatan QA. Memerlukan keputusan supervisor.",
                            stage_idx=idx, to_role="user"
                        )
                        store.add_event(task_id, "approval.requested", {"index": idx, "reason": "auto_fix_exhausted"})

                        event = asyncio.Event()
                        APPROVAL_EVENTS[task_id] = event
                        await event.wait()
                        APPROVAL_EVENTS.pop(task_id, None)

                        if task.get("cancelled"):
                            task["status"] = "cancelled"
                            task["completed_at"] = time.time()
                            task["final_output"] = ctx.render()
                            save_single_task(task_id)
                            store.add_event(task_id, "task.cancelled", {})
                            return

                        task["status"] = "running"
                        task["waiting_stage_index"] = None
                        task["waiting_stage_name"] = None
                        save_single_task(task_id)
                elif "VERDICT: PASSED" in output or "TEST_PASSED" in output:
                    store.add_message(
                        task_id, stage["role"], "TEST_PASSED", "Semua pengujian dan verifikasi berhasil (VERDICT: PASSED).",
                        stage_idx=idx, to_role="all", meta={"verdict": "PASSED"}
                    )
                    store.add_event(task_id, "test.passed", {"role": stage["role"], "index": idx})

            idx += 1

        if task.get("cancelled"):
            task["status"] = "cancelled"
            task["completed_at"] = time.time()
            task["final_output"] = ctx.render()
            save_single_task(task_id)
            store.add_event(task_id, "task.cancelled", {})
            return

        task["status"] = "completed"
        task["completed_at"] = time.time()
        task["final_output"] = ctx.render()
        store.add_event(task_id, "task.completed", {"stages": len(task.get("stages") or [])})

        # Extract multi-file blocks prioritizing Coder/Fixer outputs to avoid QA comments polluting code
        code_producing_roles = {"coder", "auto-fixer", "developer", "lead developer"}
        coder_outputs = [
            s.get("output", "") for s in task.get("stages", [])
            if s.get("status") == "completed" and any(r in s.get("role", "").lower() or r in s.get("name", "").lower() for r in code_producing_roles)
        ]
        text_for_extraction = "\n\n".join(coder_outputs) if coder_outputs else ctx.render()
        extracted = extract_code_files(text_for_extraction, scope_matrix=task.get("scope_matrix"))
        task["extracted_files"] = extracted

        # Auto-write files if requested and working directory is set (with Scope Matrix enforcement & Snapshot)
        if task.get("auto_apply_files") and wdir and extracted:
            if not store.check_agent_permission("Coder", "filesystem", "write"):
                store.add_event(task_id, "permission.denied", {"role": "Coder", "action": "filesystem.write"})
            else:
                written_files = []
                blocked_files = []
                snapshot_records = []
                for item in extracted:
                    rel_p = item["path"]
                    if item.get("blocked"):
                        blocked_files.append({"path": rel_p, "reason": item.get("blocked_reason")})
                        continue
                    try:
                        full_p = sanitize_path(os.path.join(wdir, rel_p.lstrip("/")), base_dir=wdir)
                        os.makedirs(os.path.dirname(full_p), exist_ok=True)
                        existed = os.path.exists(full_p)
                        backup_p = f"{full_p}.bak"
                        if existed:
                            shutil.copy2(full_p, backup_p)
                        with open(full_p, "w", encoding="utf-8") as fp:
                            fp.write(item["content"])
                        written_files.append(rel_p)
                        snapshot_records.append({
                            "target": full_p,
                            "backup": backup_p,
                            "existed": existed,
                            "rel_path": rel_p
                        })
                    except Exception as e:
                        print(f"Error applying file {item['path']}: {e}")
                task["applied_files"] = written_files
                task["blocked_files"] = blocked_files
                if snapshot_records:
                    save_apply_snapshot(wdir, task_id, snapshot_records, session_id=task.get("session_id"))

        # Auto-save deliverable document if enabled
        if task.get("auto_save_artifact") and wdir:
            try:
                if os.path.exists(wdir) and os.path.isdir(wdir):
                    artifact_name = f"DELIVERABLE_{task_id}.md"
                    artifact_path = sanitize_path(os.path.join(wdir, artifact_name), base_dir=wdir)
                    with open(artifact_path, "w", encoding="utf-8") as f:
                        f.write(ctx.render())
                    task["saved_artifact_path"] = artifact_path
            except Exception as e:
                task["saved_artifact_error"] = str(e)

    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)
        store.add_event(task_id, "task.error", {"error": str(e)})
    finally:
        save_single_task(task_id)

@app.get("/api/presets")
async def get_presets():
    return list(PRESETS.values())

def get_workspace_presets() -> List[Dict[str, str]]:
    """Quick-switch workspaces from WORKSPACE_PRESETS env ('path|Label;path|Label').

    Machine-specific paths live in .env (gitignored) or the service unit, never
    in source.
    """
    raw = os.environ.get("WORKSPACE_PRESETS", "")
    presets: List[Dict[str, str]] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        path, _, label = chunk.partition("|")
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip())))
        if not path:
            continue
        presets.append({"path": path, "label": (label.strip() or os.path.basename(path.rstrip("/")) or path)})
    if not presets:
        presets.append({"path": DEFAULT_WORKSPACE, "label": os.path.basename(DEFAULT_WORKSPACE.rstrip("/")) or "workspace"})
    return presets

@app.get("/api/workspace/presets")
async def api_workspace_presets():
    return {"default": DEFAULT_WORKSPACE, "presets": get_workspace_presets(), "roots": ALLOWED_ROOTS}

@app.get("/api/workspace/info")
async def get_workspace_info(path: Optional[str] = None):
    target = sanitize_path(path or DEFAULT_WORKSPACE)
    if not os.path.exists(target):
        return {"exists": False, "path": target, "files": [], "agents_md": "", "agents_md_file": None, "git": {"is_git": False}}

    files_list = []
    agents_md_content = ""
    readme_content = ""

    try:
        entries = sorted(os.scandir(target), key=lambda e: (not e.is_dir(), e.name.lower()))
        for entry in entries[:60]:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
            files_list.append({
                "name": entry.name,
                "is_dir": is_dir,
                "size": size
            })

            if entry.name.lower() == "agents.md" and not is_dir:
                try:
                    with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                        agents_md_content = f.read(15000)
                except Exception:
                    pass
            elif entry.name.lower() == "readme.md" and not is_dir and not agents_md_content:
                try:
                    with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                        readme_content = f.read(10000)
                except Exception:
                    pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    git_info = get_git_info(target)

    return {
        "exists": True,
        "path": target,
        "files": files_list,
        "agents_md": agents_md_content or readme_content,
        "agents_md_file": "AGENTS.md" if agents_md_content else ("README.md" if readme_content else None),
        "git": git_info
    }

@app.get("/api/workspace/files")
async def get_workspace_files(path: str, subpath: str = ""):
    target_dir = sanitize_path(path)
    clean_sub = subpath.strip().strip("/")
    cur_dir = sanitize_path(os.path.join(target_dir, clean_sub), base_dir=target_dir) if clean_sub else target_dir

    if not os.path.exists(cur_dir) or not os.path.isdir(cur_dir):
        raise HTTPException(status_code=404, detail="Direktori tidak ditemukan.")

    try:
        entries = sorted(os.scandir(cur_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
        items = []
        for entry in entries[:120]:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
            rel = os.path.relpath(entry.path, target_dir)
            items.append({
                "name": entry.name,
                "rel_path": rel,
                "is_dir": is_dir,
                "size": size
            })

        parent_sub = os.path.dirname(clean_sub) if clean_sub else None
        if parent_sub == "" and clean_sub:
            parent_sub = ""

        return {
            "root": target_dir,
            "subpath": clean_sub,
            "parent_subpath": parent_sub,
            "entries": items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _build_tree(base_dir: str, rel_dir: str, depth: int, max_depth: int, ignored: set) -> List[Dict[str, Any]]:
    if depth > max_depth:
        return []
    cur = os.path.join(base_dir, rel_dir) if rel_dir else base_dir
    try:
        entries = sorted(os.scandir(cur), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return []
    result = []
    for e in entries:
        if e.name.startswith(".") or e.name in ignored:
            continue
        # skip symlinks — prevents traversal outside base_dir via symlink escape
        if e.is_symlink():
            continue
        rel = os.path.relpath(e.path, base_dir)
        if e.is_dir(follow_symlinks=False):
            truncated = depth + 1 > max_depth
            children = [] if truncated else _build_tree(base_dir, rel, depth + 1, max_depth, ignored)
            result.append({"name": e.name, "rel_path": rel, "is_dir": True, "children": children, "truncated": truncated})
        else:
            result.append({"name": e.name, "rel_path": rel, "is_dir": False, "size": e.stat().st_size, "children": []})
    return result

@app.get("/api/workspace/tree")
async def get_workspace_tree(path: str, depth: int = 4):
    target_dir = sanitize_path(path)
    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        raise HTTPException(status_code=404, detail="Direktori tidak ditemukan.")
    ignored = {"__pycache__", "node_modules", ".git", ".snapshots", "venv", ".venv", ".hermes", "dist", "build"}
    tree = _build_tree(target_dir, "", 0, min(depth, 6), ignored)
    return {"root": target_dir, "tree": tree}

@app.get("/api/workspace/file-content")
async def get_file_content(path: str, filename: str):
    target_dir = sanitize_path(path)
    clean_file = filename.strip().strip("/")
    file_path = sanitize_path(os.path.join(target_dir, clean_file), base_dir=target_dir)
    if not os.path.exists(file_path) or os.path.isdir(file_path):
        raise HTTPException(status_code=404, detail="File tidak ditemukan.")
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(50000)
        return {"filename": clean_file, "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workspace/save-file")
async def save_workspace_file(req: WorkspaceSaveFileRequest):
    target_dir = sanitize_path(req.path)
    clean_file = req.rel_path.strip().strip("/")
    file_path = sanitize_path(os.path.join(target_dir, clean_file), base_dir=target_dir)
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"status": "ok", "path": file_path, "rel_path": clean_file}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workspace/create-item")
async def create_workspace_item(req: WorkspaceCreateItemRequest):
    target_dir = sanitize_path(req.path)
    clean_rel = req.rel_path.strip().strip("/")
    if not clean_rel:
        raise HTTPException(status_code=400, detail="Nama item tidak boleh kosong.")
    dest_path = sanitize_path(os.path.join(target_dir, clean_rel), base_dir=target_dir)
    if os.path.exists(dest_path):
        raise HTTPException(status_code=400, detail="File atau folder sudah ada.")
    try:
        if req.is_dir:
            os.makedirs(dest_path, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write("")
        return {"status": "ok", "path": dest_path, "rel_path": clean_rel, "is_dir": req.is_dir}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workspace/delete-item")
async def delete_workspace_item(req: WorkspaceDeleteItemRequest):
    target_dir = sanitize_path(req.path)
    clean_rel = req.rel_path.strip().strip("/")
    if not clean_rel:
        raise HTTPException(status_code=400, detail="Tidak dapat menghapus root.")
    dest_path = sanitize_path(os.path.join(target_dir, clean_rel), base_dir=target_dir)
    if dest_path == target_dir:
        raise HTTPException(status_code=403, detail="Akses ditolak di luar direktori workspace.")
    if not os.path.exists(dest_path):
        raise HTTPException(status_code=404, detail="Item tidak ditemukan.")
    try:
        if os.path.isdir(dest_path):
            shutil.rmtree(dest_path)
        else:
            os.remove(dest_path)
        return {"status": "ok", "deleted": clean_rel}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workspace/rename-item")
async def rename_workspace_item(req: WorkspaceRenameItemRequest):
    target_dir = sanitize_path(req.path)
    old_clean = req.old_rel_path.strip().strip("/")
    new_clean = req.new_rel_path.strip().strip("/")
    if not old_clean or not new_clean:
        raise HTTPException(status_code=400, detail="Nama item tidak valid.")
    old_path = sanitize_path(os.path.join(target_dir, old_clean), base_dir=target_dir)
    new_path = sanitize_path(os.path.join(target_dir, new_clean), base_dir=target_dir)
    if not os.path.exists(old_path):
        raise HTTPException(status_code=404, detail="Item asal tidak ditemukan.")
    if os.path.exists(new_path):
        raise HTTPException(status_code=400, detail="Item tujuan sudah ada.")
    try:
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.rename(old_path, new_path)
        return {"status": "ok", "old": old_clean, "new": new_clean}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workspace/save-context")
async def save_workspace_context(req: WorkspaceSaveContextRequest):
    target_dir = sanitize_path(req.path)
    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        raise HTTPException(status_code=400, detail="Direktori tidak valid.")
    agents_path = os.path.join(target_dir, "AGENTS.md")
    try:
        with open(agents_path, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"status": "ok", "path": agents_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/workspace/save-artifact")
async def save_workspace_artifact(req: WorkspaceSaveArtifactRequest):
    target_dir = sanitize_path(req.path)
    if not os.path.exists(target_dir) or not os.path.isdir(target_dir):
        raise HTTPException(status_code=400, detail="Direktori tidak valid.")
    fname = req.filename or f"DELIVERABLE_{int(time.time())}.md"
    # Artifact name must be a plain file name: strip any directory part and re-check the boundary.
    fname = os.path.basename(fname.replace("\\", "/").strip())
    if not fname or fname in (".", ".."):
        raise HTTPException(status_code=400, detail="Nama artifact tidak valid.")
    file_path = sanitize_path(os.path.join(target_dir, fname), base_dir=target_dir)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(req.content)
        return {"status": "ok", "path": file_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class EnhancePromptRequest(BaseModel):
    prompt: str
    working_directory: Optional[str] = None
    preset_id: Optional[str] = "auto"
    skills: Optional[List[str]] = None

@app.post("/api/prompt/enhance")
async def enhance_prompt_endpoint(req: EnhancePromptRequest):
    raw_prompt = req.prompt.strip()
    if not raw_prompt:
        raise HTTPException(status_code=400, detail="Prompt tidak boleh kosong.")

    context_snippet = ""
    if req.working_directory:
        try:
            wdir = sanitize_path(req.working_directory)
            agents_md = os.path.join(wdir, "AGENTS.md")
            if os.path.exists(agents_md):
                with open(agents_md, "r", encoding="utf-8") as f:
                    context_snippet += f"\n[Tech Stack / Rules dari AGENTS.md]:\n{f.read()[:600]}"
            repo_map = generate_repo_map(wdir, max_files=15)
            if repo_map:
                context_snippet += f"\n[Struktur Codebase]:\n{repo_map}"
        except Exception:
            pass

    system_prompt = (
        "Kamu adalah Senior Technical Prompt Engineer untuk autonomous AI multi-agent workstation.\n"
        "Tugasmu: menerima instruksi mentah/singkat dari pengguna dan mengembangkannya menjadi prompt spesifikasi teknis yang terstruktur, tajam, dan siap dieksekusi oleh tim agent (Architect, Coder, QA).\n"
        "Aturan ketat:\n"
        "1. Tulis langsung dalam Bahasa Indonesia yang lugas dan teknis (istilah teknis pemrograman tetap dalam Bahasa Inggris).\n"
        "2. Jangan gunakan emoji apapun (hindari semua emotikon dekoratif).\n"
        "3. Format langsung menjadi 3-4 bagian terstruktur padat: Target/Goal, Komponen/Arsitektur yang Harus Dibuat, Batasan Teknis/Konvensi, dan Kriteria Keberhasilan (DoD).\n"
        "4. JANGAN berikan kalimat pembuka atau penutup basa-basi. Kembalikan HANYA teks prompt hasil perbaikan."
    )
    user_content = f"Instruksi mentah pengguna:\n{raw_prompt}\n{context_snippet}"

    try:
        enhanced = await call_llm(system_prompt, user_content, temperature=0.3)
        return {"original": raw_prompt, "enhanced": enhanced}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memoles prompt: {str(e)}")

@app.get("/api/tasks")
async def get_tasks():
    sorted_tasks = sorted(
        tasks_store.values(),
        key=lambda x: x.get("created_at", 0),
        reverse=True
    )
    return sorted_tasks

@app.post("/api/tasks")
async def create_task(req: TaskCreateRequest):
    stages = []
    if req.preset_id == "auto" or not req.preset_id:
        preset = PRESETS["auto"]
        for s in preset["stages"]:
            stages.append({
                "role": s["role"],
                "name": s["name"],
                "icon": s.get("icon", "🧠"),
                "system": s["system"],
                "temperature": s.get("temperature", 0.1),
                "status": "waiting",
                "output": "",
                "error": None
            })
    elif req.stages and len(req.stages) > 0:
        for s in req.stages:
            if s.enabled is False:
                continue
            stages.append({
                "role": s.role,
                "name": s.name,
                "icon": s.icon or "🤖",
                "system": s.system,
                "temperature": s.temperature if s.temperature is not None else 0.2,
                "status": "waiting",
                "output": "",
                "error": None
            })
    elif req.preset_id and req.preset_id in PRESETS:
        preset = PRESETS[req.preset_id]
        for s in preset["stages"]:
            stages.append({
                "role": s["role"],
                "name": s["name"],
                "icon": s.get("icon", "🤖"),
                "system": s["system"],
                "temperature": s.get("temperature", 0.2),
                "status": "waiting",
                "output": "",
                "error": None
            })
    else:
        raise HTTPException(status_code=400, detail="Invalid preset or stages configuration")

    if not stages:
        raise HTTPException(status_code=400, detail="Minimal 1 stage harus aktif")

    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "title": req.title or req.prompt[:40] + "...",
        "prompt": req.prompt,
        "preset_id": req.preset_id,
        "working_directory": req.working_directory,
        "project_context": req.project_context,
        "skills": req.skills or [],
        "auto_save_artifact": req.auto_save_artifact,
        "auto_apply_files": req.auto_apply_files,
        "require_approval": req.require_approval,
        "auto_fix_loops": req.auto_fix_loops if req.auto_fix_loops is not None else 1,
        "current_fix_loop": 0,
        "status": "queued",
        "created_at": time.time(),
        "started_at": None,
        "completed_at": None,
        "stages": stages,
        "stages_approved": {},
        "final_output": "",
        "extracted_files": [],
        "session_id": req.session_id or "default",
        "cancelled": False
    }
    tasks_store[task_id] = task
    s_id = req.session_id or "default"
    if s_id in workstation_sessions_store:
        if task_id not in workstation_sessions_store[s_id].get("task_ids", []):
            workstation_sessions_store[s_id].setdefault("task_ids", []).append(task_id)
        workstation_sessions_store[s_id]["updated_at"] = time.time()
        save_workstation_sessions()
    save_tasks()

    store.add_event(task_id, "task.created", {"title": task["title"], "preset": req.preset_id,
                                              "stages": len(stages)})
    asyncio.create_task(execute_pipeline(task_id))
    return task

@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.post("/api/tasks/{task_id}/approve")
async def approve_task_stage(task_id: str, req: TaskApproveRequest):
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    if req.action == "reject":
        task["cancelled"] = True
        task["status"] = "cancelled"
        store.add_event(task_id, "approval.rejected", {"feedback": req.feedback})
    else:
        store.add_event(task_id, "approval.resumed", {"index": task.get("waiting_stage_index")})
        stage_idx = str(task.get("waiting_stage_index", 0))
        task.setdefault("stages_approved", {})[stage_idx] = True
        if req.feedback:
            task["latest_feedback"] = req.feedback

    save_tasks()

    if task_id in APPROVAL_EVENTS:
        APPROVAL_EVENTS[task_id].set()

    return {"status": "ok", "task_status": task["status"]}


@app.get("/api/tasks/{task_id}/messages")
async def get_task_messages(task_id: str, kind: Optional[str] = None, to_role: Optional[str] = None):
    task = tasks_store.get(task_id) or store.load_tasks().get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task tidak ditemukan.")
    return {"task_id": task_id, "messages": store.list_messages(task_id, kind=kind, to_role=to_role)}


class PostTaskMessageRequest(BaseModel):
    role: str = "user"
    to_role: str = "all"
    kind: str = "QUESTION"  # QUESTION, ANSWER, FINDING, REQUEST_CHANGE, APPROVAL_REQUIRED, APPROVED, REJECTED
    content: str
    meta: Optional[Dict[str, Any]] = None


@app.post("/api/tasks/{task_id}/messages")
async def post_task_message(task_id: str, req: PostTaskMessageRequest):
    task = tasks_store.get(task_id) or store.load_tasks().get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task tidak ditemukan.")
    msg_id = store.add_message(task_id, req.role, req.kind, req.content, to_role=req.to_role, meta=req.meta)
    store.add_event(task_id, f"agent.{req.kind.lower()}", {"from": req.role, "to": req.to_role, "summary": req.content[:80]})

    # If task is waiting approval and user responds with approval / rejection, resume or cancel
    if task.get("status") == "waiting_approval" and req.role == "user":
        if req.kind in ("APPROVED", "approval.granted"):
            await approve_task_stage(task_id, TaskApproveRequest(action="approve", feedback=req.content))
        elif req.kind in ("REJECTED", "approval.rejected"):
            await approve_task_stage(task_id, TaskApproveRequest(action="reject", feedback=req.content))

    return {"status": "ok", "message_id": msg_id}


@app.get("/api/agents")
async def get_agents():
    return {"agents": store.load_agents()}


class AgentPermissionsRequest(BaseModel):
    permissions: Dict[str, Any]


@app.post("/api/agents/{role}/permissions")
async def update_agent_permissions(role: str, req: AgentPermissionsRequest):
    agents = store.load_agents()
    matched = None
    for k in agents.keys():
        if k.lower() == role.lower():
            matched = k
            break
    if not matched:
        raise HTTPException(status_code=404, detail=f"Agent '{role}' tidak ditemukan.")
    agent = agents[matched]
    agent["permissions"] = req.permissions
    store.upsert_agent(agent)
    return {"status": "ok", "role": matched, "permissions": agent["permissions"]}


class CheckAgentPermissionRequest(BaseModel):
    capability: str
    subaction: Optional[str] = None


@app.post("/api/agents/{role}/check-permission")
async def check_agent_permission_endpoint(role: str, req: CheckAgentPermissionRequest):
    allowed = store.check_agent_permission(role, req.capability, req.subaction)
    return {"role": role, "capability": req.capability, "subaction": req.subaction, "allowed": allowed}


@app.get("/api/tasks/{task_id}/extracted-files")
async def get_task_extracted_files(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"files": task.get("extracted_files", [])}

@app.post("/api/tasks/{task_id}/apply-files")
async def apply_task_files(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    wdir = task.get("working_directory")
    if not wdir or not os.path.isdir(wdir):
        raise HTTPException(status_code=400, detail="Working directory tidak valid.")

    # Scoped permissions: verify Coder role has filesystem: write permission
    if not store.check_agent_permission("Coder", "filesystem", "write"):
        raise HTTPException(status_code=403, detail="Izin ditolak: Agent 'Coder' tidak memiliki izin 'write' pada filesystem.")

    files = task.get("extracted_files", [])
    if not files:
        files = extract_code_files(task.get("final_output", ""), scope_matrix=task.get("scope_matrix"))

    if not files:
        raise HTTPException(status_code=400, detail="Tidak ada blok file kode (### FILE: path) yang terdeteksi.")

    applied = []
    blocked = []
    snapshot_records = []

    for item in files:
        rel_p = item["path"]
        if item.get("blocked"):
            blocked.append({"path": rel_p, "reason": item.get("blocked_reason", "Dilarang oleh Scope Matrix")})
            continue

        try:
            full_p = sanitize_path(os.path.join(wdir, rel_p.lstrip("/")), base_dir=wdir)
            os.makedirs(os.path.dirname(full_p), exist_ok=True)
            existed = os.path.exists(full_p)
            backup_p = f"{full_p}.bak"
            if existed:
                shutil.copy2(full_p, backup_p)
            with open(full_p, "w", encoding="utf-8") as fp:
                fp.write(item["content"])
            applied.append({"path": rel_p, "lines": item["lines"]})
            snapshot_records.append({
                "target": full_p,
                "backup": backup_p,
                "existed": existed,
                "rel_path": rel_p
            })
        except Exception as e:
            print(f"Error applying file {item['path']}: {e}")

    task["applied_files"] = [a["path"] for a in applied]
    task["blocked_files"] = blocked
    if snapshot_records:
        save_apply_snapshot(wdir, task_id, snapshot_records, session_id=task.get("session_id"))
    save_tasks()
    return {
        "status": "ok",
        "applied": applied,
        "count": len(applied),
        "blocked": blocked,
        "blocked_count": len(blocked)
    }

def _clean_terminal_env() -> Dict[str, str]:
    """Sanitize environment variables for spawned terminal subprocesses.
    
    Removes master auth tokens, provider secrets, and credential keys so normal
    command execution (such as `env`, `printenv`, `set`) inside the workspace cannot
    dump server secrets directly.
    """
    clean = dict(os.environ)
    sensitive_keys = {
        "AI_TEAM_AUTH_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN", "GH_TOKEN", "GIT_TOKEN", "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL", "SECRET_KEY"
    }
    for k in list(clean.keys()):
        if k in sensitive_keys or any(sub in k.upper() for sub in ("AUTH_TOKEN", "SECRET_KEY", "PRIVATE_KEY", "API_KEY", "PASSWORD", "CREDENTIAL")):
            del clean[k]
    return clean


def _mask_sensitive_text(text: str) -> str:
    """Mask known sensitive environment tokens, API keys, and patterns in terminal outputs."""
    if not text:
        return text
    masked = text
    # Mask explicitly configured keys/tokens (check module AUTH_TOKEN, current env, and .env file values)
    configured_secrets = set()
    global AUTH_TOKEN
    if AUTH_TOKEN and len(AUTH_TOKEN) >= 4:
        configured_secrets.add(AUTH_TOKEN)
    for env_name in ("AI_TEAM_AUTH_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val and len(val) >= 4:
            configured_secrets.add(val)

    for sec in configured_secrets:
        masked = masked.replace(sec, "[REDACTED_SECRET]")

    # Mask key=value assignments: AI_TEAM_AUTH_TOKEN=..., LLM_API_KEY=..., etc. without catastrophic regex backtracking
    if "=" in masked:
        def _redact_env(m):
            k = m.group(1)
            if any(sub in k.upper() for sub in ("KEY", "TOKEN", "SECRET", "PASS", "CREDENTIAL")):
                return f"{k}=[REDACTED_SECRET]"
            return m.group(0)
        masked = re.sub(r'\b([A-Za-z0-9_]{1,64})=([^\s&|;]+)', _redact_env, masked)

    # Mask standard API key / token formats: sk-..., Bearer ..., gh[pousr]-...
    masked = re.sub(r'sk-[a-zA-Z0-9_-]{20,}', '[REDACTED_API_KEY]', masked)
    masked = re.sub(r'gh[pousr]_[a-zA-Z0-9]{36,}', '[REDACTED_GITHUB_TOKEN]', masked)
    masked = re.sub(r'(Bearer\s+)[a-zA-Z0-9_\-\.]{16,}', r'\1[REDACTED_TOKEN]', masked, flags=re.IGNORECASE)
    masked = re.sub(r'(https?://)[^:\s]+:[^@\s]+@', r'\1[REDACTED_CREDENTIALS]@', masked)
    return masked


class TerminalExecuteRequest(BaseModel):
    command: str
    path: Optional[str] = None
    timeout: Optional[int] = 30


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Platform-aware process tree termination.
    
    On Windows: uses taskkill /F /T /PID to recursively kill process tree.
    On Unix: uses os.killpg with SIGKILL on the process group.
    """
    if not proc or proc.returncode is not None:
        return

    pid = proc.pid
    if IS_WINDOWS:
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(kill_proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


@app.post("/api/workspace/terminal")
async def execute_workspace_terminal(req: TerminalExecuteRequest):
    """Execute a shell/CLI command scoped strictly inside ALLOWED_ROOTS workspace directory."""
    raw_cmd = (req.command or "").strip()
    if not raw_cmd:
        raise HTTPException(status_code=400, detail="Perintah tidak boleh kosong.")

    # 1. Primary Security Control: Path & CWD Boundary check strictly within ALLOWED_ROOTS
    cwd = sanitize_path(req.path or DEFAULT_WORKSPACE)
    if not os.path.isdir(cwd):
        raise HTTPException(status_code=400, detail="Direktori kerja (cwd) tidak valid.")

    # 2. Timeout and output size limits
    timeout_sec = min(max(int(req.timeout or 30), 1), 120)
    max_output_chars = int(os.environ.get("TERMINAL_MAX_OUTPUT", 100_000))

    start_t = time.time()
    try:
        # Execute asynchronously with process group to guarantee clean subprocess teardown
        # and filtered environment to prevent secret leakage via `env`/`printenv`
        subproc_kwargs = {
            "cwd": cwd,
            "env": _clean_terminal_env(),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if not IS_WINDOWS:
            subproc_kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_shell(
            raw_cmd,
            **subproc_kwargs
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_sec
            )
            exit_code = proc.returncode if proc.returncode is not None else 0
        except asyncio.TimeoutError:
            await _kill_process_tree(proc)
            try:
                # Non-blocking wait for communication completion after kill
                stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            except Exception:
                stdout_data, stderr_data = b"", b""
            duration_ms = int((time.time() - start_t) * 1000)
            return {
                "stdout": _mask_sensitive_text(stdout_data.decode("utf-8", errors="ignore")[:max_output_chars]),
                "stderr": f"Error: Command timed out after {timeout_sec} seconds.",
                "exit_code": -1,
                "duration_ms": duration_ms,
                "cwd": cwd,
                "timed_out": True
            }

        duration_ms = int((time.time() - start_t) * 1000)
        stdout_str = stdout_data.decode("utf-8", errors="ignore")
        stderr_str = stderr_data.decode("utf-8", errors="ignore")

        if len(stdout_str) > max_output_chars:
            stdout_str = stdout_str[:max_output_chars] + f"\n... [Output truncated: exceeded {max_output_chars} chars]"
        if len(stderr_str) > max_output_chars:
            stderr_str = stderr_str[:max_output_chars] + f"\n... [Error output truncated: exceeded {max_output_chars} chars]"

        return {
            "stdout": _mask_sensitive_text(stdout_str),
            "stderr": _mask_sensitive_text(stderr_str),
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "cwd": cwd,
            "timed_out": False
        }
    except Exception as e:
        duration_ms = int((time.time() - start_t) * 1000)
        return {
            "stdout": "",
            "stderr": f"Execution error: {str(e)}",
            "exit_code": 1,
            "duration_ms": duration_ms,
            "cwd": cwd,
            "timed_out": False
        }


@app.get("/api/workspace/git/status")
async def api_workspace_git_status(path: Optional[str] = None):
    """Get git status scoped strictly to the current workspace repository."""
    target_dir = sanitize_path(path or DEFAULT_WORKSPACE)
    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=400, detail="Direktori workspace tidak valid.")
    git_info = get_git_info(target_dir)
    return {
        "path": target_dir,
        "is_git": git_info["is_git"],
        "branch": git_info["branch"],
        "clean": git_info["clean"],
        "status_lines": git_info["status_lines"]
    }


@app.get("/api/workspace/git/diff")
async def api_workspace_git_diff(path: Optional[str] = None, file_path: Optional[str] = None):
    """Get read-only git unified diff scoped strictly to the current workspace repository."""
    target_dir = sanitize_path(path or DEFAULT_WORKSPACE)
    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=400, detail="Direktori workspace tidak valid.")

    git_dir = os.path.join(target_dir, ".git")
    if not os.path.exists(git_dir):
        return {
            "path": target_dir,
            "is_git": False,
            "diff_lines": [],
            "raw_diff": "",
            "is_empty": True
        }

    cmd = ["git", "-C", target_dir, "diff", "HEAD"]
    if file_path:
        # Sanitize single file relative path inside target_dir
        safe_file = sanitize_path(os.path.join(target_dir, file_path.lstrip("/")), base_dir=target_dir)
        rel_f = os.path.relpath(safe_file, target_dir)
        cmd.extend(["--", rel_f])

    try:
        raw_diff = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=10).decode("utf-8", errors="ignore")
    except Exception as e:
        raw_diff = ""

    parsed_lines = []
    additions = 0
    deletions = 0
    files_changed = 0

    for line in raw_diff.splitlines():
        line_clean = line.rstrip("\r\n")
        if line_clean.startswith("diff --git"):
            files_changed += 1
            parsed_lines.append({"type": "file_header", "text": line_clean})
        elif line_clean.startswith("+++") or line_clean.startswith("---"):
            parsed_lines.append({"type": "header", "text": line_clean})
        elif line_clean.startswith("@@"):
            parsed_lines.append({"type": "chunk", "text": line_clean})
        elif line_clean.startswith("+"):
            additions += 1
            parsed_lines.append({"type": "add", "text": line_clean[1:]})
        elif line_clean.startswith("-"):
            deletions += 1
            parsed_lines.append({"type": "del", "text": line_clean[1:]})
        else:
            txt = line_clean[1:] if line_clean.startswith(" ") else line_clean
            parsed_lines.append({"type": "ctx", "text": txt})

    return {
        "path": target_dir,
        "is_git": True,
        "is_empty": len(raw_diff.strip()) == 0,
        "raw_diff": _mask_sensitive_text(raw_diff[:100_000]),
        "diff_lines": parsed_lines[:2000],
        "files_changed": files_changed,
        "additions": additions,
        "deletions": deletions
    }


class DiffRequest(BaseModel):
    path: str
    rel_path: str
    new_content: str

@app.post("/api/workspace/diff")
async def compute_workspace_diff(req: DiffRequest):
    wdir = sanitize_path(req.path)
    full_path = sanitize_path(os.path.join(wdir, req.rel_path.lstrip("/")), base_dir=wdir)

    old_content = ""
    exists = os.path.exists(full_path)
    if exists:
        try:
            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                old_content = f.read()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Gagal membaca file asli: {e}")

    old_lines = old_content.splitlines(keepends=True)
    new_lines = req.new_content.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{req.rel_path}",
        tofile=f"b/{req.rel_path}",
        lineterm=""
    ))

    parsed_lines = []
    additions = 0
    deletions = 0
    for line in diff:
        line_clean = line.rstrip("\r\n")
        if line_clean.startswith("+++") or line_clean.startswith("---"):
            parsed_lines.append({"type": "header", "text": line_clean})
        elif line_clean.startswith("@@"):
            parsed_lines.append({"type": "chunk", "text": line_clean})
        elif line_clean.startswith("+"):
            additions += 1
            parsed_lines.append({"type": "add", "text": line_clean[1:]})
        elif line_clean.startswith("-"):
            deletions += 1
            parsed_lines.append({"type": "del", "text": line_clean[1:]})
        else:
            txt = line_clean[1:] if line_clean.startswith(" ") else line_clean
            parsed_lines.append({"type": "ctx", "text": txt})

    return {
        "rel_path": req.rel_path,
        "exists": exists,
        "additions": additions,
        "deletions": deletions,
        "diff_lines": parsed_lines
    }

class CheckSyntaxRequest(BaseModel):
    rel_path: str
    content: str

@app.post("/api/workspace/check-syntax")
async def check_syntax(req: CheckSyntaxRequest):
    ext = os.path.splitext(req.rel_path)[1].lower()
    content = req.content

    if ext == ".py":
        try:
            compile(content, req.rel_path, "exec")
            return {
                "valid": True,
                "language": "python",
                "message": "Sintaks Python valid (bebas SyntaxError)."
            }
        except SyntaxError as e:
            return {
                "valid": False,
                "language": "python",
                "line": e.lineno,
                "col": e.offset,
                "message": f"SyntaxError di baris {e.lineno}: {e.msg}"
            }
        except Exception as e:
            return {
                "valid": False,
                "language": "python",
                "message": f"CompileError: {str(e)}"
            }
    elif ext == ".json":
        try:
            json.loads(content)
            return {
                "valid": True,
                "language": "json",
                "message": "Format JSON valid."
            }
        except json.JSONDecodeError as e:
            return {
                "valid": False,
                "language": "json",
                "line": e.lineno,
                "col": e.colno,
                "message": f"JSONDecodeError di baris {e.lineno}, kolom {e.colno}: {e.msg}"
            }
    elif ext in [".yaml", ".yml"]:
        try:
            yaml.safe_load(content)
            return {
                "valid": True,
                "language": "yaml",
                "message": "Format YAML valid."
            }
        except Exception as e:
            return {
                "valid": False,
                "language": "yaml",
                "message": f"YAMLError: {str(e)[:150]}"
            }
    elif ext in [".js", ".mjs", ".cjs"]:
        # Use real node -c verification with fallback to known NVM node paths
        node_bin = os.environ.get("NODE_BIN") or shutil.which("node")
        if not node_bin:
            # Check NVM paths on POSIX and Windows
            if IS_WINDOWS:
                nvm_home = os.environ.get("NVM_HOME") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "nvm")
                candidates = sorted(glob.glob(os.path.join(nvm_home, "v*", "node.exe"))) or glob.glob(os.path.join(nvm_home, "node.exe"))
            else:
                nvm_pattern = os.path.join(os.path.expanduser("~"), ".nvm", "versions", "node", "*", "bin", "node")
                candidates = sorted(glob.glob(nvm_pattern))
            if candidates:
                node_bin = candidates[-1]
        if node_bin:
            try:
                proc = subprocess.run(
                    [node_bin, "--input-type=module", "-c"],
                    input=content,
                    text=True,
                    capture_output=True,
                    timeout=5
                )
                if proc.returncode == 0:
                    return {
                        "valid": True,
                        "language": "javascript",
                        "message": "Sintaks JavaScript valid (lolos verifikasi node -c)."
                    }
                else:
                    err_lines = [l.strip() for l in proc.stderr.splitlines() if l.strip() and not l.strip().startswith("at ")]
                    err_msg = err_lines[-1] if err_lines else "Syntax error"
                    line_no = None
                    m_line = re.search(r'\[stdin\]:(\d+)', proc.stderr)
                    if m_line:
                        line_no = int(m_line.group(1))
                    return {
                        "valid": False,
                        "language": "javascript",
                        "line": line_no,
                        "message": f"Node SyntaxError{f' di baris {line_no}' if line_no else ''}: {err_msg}"
                    }
            except Exception as e:
                pass

        # Fallback bracket checker with string/comment stripping
        clean_content = re.sub(r'(\/\*[\s\S]*?\*\/|\/\/[^\n]*|\"[^\"\\]*(?:\\.[^\"\\]*)*\"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'|`[^`\\]*(?:\\.[^`\\]*)*`)', '', content)
        stack = []
        pairs = {')': '(', '}': '{', ']': '['}
        for idx_ch, ch in enumerate(clean_content):
            if ch in "({[":
                stack.append(ch)
            elif ch in ")}]":
                if not stack or stack[-1] != pairs[ch]:
                    return {
                        "valid": False,
                        "language": "javascript",
                        "message": f"Mismatched bracket '{ch}'."
                    }
                stack.pop()
        if stack:
            return {
                "valid": False,
                "language": "javascript",
                "message": f"Unclosed bracket '{stack[-1]}'."
            }
        return {
            "valid": True,
            "language": "javascript",
            "message": "Struktur kurung dan blok seimbang."
        }
    else:
        # Check matching brackets for Dart/HTML/other with string literal stripping
        clean_content = re.sub(r'(\/\*[\s\S]*?\*\/|\/\/[^\n]*|\"[^\"\\]*(?:\\.[^\"\\]*)*\"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'|`[^`\\]*(?:\\.[^`\\]*)*`)', '', content)
        stack = []
        pairs = {')': '(', '}': '{', ']': '['}
        for idx_ch, ch in enumerate(clean_content):
            if ch in "({[":
                stack.append(ch)
            elif ch in ")}]":
                if not stack or stack[-1] != pairs[ch]:
                    return {
                        "valid": False,
                        "language": ext.lstrip(".") or "code",
                        "message": f"Mismatched bracket '{ch}'."
                    }
                stack.pop()
        if stack:
            return {
                "valid": False,
                "language": ext.lstrip(".") or "code",
                "message": f"Unclosed bracket '{stack[-1]}'."
            }
        return {
            "valid": True,
            "language": ext.lstrip(".") or "code",
            "message": "Struktur kurung dan blok seimbang."
        }

class RollbackRequest(BaseModel):
    path: Optional[str] = None
    session_id: Optional[str] = None

@app.post("/api/workspace/rollback")
async def rollback_last_apply(req: RollbackRequest):
    snap_file = None
    if req.session_id:
        safe_sid = re.sub(r'[^a-zA-Z0-9_\-]', '_', req.session_id)
        candidate = os.path.join(SNAPSHOTS_DIR, f"last_apply_{safe_sid}.json")
        if os.path.exists(candidate):
            snap_file = candidate
    if not snap_file:
        snap_file = os.path.join(SNAPSHOTS_DIR, "last_apply.json")

    if not os.path.exists(snap_file):
        raise HTTPException(status_code=400, detail="Tidak ada snapshot perubahan terakhir untuk di-rollback.")
    try:
        with open(snap_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal membaca snapshot: {e}")

    restored = []
    errors = []
    for rec in data.get("records", []):
        target = rec["target"]
        backup = rec.get("backup")
        existed = rec.get("existed", True)
        rel_path = rec.get("rel_path", os.path.basename(target))
        try:
            if existed and backup and os.path.exists(backup):
                shutil.copy2(backup, target)
                restored.append({"path": rel_path, "action": "restored_from_backup"})
            elif not existed and os.path.exists(target):
                os.remove(target)
                restored.append({"path": rel_path, "action": "removed_new_file"})
        except Exception as err:
            errors.append({"path": rel_path, "error": str(err)})

    return {
        "status": "ok",
        "restored": restored,
        "restored_count": len(restored),
        "errors": errors,
        "task_id": data.get("task_id")
    }

class SessionCreateRequest(BaseModel):
    title: str
    working_directory: Optional[str] = None

class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None

@app.get("/api/workstation/sessions")
def get_workstation_sessions():
    s_list = list(workstation_sessions_store.values())
    s_list.sort(key=lambda s: (not s.get("pinned", False), -s.get("updated_at", 0)))
    return {"sessions": s_list}

@app.post("/api/workstation/sessions")
def create_workstation_session(req: SessionCreateRequest):
    s_id = str(uuid.uuid4())[:8]
    session_data = {
        "id": s_id,
        "title": req.title.strip() or f"Session {s_id}",
        "created_at": time.time(),
        "updated_at": time.time(),
        "pinned": False,
        "working_directory": req.working_directory or DEFAULT_WORKSPACE,
        "task_ids": []
    }
    workstation_sessions_store[s_id] = session_data
    save_workstation_sessions()
    return session_data

@app.patch("/api/workstation/sessions/{session_id}")
def update_workstation_session(session_id: str, req: SessionUpdateRequest):
    s = workstation_sessions_store.get(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    if req.title is not None:
        s["title"] = req.title.strip()
    if req.pinned is not None:
        s["pinned"] = req.pinned
    s["updated_at"] = time.time()
    save_workstation_sessions()
    return s

@app.delete("/api/workstation/sessions/{session_id}")
def delete_workstation_session(session_id: str):
    if session_id == "default":
        raise HTTPException(status_code=400, detail="Sesi default tidak dapat dihapus.")
    if session_id in workstation_sessions_store:
        del workstation_sessions_store[session_id]
        save_workstation_sessions()
    return {"status": "ok", "session_id": session_id}

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["cancelled"] = True
    task["status"] = "cancelled"
    save_tasks()
    if task_id in APPROVAL_EVENTS:
        APPROVAL_EVENTS[task_id].set()
    return {"status": "ok", "task_status": "cancelled"}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    if task_id in tasks_store:
        del tasks_store[task_id]
        save_tasks()
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Task not found")

# --- HERMES BRIDGE ENDPOINTS ---

@app.get("/api/hermes/status")
def api_hermes_status():
    installed = os.path.isdir(HERMES_DIR)
    skills = get_hermes_skills()
    sessions = get_hermes_sessions(limit=5)
    model_info = get_hermes_model_info()
    return {
        "installed": installed,
        "skills_count": len(skills),
        "active_model": model_info.get("active_model", "bai"),
        "recent_sessions_count": len(sessions)
    }

@app.get("/api/hermes/skills")
def api_hermes_skills():
    return {"skills": get_hermes_skills()}

@app.get("/api/hermes/skills/{category}/{name}")
def api_hermes_skill_detail(category: str, name: str):
    content = read_skill_content(name)
    if not content:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"name": name, "category": category, "content": content}

@app.get("/api/hermes/sessions")
def api_hermes_sessions(limit: int = 50):
    return {"sessions": get_hermes_sessions(limit=limit)}

@app.get("/api/hermes/sessions/{session_id}/messages")
def api_hermes_session_messages(session_id: str, limit: int = 100):
    return {"session_id": session_id, "messages": get_session_messages(session_id, limit=limit)}

@app.get("/api/hermes/model")
def api_hermes_model():
    return get_hermes_model_info()

@app.post("/api/hermes/model")
def api_hermes_model_update(req: ModelUpdateRequest):
    scope = update_hermes_model(req.model, apply_globally=bool(req.apply_globally))
    if not scope:
        raise HTTPException(status_code=500, detail="Gagal memperbarui model")
    return {"status": "ok", "active_model": req.model.strip(), "scope": scope}

@app.get("/api/system/status")
async def get_system_status():
    router_ok = False
    key = get_9router_key()
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            res = await client.get(
                "http://127.0.0.1:20128/v1/models",
                headers={"Authorization": f"Bearer {key}"}
            )
            router_ok = res.status_code == 200
    except Exception:
        router_ok = False
    
    running_count = sum(1 for t in tasks_store.values() if t.get("status") in ["running", "waiting_approval"])
    completed_count = sum(1 for t in tasks_store.values() if t.get("status") == "completed")
    
    return {
        "router_ok": router_ok,
        "model": "bai (deepseek-v4.1-flash)",
        "public_host": os.environ.get("PUBLIC_HOST") or LAN_HOST or "",
        "default_workspace": DEFAULT_WORKSPACE,
        "auth_required": bool(AUTH_TOKEN),
        "running_count": running_count,
        "completed_count": completed_count,
        "total_tasks": len(tasks_store)
    }

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>AI Team Dashboard is initializing...</h1>"


# Mount static assets directory for modular frontend components
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ============================================================================
# PHASE 2 & 3 — Level 1 & 2 Collaborative Chat (User ↔ Hermes ↔ Specialists)
# ============================================================================

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    workspace_root: Optional[str] = None
    model: Optional[str] = None
    skills: Optional[List[str]] = None
    agent: Optional[str] = None  # Specific agent: "Hermes", "Researcher", "Coder", "Critic", "QA", "Tutor", etc.
    active_agents: Optional[List[str]] = None


@app.get("/api/chat/sessions")
def api_chat_sessions():
    return {"sessions": store.list_chat_sessions()}


@app.post("/api/chat/sessions")
def api_chat_session_create(req: dict):
    title = (req.get("title") or "New Chat").strip()
    ws = req.get("workspace_root")
    model = req.get("model")
    active_agents = req.get("active_agents") or ["Hermes"]
    sid = store.create_chat_session(title, workspace_root=ws, model=model, active_agents=active_agents)
    return {"session": store.get_chat_session(sid)}


@app.get("/api/chat/sessions/{session_id}/messages")
def api_chat_session_messages(session_id: str, limit: int = 100):
    return {"messages": store.list_chat_messages(session_id, limit=limit)}


@app.delete("/api/chat/sessions/{session_id}")
def api_chat_session_delete(session_id: str):
    ok = store.delete_chat_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan.")
    return {"status": "ok"}


@app.get("/api/chat/sessions/{session_id}/agents")
def api_chat_session_get_agents(session_id: str):
    sess = store.get_chat_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan.")
    return {"session_id": session_id, "active_agents": sess.get("active_agents", ["Hermes"])}


class ChatSessionAgentsModifyRequest(BaseModel):
    action: str = "add"  # "add" or "remove"
    agent: str


@app.post("/api/chat/sessions/{session_id}/agents")
def api_chat_session_modify_agents(session_id: str, req: ChatSessionAgentsModifyRequest):
    sess = store.get_chat_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan.")
    agents = list(sess.get("active_agents") or ["Hermes"])
    catalog = store.load_agents()
    matched = None
    for k in catalog.keys():
        if k.lower() == req.agent.lower():
            matched = k
            break
    if not matched:
        raise HTTPException(status_code=400, detail=f"Agent '{req.agent}' tidak terdaftar di katalog.")

    if req.action == "add":
        if matched not in agents:
            agents.append(matched)
    elif req.action == "remove":
        if matched != "Hermes" and matched in agents:
            agents.remove(matched)

    store.update_chat_session_agents(session_id, agents)
    return {"session_id": session_id, "active_agents": agents}


def _detect_chat_agent(message: str, requested_agent: Optional[str], catalog: Dict[str, Any]) -> str:
    """Detect which agent should respond based on explicit request, mentions, or natural language."""
    # 1. Mention check in message takes highest priority (@researcher, @coder, @critic, @tutor, etc.)
    mention_m = re.search(r'@([a-zA-Z0-9_\-]+)', message)
    if mention_m:
        cand = mention_m.group(1).lower()
        for k in catalog.keys():
            if k.lower() == cand:
                return k

    # 2. Natural language invocation: "Ask Researcher...", "Tanya ke Coder...", "Minta Critic...", "Suruh QA..."
    phrase_m = re.search(r'\b(?:ask|tanya|minta|suruh|hubungi)\s+(?:ke\s+|to\s+)?([a-zA-Z0-9_\-]+)\b', message, re.IGNORECASE)
    if phrase_m:
        cand = phrase_m.group(1).lower()
        for k in catalog.keys():
            if k.lower() == cand:
                return k

    # 3. Explicit requested_agent parameter (from UI dropdown) if user specifically chose a non-default specialist
    if requested_agent and requested_agent.lower() not in ("auto", "none"):
        for k in catalog.keys():
            if k.lower() == requested_agent.lower():
                return k

    return "Hermes"


def _build_chat_system_prompt(workspace_root: Optional[str], skills: Optional[List[str]],
                               agent_role: str = "Hermes",
                               active_agents: Optional[List[str]] = None) -> str:
    """Build a system prompt supporting Hermes orchestrator and specialized collaborative agents."""
    catalog = store.load_agents()
    agent_spec = catalog.get(agent_role) or catalog.get("Hermes") or {}
    agent_name = agent_spec.get("name") or agent_role
    active_list_str = ", ".join(active_agents or ["Hermes"])

    if agent_role == "Hermes":
        parts = [
            f"Kamu adalah Hermes ({agent_name}), asisten AI primer yang cerdas, adaptif, dan berorientasi pada tindakan nyata. "
            "Kamu membantu pengguna untuk coding, tugas kuliah, riset, analisis data, menulis, brainstorming, dan tugas apapun. "
            "Jawab dengan bahasa yang sesuai pertanyaan user (Bahasa Indonesia atau English). "
            "Gunakan markdown untuk format respons.\n"
            f"Kamu bekerja dalam workspace AI Team. Tim kolaboratif aktif saat ini: [{active_list_str}]. "
            "Jika pengguna meminta bantuan agen spesialis (seperti @researcher, @coder, @critic, @qa, @tutor, @analyst), "
            "kamu dapat berkolaborasi dengan mereka atau menjawab pertanyaan secara langsung."
        ]
    else:
        parts = [
            f"Kamu adalah [{agent_name}], agen spesialis ({agent_role}) dalam tim kolaboratif AI Team Workspace. "
            f"Tim aktif saat ini: [{active_list_str}].\n"
            f"Instruksi dan keahlian peranmu:\n{agent_spec.get('system', '')}\n"
            "Jawab pertanyaan pengguna secara fokus dan mendalam sesuai domain spesialisasi keahlianmu. "
            "Gunakan markdown untuk format respons."
        ]

    if workspace_root:
        try:
            ws_path = sanitize_path(workspace_root)
            if os.path.isdir(ws_path):
                repo_map = generate_repo_map(ws_path)
                if repo_map:
                    parts.append(f"\n=== WORKSPACE: {ws_path} ===\n{repo_map}")
                # Inject AGENTS.md / README.md if present
                for ctx_file in ("AGENTS.md", "README.md"):
                    ctx_path = os.path.join(ws_path, ctx_file)
                    if os.path.exists(ctx_path):
                        try:
                            with open(ctx_path, "r", encoding="utf-8", errors="ignore") as f:
                                ctx_content = f.read(8000)
                            parts.append(f"\n=== {ctx_file} ===\n{ctx_content}")
                        except Exception:
                            pass
                        break
        except Exception:
            pass

    if skills:
        for sk in skills:
            content = read_skill_content(sk)
            if content:
                parts.append(f"\n=== HERMES SKILL: {sk} ===\n{content[:3000]}")

    return "\n".join(parts)


async def _stream_chat_llm(messages: List[Dict[str, str]], model: Optional[str] = None,
                            temperature: float = 0.3):
    """Async generator that yields SSE `data: …` lines with incremental content chunks."""
    base_url, default_model, key = get_llm_config()
    use_model = model or default_model

    url = f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": max(0.0, min(1.0, float(temperature))),
        "stream": True,
    }

    timeout = httpx.Timeout(180.0, connect=15.0)
    full_content: List[str] = []

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as res:
                if res.status_code != 200:
                    body = await res.aread()
                    err = f"LLM HTTP {res.status_code}: {body.decode(errors='ignore')[:300]}"
                    yield f"data: {json.dumps({'error': err})}\n\n"
                    return

                async for line in res.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0]["delta"]
                        c = delta.get("content")
                        if c:
                            full_content.append(c)
                            yield f"data: {json.dumps({'chunk': c})}\n\n"
                    except Exception:
                        pass
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)[:300]})}\n\n"

    final = "".join(full_content).strip()
    yield f"data: {json.dumps({'done': True, 'full_content': final, 'model': use_model})}\n\n"


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """Streaming chat endpoint. Returns text/event-stream with incremental chunks.

    Uses POST (not GET) so we can send message + context. The browser must use
    fetch() + ReadableStream to consume SSE from a POST — native EventSource is
    GET-only and will not be used.
    """
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Pesan kosong.")

    # Resolve or create chat session
    session_id = req.session_id
    if session_id:
        sess = store.get_chat_session(session_id)
        if not sess:
            raise HTTPException(status_code=404, detail="Sesi chat tidak ditemukan.")
    else:
        title = req.message.strip()[:60]
        session_id = store.create_chat_session(
            title, workspace_root=req.workspace_root, model=req.model
        )

    # Session-level model override: request > session > global
    sess = store.get_chat_session(session_id) or {}
    model_override = req.model or sess.get("model") or None

    catalog = store.load_agents()
    target_agent = _detect_chat_agent(req.message, req.agent, catalog)

    # Track active agents in session
    current_active_agents = list(sess.get("active_agents") or ["Hermes"])
    if target_agent not in current_active_agents:
        current_active_agents.append(target_agent)
        store.update_chat_session_agents(session_id, current_active_agents)

    # Persist user message
    store.add_chat_message(session_id, "user", req.message.strip(), agent="user")

    # Build system prompt with workspace + skills context + agent role
    ws_root = req.workspace_root or sess.get("workspace_root")
    system_prompt = _build_chat_system_prompt(
        ws_root, req.skills, agent_role=target_agent, active_agents=current_active_agents
    )

    # Build messages array: system + recent history + new user message
    history = store.get_recent_chat_context(session_id, n_turns=20)
    llm_messages = [{"role": "system", "content": system_prompt}] + history

    async def event_generator():
        collected: List[str] = []
        used_model = model_override
        agent_spec = catalog.get(target_agent) or {}

        # Emit agent.started event so client knows which specialist is working
        yield f"data: {json.dumps({'type': 'agent.started', 'agent': target_agent, 'icon': agent_spec.get('icon', '🤖')})}\n\n"

        target_temp = float(agent_spec.get("temperature", 0.3))
        async for sse_line in _stream_chat_llm(llm_messages, model=model_override, temperature=target_temp):
            # Parse final 'done' or 'error' event to persist assistant message
            try:
                raw_data = sse_line[6:].strip() if sse_line.startswith("data: ") else sse_line.strip()
                payload = json.loads(raw_data)
                if payload.get("done"):
                    collected_text = payload.get("full_content", "")
                    used_model = payload.get("model")
                    if collected_text:
                        store.add_chat_message(session_id, "assistant", collected_text, model=used_model, agent=target_agent)
                    payload["agent"] = target_agent
                    payload["active_agents"] = current_active_agents
                    yield f"data: {json.dumps(payload)}\n\n"
                    continue
                elif payload.get("error"):
                    err_msg = f"[Error: {payload.get('error')}]"
                    store.add_chat_message(session_id, "assistant", err_msg, model=used_model, agent=target_agent)
            except Exception:
                pass
            yield sse_line

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Chat-Session-Id": session_id,
        },
    )

# Phase 1 boot: seed workspace registry from config presets (runs after all defs)
_seeded_workspaces = store.seed_workspaces(get_workspace_presets(), DEFAULT_WORKSPACE)
if _seeded_workspaces:
    print(f"Workspace registry: {_seeded_workspaces} workspace(s) seeded")

if __name__ == "__main__":
    # Local launcher (setup.sh). The systemd unit passes its own --host/--port.
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8090")),
    )
