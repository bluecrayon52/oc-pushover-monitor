# AGENTS.md

## What this is

Single-file Python script (`pushover_monitor.py`) that monitors running OpenCode sessions and sends Pushover push notifications when sessions become idle, waiting, or errored.

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

- No package structure, no `pyproject.toml`, no linter config. The entire app is `pushover_monitor.py`.
- DB access is via `subprocess.run(["opencode", "db", <raw SQL>, "--format", "json"])` -- not a direct SQLite connection. The `opencode` CLI must be on `$PATH`.
- Session discovery: detects running `opencode` processes (`ps` + `lsof -d cwd`), then queries `~/.local/share/opencode/opencode.db` for the most recent session per working directory. Falls back to a recency-based query (`--max-age-hours`) if process detection fails.
- On startup, existing session states are seeded silently -- notifications only fire on **subsequent** state changes.
- **Notification timing** uses duration-based thresholds, not simple state transitions. WAITING notifications only fire after the session has been continuously waiting for `--wait-minutes` (default 2). This prevents noisy alerts during active conversations and avoids the "stuck on WAITING" bug where a missed ACTIVE→WAITING transition silently suppresses all future notifications. Tracking state lives in `TrackedSession` dataclass instances keyed by session ID.
- **Waiting detection** uses the `message` table: if the latest message for a session has `role = "assistant"` and `json_extract(data, '$.finish') = "stop"`, the assistant has completed its turn and the session is waiting for user input. Additionally checks the `part` table for a `question` tool with `status` of `pending` or `running`, which catches the case where the assistant is blocking on a question tool call. This is a reliable, structural signal -- not a heuristic.
- **Error detection** is still a title-keyword heuristic (`"error"`, `"failed"`, `"fail"` in session title). Tool-level errors in the `part` table are transient and self-correcting, so they are not used.
- **Idle detection** is computed from `time_updated` (epoch milliseconds) via simple arithmetic -- triggers when the session exceeds the idle threshold and is *not* already in the WAITING state.
- SQL queries use f-string interpolation of directory paths (not parameterized). Be aware of this if modifying query construction.

## Testing

```bash
# Offline tests (state detection, idle logic):
./venv/bin/python3 test_monitor.py

# Full tests including Pushover delivery:
PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..." ./venv/bin/python3 test_monitor.py
```

- Tests use a hand-rolled runner in `test_monitor.py` (not `unittest` or `pytest`). Exit code is 0 on success, 1 on any failure.
- Process detection and session discovery tests require at least one running `opencode` session. State detection tests run fully offline.
- Pushover delivery tests are skipped when env vars are unset.

## Style

- `.ruff_cache/` exists, suggesting ruff has been used for formatting/linting, but there is no ruff config file. Run `ruff check` / `ruff format` if available.
- No CI. No pre-commit hooks.
