#!/bin/bash
# OpenCode Pushover Monitor Launcher
# Automatically manages virtual environment and dependencies
# Add this to your shell profile or run manually
#
# Credentials are read by pushover_monitor.py at runtime, in this order:
#   1. $PUSHOVER_APP_TOKEN / $PUSHOVER_USER_KEY (if set)
#   2. macOS Keychain (service "oc-pushover-monitor")
# Store them once in the Keychain with:
#   ./start_monitor.sh --setup-keychain
# They are never passed on the command line, so they don't show up in `ps`.

# Optional configuration
export IDLE_MINUTES="${IDLE_MINUTES:-10}"
export WAIT_MINUTES="${WAIT_MINUTES:-2}"
export STALL_MINUTES="${STALL_MINUTES:-10}"
export CHECK_INTERVAL="${CHECK_INTERVAL:-30}"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip3"
KEYCHAIN_SERVICE="oc-pushover-monitor"

# Create virtual environment if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "🐍 Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "❌ Failed to create virtual environment"
        exit 1
    fi
fi

# Install/update dependencies
if [ ! -f "$VENV_DIR/.dependencies_installed" ] || [ "$SCRIPT_DIR/pushover_monitor.py" -nt "$VENV_DIR/.dependencies_installed" ]; then
    echo "📦 Installing dependencies..."
    "$PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    if [ $? -eq 0 ]; then
        touch "$VENV_DIR/.dependencies_installed"
        echo "✓ Dependencies installed"
    else
        echo "❌ Failed to install dependencies"
        exit 1
    fi
fi

if [ "$1" = "--setup-keychain" ]; then
    exec "$PYTHON" "$SCRIPT_DIR/pushover_monitor.py" --setup-keychain
fi

# Check credentials are available (env vars or Keychain) without reading
# the secret values themselves.
has_credential() {
    local env_value="$1" account="$2"
    [ -n "$env_value" ] && return 0
    command -v security > /dev/null 2>&1 &&
        security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$account" > /dev/null 2>&1
}

if ! has_credential "$PUSHOVER_APP_TOKEN" app-token || \
   ! has_credential "$PUSHOVER_USER_KEY" user-key; then
    echo "⚠️  Error: Pushover credentials not found!"
    echo ""
    echo "Store them in the macOS Keychain (recommended):"
    echo "  $0 --setup-keychain"
    echo ""
    echo "Get your credentials from: https://pushover.net"
    exit 1
fi

# Check if monitor is already running
if pgrep -f "pushover_monitor.py" > /dev/null; then
    echo "✓ OpenCode Pushover Monitor is already running"
    exit 0
fi

# Start the monitor using virtual environment Python
"$PYTHON" "$SCRIPT_DIR/pushover_monitor.py" \
    --idle-minutes "$IDLE_MINUTES" \
    --wait-minutes "$WAIT_MINUTES" \
    --stall-minutes "$STALL_MINUTES" \
    --interval "$CHECK_INTERVAL" &

MONITOR_PID=$!
echo "🚀 OpenCode Pushover Monitor started (PID: $MONITOR_PID)"
echo "   Virtual environment: $VENV_DIR"
echo "   Logs will appear below..."
echo ""
