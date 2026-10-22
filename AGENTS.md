# AGENTS.md

## What this is

Single-file Python script (`pushover_monitor.py`) that monitors running OpenCode sessions and sends Pushover push notifications when sessions become idle, waiting for input, errored, or stalled.

## Setup & run

- `start_monitor.sh` auto-creates a venv in `./venv/`, installs `requests`, and launches the monitor in the background.
- Only runtime dependency: `requests` (also in `requirements.txt`).
- Requires OpenCode **v2** (`opencode api` must exist; v1's `opencode db` was removed).
- Pushover credentials are resolved at runtime by `resolve_credential()` in this order: `--app-token`/`--user-key` flag, then `PUSHOVER_APP_TOKEN`/`PUSHOVER_USER_KEY` env, then the macOS Keychain (service `oc-pushover-monitor`, accounts `app-token`/`user-key`). `start_monitor.sh` never passes secrets on argv. It only checks that they exist.

```bash
# One-time: store credentials in the Keychain
./start_monitor.sh --setup-keychain

# Foreground:
python3 pushover_monitor.py

# Background via launcher:
./start_monitor.sh
```

## Architecture

- No package structure, no `pyproject.toml`. The entire app is `pushover_monitor.py`.
- Data access goes through the OpenCode v2 HTTP API: `subprocess.run(["opencode", "api", "get", <path>])` (see `_api_get`). `opencode api` handles service discovery and auth (the raw HTTP port returns 401). The monitor never reads the SQLite DB. Query params **must be embedded in the path** (`?limit=5&order=desc`), because `--param` only fills OpenAPI path params and silently ignores query params. The `directory` filter on `/api/session` is ignored by the server, so filtering happens client-side.
- Session discovery: detects running `opencode` client processes (`ps` + `lsof -d cwd`; `_is_opencode_client` excludes `opencode serve --service` and other subcommands). It then lists recent top-level sessions (`/api/session?parentID=null&limit=50`) and keeps the most recent one per working directory (`location.directory`). Falls back to `--max-age-hours` recency if process detection fails.
- On startup, existing session states are seeded silently -- notifications only fire on **subsequent** state changes.
- **Notification timing** uses duration-based thresholds, not simple state transitions. WAITING notifications only fire after the session has been continuously waiting for `--wait-minutes` (default 2). IDLE notifications fire after `--idle-minutes` (default 10). This prevents noisy alerts during active conversations and avoids the "stuck on WAITING" bug where a missed ACTIVE→WAITING transition silently suppresses all future notifications. Tracking state lives in `TrackedSession` dataclass instances keyed by session ID.
- **Latest turn message**: v2 messages are typed (`user`, `assistant`, `idle`, `system`, ...). `_latest_turn_message` takes the newest message whose type is in `SessionInfo.TURN_MESSAGE_TYPES`, skipping bookkeeping messages. An `idle` message marks the end of a turn: `outcome = "succeeded"` maps to `finish = "stop"`, and its `error.type` is used for error detection. `_session_from_api` does the mapping, which lets `_check_idle_status` stay API-agnostic.
- **Waiting detection**: a `question` tool in the latest assistant message's `content` with `state.status` `streaming`/`running`, or a pending form (`/api/form`) or permission request (`/api/permission/request`) for the session. Both of those endpoints are location-scoped (`?location[directory]=...`). Additionally, if the turn finished (`finish = "stop"`), fetches the last `text` block and checks whether the last paragraph (after stripping fenced code blocks) contains a `?` -- this detects prose questions that don't use the `question` tool. Fenced code blocks are stripped to avoid false positives from `?` in SQL queries, URLs, etc.
- **Error detection**: if the latest assistant or idle message has `error.type` in `SessionInfo.NOTIFIABLE_ERRORS` (`provider.error`, `provider.content-filter`, `unknown`, `output-limit`), the session is in an error state. User-initiated aborts (`aborted`) are excluded. The set of notifiable errors is defined in `SessionInfo.NOTIFIABLE_ERRORS`.
- **Idle detection** triggers when the assistant finishes (`finish = "stop"`) without asking a prose question. This is a structural signal, not time-based -- the notification delay is controlled by `--idle-minutes`.
- **Stalled detection** triggers when the assistant was working (`finish = "tool-calls"` or in-flight `finish = null`) and the turn has no `idle` marker, but `time.updated` hasn't advanced for `--stall-minutes` (default 10). This catches rare cases where the assistant stopped making progress mid-task.
- Large API responses can be truncated when piped in a shell. Keep page sizes small (`MESSAGE_PAGE_SIZE`, `limit=1&type=assistant`); `_api_get` returns `None` on invalid JSON.

## Testing

```bash
# Offline tests (state detection, idle logic) plus live discovery if opencode is running, and Pushover delivery if credentials are in env/Keychain:
./venv/bin/python3 test_monitor.py
```

- Tests use a hand-rolled runner in `test_monitor.py` (not `unittest` or `pytest`). Exit code is 0 on success, 1 on any failure.
- Process detection and session discovery tests require at least one running `opencode` session. State detection, prose question detection, and notification timing tests run fully offline.
- V2 API mapping, process filtering, and credential resolution tests run offline (Keychain is monkeypatched).
- Pushover delivery tests run when credentials resolve (env or Keychain) and are skipped otherwise.

## Style

- `ruff` is available and has been used for formatting/linting (`.ruff_cache/` exists). No ruff config file -- defaults apply. Run `ruff check` / `ruff format` before committing.
- No CI. No pre-commit hooks.
