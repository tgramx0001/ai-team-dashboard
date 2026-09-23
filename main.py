import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(BASE_DIR, "tasks.json")
ALLOWED_ROOT = os.environ.get("WORKSPACE_ROOT", os.path.expanduser("~"))

# Hermes Integration Constants
HERMES_DIR = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
HERMES_SKILLS_DIR = os.path.join(HERMES_DIR, "skills")
LOCAL_SKILLS_DIR = os.path.join(BASE_DIR, "skills")
HERMES_STATE_DB = os.path.join(HERMES_DIR, "state.db")
HERMES_CONFIG_FILE = os.path.join(HERMES_DIR, "config.yaml")

app = FastAPI(title="AI Team Dashboard - Autonomous Workstation")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory approval events
APPROVAL_EVENTS: Dict[str, asyncio.Event] = {}

def get_llm_config() -> Tuple[str, str, str]:
    # Check Hermes config first if model not overridden by environment
    hermes_model = None
    if os.path.exists(HERMES_CONFIG_FILE):
        try:
            with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
                hcfg = yaml.safe_load(f) or {}
                hermes_model = hcfg.get("model", {}).get("default")
        except Exception:
            pass

    base_url = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL") or hermes_model or "bai"
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

    # Auto-detect local 9Router sqlite if key not passed in env
    if not key:
        db_path = os.path.expanduser("~/.9router/db/data.sqlite")
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
    active_model = "bai"
    provider = "custom"
    base_url = "http://127.0.0.1:20128/v1"

    if os.path.exists(HERMES_CONFIG_FILE):
        try:
            with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            m = cfg.get("model", {})
            active_model = m.get("default", active_model)
            provider = m.get("provider", provider)
            base_url = m.get("base_url", base_url)
        except Exception:
            pass

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
        "available_models": list(dict.fromkeys(available))
    }

def update_hermes_model(model_name: str) -> bool:
    if not os.path.exists(HERMES_CONFIG_FILE):
        return False
    try:
        with open(HERMES_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if "model" not in cfg:
            cfg["model"] = {}
        cfg["model"]["default"] = model_name
        with open(HERMES_CONFIG_FILE, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False)
        return True
    except Exception:
        return False

SPECIALIST_CATALOG: Dict[str, Dict[str, Any]] = {
    "Architect": {
        "role": "Architect",
        "name": "System Architect",
        "icon": "📐",
        "temperature": 0.1,
        "system": (
            "Kamu adalah Principal System Architect. Rancang arsitektur sistem untuk kebutuhan fitur berikut. "
            "Ikuti aturan arsitektur proyek (AGENTS.md) jika ada. Tentukan entity relationship/schema database, struktur tabel, nama file yang akan dibuat/diubah, dan pola endpoint API."
        )
    },
    "UI/UX": {
        "role": "UI/UX",
        "name": "UI/UX Specialist",
        "icon": "🎨",
        "temperature": 0.3,
        "system": (
            "Kamu adalah Senior UI/UX Specialist. Berdasarkan spesifikasi arsitektur, buat wireframe tata letak layar (screen layout), alur navigasi pengguna (user flow), daftar komponen interaktif, dan panduan styling yang jelas."
        )
    },
    "Coder": {
        "role": "Coder",
        "name": "Lead Developer",
        "icon": "⚡",
        "temperature": 0.1,
        "system": (
            "Kamu adalah Lead Full-Stack Developer handal. Tulis kode implementasi produksi yang bersih, modular, dan lengkap (backend/frontend). Tanpa placeholder malas (tulis kode nyata).\n\n"
            "PENTING: Setiap file kode yang kamu buat/ubah WAJIB ditulis dengan header format multi-file terpisah berikut:\n"
            "### FILE: path/to/file.ext\n```ext\n// kode lengkap di sini\n```"
        )
    },
    "Security": {
        "role": "Security",
        "name": "Security Auditor",
        "icon": "🔍",
        "temperature": 0.1,
        "system": (
            "Kamu adalah Senior Application Security Auditor & Penetration Tester. Analisis codebase target secara mendalam: periksa celah OWASP, SQL Injection, Auth Bypass, Broken Access Control / IDOR, Race Condition, Token Leakage, dan penanganan input yang tidak aman. Sajikan laporan audit terstruktur: Lokasi file/line, tingkat keparahan (Critical/High/Medium), bukti celah, dan rekomendasi mitigasi teknis yang konkret."
        )
    },
    "QA": {
        "role": "QA",
        "name": "QA & Security Engineer",
        "icon": "🛡️",
        "temperature": 0.1,
        "system": (
            "Kamu adalah Senior QA & Security Engineer. Audit kode dari Coder. Cari celah keamanan, race condition, error penanganan null, dan edge case. Periksa apakah kode sesuai dengan struktur proyek.\n\nPENTING: Di baris paling akhir dari evaluasimu, WAJIB tuliskan satu baris penutup status:\nVERDICT: PASSED atau VERDICT: NEEDS_REVISION\nJika NEEDS_REVISION, sertakan daftar ringkas poin bug yang wajib diperbaiki."
        )
    },
    "Researcher": {
        "role": "Researcher",
        "name": "Pustaka & Riset",
        "icon": "🔍",
        "temperature": 0.3,
        "system": (
            "Kamu adalah Peneliti & Akademisi Senior. Analisis topik tugas/makalah, petakan konsep teori utama, fakta relevan, terminologi penting, dan tinjauan literatur yang wajib dimasukkan. Perhatikan aturan proyek (AGENTS.md) jika ada."
        )
    },
    "Planner": {
        "role": "Planner",
        "name": "Struktur & Outline",
        "icon": "📋",
        "temperature": 0.2,
        "system": (
            "Kamu adalah Perancang Struktur Tulisan Akademik. Menggunakan hasil temuan Researcher dan konteks proyek, buatlah struktur outline laporan lengkap (Bab 1 Pendahuluan, Bab 2 Kajian Teori, Bab 3 Pembahasan/Analisis, Bab 4 Kesimpulan & Saran)."
        )
    },
    "Writer": {
        "role": "Writer",
        "name": "Penulis Makalah",
        "icon": "✍️",
        "temperature": 0.4,
        "system": (
            "Kamu adalah Penulis Akademik Ilmiah. Berdasarkan outline dari Planner dan rujukan Researcher, tuliskan draf pembahasan komprehensif dengan bahasa formal baku Indonesia (EYD), argumentasi logis, dan alur terstruktur."
        )
    },
    "Reviewer": {
        "role": "Reviewer",
        "name": "Reviewer & QA Akademik",
        "icon": "🎯",
        "temperature": 0.2,
        "system": (
            "Kamu adalah Dosen Reviewer Akademik. Evaluasi tulisan dari Penulis. Periksa konsistensi argumen, kejelasan bahasa, kelengkapan pembahasan, dan perbaiki bagian yang kurang tajam. Di baris paling akhir dari evaluasimu, berikan status kelayakan dalam format wajib:\nVERDICT: PASSED atau VERDICT: NEEDS_REVISION\nJika NEEDS_REVISION, sertakan catatan perbaikan spesifik."
        )
    }
}

def parse_orchestrator_plan(text: str) -> List[Dict[str, Any]]:
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

    stages = []
    if data and isinstance(data, dict):
        roles = data.get("selected_roles") or data.get("stages") or []
        for r in roles:
            role_key = None
            for k in SPECIALIST_CATALOG.keys():
                if k.lower() == str(r).lower() or k.lower() in str(r).lower() or str(r).lower() in k.lower():
                    role_key = k
                    break
            if role_key and role_key in SPECIALIST_CATALOG:
                spec = SPECIALIST_CATALOG[role_key]
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
        elif any(w in text_lower for w in ["kuliah", "makalah", "riset", "paper", "jurnal"]):
            fallback_roles = ["Researcher", "Writer", "Reviewer"]
        elif any(w in text_lower for w in ["ui", "ux", "tampilan", "layar", "screen", "frontend", "flutter"]):
            fallback_roles = ["Architect", "UI/UX", "Coder", "QA"]
        else:
            fallback_roles = ["Architect", "Coder", "QA"]
        for r in fallback_roles:
            spec = SPECIALIST_CATALOG[r]
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

    return stages

PRESETS: Dict[str, Dict[str, Any]] = {
    "auto": {
        "id": "auto",
        "title": "Mode Auto-Pilot (Dynamic Orchestrator)",
        "icon": "🤖",
        "description": "Orchestrator AI menganalisis tugas dan otomatis memilih tim spesialis yang tepat (mem-bypass UI/UX jika tidak dibutuhkan).",
        "stages": [
            {
                "role": "Orchestrator",
                "name": "AI Team Orchestrator",
                "icon": "🧠",
                "temperature": 0.1,
                "system": (
                    "Kamu adalah AI Team Orchestrator & Technical Project Lead handal. "
                    "Tugasmu adalah menganalisis target tugas dari user, konteks arsitektur proyek (AGENTS.md), dan repositori skeleton, "
                    "lalu menyusun tim kerja (pipeline) yang paling efisien dari katalog spesialis yang tersedia.\n\n"
                    "KATALOG SPESIALIS YANG TERSEDIA:\n"
                    "- 'Architect' (System Architect): Rancang arsitektur, skema DB/ERD, struktur modul. HANYA untuk fitur baru atau refactor besar.\n"
                    "- 'UI/UX' (UI/UX Specialist): Wireframe visual, user flow, component styling. HANYA jika tugas melibatkan tampilan visual / frontend UI. DILARANG diikutsertakan jika tugas murni backend, API, security audit, database, atau bugfix non-UI!\n"
                    "- 'Coder' (Lead Developer): Menulis/mengubah kode sumber multi-file produksi (### FILE:).\n"
                    "- 'Security' (Security Auditor): Khusus audit celah OWASP, SQLi, Auth, Token, Race Condition.\n"
                    "- 'QA' (QA & Security Engineer): Verifikasi kode akhir & penguji batas (VERDICT: PASSED/NEEDS_REVISION).\n"
                    "- 'Researcher' (Pustaka & Riset): Konsep teori & literatur (khusus makalah/kuliah).\n"
                    "- 'Writer' (Penulis Makalah): Penulisan draf akademik (khusus makalah/kuliah).\n"
                    "- 'Reviewer' (Reviewer Akademik): Telaah naskah akademik (khusus makalah/kuliah).\n\n"
                    "PANDUAN PEMILIHAN TIM:\n"
                    "1. Pilih minimal 2 peran dan maksimal 4 peran yang BENAR-BENAR relevan.\n"
                    "2. JANGAN PERNAH menyertakan 'UI/UX' jika tidak ada pembuatan atau modifikasi layar visual antarmuka pengguna!\n"
                    "3. Jika tugas adalah Security Audit / Celah Keamanan / Bugfix: gunakan [Security, Coder, QA] atau [Coder, QA].\n"
                    "4. Jika tugas adalah Fitur UI Mobile/Web lengkap: gunakan [Architect, UI/UX, Coder, QA].\n"
                    "5. Jika tugas adalah Backend API / Database saja: gunakan [Architect, Coder, QA].\n"
                    "6. Jika tugas akademik / kuliah: gunakan [Researcher, Writer, Reviewer].\n\n"
                    "FORMAT KELUARAN:\n"
                    "Tuliskan analisis 1-2 paragraf mengenai strategi pengerjaan tugas, tim spesialis yang ditugaskan, dan peran yang sengaja DI-EXCLUDE (dilewati).\n"
                    "Di baris paling akhir, WAJIB sertakan blok JSON valid berikut:\n"
                    "```json\n"
                    "{\n"
                    "  \"task_type\": \"SECURITY_AUDIT | FEATURE_DEV | BACKEND_API | BUGFIX | ACADEMIC\",\n"
                    "  \"selected_roles\": [\"Role1\", \"Role2\", ...],\n"
                    "  \"reasoning\": \"Penjelasan singkat mengapa tim ini yang dipilih\"\n"
                    "}\n"
                    "```"
                )
            }
        ]
    },
    "kuliah": {
        "id": "kuliah",
        "title": "Mode Kuliah & Riset Akademik",
        "icon": "🎓",
        "description": "Penyusunan tugas, makalah, review jurnal, dan laporan berbasis metodologi.",
        "stages": [
            {
                "role": "Researcher",
                "name": "Pustaka & Riset",
                "icon": "🔍",
                "temperature": 0.3,
                "system": "Kamu adalah Peneliti & Akademisi Senior. Analisis topik tugas/makalah, petakan konsep teori utama, fakta relevan, terminologi penting, dan tinjauan literatur yang wajib dimasukkan. Perhatikan aturan proyek (AGENTS.md) jika ada."
            },
            {
                "role": "Planner",
                "name": "Struktur & Outline",
                "icon": "📋",
                "temperature": 0.2,
                "system": "Kamu adalah Perancang Struktur Tulisan Akademik. Menggunakan hasil temuan Researcher dan konteks proyek, buatlah struktur outline laporan lengkap (Bab 1 Pendahuluan, Bab 2 Kajian Teori, Bab 3 Pembahasan/Analisis, Bab 4 Kesimpulan & Saran)."
            },
            {
                "role": "Writer",
                "name": "Penulis Makalah",
                "icon": "✍️",
                "temperature": 0.4,
                "system": "Kamu adalah Penulis Akademik Ilmiah. Berdasarkan outline dari Planner dan rujukan Researcher, tuliskan draf pembahasan komprehensif dengan bahasa formal baku Indonesia (EYD), argumentasi logis, dan alur terstruktur."
            },
            {
                "role": "Reviewer",
                "name": "Reviewer & QA Akademik",
                "icon": "🎯",
                "temperature": 0.2,
                "system": "Kamu adalah Dosen Reviewer Akademik. Evaluasi tulisan dari Penulis. Periksa konsistensi argumen, kejelasan bahasa, kelengkapan pembahasan, dan perbaiki bagian yang kurang tajam. Di baris paling akhir dari evaluasimu, berikan status kelayakan dalam format wajib:\nVERDICT: PASSED atau VERDICT: NEEDS_REVISION\nJika NEEDS_REVISION, sertakan catatan perbaikan spesifik."
            }
        ]
    },
    "dev": {
        "id": "dev",
        "title": "Mode Software & Web/App Dev",
        "icon": "💻",
        "description": "Pengembangan aplikasi dari arsitektur sistem, UI/UX, implementasi kode multi-file, hingga security & QA loop.",
        "stages": [
            {
                "role": "Architect",
                "name": "System Architect",
                "icon": "📐",
                "temperature": 0.1,
                "system": "Kamu adalah Principal System Architect. Rancang arsitektur sistem untuk kebutuhan fitur berikut. Ikuti spesifikasi arsitektur proyek (AGENTS.md) jika ada. Tentukan entity relationship/schema database, struktur tabel, nama file yang akan dibuat/diubah, dan pola endpoint API."
            },
            {
                "role": "UI/UX",
                "name": "UI/UX Specialist",
                "icon": "🎨",
                "temperature": 0.4,
                "system": "Kamu adalah Senior UI/UX Specialist. Berdasarkan spesifikasi arsitektur, buat wireframe tata letak layar (screen layout), alur navigasi pengguna (user flow), daftar komponen interaktif, dan panduan styling yang jelas."
            },
            {
                "role": "Coder",
                "name": "Lead Developer",
                "icon": "⚡",
                "temperature": 0.1,
                "system": "Kamu adalah Lead Full-Stack Developer handal. Tulis kode implementasi produksi yang bersih, modular, dan lengkap (backend/frontend) sesuai instruksi Architect dan UI/UX sebelumnya. Tanpa placeholder malas (tulis kode nyata).\n\nPENTING: Setiap file kode yang kamu buat/ubah WAJIB ditulis dengan header format multi-file terpisah berikut:\n### FILE: path/to/file.ext\n```ext\n// kode lengkap di sini\n```\nContoh:\n### FILE: lib/services/auth_service.dart\n```dart\n// kode\n```"
            },
            {
                "role": "QA",
                "name": "QA & Security Engineer",
                "icon": "🛡️",
                "temperature": 0.1,
                "system": "Kamu adalah Senior QA & Security Engineer. Audit kode dari Coder. Cari celah keamanan, race condition, error penanganan null, dan edge case. Periksa apakah kode sesuai dengan struktur proyek.\n\nPENTING: Di baris paling akhir dari evaluasimu, WAJIB tuliskan satu baris penutup status:\nVERDICT: PASSED atau VERDICT: NEEDS_REVISION\nJika NEEDS_REVISION, sertakan daftar ringkas poin bug yang wajib diperbaiki."
            }
        ]
    },
    "security": {
        "id": "security",
        "title": "Mode Audit & Security Fix",
        "icon": "🛡️",
        "description": "Audit celah keamanan kode (injection, race condition, token leakage) dan patch kode perbaikan tanpa tahap UI/UX.",
        "stages": [
            {
                "role": "Security",
                "name": "Security Auditor",
                "icon": "🔍",
                "temperature": 0.1,
                "system": "Kamu adalah Senior Application Security Auditor & Penetration Tester. Analisis codebase target secara mendalam: periksa celah OWASP, SQL Injection, Auth Bypass, Broken Access Control / IDOR, Race Condition, Token Leakage, dan penanganan input yang tidak aman. Sajikan laporan audit terstruktur: Lokasi file/line, tingkat keparahan (Critical/High/Medium), bukti celah, dan rekomendasi mitigasi teknis yang konkret. DILARANG keras memanggil tool, format XML DSML, atau sintaks perintah bash."
            },
            {
                "role": "Coder",
                "name": "Security Patch Developer",
                "icon": "🔧",
                "temperature": 0.1,
                "system": "Kamu adalah Senior Security Engineer & Backend Specialist. Berdasarkan temuan Security Auditor sebelumnya, tuliskan implementasi kode perbaikan (security patch / hardening) yang bersih, modular, dan aman. Tanpa placeholder malas.\n\nPENTING: Setiap file kode yang kamu buat/ubah WAJIB ditulis dengan header format multi-file terpisah berikut:\n### FILE: path/to/file.ext\n```ext\n// kode perbaikan lengkap di sini\n```"
            },
            {
                "role": "QA",
                "name": "Verification QA",
                "icon": "🛡️",
                "temperature": 0.1,
                "system": "Kamu adalah QA Lead. Verifikasi kode perbaikan dari Security Patch Developer. Pastikan semua celah yang dilaporkan telah tertutup sempurna tanpa merusak fungsionalitas sistem yang sudah ada.\n\nPENTING: Di baris paling akhir dari evaluasimu, WAJIB tuliskan satu baris penutup status:\nVERDICT: PASSED atau VERDICT: NEEDS_REVISION\nJika NEEDS_REVISION, sertakan daftar celah yang belum tertutup."
            }
        ]
    }
}

tasks_store: Dict[str, Dict[str, Any]] = {}

def load_tasks():
    global tasks_store
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                tasks_store = json.load(f)
        except Exception as e:
            print(f"Error loading tasks: {e}")
            tasks_store = {}

def save_tasks():
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks_store, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving tasks: {e}")

load_tasks()

def sanitize_path(path: str, base_dir: Optional[str] = None) -> str:
    path = os.path.abspath(path.strip())
    root = os.path.abspath(base_dir) if base_dir else os.path.abspath(ALLOWED_ROOT)
    try:
        resolved_path = Path(path).resolve()
        resolved_root = Path(root).resolve()
        if not (resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)):
            raise HTTPException(status_code=400, detail="Akses direktori di luar batas diizinkan.")
    except Exception:
        raise HTTPException(status_code=400, detail="Akses direktori di luar batas diizinkan.")
    return path

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

def extract_code_files(text: str) -> List[Dict[str, Any]]:
    pattern = r'(?:###\s*FILE:|(?:\*\*|#)?FILE:(?:\*\*)?)\s*[`"]?([a-zA-Z0-9_\-\.\/\\]+)[`"]?\s*\n+```([a-zA-Z0-9_\-]+)?\n([\s\S]*?)```'
    matches = re.findall(pattern, text)
    files_map: Dict[str, Dict[str, Any]] = {}
    for m in matches:
        fpath = m[0].strip().replace('\\', '/').strip('/')
        lang = m[1].strip() or "text"
        code = m[2]
        if fpath:
            # Match paling akhir menimpa match sebelumnya (hasil revisi auto-fix menang)
            files_map[fpath] = {
                "path": fpath,
                "language": lang,
                "content": code,
                "lines": len(code.strip().splitlines())
            }
    return list(files_map.values())

def get_git_info(target_dir: str) -> Dict[str, Any]:
    git_dir = os.path.join(target_dir, ".git")
    if not os.path.exists(git_dir):
        return {"is_git": False, "branch": None, "clean": True}
    try:
        branch = subprocess.check_output(
            ["git", "-C", target_dir, "branch", "--show-current"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        status = subprocess.check_output(
            ["git", "-C", target_dir, "status", "--porcelain"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        return {"is_git": True, "branch": branch or "HEAD", "clean": len(status) == 0}
    except Exception:
        return {"is_git": True, "branch": "unknown", "clean": True}

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

async def execute_pipeline(task_id: str):
    task = tasks_store.get(task_id)
    if not task:
        return
    task["status"] = "running"
    task["started_at"] = time.time()
    save_tasks()

    context_chain = ""
    wdir = None
    if task.get("working_directory"):
        try:
            wdir = sanitize_path(task["working_directory"])
            context_chain += f"=== WORKING DIRECTORY: {wdir} ===\n"
            
            # Auto-inject codebase skeleton / repo map
            repo_map = generate_repo_map(wdir)
            if repo_map:
                context_chain += f"=== EXISTING REPOSITORY SKELETON (CODEBASE) ===\n{repo_map}\n\n"
        except Exception:
            pass

    if task.get("project_context"):
        context_chain += f"=== PROJECT RULES & CONTEXT (AGENTS.md) ===\n{task['project_context']}\n\n"
    
    # Auto-inject Hermes Skills SOPs
    if task.get("skills"):
        skills_context = ""
        for sk in task["skills"]:
            content = read_skill_content(sk)
            if content:
                skills_context += f"--- HERMES SKILL SOP: {sk} ---\n{content}\n\n"
        if skills_context:
            context_chain += f"=== ACTIVE HERMES SKILLS / GUIDELINES ===\n{skills_context}\n"

    context_chain += f"=== TARGET TUGAS / GOAL ===\n{task['prompt']}\n\n"

    try:
        idx = 0
        while idx < len(task["stages"]):
            stage = task["stages"][idx]
            if task.get("cancelled", False):
                stage["status"] = "cancelled"
                break

            stage["status"] = "running"
            stage["started_at"] = time.time()
            save_tasks()

            user_msg = (
                f"{context_chain}\n"
                f"Tugas kamu sekarang sebagai [{stage['role']} - {stage['name']}]:\n"
                f"Lakukan tugas sesuai peran dan panduan spesialisasi yang diberikan."
            )

            try:
                temp = stage.get("temperature", 0.2)
                output = await call_llm(stage["system"], user_msg, temperature=temp)
                stage["output"] = output
                stage["status"] = "completed"
                stage["completed_at"] = time.time()
                context_chain += f"=== HASIL DARI [{stage['role']} - {stage['name']}] ===\n{output}\n\n"
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
                save_tasks()
                return

            save_tasks()

            # --- DYNAMIC ORCHESTRATION ---
            # If current stage is Orchestrator, parse the execution plan and dynamically append worker stages
            if stage["role"] == "Orchestrator":
                dynamic_stages = parse_orchestrator_plan(output)
                if dynamic_stages:
                    for ds in dynamic_stages:
                        task["stages"].append(ds)
                    save_tasks()

            # --- HUMAN APPROVAL GATE ---
            # If enabled and current stage is Orchestrator, Architect or Planner, pause for human steering
            if task.get("require_approval") and stage["role"] in ["Orchestrator", "Architect", "Planner"] and not task.get("stages_approved", {}).get(str(idx)):
                task["status"] = "waiting_approval"
                task["waiting_stage_index"] = idx
                task["waiting_stage_name"] = stage["name"]
                save_tasks()

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
                    context_chain += f"=== HUMAN SUPERVISOR FEEDBACK & DIRECTIVES ===\n{task['latest_feedback']}\n\n"
                    task["latest_feedback"] = None
                save_tasks()

            # --- AUTO-FIX / VERIFICATION LOOP ---
            # If QA stage returns VERDICT: NEEDS_REVISION, loop back to Coder
            if stage["role"] in ["QA", "Reviewer"] and "VERDICT: NEEDS_REVISION" in output:
                curr_loop = task.get("current_fix_loop", 0)
                max_loops = task.get("auto_fix_loops", 1)
                if curr_loop < max_loops:
                    task["current_fix_loop"] = curr_loop + 1
                    
                    fix_stage = {
                        "role": "Coder",
                        "name": f"Lead Developer (Auto-Fix Cycle #{task['current_fix_loop']})",
                        "icon": "🔧",
                        "temperature": 0.1,
                        "system": "Kamu adalah Lead Full-Stack Developer. QA menemukan catatan perbaikan/bug pada kode sebelumnya. Analisis kritik QA, perbaiki implementasi secara presisi dan menyeluruh. Setiap file wajib ditulis dengan format `### FILE: path/to/file.ext`.",
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
                    save_tasks()

            idx += 1

        if task.get("cancelled"):
            task["status"] = "cancelled"
            task["completed_at"] = time.time()
            task["final_output"] = context_chain
            save_tasks()
            return

        task["status"] = "completed"
        task["completed_at"] = time.time()
        task["final_output"] = context_chain

        # Extract multi-file blocks from entire conversation/coder outputs
        extracted = extract_code_files(context_chain)
        task["extracted_files"] = extracted

        # Auto-write files if requested and working directory is set
        if task.get("auto_apply_files") and wdir and extracted:
            written_files = []
            for item in extracted:
                try:
                    rel_p = item["path"]
                    full_p = sanitize_path(os.path.join(wdir, rel_p.lstrip("/")), base_dir=wdir)
                    os.makedirs(os.path.dirname(full_p), exist_ok=True)
                    if os.path.exists(full_p):
                        shutil.copy2(full_p, f"{full_p}.bak")
                    with open(full_p, "w", encoding="utf-8") as fp:
                        fp.write(item["content"])
                    written_files.append(rel_p)
                except Exception as e:
                    print(f"Error applying file {item['path']}: {e}")
            task["applied_files"] = written_files

        # Auto-save deliverable document if enabled
        if task.get("auto_save_artifact") and wdir:
            try:
                if os.path.exists(wdir) and os.path.isdir(wdir):
                    artifact_name = f"DELIVERABLE_{task_id}.md"
                    artifact_path = sanitize_path(os.path.join(wdir, artifact_name), base_dir=wdir)
                    with open(artifact_path, "w", encoding="utf-8") as f:
                        f.write(context_chain)
                    task["saved_artifact_path"] = artifact_path
            except Exception as e:
                task["saved_artifact_error"] = str(e)

    except Exception as e:
        task["status"] = "error"
        task["error"] = str(e)
    finally:
        save_tasks()

@app.get("/api/presets")
async def get_presets():
    return list(PRESETS.values())

@app.get("/api/workspace/info")
async def get_workspace_info(path: Optional[str] = None):
    target = sanitize_path(path or "/home/andreadst/projects")
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
    file_path = os.path.join(target_dir, fname)
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
        "cancelled": False
    }
    tasks_store[task_id] = task
    save_tasks()

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
    else:
        stage_idx = str(task.get("waiting_stage_index", 0))
        task.setdefault("stages_approved", {})[stage_idx] = True
        if req.feedback:
            task["latest_feedback"] = req.feedback

    save_tasks()

    if task_id in APPROVAL_EVENTS:
        APPROVAL_EVENTS[task_id].set()

    return {"status": "ok", "task_status": task["status"]}

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
    if not wdir or not os.path.exists(wdir):
        raise HTTPException(status_code=400, detail="Working directory tidak valid.")

    files = task.get("extracted_files", [])
    if not files:
        files = extract_code_files(task.get("final_output", ""))

    if not files:
        raise HTTPException(status_code=400, detail="Tidak ada blok file kode (### FILE: path) yang terdeteksi.")

    applied = []
    for item in files:
        rel_p = item["path"]
        full_p = sanitize_path(os.path.join(wdir, rel_p.lstrip("/")), base_dir=wdir)
        os.makedirs(os.path.dirname(full_p), exist_ok=True)
        if os.path.exists(full_p):
            shutil.copy2(full_p, f"{full_p}.bak")
        with open(full_p, "w", encoding="utf-8") as fp:
            fp.write(item["content"])
        applied.append({"path": rel_p, "lines": item["lines"]})

    task["applied_files"] = [a["path"] for a in applied]
    save_tasks()
    return {"status": "ok", "applied": applied, "count": len(applied)}

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
    success = update_hermes_model(req.model)
    if not success:
        raise HTTPException(status_code=500, detail="Gagal memperbarui model ke config Hermes")
    return {"status": "ok", "active_model": req.model}

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
        "tailscale_ip": "100.104.131.60",
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
