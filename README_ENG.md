# CodeForge

> A lightweight **AI Agent IDE** running on **Termux / Linux / macOS / Windows**.
> Built on Flask + OpenAI-compatible protocol, with a built-in **plan / act / answer** three-stage workflow
> that puts the proposal on the table for your approval before the model touches your code.

[中文文档](README.md)

---

## Core Highlights

| Highlight | What it does |
| --- | --- |
| **Plan-before-Act Workflow** | Before touching any file, the model must call the `plan` tool with a structured proposal (intent / direction / basis / affected files / steps / risk level). The loop stops here for user review. Not a prompt "suggestion" — a hard constraint enforced at the loop level |
| **Centralized Approval Gate** | All 7 tools' approval policies are enforced centrally in `dispatch()`. New tools inherit the gate automatically — no "forgot to add an approval check" loophole. `run_command` ignores the `auto` switch; every call requires per-item confirmation |
| **One-time Scoped Approval** | Confirming "delete a.txt" does not also allow "delete b.txt" — the approval record matches action + key parameters (`file_path` / `command`+`cwd`), is consumed once, and never opens a global switch |
| **SHA256 Overwrite Protection** | `edit_file` patches stay in memory only. On "Apply", the disk content's SHA256 is compared against the baseline; mismatch returns `409` — your manual editor saves won't be silently overwritten by the agent's patch |
| **Self-Modification Block** | `prompt.json` / `model.json` / `.config.json` / `sandbox.py` are write-protected from the agent even inside the workspace. Blocks the "agent rewrites its own system prompt, permanently affecting all future sessions" attack vector |
| **DNS Rebinding Defense** | Three-layer auth: Host allowlist (loopback names / IP literals / explicit config only) → Origin same-origin → `X-CodeForge-Token` (constant-time comparison). An attacker's webpage can rebind their domain to 127.0.0.1 to bypass browser same-origin, but the Host header exposes the domain and is rejected here |
| **Three-Layer Key Resolution + Leak Prevention** | API key priority: env var → untracked `model.local.json` → tracked `model.json`. The system never writes back to `model.json`. API responses are serialized and scanned against all in-process keys (full string + 12-char sliding window); matches are replaced with `500 key_leak` instead of leaking the key |
| **Per-Tab Session Isolation** | `X-CodeForge-Session` isolates history / approvals / diff pools per sid. Two tabs with different sids don't interfere. RLock-protected; single-user defaults to the `default` session |
| **Hot Reload Without Restart** | Model list, system prompt, and global switches all support file-watcher hot reload with atomic writes (`os.replace`) and self-trigger suppression. No restart needed after changes |
| **SSE Streaming + Retry Backoff** | Push as it thinks; frontend shows "Round N / calling tool X / tool returned" in real time. Model request failures retry with exponential backoff; deterministic errors (permission/path) are not retried |

---

## Directory Structure

```
codeforge/
├── main.py                 # Flask entry + all HTTP API (46 routes)
├── auth.py                 # Host / Origin / token three layers
├── sandbox.py              # Path sandbox & governance file protection
├── ssh.py                  # SSH session manager (asyncssh)
├── start.sh                # Linux / macOS / Termux one-click launch
├── start.ps1               # Windows PowerShell one-click launch
├── requirements.txt        # Dependency list (with upper bounds)
├── pyproject.toml          # Project metadata + pytest config
├── .config.json            # Global switches (flow / max_round / workspace_roots)
├── AGENTS.md               # AI Agent security/code/reply guidelines
├── agent/                  # Agent core
│   ├── loop.py             #   Main loop (run / run_stream)
│   ├── session.py          #   Per-sid isolated session state (history / approvals / diffs)
│   ├── state.py            #   Compatibility layer, delegates to session
│   ├── tool.py             #   Tool registration, approval gate, dispatch
│   ├── diff.py             #   Baseline snapshot, pending patch, apply / revert
│   └── prompt.json         #   System prompt + tool rules
├── explorer/               # Local file system
│   ├── file.py             #   CRUD + encoding/line-ending detection + atomic write
│   └── search.py           #   Global substring search
├── git/engine.py           # Git subprocess wrapper
├── models/                 # Model management
│   ├── model.json          #   Model list + currently active + apiKey
│   └── request.py          #   OpenAI-compatible protocol call + hot reload
├── terminal/engine.py      # Local shell executor
├── static/                 # Web frontend (index.html / app.js / style.css)
├── tests/                  # pytest suite
└── log/                    # Runtime logs (auto-generated)
```

---

## Quick Start

### Requirements
- **Python 3.10+** (developed and tested on 3.11)
- Internet access, or a self-hosted OpenAI-compatible API

### Linux / macOS / Termux

```bash
git clone https://github.com/aponia-coola/codeforge.git
cd codeforge
./start.sh                       # Default 127.0.0.1:9191, auto-opens browser
./start.sh --port 8080           # Custom port
./start.sh --host 0.0.0.0        # Listen on all interfaces (prints risk warning)
./start.sh --no-browser          # Don't auto-open browser
./start.sh --rebuild             # Force rebuild .venv
./start.sh --update              # Force reinstall dependencies
./start.sh --dev                 # Dev mode (enables Flask debug)
```

### Windows

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned    # First time only

.\start.ps1
.\start.ps1 -Port 8080
.\start.ps1 -ListenHost 0.0.0.0
.\start.ps1 -Dev
.\start.ps1 -Console             # Output stays in console, no log/ files
```

Both scripts do the same thing:

1. Detect/create `.venv`
2. Compare `requirements.txt` sha256 with `.venv/.deps_installed`; reinstall only if mismatched
3. Detect port conflicts; ask whether to continue if occupied
4. Generate `CODEFORGE_TOKEN` and pass it to `main.py`
5. Print the access URL with token, launch browser in background, keep Flask running in foreground
6. Redirect stdout / stderr to `log/server.log` and `log/server.err.log` respectively

After launch, the terminal prints something like this — **use this URL directly so you don't have to type the token manually**:

```
════════════════════════════════════════════════════════════════════
  CodeForge
  URL   : http://127.0.0.1:9191/#token=Xk3n…
  token : Xk3n…
  source: randomly generated on this launch
  header: X-CodeForge-Token: <token>
          X-CodeForge-Session: <sid> (optional, defaults to "default" session)
════════════════════════════════════════════════════════════════════
```

The bind address is not the access address: `--host 0.0.0.0` is a wildcard bind; the browser should still use the loopback address. The banner shows the clickable URL and prints an additional LAN exposure risk warning.

---

## Authentication

All `/api/*` endpoints require authentication. Only `GET /`, `GET /favicon.ico`, and `/static/*` are exempt.

### Where the token comes from

- If the environment variable `CODEFORGE_TOKEN` is set, it is used;
- Otherwise, a random one is generated on each launch (`secrets.token_urlsafe(32)`), printed in the startup banner.
- When using the launch script, the token is generated by the script and exported to `main.py`, so the URL printed by the script and the one in the backend banner are always the same.

### How to pass it

| Method | Usage |
| --- | --- |
| Browser | Open `http://<host>:<port>/#token=<token>`; the frontend stores it in `sessionStorage` and clears the hash; if no token is present, a prompt asks you to enter it manually |
| CLI | Add the `X-CodeForge-Token: <token>` header to every request |

```bash
curl -H "X-CodeForge-Token: $CODEFORGE_TOKEN" http://127.0.0.1:9191/api/config
```

Missing or incorrect tokens always return `401 {"ok":false,"error":"unauthorized"}` (constant-time comparison, no prefix leakage).

### Session header

`X-CodeForge-Session: <sid>` is optional; defaults to the `default` session. History, `plan_model`, `auto`, pending, and diff pools are all scoped to the session, so two tabs with different sids won't interfere with each other. The sid only accepts `[A-Za-z0-9-_]` and up to 64 characters; invalid values fall back to default.

### The other two layers

| Layer | Rule | On rejection |
| --- | --- | --- |
| Host allowlist | Only accepts loopback names (`localhost`, etc.), IP literals, and names explicitly listed in `CODEFORGE_ALLOWED_HOSTS`; port must match the server port | `403 forbidden_host` |
| Origin same-origin | No Origin is treated as same-origin; if Origin is present, it must match Host exactly | `403 forbidden_origin` |

The Host allowlist defends against **DNS rebinding**: an attacker's webpage can rebind their domain to `127.0.0.1` to bypass the browser's same-origin policy, but the Host header carries that domain and is rejected at this layer. IP literals cannot be rebound, so they are allowed; LAN access via IP is unaffected.

---

## Configuration

### `.config.json` — Global switches

**This is strict JSON; comments are not allowed.** If you write comments, parsing fails, and the config reader swallows the exception into `{}`, silently reverting all switches to defaults — you won't see any error, just find that your settings didn't take effect.

```json
{
    "flow": true,
    "max_round": 20,
    "workspace_roots": ["~/projects", "~/work"]
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `flow` | bool | `false` | Whether to use SSE streaming by default. Strings like `"true"` / `"1"` / `"yes"` / `"on"` are also accepted |
| `max_round` | int | `20` | Agent round limit per task; valid range 1–200; out-of-bounds reverts to 20 |
| `workspace_roots` | list[str] | see below | Root directories the sandbox allows read/write; supports `~` |

No restart needed after changes: config has a 2-second cache; after expiry, it checks `(mtime, size)` to decide whether to actually re-read.

`workspace_roots` defaults to the user's home directory (`/sdcard` is appended if it exists, i.e., on mobile), plus a fallback — if the install directory is not under the above roots, it is automatically added. All paths must fall within these roots, otherwise `403 {"code":"sandbox"}`.

The following four files are **forbidden from agent writes** even if inside a root (manual edits in the editor are unrestricted): `agent/prompt.json`, `models/model.json`, `.config.json`, `sandbox.py`. This blocks the self-modification loop where "the agent rewrites its own system prompt, permanently affecting all future sessions."

### `models/model.json` — Model list

```json
{
    "current_model": "MiniMax-M3",
    "models": [
        {
            "id": "MiniMax-M3",
            "name": "MiniMax-M3",
            "vendor": "MiniMax",
            "apiKey": "sk-...",
            "url": "https://api.minimaxi.com/v1",
            "maxInputTokens": 1000000,
            "maxOutputTokens": 8192,
            "supportsToolCall": true
        }
    ],
    "availableModels": ["MiniMax-M3"]
}
```

- `current_model`: the model id activated by default on launch
- `availableModels`: whitelist for the frontend dropdown; omit to show all
- `url` can be written as `.../chat/completions`; it will be trimmed to `base_url` internally
- The file is watched by watchdog; changes are hot-reloaded without restart

### `models/model.local.json` — Local override layer (untracked)

Identical structure to `model.json` but **not tracked by git** (already in `.gitignore`); this is the recommended place for keys. See `models/model.local.example.json` for a sample.

```json
{
    "models": [
        { "id": "MiniMax-M3", "apiKeyEnv": "MINIMAX_API_KEY" },
        { "id": "my-local", "url": "http://127.0.0.1:8000/v1", "apiKey": "sk-..." }
    ]
}
```

- Overrides `model.json` **field by field** keyed by `id`: a local entry with only `{"id", "apiKey"}` inherits `url` etc.
- New ids are appended directly; top-level `current_model` / `availableModels` are overridden if written locally
- If the file is missing, empty, or corrupted, everything works as if only `model.json` exists

**Key resolution priority (high to low):**

| Order | Source | `keySource` |
| --- | --- | --- |
| 1 | Environment variable specified by entry's `apiKeyEnv` | `env` |
| 2 | `apiKey` in the same-id entry in `model.local.json` | `local` |
| 3 | `apiKey` in `model.json` (backward compatible) | `tracked` |
| — | None of the three; request fails with 401 instead of startup failure | `none` |

Switching models, adding/modifying/deleting models only writes to `model.local.json` (temp file + `os.replace` atomic write); `model.json` remains read-only throughout, so plaintext keys are never rewritten into a tracked file due to a UI operation.

### Environment Variables

| Variable | Purpose |
| --- | --- |
| `CODEFORGE_TOKEN` | Fixed auth token; randomly generated on each launch if not set |
| `CODEFORGE_ALLOWED_HOSTS` | Additional allowed Hosts, comma-separated |
| `CODEFORGE_HOST` / `CODEFORGE_PORT` | Defaults for `--host` / `--port` |
| `CODEFORGE_DEBUG` | `1` / `true` enables Flask debug, equivalent to `--debug` |
| `CODEFORGE_MODEL_TIMEOUT` | Model request timeout in seconds, default 120 |
| `CODEFORGE_MODEL_RETRIES` | Model request retry count, default 2 |
| `CODEFORGE_SSH_KNOWN_HOSTS` | known_hosts path, default `~/.ssh/known_hosts` |
| Any var name specified by entry's `apiKeyEnv` | API key for that model; takes priority over `apiKey` in config files |

---

## Approval Semantics

Of the agent's 7 tools, `plan` / `list_dir` / `read_file` are read-only and execute directly; the other 4 mutate external state and must pass the approval gate. The gate is enforced centrally in `dispatch()`, not copied into each tool function body — the "forgot to copy when adding a new tool" path no longer exists.

| Tool | Risk | Approval requirement |
| --- | --- | --- |
| `create_file` | medium | `auto=true` or a one-time approval for this call |
| `edit_file` | medium | Same as above |
| `remove_file` | high | Same as above |
| `run_command` | high | **Ignores `auto`; every call requires per-item confirmation** |

### Three states

- **Per-step confirmation (default, `auto=false`)**: The tool call is intercepted, a pending entry is written, and `{"status":"pending_approval", ...}` is returned; the loop stops and waits. The frontend shows a confirmation card; clicking confirm calls `POST /api/agent/pending/confirm`.
- **One-time approval**: Confirming **does not** turn on the global switch. It only writes an approved record remembering "which action + which key parameters"; the matching call consumes it and it becomes invalid. Confirming "delete a.txt" does not also allow "delete b.txt"; confirming command A is not used to execute command B (`run_command` compares `command` and `cwd`; file tools compare `file_path`).
- **Full auto (`auto=true`)**: Enabled via `POST /api/agent/state {"auto":true}`; mutating tools other than `run_command` are no longer asked per-item. **Use only when you fully understand the consequences.**

`plan_model` defaults to `true`: before touching any file, the model must call `plan` to submit a structured proposal (intent / direction / basis / affected_files / steps / risk); the loop stops here for your review.

### How changes are written to disk

| Tool | After approval |
| --- | --- |
| `create_file` | **Writes to disk immediately**, stores a baseline (empty means the file didn't exist before) |
| `remove_file` | **Deletes immediately**, stores the original content as baseline before deletion |
| `edit_file` | **Does not write to disk**; the patch stays in the session's in-memory diff pool; the frontend renders a unified diff |

For pending patches produced by `edit_file`, there are three exits:

- `POST /api/diff/apply` — Writes to disk. **Before writing, compares the sha256 of disk content with the baseline from when the patch was generated; returns `409` if mismatched** — meaning your manual saves in the editor won't be silently overwritten by the agent's patch.
- `POST /api/diff/discard` — Discards the patch; the disk was never touched.
- `POST /api/diff/revert` — Restores the file to baseline: agent-created files are deleted, deleted files are written back. This is for operations like `create_file` / `remove_file` that have already hit disk.

---

## HTTP API

46 routes. All `/api/*` require `X-CodeForge-Token`; `X-CodeForge-Session` is optional. Request bodies are always JSON, max 8 MB. **Parameters marked "required" cause immediate failure per the "response code" column if missing; no default fallback.**

### Pages

| Method | Path | Params | Description |
| --- | --- | --- | --- |
| GET | `/` | — | SPA entry, **the only page that doesn't require a token** |

### Models

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/models` | — | 200 `{current, models, base_url}` |
| POST | `/api/model` | `{model}` **required** | 200 / 400 missing model / 404 unknown model |

### Model Management

Writes to `models/model.local.json`; `model.json` is read-only. **The responses of these four endpoints never contain plaintext keys**: after serialization, each key in the process is compared (full string + 12-char sliding window); if matched, it is replaced with `500 key_leak` instead of sending the key out.

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/models/admin` | — | 200 `{ok, models: [...]}` / 500 `key_leak` |
| POST | `/api/models/upsert` | See table below, `id` **required** | 200 `{ok, id, models}` / 400 `invalid_spec`, `upsert_failed` |
| POST | `/api/models/delete` | `{id}` **required**, only entries with `origin=="local"` can be deleted | 200 `{ok, id, models}` / 400 `invalid_spec`, `delete_failed` |
| POST | `/api/models/test` | `{id}` **required** | 200 `{ok, id, latency_ms}` / 400 `invalid_spec` / 502 `test_failed` / 504 `timeout` (15s) |

Fields for each item in `/api/models/admin`:

| Field | Type | Description |
| --- | --- | --- |
| `id` / `name` / `vendor` / `url` | str | Entry basic info |
| `maxInputTokens` / `maxOutputTokens` | int \| null | `null` when not configured |
| `supportsToolCall` | bool | Whether tool calling is supported |
| `hasKey` | bool | Whether a key was obtained after three-layer resolution |
| `keySource` | `env` \| `local` \| `tracked` \| `none` | Which layer the key came from |
| `origin` | `local` \| `tracked` | Which layer provides the entry |
| `editable` | bool | Equivalent to `origin == "local"`; only these can be deleted |

Body fields and validation for `/api/models/upsert`:

| Field | Required | Validation |
| --- | --- | --- |
| `id` | Yes | Non-empty, ≤128 chars, no path separators, doesn't start with `.`, no control characters |
| `name` / `vendor` | No | String, ≤128 chars |
| `url` | No | String, ≤512 chars, must be `http://` / `https://` with a hostname; if the tracked layer also has no url when adding a new entry, an error is returned |
| `apiKey` | No | String ≤4096 chars. **Empty string = keep current value**, **`null` = clear local key**, omitted = no change |
| `apiKeyEnv` | No | Environment variable name, ≤128 chars, must match `[A-Za-z_][A-Za-z0-9_]*` |
| `maxInputTokens` | No | Integer, 1..20000000 |
| `maxOutputTokens` | No | Integer, 1..1000000 |
| `supportsToolCall` | No | bool (strings like `"true"` / `"1"` / `"on"` are also accepted) |

### Config

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/config` | — | 200 `{flow, max_round, path}` |
| POST | `/api/config` | `{flow?: bool, max_round?: int}`, at least one required | 200 / 400 no fields to change, max_round not an integer or out of bounds |

### Directory & Search

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/folder` | `?path=` (optional, defaults to server-detected start dir) | 200 / 400 not a directory / 403 sandbox / 404 not found |
| GET | `/api/search` | `?q=` (optional, empty returns empty results) `&path=` (optional) | 200 / 404 root not found |
| POST | `/api/folder/create` | `{path, name}` **both required** | 200 / 400 / 409 already exists |

> The start directory only checks whether `/sdcard` actually exists on the server filesystem, not the User-Agent.

### Files

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/file/read` | `?path=` **required**, absolute path | 200 / 400 missing path / 404 / 413 over 2 MB / 415 not text |
| POST | `/api/file/create` | `{path, name}` **required**, `{content?}` defaults to `""` | 200 / 400 / 409 already exists |
| POST | `/api/file/save` | `{path, content}` **required**, `{encoding?='utf-8', newline?='\n'}` | 200 / 400 unknown encoding or invalid newline / 404 |
| POST | `/api/file/rename` | `{path, new_name}` **both required** | 200 / 400 / 404 / 409 target exists |
| POST | `/api/file/duplicate` | `{path}` **required** | 200 / 400 |
| POST | `/api/file/move` | `{path, to_dir}` **required**, `{new_name?}` defaults to original name | 200 / 400 / 404 / 409 |
| POST | `/api/file/delete` | `{path}` **required**, files only | 200 / 400 / 404 |

> File reading detects encoding in order: `utf-8 -> utf-8-sig -> locale -> latin-1`; line-ending detection result is in the `newline` field; `content` is normalized to LF for the editor. On save, pass back the `encoding` / `newline` returned by `read` as-is, so GBK `.bat` files and CRLF files are not rewritten.

### Chat

| Method | Path | Params | Description |
| --- | --- | --- | --- |
| GET | `/api/chat` | — | Current session history + state |
| POST | `/api/chat` | `{message?, history?, max_rounds?, plan_model?, cwd?}` all optional | Blocking run of one loop iteration |
| POST | `/api/chat/stream` | Same as above, plus `{flow?}` | **SSE**, events `round / tool_call / cancelled / error / done` |
| POST | `/api/chat/clear` | — | Clears history + pending + one-time approvals |
| POST | `/api/chat/stop` | — | Sets cancel flag; loop stops at the next event boundary |

> If `history` is passed, the frontend takes precedence; if not, the session's stored history is used. If `max_rounds` is not passed, `max_round` from `.config.json` is used. `stop` cannot interrupt a tool call **currently in progress**; it will finish first. The return value's `stopped` is one of `answer` / `pending` / `max_rounds` / `error` / `cancelled`.

### Agent State & Approval

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/agent/state` | — | 200 `{plan_model, auto, pending, sid}` |
| POST | `/api/agent/state` | `{plan_model?: bool, auto?: bool}` | 200 |
| POST | `/api/agent/pending/confirm` | `{resume?=true, history?, cwd?, max_rounds?, plan_model?}` | 200 / **400 no pending operation** |
| POST | `/api/agent/pending/reject` | — | 200 |

> `confirm` defaults to resuming the loop; `resume:false` means approve only without resuming, letting the frontend call `/api/chat/stream` again.

### Diff

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/diffs` | — | 200 list of pending files |
| GET | `/api/diff` | `?path=` **required** | 200 / 400 missing path / 404 no diff |
| GET | `/api/diff/baseline` | `?path=` **required** | 200 / 400 missing path / 404 no baseline |
| POST | `/api/diff/clear` | `{path?}` omitted means clear all | 200 |
| POST | `/api/diff/revert` | `{path}` **required** | 200 / 400 |
| POST | `/api/diff/apply` | `{path}` **required** | 200 / 400 / **409 file modified externally** |
| POST | `/api/diff/discard` | `{path}` **required** | 200 / 400 |

### Git

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| GET | `/api/git/status` | `?path=` (optional, defaults to start dir) | 200 (returns `is_git:false` if not a repo) / 400 path not found |
| GET | `/api/git/diff` | `?path=` **required** `&cwd=` **required** `&staged=1` (optional) | 200 / **400 missing path/cwd** |
| POST | `/api/git/stage` | `{cwd, path}` **both required**, `path:"-A"` means `git add -A` | 200 / 400 |
| POST | `/api/git/unstage` | `{cwd}` **required**; `{path}` omitted or `"-A"` means unstage all | 200 / 400 missing cwd |
| POST | `/api/git/discard` | `{cwd, path}` **both required** | 200 / 400 |
| POST | `/api/git/commit` | `{cwd, message}` **both required** | 200 / 400 |
| POST | `/api/git/push` | `{cwd}` **required**, `{remote?, branch?}` defaults to upstream | 200 / 400 missing cwd |

> `cwd` is the repo path; `path` is the file path within the repo — missing either one returns 400, no fallback to current directory.

### SSH

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| POST | `/api/ssh/connect` | `{host, user}` **required**, `{port?=22}`, `password` and `key_path` **at least one required** | 200 / 400 / **409 host key pending confirmation** / 500 |
| POST | `/api/ssh/host_key/confirm` | `{token}` **required**, from the 409 response | 200 / 400 |
| POST | `/api/ssh/host_key/cancel` | `{token}` **required** | 200 |
| GET | `/api/ssh/sessions` | — | 200 |
| POST | `/api/ssh/disconnect` | `{sid}` **required** | 200 / 400 / 500 |
| GET | `/api/ssh/list` | `?sid=` **required**, `&path=` defaults to `"."` | 200 / **404 session not found** |
| POST | `/api/ssh/exec` | `{sid, command}` **required**, `{cwd?, timeout?=30}` | 200 / 400 / 404 |

> On first connection to a host, a `409` + fingerprint is returned; after verification, call `host_key/confirm` to write it to known_hosts (TOFU). **You must then re-issue a `connect`** — the ticket does not cache the password. If the fingerprint doesn't match an existing record, no token is issued; known_hosts must be resolved manually.

### Terminal

| Method | Path | Params | Response codes |
| --- | --- | --- | --- |
| POST | `/api/terminal/run` | `{command}` **required**, `{cwd?, timeout?=30, ssh_sid?}` | 200 / 400 missing command / 404 ssh session not found |

> If `ssh_sid` is passed, execution goes to the remote; in this case `cwd` belongs to the remote filesystem and bypasses the local sandbox. For local execution, `cwd` passes through `sandbox.resolve()` first. On timeout, the process group is terminated; grandchild processes are killed as well.

### Error format

All errors under `/api/*` are normalized to JSON:

```json
{"ok": false, "error": "...", "code": "sandbox | unauthorized | forbidden_host | internal | ..."}
```

---

## Development & Testing

```bash
.venv/Scripts/python.exe -m pytest          # Windows
.venv/bin/python -m pytest                  # Linux / macOS
```

`tests/` is a pytest suite covering auth / sandbox / diff / tools / loop / explorer / terminal / http / session. Three hard constraints:

- No real network or LLM calls (`no_network` is an autouse fixture; stray calls raise immediately)
- No writes to the repo (`tmp_workspace` swaps the sandbox root to a temp directory)
- Global state reset between cases (session table, diff pool, sandbox root)

Test cases requiring a real model are in `tests/test_live_agent.py`, marked with `live`, excluded by default via `-m "not live"` in `pyproject.toml`. **They make real paid calls**; to run them, specify explicitly:

```bash
.venv/Scripts/python.exe -m pytest -m live
```

When debugging the server, add `--debug` to enable the Flask debugger:

```bash
python main.py --port 9191 --debug
```

**Debug is off by default.** `--debug` (or `CODEFORGE_DEBUG=1`, `start.sh --dev`, `start.ps1 -Dev`) turns it on. Even with debug on, the reloader is always disabled (`use_reloader=False`) — otherwise the agent editing project files would trigger a restart, clearing session history, diffs, and pending, and breaking the SSE stream.

---

## Logging

The launch scripts redirect stdout / stderr to `log/server.log` and `log/server.err.log` respectively. On first launch, legacy logs in the root directory are migrated into `log/`.

There is one platform difference: `start.sh` appends (`>>`); Windows redirection cannot append, so `start.ps1` first rotates the previous log to `log/server.prev.log` / `log/server.err.prev.log` before writing new ones. Use `.\start.ps1 -Console` to skip file logging.

---

## Roadmap

- [ ] Vendor CDN dependencies locally
- [ ] `read_file` chunked reading + history compression to control token growth in long sessions
- [ ] Pause explorer polling when the page is hidden
- [ ] Multi-Agent collaboration / Sub-Agent
- [ ] Persistent sessions (currently history is in-memory only)

---

## License

[GNU General Public License v3.0](LICENSE).
