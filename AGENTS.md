# AGENTS.md

## What this is

Single-file Python script (`pushover_monitor.py`) that monitors all OpenCode sessions and sends Pushover push notifications when sessions become idle, waiting, or errored.

## Setup & run

- `start_monitor.sh` auto-creates a venv in `./venv/`, installs `requests`, and launches the monitor in the background.
- Only runtime dependency: `requests` (also listed in `requirements.txt`).
- Pushover credentials (`PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`) must be set as environment variables before running. `start_monitor.sh` defaults to placeholder values that the script rejects -- the user must export real tokens in their shell.

## Architecture notes

- No package structure, no `setup.py`/`pyproject.toml`, no linter config. The entire app is `pushover_monitor.py`.
- Sessions are discovered by detecting running `opencode` processes (`ps` + `lsof -d cwd`), then querying the SQLite database at `~/.local/share/opencode/opencode.db` for the most recent session in each process's working directory. This returns exactly one session per running OpenCode instance, across all projects. Falls back to a recency-based query (`--max-age-hours`) if process detection fails.
- Only sessions updated within `--max-age-hours` (default 24) are queried in the fallback path. On startup, existing session states are seeded silently -- notifications only fire on **subsequent** state changes.
- Session state detection is heuristic: it matches keywords like "waiting", "error", "idle" in the session title string. There is no explicit state column in the DB. Changes here are fragile and should be tested manually against real sessions.
- Idle time is computed from the `time_updated` column (epoch milliseconds) using simple arithmetic -- no date-string parsing.

## Running

```bash
# Foreground (pass tokens directly):
python3 pushover_monitor.py --app-token TOKEN --user-key KEY

# Background via launcher (reads tokens from env vars):
export PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..."
./start_monitor.sh
```

## Testing

```bash
# Offline tests (state detection, process discovery, session matching):
./venv/bin/python3 test_monitor.py

# Full tests including Pushover delivery:
PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..." ./venv/bin/python3 test_monitor.py
```

Requires at least one running `opencode` session for process detection and session discovery tests to pass. State detection tests run offline with no dependencies.
