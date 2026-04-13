#!/bin/bash
# OpenCode Pushover Monitor Launcher
# Automatically manages virtual environment and dependencies
# Add this to your shell profile or run manually

# Configuration - Get these from https://pushover.net
# Set these in your shell environment, or replace the defaults below.
export PUSHOVER_APP_TOKEN="${PUSHOVER_APP_TOKEN:-your_app_token_here}"
export PUSHOVER_USER_KEY="${PUSHOVER_USER_KEY:-your_user_key_here}"

# Optional configuration
export IDLE_MINUTES="10"
export WAIT_MINUTES="2"
export CHECK_INTERVAL="30"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PYTHON="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip3"

# Check if tokens are configured (using default check - only checks for empty/placeholder)
if [ -z "$PUSHOVER_APP_TOKEN" ] || [ -z "$PUSHOVER_USER_KEY" ] || \
   [ "$PUSHOVER_APP_TOKEN" = "your_app_token_here" ] || \
   [ "$PUSHOVER_USER_KEY" = "your_user_key_here" ]; then
    echo "⚠️  Error: Please configure your Pushover credentials first!"
    echo ""
    echo "Export them in your shell before running this script:"
    echo "  export PUSHOVER_APP_TOKEN=\"your_actual_app_token\""
    echo "  export PUSHOVER_USER_KEY=\"your_actual_user_key\""
    echo ""
    echo "Get your credentials from: https://pushover.net"
    exit 1
fi

# Check if monitor is already running
if pgrep -f "pushover_monitor.py" > /dev/null; then
    echo "✓ OpenCode Pushover Monitor is already running"
    exit 0
fi

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
    "$PIP" install --quiet requests
    if [ $? -eq 0 ]; then
        touch "$VENV_DIR/.dependencies_installed"
        echo "✓ Dependencies installed"
    else
        echo "❌ Failed to install dependencies"
        exit 1
    fi
fi

# Start the monitor using virtual environment Python
"$PYTHON" "$SCRIPT_DIR/pushover_monitor.py" \
    --app-token "$PUSHOVER_APP_TOKEN" \
    --user-key "$PUSHOVER_USER_KEY" \
    --idle-minutes "${IDLE_MINUTES:-10}" \
    --wait-minutes "${WAIT_MINUTES:-2}" \
    --interval "${CHECK_INTERVAL:-30}" &

MONITOR_PID=$!
echo "🚀 OpenCode Pushover Monitor started (PID: $MONITOR_PID)"
echo "   Virtual environment: $VENV_DIR"
echo "   Logs will appear below..."
echo ""
