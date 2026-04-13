# OpenCode Pushover Monitor

Get push notifications on your phone when [OpenCode](https://opencode.ai) sessions need your attention -- waiting for input, errored, or idle.

Uses [Pushover](https://pushover.net) to deliver notifications with per-state priority and sound.

## Quick Start

```bash
git clone <repo-url> && cd pushover_monitor

# Set your Pushover credentials (get them from https://pushover.net)
export PUSHOVER_APP_TOKEN="your_app_token"
export PUSHOVER_USER_KEY="your_user_key"

# Launch (creates a venv, installs deps, starts monitoring in background)
./start_monitor.sh
```

Or run directly without the launcher:

```bash
pip install -r requirements.txt

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

Example with custom settings:

```bash
python3 pushover_monitor.py \
    --app-token "$PUSHOVER_APP_TOKEN" \
    --user-key "$PUSHOVER_USER_KEY" \
    --idle-minutes 5 \
    --interval 60
```

## Auto-Start (Optional)

Add to your `~/.zshrc` or `~/.bashrc`:

```bash
# Start OpenCode Pushover Monitor if not running
if ! pgrep -f "pushover_monitor.py" > /dev/null; then
    /path/to/pushover_monitor/start_monitor.sh > /dev/null 2>&1
fi
```

## How It Works

1. Polls `opencode session list` every 30 seconds (prefers `--json`, falls back to text parsing)
2. Detects state changes by matching keywords in session titles
3. Sends a Pushover notification when a session transitions to waiting, error, or idle
4. Tracks previous state per session to avoid duplicate notifications

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
