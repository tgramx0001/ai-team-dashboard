"""Workspace boundary security primitives shared by the API and terminal routes.

Moved out of main.py so route modules can import them without circular imports.
Keeps the exact same names and behavior as before (main.py re-exports them).
"""
from __future__ import annotations

import asyncio
import os
import re
import signal as _signal
import sys
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException

IS_WINDOWS = sys.platform.startswith("win")


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
    # Mask explicitly configured keys/tokens. AUTH_TOKEN is resolved from the
    # main module first so runtime overrides (tests, future config) are honored.
    configured_secrets = set()
    main_mod = sys.modules.get("main")
    auth = getattr(main_mod, "AUTH_TOKEN", None) if main_mod else None
    if auth and len(auth) >= 4:
        configured_secrets.add(auth)
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


async def _kill_process_tree(proc: "asyncio.subprocess.Process") -> None:
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
            os.killpg(os.getpgid(pid), _signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
