# AI Team Workstation (AI Team Hub)

## Overview & Architecture
- **Type**: Autonomous Multi-Agent Workstation & Project Orchestrator
- **Backend**: FastAPI (`main.py`, `store.py`) with zero-overhead async architecture and SQLite persistent storage.
- **Frontend**: Clean modular UI (`index.html`, `static/js/app.js`, `static/js/chat.js`) inspired by Linear & Claude Design System (Inter, JetBrains Mono, dark aesthetic).
- **Service**: Systemd user unit `ai-team.service` on port `8090`. Memory cap: 300MB.
- **LLM Gateway**: 9Router local endpoint (`http://127.0.0.1:20128/v1/chat/completions`) model `bai` (deepseek-v4.1-flash).

## Conventions & Rules
1. **Lightweight & High Efficiency**: Do NOT introduce heavy frameworks (Docker, npm/node build steps, heavy ORM). Keep backend and frontend clean, vanilla, and fast.
2. **Cross-Platform Compatibility**: Path operations MUST use `os.path` / `pathlib.Path` abstractions and avoid assuming POSIX-only paths or commands. Support both Linux and Windows environments cleanly.
3. **Path Sanitization & Boundary Control**: All file and terminal operations MUST sanitize paths within the designated workspace roots (`ALLOWED_ROOTS`) via `sanitize_path()` to prevent directory traversal.
4. **Interactive Terminal Security**: The terminal API (`/api/workspace/terminal`) must enforce strict CWD boundaries, process group termination (`_kill_process_tree`), output size limits, and secret masking (`_mask_sensitive_text`). Environment secrets must remain stripped from child process environments.
5. **Git Tooling**: Git inspection endpoints (`/api/workspace/git/status` and `/api/workspace/git/diff`) are strictly read-only and bound to the workspace directory.
6. **Multi-File Code Extraction**: Agents generate code using standardized `### FILE: path/to/file.ext` blocks.
7. **Safety Backups**: Overwriting existing files during multi-file direct apply MUST create `.bak` copies.
8. **Testing Organization**: Tests live under `tests/` categorized by functionality (`security/`, `persistence/`, `agents/`, `orchestration/`, `api/`, `terminal/`, `cross_platform/`).
9. **Linear Dark Theme**: Keep surface `#08090a`, panel `#0f1011`, border `rgba(255,255,255,0.07)`, accent Linear Indigo `#5e6ad2`.
