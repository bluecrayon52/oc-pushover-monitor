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
    TrackedSession,
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

now_ms = int(time.time() * 1000)


def _session(
    title="Test session",
    updated_ms=None,
    role="",
    finish="",
    pending_question=False,
):
    """Helper to build a SessionInfo with sensible defaults."""
    return SessionInfo(
        id="test",
        title=title,
        updated_ms=updated_ms if updated_ms is not None else now_ms,
        directory="/tmp/test_project",
        last_message_role=role,
        last_message_finish=finish,
        has_pending_question=pending_question,
        state=SessionState.UNKNOWN,
    )


# -- Waiting: assistant message with finish="stop" --------------------------
check(
    "assistant finish=stop -> waiting",
    monitor._check_idle_status(_session(role="assistant", finish="stop"))
    == SessionState.WAITING,
)

check(
    "assistant finish=stop (old session) -> waiting",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            updated_ms=int((time.time() - 1800) * 1000),
        )
    )
    == SessionState.WAITING,
)

# -- Waiting: question tool pending -----------------------------------------
check(
    "pending question tool -> waiting",
    monitor._check_idle_status(_session(pending_question=True)) == SessionState.WAITING,
)

check(
    "pending question + tool-calls -> waiting",
    monitor._check_idle_status(
        _session(role="assistant", finish="tool-calls", pending_question=True)
    )
    == SessionState.WAITING,
)

# -- Not waiting: assistant still working (tool-calls) ----------------------
check(
    "assistant finish=tool-calls -> active",
    monitor._check_idle_status(_session(role="assistant", finish="tool-calls"))
    == SessionState.ACTIVE,
)

# -- Not waiting: latest message is from user --------------------------------
check(
    "user message (no finish) -> active",
    monitor._check_idle_status(_session(role="user", finish="")) == SessionState.ACTIVE,
)

# -- Error: title keyword heuristic -----------------------------------------
error_cases = [
    "Build failed with exit code 1",
    "TypeError: cannot read property of undefined",
    "Fix the failing test suite",
]
for title in error_cases:
    check(
        f"'{title[:45]}...' -> error",
        monitor._check_idle_status(_session(title=title)) == SessionState.ERROR,
        f"got {monitor._check_idle_status(_session(title=title)).value}",
    )

# -- Waiting takes precedence over error title -------------------------------
check(
    "finish=stop + error title -> waiting (precedence)",
    monitor._check_idle_status(
        _session(title="Build failed", role="assistant", finish="stop")
    )
    == SessionState.WAITING,
)

# -- Active: neutral title, recent, assistant still working ------------------
active_cases = [
    "Implement new feature for dashboard",
    "Refactor database connection pool",
]
for title in active_cases:
    check(
        f"'{title[:45]}...' -> active",
        monitor._check_idle_status(_session(title=title)) == SessionState.ACTIVE,
    )

# -- Idle: session old, assistant not finished -------------------------------
check(
    "30-min-old neutral session -> idle",
    monitor._check_idle_status(_session(updated_ms=int((time.time() - 1800) * 1000)))
    == SessionState.IDLE,
)

# -- Not idle: session recent ------------------------------------------------
check(
    "2-min-old neutral session -> active",
    monitor._check_idle_status(_session(updated_ms=int((time.time() - 120) * 1000)))
    == SessionState.ACTIVE,
)

# ---------------------------------------------------------------------------
# 1b. Duration-based notification logic (offline)
# ---------------------------------------------------------------------------
print("\n== Notification Timing ==")

# Use a monitor with a short wait threshold for testing (0.01 minutes ≈ 0.6s)
timing_monitor = OpenCodeSessionMonitor("dummy", "dummy", wait_threshold_minutes=0)

# Simulate: session enters WAITING, check_and_notify should NOT notify
# immediately on first sight (seed behaviour handled by run(), but
# check_and_notify seeds on first encounter too).

# First poll: new session in WAITING state -- seeds, no notification
timing_monitor.tracked.clear()
s_wait = _session(title="Test timing", role="assistant", finish="stop")
s_wait_state = timing_monitor._check_idle_status(s_wait)
check("Waiting session detected correctly", s_wait_state == SessionState.WAITING)

# Manually seed the session as if check_and_notify saw it for the first time
timing_monitor.tracked["test"] = TrackedSession(
    state=SessionState.ACTIVE,  # pretend it was ACTIVE last poll
)

# Now simulate check_and_notify seeing WAITING: it should start the timer
# but with wait_threshold=0 it should also immediately fire.
# We'll capture whether _send_state_notification would be called by
# checking the tracked state after.
notifications_sent = []
_orig_send = timing_monitor._send_state_notification


def _capture_send(session, state):
    notifications_sent.append((os.path.basename(session.directory), state))


timing_monitor._send_state_notification = _capture_send

# Mock get_sessions to return our test session
timing_monitor.get_sessions = lambda: [s_wait]
timing_monitor.check_and_notify()

check(
    "WAITING notification sent after threshold (0 min)",
    len(notifications_sent) == 1 and notifications_sent[0][1] == SessionState.WAITING,
    f"sent {len(notifications_sent)} notifications",
)

# Second call: already notified, should NOT send again
notifications_sent.clear()
timing_monitor.check_and_notify()
check(
    "No duplicate WAITING notification",
    len(notifications_sent) == 0,
    f"sent {len(notifications_sent)}",
)

# Session goes ACTIVE, then back to WAITING: should re-notify
s_active = _session(title="Test timing", role="assistant", finish="tool-calls")
timing_monitor.get_sessions = lambda: [s_active]
timing_monitor.check_and_notify()  # sees ACTIVE

notifications_sent.clear()
timing_monitor.get_sessions = lambda: [s_wait]
timing_monitor.check_and_notify()  # sees WAITING again

check(
    "Re-notification after ACTIVE -> WAITING cycle",
    len(notifications_sent) == 1 and notifications_sent[0][1] == SessionState.WAITING,
    f"sent {len(notifications_sent)} notifications",
)

# Test with a longer threshold: should NOT notify immediately
timing_monitor_slow = OpenCodeSessionMonitor(
    "dummy", "dummy", wait_threshold_minutes=60
)
slow_notifications = []
timing_monitor_slow._send_state_notification = lambda s, st: slow_notifications.append(
    (os.path.basename(s.directory), st)
)
timing_monitor_slow.tracked["test"] = TrackedSession(state=SessionState.ACTIVE)
timing_monitor_slow.get_sessions = lambda: [s_wait]
timing_monitor_slow.check_and_notify()

check(
    "WAITING notification suppressed before threshold (60 min)",
    len(slow_notifications) == 0,
    f"sent {len(slow_notifications)}",
)

# Verify the timer was started
check(
    "Waiting timer started",
    timing_monitor_slow.tracked["test"].waiting_since is not None,
)

# -- WAITING -> WAITING with activity (the "stuck on WAITING" bug fix) ------
# Simulate: monitor already notified for a WAITING session, then the session
# has activity (updated_ms changes) but state stays WAITING (poll missed the
# brief ACTIVE window).  The timer should reset and re-notify after threshold.
activity_monitor = OpenCodeSessionMonitor("dummy", "dummy", wait_threshold_minutes=0)
activity_notifications = []
activity_monitor._send_state_notification = lambda s, st: activity_notifications.append(
    st
)

# Seed as WAITING with updated_ms=1000
s_wait_v1 = _session(role="assistant", finish="stop")
s_wait_v1.updated_ms = 1000
activity_monitor.tracked["test"] = TrackedSession(
    state=SessionState.WAITING,
    last_updated_ms=1000,
    waiting_since=time.time() - 300,  # been waiting 5 min
    notified_waiting=True,  # already notified
)

# Poll with same updated_ms -- no activity, no re-notification
activity_monitor.get_sessions = lambda: [s_wait_v1]
activity_monitor.check_and_notify()
check(
    "WAITING->WAITING no activity: no re-notification",
    len(activity_notifications) == 0,
    f"sent {len(activity_notifications)}",
)

# Poll with changed updated_ms -- activity detected, should re-notify
s_wait_v2 = _session(role="assistant", finish="stop")
s_wait_v2.updated_ms = 2000  # changed!
activity_notifications.clear()
activity_monitor.get_sessions = lambda: [s_wait_v2]
activity_monitor.check_and_notify()
check(
    "WAITING->WAITING with activity: re-notification",
    len(activity_notifications) == 1
    and activity_notifications[0] == SessionState.WAITING,
    f"sent {len(activity_notifications)}",
)

# Restore original method
timing_monitor._send_state_notification = _orig_send

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
    state = monitor._check_idle_status(s)
    print(
        f"         {s.id[:20]}... "
        f"finish={s.last_message_finish or 'n/a':12s} "
        f"state={state.value:8s} "
        f"{os.path.basename(s.directory)}"
    )

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
