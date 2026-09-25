# AI Team Workstation

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey.svg)]()

> **Autonomous Multi-Agent Workspace & Engineering Orchestrator**  
> An ultra-lightweight, browser-based AI workspace built around **Hermes Agent**. Orchestrates specialized AI roles (Architect, Coder, QA, Security, Researcher, Data Analyst, and more) with integrated filesystem execution, real-time collaboration, interactive terminal tooling, git visualization, and automated verification loops.

---

## Key Features

- **Resource-Efficient & Zero-Build**: Built on Python/FastAPI backend and clean modular vanilla JavaScript. Runs under 60MB RAM with no heavy Docker containers, Node.js runtime, or npm build steps required.
- **Hierarchical & Autonomous Multi-Agent Workflows**:
  - **Level 1**: Direct conversational interaction with the primary Hermes Agent via Server-Sent Events (SSE).
  - **Level 2**: Dynamic team collaboration with `@mention` routing and structured agent signals (`[SIGNAL:...]`).
  - **Level 3**: Multi-agent task pipelines featuring automated verification and iterative self-correction (*Auto-Fix QA Loops*).
- **Interactive Workspace Terminal**:
  - Sandboxed execution strictly confined within configured `ALLOWED_ROOTS`.
  - Process group lifecycle management with cross-platform termination (`taskkill` on Windows, `os.killpg` on Linux).
  - Configurable timeouts and output buffer truncation.
  - Automated credential and sensitive token redaction (ReDoS-safe regex engine).
  - Sanitized subprocess environments to prevent secret exposure.
  - **stdin injection** (`stdin` field) to answer shell prompts in one-shot runs.
  - **PTY mode** (`pty: true`, POSIX) so commands see a real TTY (`test -t 1`).
  - **Interactive WebSocket sessions** (`/api/workspace/terminal/ws`): PTY-backed
    streaming output; while a run is in flight, Enter in the terminal drawer is
    forwarded as stdin. Falls back to plain HTTP when the socket is not ready.
- **Read-Only Git Workspace Tooling**:
  - Real-time Git status (`/api/workspace/git/status`).
  - Unified diff inspection (`/api/workspace/git/diff`) directly inside the console drawer.
- **Hermes Core & 9Router Integration**:
  - Directly loads over 80+ Hermes Agent SOP skills (`~/.hermes/skills/`).
  - Connects seamlessly with local 9Router instances (`http://127.0.0.1:20128/v1`) or any OpenAI-compatible LLM endpoint.
  - Runtime model switching with session-scoped or global configuration options.
- **Durable SQLite Storage**:
  - Transactional persistence for workspaces, registered agents, tasks, execution stages, and chat histories (`ai_team.db`).
  - Automated restart reconciliation for interrupted or in-flight tasks.
  - **SQLite is the single source of truth**: the legacy `tasks.json`
    write-through mirror was removed (a one-time import migration remains) and
    `GET /api/tasks/export` serves explicit JSON dumps on demand.
  - **Per-workspace write locks** serialize concurrent file mutations
    (drawer CRUD, apply-files, pipeline auto-apply).
- **Cross-Platform**: Fully compatible with Linux (systemd/POSIX) and Windows environments.

---

## System Architecture

```text
┌────────────────────────────────────────────────────────────────────────┐
│                        Web Browser Frontend                            │
│  (Tailwind CSS CDN + Modular Vanilla JS: app.js, chat.js, Monaco/Prism)│
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP / SSE / REST
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        FastAPI Backend Engine                          │
│                                                                        │
│   ┌──────────────────────┐  ┌───────────────────┐  ┌────────────────┐  │
│   │  Router & Auth Gate  │  │ Terminal Executor │  │ Git Explorer   │  │
│   │  (Bearer / Localhost)│  │ (Process Groups)  │  │ (Diff / Status)│  │
│   └──────────┬───────────┘  └─────────┬─────────┘  └───────┬────────┘  │
│              │                        │                    │           │
│   ┌──────────▼────────────────────────▼────────────────────▼────────┐  │
│   │              Filesystem Boundary: ALLOWED_ROOTS                 │  │
│   │              (Path Sanitization & Symlink Resolution)           │  │
│   └───────────────────────────────────┬─────────────────────────────┘  │
│                                       │                                │
│   ┌──────────────────────┐  ┌─────────▼─────────┐  ┌────────────────┐  │
│   │ SQLite Store Layer   │  │   Hermes Bridge   │  │ 9Router / LLM  │  │
│   │ (ai_team.db)         │  │   (Skills & State)│  │ Client Gateway │  │
│   └──────────────────────┘  └───────────────────┘  └────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Configuration & Environment Variables

All settings can be configured via environment variables or a `.env` file in the project root:

| Variable | Description | Default |
|---|---|---|
| `AI_TEAM_AUTH_TOKEN` | Bearer authentication secret. If unset, access is restricted to loopback/localhost only. | *None* |
| `WORKSPACE_ROOT` | Primary directory path for workspace projects. | `~/projects` (Linux) / `%USERPROFILE%\projects` (Win) |
| `WORKSPACE_EXTRA_ROOTS` | Comma-separated list of secondary allowed directory trees. | `~/Documents` |
| `DEFAULT_WORKSPACE` | Active default directory on first initialization. | Value of `WORKSPACE_ROOT` |
| `HERMES_HOME` | Directory containing Hermes configuration, state, and skills. | `~/.hermes` |
| `NINEROUTER_DB` | Path to 9Router SQLite database for automatic API key resolution. | `~/.9router/db/data.sqlite` |
| `LLM_BASE_URL` | Base URL of the OpenAI-compatible LLM endpoint. | `http://127.0.0.1:20128/v1` |
| `LLM_MODEL` | Default model name for inference. | `bai` |
| `LLM_API_KEY` | API key for authentication with the LLM backend. | Auto-detected from 9Router or `sk-local-key` |
| `TERMINAL_MAX_OUTPUT`| Maximum character limit for terminal stdout/stderr before truncation. | `100000` |
| `HOST` | Server network bind address. | `127.0.0.1` |
| `PORT` | Server listening port. | `8090` |

---

## Getting Started

### Prerequisites
- Python 3.10 or higher
- Git
- Access to an LLM provider (Ollama, LM Studio, 9Router, OpenAI, etc.)

---

### Installation & Execution

#### Linux / macOS
```bash
# 1. Clone the repository
git clone https://github.com/andreadst/ai-team-dashboard.git
cd ai-team-dashboard

# 2. Automated setup (creates venv and installs dependencies)
chmod +x setup.sh
./setup.sh

# Or start manually:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

#### Windows
Run the setup batch file directly or via Command Prompt:
```cmd
cd path\to\ai-team-dashboard
setup.bat
```
Alternatively, using PowerShell:
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

The workstation will be available at **`http://localhost:8090`**.

---

### Running as a Service (Linux Systemd)

For continuous production deployments on Linux servers:
```bash
# Check service status
systemctl --user status ai-team.service

# Restart service
systemctl --user restart ai-team.service

# Stream service logs
journalctl --user -u ai-team.service -f
```

---

## Testing & Continuous Integration

The project includes an organized, isolated test suite under `tests/`:

```text
tests/
├── security/             # Boundary traversal, token validation, ReDoS & secret sanitization
├── persistence/          # SQLite schema, transactions, migrations, and session persistence
├── agents/               # Mention resolution, agent routing, and structured signal parsing
├── orchestration/        # ContextLog model, pipeline state management, and task recovery
├── api/                  # REST endpoints, static assets, and HTTP responses
├── terminal/             # Command execution, timeouts, process cleanup, and Git inspection
└── cross_platform/       # Path normalization (POSIX/Win) and process group teardown
```

### Backend Modules

| Module | Responsibility |
|---|---|
| `main.py` | App wiring, auth middleware, workspace/file/context/chat/pipeline routes |
| `routes_tools.py` | Terminal routes (HTTP + PTY + WebSocket) and read-only git inspection |
| `security_utils.py` | Path boundary (`sanitize_path`, `ALLOWED_ROOTS`), secret masking, env scrubbing, process-tree kill |
| `store.py` | SQLite persistence (schema, tasks, sessions, messages, events) |

### Running Tests

Execute the full suite using Python's built-in `unittest` runner:
```bash
# Run all tests
python3 -m unittest discover -s tests -p "test_*.py"

# Run specific domain test suites
python3 -m unittest discover -s tests/security -p "test_*.py"
python3 -m unittest discover -s tests/terminal -p "test_*.py"
python3 -m unittest discover -s tests/cross_platform -p "test_*.py"
```

---

## License

This project is licensed under the MIT License.
