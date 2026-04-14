# AGENTS.md

## What this is

Single-file Python script (`pushover_monitor.py`) that monitors running OpenCode sessions and sends Pushover push notifications when sessions become idle, waiting for input, errored, or stalled.

## Setup & run

- `start_monitor.sh` auto-creates a venv in `./venv/`, installs `requests`, and launches the monitor in the background.
- Only runtime dependency: `requests` (also in `requirements.txt`).
- Pushover credentials (`PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`) must be exported before running. `start_monitor.sh` rejects the built-in placeholder values.

```bash
# Foreground:
python3 pushover_monitor.py --app-token TOKEN --user-key KEY

# Background via launcher:
export PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..."
./start_monitor.sh
```

## Architecture

- No package structure, no `pyproject.toml`. The entire app is `pushover_monitor.py`.
- DB access is via `subprocess.run(["opencode", "db", <raw SQL>, "--format", "json"])` -- not a direct SQLite connection. The `opencode` CLI must be on `$PATH`.
- Session discovery: detects running `opencode` processes (`ps` + `lsof -d cwd`), then queries `~/.local/share/opencode/opencode.db` for the most recent session per working directory. Falls back to a recency-based query (`--max-age-hours`) if process detection fails.
- On startup, existing session states are seeded silently -- notifications only fire on **subsequent** state changes.
- **Notification timing** uses duration-based thresholds, not simple state transitions. WAITING notifications only fire after the session has been continuously waiting for `--wait-minutes` (default 2). IDLE notifications fire after `--idle-minutes` (default 10). This prevents noisy alerts during active conversations and avoids the "stuck on WAITING" bug where a missed ACTIVE→WAITING transition silently suppresses all future notifications. Tracking state lives in `TrackedSession` dataclass instances keyed by session ID.
- **Waiting detection** checks the `part` table for a `question` tool with `status` of `pending` or `running` (assistant is blocking on a question tool call). Additionally, if the latest message has `finish = "stop"`, fetches the last `text` part and checks whether the last paragraph (after stripping fenced code blocks) contains a `?` -- this detects prose questions that don't use the `question` tool. Fenced code blocks are stripped to avoid false positives from `?` in SQL queries, URLs, etc.
- **Error detection** uses the `message` table: if the latest assistant message has `json_extract(data, '$.error.name')` set to a notifiable error type (`APIError`, `UnknownError`), the session is in an error state. User-initiated aborts (`MessageAbortedError`) are excluded. The set of notifiable errors is defined in `SessionInfo.NOTIFIABLE_ERRORS`.
- **Idle detection** triggers when the assistant finishes (`finish = "stop"`) without asking a prose question. This is a structural signal, not time-based -- the notification delay is controlled by `--idle-minutes`.
- **Stalled detection** triggers when the assistant was working (`finish = "tool-calls"` or in-flight `finish = null`) but `time_updated` hasn't advanced for `--stall-minutes` (default 10). This catches rare cases where the assistant stopped making progress mid-task.
- SQL queries use f-string interpolation of directory paths (not parameterized). Be aware of this if modifying query construction.

## Testing

```bash
# Offline tests (state detection, idle logic):
./venv/bin/python3 test_monitor.py

# Full tests including Pushover delivery:
PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..." ./venv/bin/python3 test_monitor.py
```

- Tests use a hand-rolled runner in `test_monitor.py` (not `unittest` or `pytest`). Exit code is 0 on success, 1 on any failure.
- Process detection and session discovery tests require at least one running `opencode` session. State detection, prose question detection, and notification timing tests run fully offline.
- Pushover delivery tests are skipped when env vars are unset.

## Style

- `ruff` is available and has been used for formatting/linting (`.ruff_cache/` exists). No ruff config file -- defaults apply. Run `ruff check` / `ruff format` before committing.
- No CI. No pre-commit hooks.
