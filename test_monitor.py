#!/usr/bin/env python3
"""
Smoke tests for pushover_monitor.

Offline tests (no tokens required):
    ./venv/bin/python3 test_monitor.py

Full tests including Pushover delivery:
    PUSHOVER_APP_TOKEN="..." PUSHOVER_USER_KEY="..." ./venv/bin/python3 test_monitor.py
"""

import os
import sys
import time

from pushover_monitor import (
    OpenCodeSessionMonitor,
    PushoverNotifier,
    SessionInfo,
    SessionState,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f"  ({detail})"
        print(msg)


# ---------------------------------------------------------------------------
# 1. State detection (offline -- no tokens, no processes needed)
# ---------------------------------------------------------------------------
print("\n== State Detection ==")

monitor = OpenCodeSessionMonitor("dummy", "dummy")

cases = [
    # (title, expected_state)
    ("Waiting for user input on file change", SessionState.WAITING),
    ("Session is waiting", SessionState.WAITING),
    ("Answer the question about config", SessionState.WAITING),
    ("Build failed with exit code 1", SessionState.ERROR),
    ("TypeError: cannot read property of undefined", SessionState.ERROR),
    ("Fix the failing test suite", SessionState.ERROR),
    ("Implement new feature for dashboard", SessionState.ACTIVE),
    ("Refactor database connection pool", SessionState.ACTIVE),
]

for title, expected in cases:
    session = SessionInfo(
        id="test",
        title=title,
        # Use a recent timestamp so idle detection doesn't override
        updated_ms=int(time.time() * 1000),
        state=SessionState.UNKNOWN,
    )
    actual = monitor._check_idle_status(session)
    check(
        f"'{title[:45]}...' -> {expected.value}",
        actual == expected,
        f"got {actual.value}",
    )

# Idle detection: session updated 30 minutes ago with a neutral title
old_session = SessionInfo(
    id="test-idle",
    title="Refactor database module",
    updated_ms=int((time.time() - 1800) * 1000),  # 30 min ago
    state=SessionState.UNKNOWN,
)
check(
    "30-min-old neutral session -> idle",
    monitor._check_idle_status(old_session) == SessionState.IDLE,
)

# Not idle: session updated 2 minutes ago
recent_session = SessionInfo(
    id="test-recent",
    title="Refactor database module",
    updated_ms=int((time.time() - 120) * 1000),  # 2 min ago
    state=SessionState.UNKNOWN,
)
check(
    "2-min-old neutral session -> active",
    monitor._check_idle_status(recent_session) == SessionState.ACTIVE,
)

# ---------------------------------------------------------------------------
# 2. Process detection (requires opencode to be running)
# ---------------------------------------------------------------------------
print("\n== Process Detection ==")

active_dirs = monitor._get_active_directories()
check(
    "Detected running opencode processes",
    len(active_dirs) > 0,
    f"found {len(active_dirs)} directories"
    if active_dirs
    else "none found -- is opencode running?",
)

if active_dirs:
    for d in sorted(active_dirs):
        print(f"         {d}")

# ---------------------------------------------------------------------------
# 3. Session discovery (requires opencode to be running)
# ---------------------------------------------------------------------------
print("\n== Session Discovery ==")

sessions = monitor.get_sessions()
check(
    "get_sessions() returned results",
    len(sessions) > 0,
    f"found {len(sessions)}" if sessions else "0 -- is opencode running?",
)

if active_dirs:
    check(
        "Session count matches process count",
        len(sessions) == len(active_dirs),
        f"{len(sessions)} sessions vs {len(active_dirs)} processes",
    )

for s in sessions:
    print(f"         {s.id[:20]}... {s.title[:50]}")

# ---------------------------------------------------------------------------
# 4. Pushover delivery (requires tokens)
# ---------------------------------------------------------------------------
print("\n== Pushover Delivery ==")

app_token = os.environ.get("PUSHOVER_APP_TOKEN", "")
user_key = os.environ.get("PUSHOVER_USER_KEY", "")

if app_token and user_key:
    notifier = PushoverNotifier(app_token, user_key)
    ok = notifier.send_notification(
        message="Smoke test from test_monitor.py -- notifications are working!",
        title="OpenCode Monitor Test",
        priority=0,
        sound="pushover",
    )
    check("Send test notification", ok)
    if ok:
        print("         Check your phone for the test notification.")
else:
    print(
        "  SKIP  Pushover tokens not set -- set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY to test delivery"
    )

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'=' * 40}")
print(f"  {PASS} passed, {FAIL} failed")
print(f"{'=' * 40}")
sys.exit(1 if FAIL else 0)
