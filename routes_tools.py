"""Workspace tool routes: interactive terminal (pipe + PTY + WebSocket) and
read-only git inspection.

Extracted from main.py (Phase 5, item 2) so the backend has clear module
boundaries without changing any API contract:

  POST /api/workspace/terminal        - run one command (pipe or PTY mode)
  GET  /api/workspace/git/status      - read-only git status
  GET  /api/workspace/git/diff        - read-only unified diff
  WS   /api/workspace/terminal/ws     - interactive streaming terminal session
"""
from __future__ import annotations

import asyncio
import hmac
import os
import queue
import re
import signal
import subprocess
import threading
import time
from typing import Callable, Optional, Tuple

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from security_utils import (
    DEFAULT_WORKSPACE,
    IS_WINDOWS,
    _clean_terminal_env,
    _kill_process_tree,
    _mask_sensitive_text,
    sanitize_path,
)

router = APIRouter()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _truncate(text: str, limit: int) -> str:
    if len(text) > limit:
        return text[:limit] + f"\n... [Output truncated: exceeded {limit} chars]"
    return text


class TerminalExecuteRequest(BaseModel):
    command: str
    path: Optional[str] = None
    timeout: Optional[int] = 30
    stdin: Optional[str] = None       # data written to the command's stdin before EOF
    pty: Optional[bool] = False       # POSIX only: run under a pseudo-terminal


# ---------------------------------------------------------------------------
# PTY runner (POSIX). Runs in a worker thread; blocking by design.
# ---------------------------------------------------------------------------

def _pty_run_blocking(
    command: str,
    cwd: str,
    timeout_sec: int,
    input_q: Optional["queue.Queue"] = None,
    stop_evt: Optional[threading.Event] = None,
    on_snapshot: Optional[Callable[[str], None]] = None,
    initial_stdin: Optional[str] = None,
) -> Tuple[str, int, bool]:
    """Run `command` under a pseudo-terminal. Returns (output, exit_code, timed_out).

    `input_q` receives bytes to write to the TTY while the command runs (used by
    the WebSocket session for interactive prompts). `stop_evt` aborts early.
    `on_snapshot` is called with the full output text whenever it grows.
    """
    import fcntl
    import pty
    import select
    import termios

    master_fd, slave_fd = pty.openpty()

    def _preexec():
        # Make the child a session leader with the slave PTY as controlling terminal.
        try:
            os.setsid()
        except OSError:
            pass
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    env = _clean_terminal_env()
    # PTY programs expect a terminal-sized TERM; provide sane defaults.
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLUMNS", "200")
    env.setdefault("LINES", "50")

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=slave_fd,
            stderr=subprocess.STDOUT,
            preexec_fn=_preexec,
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    finally:
        try:
            os.close(slave_fd)
        except OSError:
            pass

    chunks = []
    sent_len = 0
    timed_out = False
    deadline = time.time() + timeout_sec

    def _snapshot(force: bool = False):
        if on_snapshot is None:
            return
        text = b"".join(chunks).decode("utf-8", errors="ignore")
        masked = _mask_sensitive_text(_strip_ansi(text))
        if force or len(masked) != sent_len:
            on_snapshot(masked)

    # Write initial stdin (prompt answers known up front), then keep it open so
    # interactive programs can still read later input from input_q.
    if initial_stdin:
        try:
            proc.stdin.write(initial_stdin.encode("utf-8", errors="ignore"))
            proc.stdin.flush()
        except Exception:
            pass

    try:
        while True:
            if stop_evt is not None and stop_evt.is_set():
                break
            if time.time() > deadline:
                timed_out = True
                break

            # Drain interactive input from the WS client.
            if input_q is not None:
                try:
                    while True:
                        data = input_q.get_nowait()
                        if data:
                            proc.stdin.write(data)
                            proc.stdin.flush()
                except (queue.Empty, ValueError, OSError):
                    pass

            r, _, _ = select.select([master_fd], [], [], 0.15)
            if r:
                try:
                    data = os.read(master_fd, 65536)
                except OSError:
                    break  # EIO: child side closed the PTY
                if not data:
                    break
                chunks.append(data)
                _snapshot()
                continue

            if proc.poll() is not None:
                # Child exited: drain anything still buffered in the PTY.
                while True:
                    r2, _, _ = select.select([master_fd], [], [], 0.05)
                    if not r2:
                        break
                    try:
                        data = os.read(master_fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    chunks.append(data)
                break
    finally:
        if timed_out or (stop_evt is not None and stop_evt.is_set()):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    exit_code = proc.returncode if proc.returncode is not None else -1
    text = b"".join(chunks).decode("utf-8", errors="ignore")
    text = _mask_sensitive_text(_strip_ansi(text))
    _snapshot(force=True)
    return text, exit_code, timed_out


# ---------------------------------------------------------------------------
# POST /api/workspace/terminal
# ---------------------------------------------------------------------------

@router.post("/api/workspace/terminal")
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

    # 2b. PTY mode (POSIX): TTY-aware commands (prompts, colors, pagers) see a real terminal.
    if req.pty:
        if IS_WINDOWS:
            return {
                "stdout": "",
                "stderr": "PTY mode tidak tersedia di Windows; jalankan tanpa flag pty.",
                "exit_code": 1,
                "duration_ms": 0,
                "cwd": cwd,
                "timed_out": False,
                "pty": False,
            }
        try:
            text, exit_code, timed_out = await asyncio.to_thread(
                _pty_run_blocking, raw_cmd, cwd, timeout_sec,
                None, None, None, req.stdin,
            )
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"Execution error: {str(e)}",
                "exit_code": 1,
                "duration_ms": int((time.time() - start_t) * 1000),
                "cwd": cwd,
                "timed_out": False,
                "pty": True,
            }
        text = _truncate(text, max_output_chars)
        return {
            "stdout": text if not timed_out else text,
            "stderr": f"Error: Command timed out after {timeout_sec} seconds." if timed_out else "",
            "exit_code": -1 if timed_out else exit_code,
            "duration_ms": int((time.time() - start_t) * 1000),
            "cwd": cwd,
            "timed_out": timed_out,
            "pty": True,
        }

    # 3. Default pipe mode: stdout and stderr captured separately.
    stdin_bytes = req.stdin.encode("utf-8", errors="ignore") if req.stdin is not None else None
    try:
        # Execute asynchronously with process group to guarantee clean subprocess teardown
        # and filtered environment to prevent secret leakage via `env`/`printenv`
        subproc_kwargs = {
            "cwd": cwd,
            "env": _clean_terminal_env(),
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if stdin_bytes is not None:
            subproc_kwargs["stdin"] = asyncio.subprocess.PIPE
        if not IS_WINDOWS:
            subproc_kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_shell(
            raw_cmd,
            **subproc_kwargs
        )

        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
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
                "timed_out": True,
                "pty": False,
            }

        duration_ms = int((time.time() - start_t) * 1000)
        stdout_str = stdout_data.decode("utf-8", errors="ignore")
        stderr_str = stderr_data.decode("utf-8", errors="ignore")

        stdout_str = _truncate(stdout_str, max_output_chars)
        if stderr_str and len(stderr_str) > max_output_chars:
            stderr_str = stderr_str[:max_output_chars] + f"\n... [Error output truncated: exceeded {max_output_chars} chars]"

        return {
            "stdout": _mask_sensitive_text(stdout_str),
            "stderr": _mask_sensitive_text(stderr_str),
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "cwd": cwd,
            "timed_out": False,
            "pty": False,
        }
    except Exception as e:
        duration_ms = int((time.time() - start_t) * 1000)
        return {
            "stdout": "",
            "stderr": f"Execution error: {str(e)}",
            "exit_code": 1,
            "duration_ms": duration_ms,
            "cwd": cwd,
            "timed_out": False,
            "pty": False,
        }


# ---------------------------------------------------------------------------
# Read-only git inspection (workspace scoped, no client-supplied repo paths)
# ---------------------------------------------------------------------------

def _get_git_info(target_dir: str):
    import main as _main
    return _main.get_git_info(target_dir)


@router.get("/api/workspace/git/status")
async def api_workspace_git_status(path: Optional[str] = None):
    """Get git status scoped strictly to the current workspace repository."""
    target_dir = sanitize_path(path or DEFAULT_WORKSPACE)
    if not os.path.isdir(target_dir):
        raise HTTPException(status_code=400, detail="Direktori workspace tidak valid.")
    git_info = _get_git_info(target_dir)
    return {
        "path": target_dir,
        "is_git": git_info["is_git"],
        "branch": git_info["branch"],
        "clean": git_info["clean"],
        "status_lines": git_info["status_lines"]
    }


@router.get("/api/workspace/git/diff")
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
    except Exception:
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


# ---------------------------------------------------------------------------
# WebSocket interactive terminal (POSIX)
# Protocol: first message MUST be {"type":"auth","token":...} -> {"type":"ready"}
#   {"type":"run","command":...,"path":...,"timeout":...}  start a PTY session
#   {"type":"input","data":"..."}                          send keystrokes while running
#   server pushes {"type":"output","text": <full masked text so far>}
#   then {"type":"exit","code":...,"timed_out":...,"duration_ms":...}
# ---------------------------------------------------------------------------

def _ws_auth_ok(token: Optional[str], websocket: WebSocket) -> bool:
    import main as _main
    expected = getattr(_main, "AUTH_TOKEN", "") or ""
    if expected:
        return bool(token) and hmac.compare_digest(str(token).encode(), expected.encode())
    # Fail closed: without a token only loopback clients may connect.
    host = websocket.client.host if websocket.client else ""
    return host in ("127.0.0.1", "::1")


@router.websocket("/api/workspace/terminal/ws")
async def workspace_terminal_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
    except Exception:
        try:
            await websocket.close(code=4401)
        except Exception:
            pass
        return
    if first.get("type") != "auth" or not _ws_auth_ok(first.get("token"), websocket):
        try:
            await websocket.close(code=4401)
        except Exception:
            pass
        return

    if IS_WINDOWS:
        await websocket.send_json({
            "type": "error",
            "detail": "Interactive WebSocket terminal hanya tersedia di POSIX; gunakan POST /api/workspace/terminal.",
        })
        await websocket.close(code=4402)
        return

    await websocket.send_json({"type": "ready"})

    loop = asyncio.get_running_loop()
    input_q: "queue.Queue[bytes]" = queue.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    stop_evt = threading.Event()
    run_task: Optional[asyncio.Future] = None
    run_started_at: float = 0.0

    def push(item):
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, item)
        except RuntimeError:
            pass

    async def drain_out_q():
        while True:
            try:
                item = out_q.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item[0] == "text":
                await websocket.send_json({"type": "output", "text": item[1]})
            else:
                await websocket.send_json(item[1])

    async def start_run(msg) -> bool:
        nonlocal run_task, run_started_at
        command = (msg.get("command") or "").strip()
        if not command:
            await websocket.send_json({"type": "error", "detail": "Perintah tidak boleh kosong."})
            return False
        try:
            cwd = sanitize_path(msg.get("path") or DEFAULT_WORKSPACE)
        except HTTPException as e:
            await websocket.send_json({"type": "error", "detail": e.detail})
            return False
        if not os.path.isdir(cwd):
            await websocket.send_json({"type": "error", "detail": "Direktori kerja (cwd) tidak valid."})
            return False
        timeout_sec = min(max(int(msg.get("timeout") or 30), 1), 120)
        stop_evt.clear()
        while not out_q.empty():
            out_q.get_nowait()
        initial_stdin = msg.get("stdin")
        run_started_at = time.time()
        run_task = loop.run_in_executor(
            None,
            _pty_run_blocking,
            command, cwd, timeout_sec, input_q, stop_evt,
            (lambda t: push(("text", t))),
            initial_stdin,
        )
        await websocket.send_json({"type": "started", "cwd": cwd, "timeout": timeout_sec})
        return True

    async def finish_run(fut) -> None:
        nonlocal run_task
        exit_code, timed_out = -1, False
        try:
            _, exit_code, timed_out = await fut
        except Exception:
            pass
        await drain_out_q()
        await websocket.send_json({
            "type": "exit",
            "code": -1 if timed_out else exit_code,
            "timed_out": bool(timed_out),
            "duration_ms": int((time.time() - run_started_at) * 1000),
        })
        run_task = None

    try:
        recv_task = asyncio.ensure_future(websocket.receive_json())
        while True:
            wait_set = {t for t in (recv_task, run_task) if t is not None}
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED, timeout=0.3)
            await drain_out_q()

            if run_task is not None and run_task.done():
                await finish_run(run_task)
                continue

            for d in done:
                if d is recv_task:
                    try:
                        msg = d.result()
                    except (WebSocketDisconnect, asyncio.CancelledError):
                        raise
                    except Exception:
                        raise WebSocketDisconnect(code=1000)
                    recv_task = asyncio.ensure_future(websocket.receive_json())
                    mtype = msg.get("type")
                    if mtype == "input":
                        if run_task is not None and not run_task.done():
                            input_q.put(str(msg.get("data", "")).encode("utf-8", errors="ignore"))
                    elif mtype == "run":
                        if run_task is not None and not run_task.done():
                            await websocket.send_json({"type": "error", "detail": "Sesi masih berjalan; tunggu selesai atau kirim exit."})
                        else:
                            await start_run(msg)
                    elif mtype == "exit":
                        stop_evt.set()
                    elif mtype == "close":
                        raise WebSocketDisconnect(code=1000)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stop_evt.set()
        if run_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(run_task), timeout=5)
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
