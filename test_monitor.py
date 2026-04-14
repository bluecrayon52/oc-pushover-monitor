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
    error="",
    text="",
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
        last_message_error=error,
        last_message_text=text,
        has_pending_question=pending_question,
        state=SessionState.UNKNOWN,
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

# -- Waiting: finish=stop with prose question (? in last paragraph) ---------
check(
    "finish=stop + prose question -> waiting",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            text="I've made the changes.\n\nDoes this look correct?",
        )
    )
    == SessionState.WAITING,
)

check(
    "finish=stop + numbered questions -> waiting",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            text=(
                "Two questions:\n\n"
                "1. **Priority** -- should it be high?\n"
                "2. **Threshold** -- reuse idle-minutes? 10 min seems reasonable."
            ),
        )
    )
    == SessionState.WAITING,
)

check(
    "finish=stop (old session) + prose question -> waiting",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            text="What do you think?",
            updated_ms=int((time.time() - 1800) * 1000),
        )
    )
    == SessionState.WAITING,
)

# -- Idle: finish=stop with no prose question (assistant is done) -----------
check(
    "finish=stop + summary text -> idle",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            text="Here's a summary of all changes.\n\nUpdated README and AGENTS.md.",
        )
    )
    == SessionState.IDLE,
)

check(
    "finish=stop + empty text -> idle",
    monitor._check_idle_status(_session(role="assistant", finish="stop", text=""))
    == SessionState.IDLE,
)

check(
    "finish=stop + no text -> idle",
    monitor._check_idle_status(_session(role="assistant", finish="stop"))
    == SessionState.IDLE,
)

# -- Prose question: code block stripping -----------------------------------
check(
    "finish=stop + ? only inside code block -> idle (stripped)",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            text="Here's the query:\n\n```sql\nSELECT * FROM foo WHERE bar = ?\n```\n\nDone.",
        )
    )
    == SessionState.IDLE,
)

check(
    "finish=stop + ? outside code block -> waiting",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="stop",
            text="Here's the query:\n\n```sql\nSELECT * FROM foo WHERE bar = ?\n```\n\nShould I run it?",
        )
    )
    == SessionState.WAITING,
)

# -- Error: message-level error field (DB-based detection) ------------------
check(
    "APIError -> error",
    monitor._check_idle_status(_session(role="assistant", error="APIError"))
    == SessionState.ERROR,
)

check(
    "UnknownError -> error",
    monitor._check_idle_status(_session(role="assistant", error="UnknownError"))
    == SessionState.ERROR,
)

# -- Not error: user-initiated abort is excluded ----------------------------
check(
    "MessageAbortedError -> active (excluded)",
    monitor._check_idle_status(_session(role="assistant", error="MessageAbortedError"))
    == SessionState.ACTIVE,
)

# -- Not error: no error field means not an error ---------------------------
check(
    "no error field -> active",
    monitor._check_idle_status(_session(role="assistant", error=""))
    == SessionState.ACTIVE,
)

# -- Waiting takes precedence over error ------------------------------------
check(
    "finish=stop + APIError -> idle (finish=stop checked first)",
    monitor._check_idle_status(
        _session(role="assistant", finish="stop", error="APIError")
    )
    == SessionState.IDLE,
)

check(
    "pending question + APIError -> waiting (question checked first)",
    monitor._check_idle_status(
        _session(role="assistant", error="APIError", pending_question=True)
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

# -- Active: recent session, assistant still working -------------------------
check(
    "recent session, tool-calls -> active",
    monitor._check_idle_status(_session(role="assistant", finish="tool-calls"))
    == SessionState.ACTIVE,
)

# -- Stalled: session old, assistant not finished ----------------------------
check(
    "30-min-old tool-calls session -> stalled",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="tool-calls",
            updated_ms=int((time.time() - 1800) * 1000),
        )
    )
    == SessionState.STALLED,
)

check(
    "30-min-old in-flight session (finish=null) -> stalled",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="",
            updated_ms=int((time.time() - 1800) * 1000),
        )
    )
    == SessionState.STALLED,
)

# -- Not stalled: recent session ---------------------------------------------
check(
    "2-min-old tool-calls session -> active",
    monitor._check_idle_status(
        _session(
            role="assistant",
            finish="tool-calls",
            updated_ms=int((time.time() - 120) * 1000),
        )
    )
    == SessionState.ACTIVE,
)

# ---------------------------------------------------------------------------
# 1b. Prose question detection (unit tests for _has_prose_question)
# ---------------------------------------------------------------------------
print("\n== Prose Question Detection ==")

check(
    "ends with ?",
    OpenCodeSessionMonitor._has_prose_question("What do you think?"),
)

check(
    "? in last paragraph (not last char)",
    OpenCodeSessionMonitor._has_prose_question(
        "Summary:\n\n1. Should I do this?\n2. This seems reasonable."
    ),
)

check(
    "no question mark -> False",
    not OpenCodeSessionMonitor._has_prose_question("All done. Updated the file."),
)

check(
    "? only in code block -> False",
    not OpenCodeSessionMonitor._has_prose_question(
        "```sql\nSELECT * WHERE x = ?\n```\n\nDone."
    ),
)

check(
    "? in code block AND in last paragraph -> True",
    OpenCodeSessionMonitor._has_prose_question(
        "```sql\nSELECT * WHERE x = ?\n```\n\nShould I run it?"
    ),
)

check(
    "empty string -> False",
    not OpenCodeSessionMonitor._has_prose_question(""),
)

check(
    "only code block with ? -> False",
    not OpenCodeSessionMonitor._has_prose_question("```\nfoo?\n```"),
)

# ---------------------------------------------------------------------------
# 1c. Duration-based notification logic (offline)
# ---------------------------------------------------------------------------
print("\n== Notification Timing ==")

# Use a monitor with a short wait threshold for testing
timing_monitor = OpenCodeSessionMonitor("dummy", "dummy", wait_threshold_minutes=0)

# Build a WAITING session (finish=stop with prose question)
s_wait = _session(
    title="Test timing",
    role="assistant",
    finish="stop",
    text="What do you think?",
)
s_wait_state = timing_monitor._check_idle_status(s_wait)
check("Waiting session detected correctly", s_wait_state == SessionState.WAITING)

# Build an IDLE session (finish=stop, no question)
s_idle = _session(
    title="Test timing",
    role="assistant",
    finish="stop",
    text="All done.",
)
s_idle_state = timing_monitor._check_idle_status(s_idle)
check("Idle session detected correctly", s_idle_state == SessionState.IDLE)

# Manually seed the session as if check_and_notify saw it for the first time
timing_monitor.tracked.clear()
timing_monitor.tracked["test"] = TrackedSession(
    state=SessionState.ACTIVE,  # pretend it was ACTIVE last poll
)

notifications_sent = []
_orig_send = timing_monitor._send_state_notification


def _capture_send(session, state):
    notifications_sent.append((os.path.basename(session.directory), state))


timing_monitor._send_state_notification = _capture_send

# Mock get_sessions to return our WAITING session
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

# -- IDLE notification timing -----------------------------------------------
idle_monitor = OpenCodeSessionMonitor("dummy", "dummy", idle_threshold_minutes=0)
idle_notifications = []
idle_monitor._send_state_notification = lambda s, st: idle_notifications.append(
    (os.path.basename(s.directory), st)
)
idle_monitor.tracked["test"] = TrackedSession(state=SessionState.ACTIVE)
idle_monitor.get_sessions = lambda: [s_idle]
idle_monitor.check_and_notify()

check(
    "IDLE notification sent after threshold (0 min)",
    len(idle_notifications) == 1 and idle_notifications[0][1] == SessionState.IDLE,
    f"sent {len(idle_notifications)} notifications",
)

# With a long threshold, IDLE should be suppressed
idle_monitor_slow = OpenCodeSessionMonitor("dummy", "dummy", idle_threshold_minutes=60)
idle_slow_notifications = []
idle_monitor_slow._send_state_notification = (
    lambda s, st: idle_slow_notifications.append(st)
)
idle_monitor_slow.tracked["test"] = TrackedSession(state=SessionState.ACTIVE)
idle_monitor_slow.get_sessions = lambda: [s_idle]
idle_monitor_slow.check_and_notify()

check(
    "IDLE notification suppressed before threshold (60 min)",
    len(idle_slow_notifications) == 0,
    f"sent {len(idle_slow_notifications)}",
)

check(
    "Idle timer started",
    idle_monitor_slow.tracked["test"].idle_since is not None,
)

# -- WAITING -> WAITING with activity (the "stuck on WAITING" bug fix) ------
activity_monitor = OpenCodeSessionMonitor("dummy", "dummy", wait_threshold_minutes=0)
activity_notifications = []
activity_monitor._send_state_notification = lambda s, st: activity_notifications.append(
    st
)

# Seed as WAITING with updated_ms=1000
s_wait_v1 = _session(role="assistant", finish="stop", text="What do you think?")
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
s_wait_v2 = _session(role="assistant", finish="stop", text="What do you think?")
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
