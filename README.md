# OpenCode Pushover Monitor

Get push notifications on your phone when [OpenCode](https://opencode.ai) sessions need your attention -- waiting for input, errored, or idle.

Uses [Pushover](https://pushover.net) to deliver notifications with per-state priority and sound.

## Quick Start

```bash
# Set your Pushover credentials (get them from https://pushover.net)
export PUSHOVER_APP_TOKEN="your_app_token"
export PUSHOVER_USER_KEY="your_user_key"
```
```bash
# Launch (creates a venv, installs deps, starts monitoring in background)
./start_monitor.sh
```

Or run directly without the launcher:

```bash
pip install -r requirements.txt
```
```bash
python3 pushover_monitor.py \
    --app-token "$PUSHOVER_APP_TOKEN" \
    --user-key "$PUSHOVER_USER_KEY"
```

## Prerequisites

1. **Python 3** installed
2. **Pushover account** -- install the [Pushover app](https://pushover.net/clients) on your phone and create an account
3. **Pushover credentials:**
   - **User Key** -- on your Pushover dashboard
   - **App Token** -- create one at https://pushover.net/apps/build (name it "OpenCode" or similar)

## Notification Types

| State | Priority | Sound | Trigger |
|-------|----------|-------|---------|
| Waiting for input | High | magic | Title contains "waiting", "input", or "question" |
| Error | High | siren | Title contains "error", "failed", or "fail" |
| Idle | Normal | none | Session inactive for >10 minutes |

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--idle-minutes` | 10 | Minutes before a session is considered idle |
| `--interval` | 30 | Seconds between polling checks |
| `--max-age-hours` | 24 | Only monitor sessions updated within this many hours |

Example with custom settings:

```bash
python3 pushover_monitor.py \
    --app-token "$PUSHOVER_APP_TOKEN" \
    --user-key "$PUSHOVER_USER_KEY" \
    --idle-minutes 5 \
    --interval 60
```

## Stopping the Monitor

```bash
pkill -f pushover_monitor.py
```

If running in the foreground, `Ctrl+C` will stop it.

## Auto-Start (Optional)

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
# Start OpenCode Pushover Monitor if not running
if ! pgrep -f "pushover_monitor.py" > /dev/null; then
    /path/to/pushover_monitor/start_monitor.sh > /dev/null 2>&1
fi
```

Verify it's running:

```bash
pgrep -fa pushover_monitor.py
```

## How It Works

1. Detects running OpenCode processes via `ps` and resolves their working directories with `lsof`
2. Queries the OpenCode SQLite database for the most recent session in each active directory -- one session per running instance
3. Detects state changes by matching keywords in session titles
4. Computes idle time from the `time_updated` epoch-millisecond column
5. Sends a Pushover notification when a session transitions to waiting, error, or idle
6. Tracks previous state per session to avoid duplicate notifications

If process detection fails (e.g. `lsof` not available), falls back to monitoring all sessions updated within `--max-age-hours`.

## Testing

Run the smoke tests to verify process detection, session discovery, and state logic:

```bash
./venv/bin/python3 test_monitor.py
```

To also test Pushover delivery:

```bash
export PUSHOVER_APP_TOKEN="your_app_token"
export PUSHOVER_USER_KEY="your_user_key"

./venv/bin/python3 test_monitor.py
```

At least one `opencode` session must be running for the process detection and session discovery tests to pass.

## Troubleshooting

**Monitor not starting?**
- Check Python 3 is installed: `python3 --version`
- Verify your tokens are set: `echo $PUSHOVER_APP_TOKEN`

**Not receiving notifications?**
- Confirm the Pushover app is installed and logged in on your phone
- Test delivery directly:
  ```bash
  curl -s --form-string "token=$PUSHOVER_APP_TOKEN" \
       --form-string "user=$PUSHOVER_USER_KEY" \
       --form-string "message=Test from OpenCode monitor" \
       https://api.pushover.net/1/messages.json
  ```

**Duplicate notifications?**
- Check only one instance is running: `pgrep -f pushover_monitor.py`
