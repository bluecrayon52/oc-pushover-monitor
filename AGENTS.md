# AGENTS.md

## What this is

Single-file Python script (`pushover_monitor.py`) that polls `opencode session list` and sends Pushover push notifications when sessions become idle, waiting, or errored.

## Setup & run

- `start_monitor.sh` auto-creates a venv in `./venv/`, installs `requests`, and launches the monitor in the background.
- Only runtime dependency: `requests` (also listed in `requirements.txt`).
- Pushover credentials (`PUSHOVER_APP_TOKEN`, `PUSHOVER_USER_KEY`) must be set as environment variables before running. `start_monitor.sh` defaults to placeholder values that the script rejects -- the user must export real tokens in their shell.

## Architecture notes

- No package structure, no `setup.py`/`pyproject.toml`, no tests, no linter config. The entire app is `pushover_monitor.py`.
- Session state detection is heuristic: it matches keywords like "waiting", "error", "idle" in the session title string. Changes here are fragile and should be tested manually against real `opencode session list` output.
- Text-output parsing in `_parse_text_output` uses hardcoded column widths (0-30, 32-110, 111+). If the `opencode` CLI changes its table format, this will silently break.
- The `--json` flag on `opencode session list` is attempted first; text parsing is the fallback.

## Running

```bash
# Foreground (pass tokens directly):
python3 pushover_monitor.py --app-token TOKEN --user-key KEY

# Background via launcher (reads tokens from env vars):
export PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..."
./start_monitor.sh
```

## No tests

There is no test suite. Verify changes by running the monitor against a live OpenCode session and confirming notifications arrive.
