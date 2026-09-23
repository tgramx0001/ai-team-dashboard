# AI Team Workstation (AI Team Hub)

## Overview & Architecture
- **Type**: Autonomous Multi-Agent Workstation & Project Orchestrator
- **Backend**: FastAPI (`main.py`) with zero-overhead async architecture.
- **Frontend**: Single-page modern engineering UI (`index.html`) inspired by Linear & Claude Design System (Inter, JetBrains Mono, dark aesthetic).
- **Service**: Systemd user unit `ai-team.service` on port `8090`. Memory cap: 300MB.
- **LLM Gateway**: 9Router local endpoint (`http://127.0.0.1:20128/v1/chat/completions`) model `bai` (deepseek-v4.1-flash).

## Conventions & Rules
1. **Lightweight & High Efficiency**: Do NOT introduce heavy frameworks (Docker, npm/node build steps, heavy ORM). Keep backend and frontend clean and fast.
2. **Path Sanitization**: All file operations MUST sanitize paths within the designated workspace root to prevent directory traversal.
3. **Multi-File Code Extraction**: Agents generate code using standardized `### FILE: path/to/file.ext` blocks.
4. **Safety Backups**: Overwriting existing files during multi-file direct apply MUST create `.bak` copies.
5. **Linear Dark Theme**: Keep surface `#08090a`, panel `#0f1011`, border `rgba(255,255,255,0.07)`, accent Linear Indigo `#5e6ad2`.
