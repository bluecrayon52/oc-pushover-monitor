#!/usr/bin/env python3
"""
OpenCode Pushover Notifier
Monitors OpenCode sessions and sends iPhone notifications via Pushover
when sessions are idle, waiting for user input, or encounter errors.
"""

import os
import subprocess
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
import requests
from dataclasses import dataclass, field
from enum import Enum


class SessionState(Enum):
    ACTIVE = "active"
    IDLE = "idle"
    WAITING = "waiting"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class SessionInfo:
    id: str
    title: str
    updated_ms: int  # epoch milliseconds from the DB
    directory: str  # absolute path of the session's working directory
    last_message_role: str  # "assistant", "user", or "" if unknown
    last_message_finish: str  # "stop", "tool-calls", or "" if unknown/null
    has_pending_question: bool  # True if a question tool is waiting for user input
    state: SessionState


@dataclass
class TrackedSession:
    """Per-session tracking state for the monitor."""

    state: SessionState
    last_updated_ms: int = 0  # session's time_updated from last poll
    waiting_since: Optional[float] = None  # time.time() when WAITING started
    notified_waiting: bool = False  # already sent a notification for this wait?
    notified_idle: bool = False  # already sent a notification for this idle period?
    notified_error: bool = False  # already sent a notification for this error?


class PushoverNotifier:
    """Pushover notification client"""

    API_URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, app_token: str, user_key: str):
        self.app_token = app_token
        self.user_key = user_key

    def send_notification(
        self,
        message: str,
        title: str = "OpenCode Alert",
        priority: int = 0,
        sound: str = "pushover",
    ) -> bool:
        """Send notification to Pushover"""
        try:
            response = requests.post(
                self.API_URL,
                data={
                    "token": self.app_token,
                    "user": self.user_key,
                    "message": message,
                    "title": title,
                    "priority": priority,
                    "sound": sound,
                    "timestamp": int(time.time()),
                },
                timeout=10,
            )
            return response.status_code == 200
        except Exception as e:
            print(f"Failed to send notification: {e}")
            return False


class OpenCodeSessionMonitor:
    """Monitors OpenCode sessions and sends notifications"""

    def __init__(
        self,
        pushover_app_token: str,
        pushover_user_key: str,
        idle_threshold_minutes: int = 10,
        wait_threshold_minutes: int = 2,
        check_interval_seconds: int = 30,
        max_age_hours: int = 24,
    ):
        self.notifier = PushoverNotifier(pushover_app_token, pushover_user_key)
        self.idle_threshold = timedelta(minutes=idle_threshold_minutes)
        self.wait_threshold = timedelta(minutes=wait_threshold_minutes)
        self.check_interval = check_interval_seconds
        self.max_age_hours = max_age_hours
        self.tracked: Dict[str, TrackedSession] = {}

    def _get_active_directories(self) -> Set[str]:
        """Return working directories of running ``opencode`` processes.

        Uses ``ps`` to find opencode PIDs, then ``lsof -d cwd`` to resolve
        each process's working directory.  Works on macOS and Linux.
        """
        try:
            ps_result = subprocess.run(
                ["ps", "-eo", "pid,command"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pids: List[str] = []
            for line in ps_result.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[1].endswith("opencode --"):
                    pids.append(parts[0])

            dirs: Set[str] = set()
            for pid in pids:
                lsof = subprocess.run(
                    ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in lsof.stdout.splitlines():
                    if line.startswith("n/"):
                        dirs.add(line[1:])
            return dirs
        except Exception as e:
            print(f"Warning: could not detect active processes: {e}")
            return set()

    def _run_db_query(self, query: str) -> List[dict]:
        """Execute a SQL query via the ``opencode db`` CLI and return parsed rows."""
        result = subprocess.run(
            ["opencode", "db", query, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"Error: opencode db query failed (rc={result.returncode})")
            return []
        return json.loads(result.stdout)

    def get_sessions(self) -> List[SessionInfo]:
        """Fetch the active OpenCode session for each running instance.

        Detects live ``opencode`` processes via ``ps``/``lsof``, then queries
        the SQLite database for the most recently updated session in each
        process's working directory.  This returns exactly one session per
        running OpenCode instance.

        For each session, also fetches the latest message to determine
        whether the assistant has finished (``finish = 'stop'``) and is
        waiting for user input.

        Falls back to a recency-based query (``max_age_hours``) if process
        detection finds nothing (e.g. ``lsof`` unavailable).
        """
        try:
            active_dirs = self._get_active_directories()

            if active_dirs:
                # Build SQL to get the most recent session per active directory
                placeholders = ",".join(f"'{d}'" for d in active_dirs)
                session_query = (
                    "SELECT s.id, s.title, s.time_updated, s.directory "
                    "FROM session s "
                    "INNER JOIN ("
                    "  SELECT directory, MAX(time_updated) AS max_updated "
                    "  FROM session "
                    f"  WHERE directory IN ({placeholders}) "
                    "  AND time_archived IS NULL "
                    "  GROUP BY directory"
                    ") latest "
                    "ON s.directory = latest.directory "
                    "AND s.time_updated = latest.max_updated "
                    "ORDER BY s.time_updated DESC"
                )
            else:
                # Fallback: no active processes detected, use recency filter
                cutoff_ms = int((time.time() - self.max_age_hours * 3600) * 1000)
                session_query = (
                    "SELECT id, title, time_updated, directory FROM session "
                    "WHERE time_archived IS NULL "
                    f"AND time_updated > {cutoff_ms} "
                    "ORDER BY time_updated DESC"
                )

            session_rows = self._run_db_query(session_query)
            if not session_rows:
                return []

            # For each session, fetch the latest message's role and finish
            # status to determine if the assistant is waiting for user input.
            session_ids = [row.get("id", "") for row in session_rows]
            id_list = ",".join(f"'{sid}'" for sid in session_ids)
            msg_query = (
                "SELECT m.session_id, "
                "  json_extract(m.data, '$.role') AS role, "
                "  json_extract(m.data, '$.finish') AS finish "
                "FROM message m "
                "INNER JOIN ("
                "  SELECT session_id, MAX(time_created) AS max_created "
                "  FROM message "
                f"  WHERE session_id IN ({id_list}) "
                "  GROUP BY session_id"
                ") latest "
                "ON m.session_id = latest.session_id "
                "AND m.time_created = latest.max_created"
            )
            msg_rows = self._run_db_query(msg_query)
            msg_by_session: Dict[str, dict] = {r["session_id"]: r for r in msg_rows}

            # Check for pending question tool calls (the question tool
            # blocks on user input but doesn't set finish="stop").
            question_query = (
                "SELECT DISTINCT p.session_id "
                "FROM part p "
                f"WHERE p.session_id IN ({id_list}) "
                "AND json_extract(p.data, '$.tool') = 'question' "
                "AND json_extract(p.data, '$.state.status') "
                "    IN ('pending', 'running')"
            )
            question_rows = self._run_db_query(question_query)
            sessions_with_question = {r["session_id"] for r in question_rows}

            sessions: List[SessionInfo] = []
            for row in session_rows:
                sid = row.get("id", "unknown")
                msg = msg_by_session.get(sid, {})
                sessions.append(
                    SessionInfo(
                        id=sid,
                        title=row.get("title", "Untitled"),
                        updated_ms=int(row.get("time_updated", 0)),
                        directory=row.get("directory", ""),
                        last_message_role=msg.get("role") or "",
                        last_message_finish=msg.get("finish") or "",
                        has_pending_question=sid in sessions_with_question,
                        state=SessionState.UNKNOWN,
                    )
                )
            return sessions

        except Exception as e:
            print(f"Error fetching sessions: {e}")
            return []

    def _check_idle_status(self, session: SessionInfo) -> SessionState:
        """Determine session state from the latest message and idle time.

        Detection logic (checked in order):

        1. **Waiting** -- the most recent message is from the assistant with
           ``finish = 'stop'``, meaning the assistant completed its turn and
           the session is blocked on user input.
        2. **Error** -- title-keyword heuristic (``error``, ``failed``,
           ``fail``).  Kept as a secondary signal; tool-level errors are
           transient and self-correcting so there is no reliable
           session-level error indicator in the DB.
        3. **Idle** -- the session's ``time_updated`` exceeds the idle
           threshold *and* the assistant has not finished (i.e. is not
           already in the WAITING state).
        4. **Active** -- none of the above matched.
        """
        # 1. Waiting: assistant finished its turn, or a question tool is
        #    blocking on user input
        if session.has_pending_question:
            return SessionState.WAITING
        if (
            session.last_message_role == "assistant"
            and session.last_message_finish == "stop"
        ):
            return SessionState.WAITING

        # 2. Error: title keyword heuristic (fallback)
        title = session.title.lower()
        if "error" in title or "failed" in title or "fail" in title:
            return SessionState.ERROR

        # 3. Idle: session hasn't been updated recently
        if session.updated_ms > 0:
            age = timedelta(milliseconds=time.time() * 1000 - session.updated_ms)
            if age > self.idle_threshold:
                return SessionState.IDLE

        return SessionState.ACTIVE

    def _log(self, msg: str):
        """Print a timestamped log line."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def check_and_notify(self):
        """Check sessions and send notifications based on state duration.

        Instead of notifying on state *transitions*, this method tracks how
        long each session has been in a given state and only notifies once
        the duration exceeds the configured threshold.  This avoids both
        the "stuck on WAITING" bug (where a missed ACTIVE window prevents
        re-notification) and noisy alerts during active conversations.
        """
        sessions = self.get_sessions()
        now = time.time()

        for session in sessions:
            sid = session.id
            current_state = self._check_idle_status(session)
            project = (
                os.path.basename(session.directory) if session.directory else "unknown"
            )

            prev = self.tracked.get(sid)

            if prev is None:
                # First time seeing this session -- seed without notifying
                ts = TrackedSession(
                    state=current_state, last_updated_ms=session.updated_ms
                )
                if current_state == SessionState.WAITING:
                    ts.waiting_since = now
                self.tracked[sid] = ts
                self._log(
                    f"[{project}] New session detected, state={current_state.value}"
                )
                continue

            # Detect activity: if the session's time_updated changed since
            # last poll, something happened (even if the state name is the
            # same).  Reset the waiting timer so a new WAITING period is
            # tracked independently.
            activity = session.updated_ms != prev.last_updated_ms
            prev.last_updated_ms = session.updated_ms

            # State changed from last poll
            if current_state != prev.state:
                self._log(f"[{project}] {prev.state.value} -> {current_state.value}")

            if current_state == SessionState.WAITING:
                if prev.state != SessionState.WAITING or activity:
                    # Just entered WAITING, or the session had activity while
                    # we kept seeing WAITING (missed a brief ACTIVE window).
                    # Either way, start a fresh timer.
                    prev.waiting_since = now
                    prev.notified_waiting = False
                elif prev.waiting_since is None:
                    # Safety: was WAITING but no timer set (shouldn't happen)
                    prev.waiting_since = now

                # Check if we've been waiting long enough to notify
                elapsed = now - prev.waiting_since
                if (
                    elapsed >= self.wait_threshold.total_seconds()
                    and not prev.notified_waiting
                ):
                    self._send_state_notification(session, current_state)
                    prev.notified_waiting = True

            elif current_state == SessionState.IDLE:
                if prev.state != SessionState.IDLE:
                    prev.notified_idle = False
                if not prev.notified_idle:
                    self._send_state_notification(session, current_state)
                    prev.notified_idle = True

            elif current_state == SessionState.ERROR:
                if prev.state != SessionState.ERROR:
                    prev.notified_error = False
                if not prev.notified_error:
                    self._send_state_notification(session, current_state)
                    prev.notified_error = True

            else:
                # ACTIVE -- reset waiting timer so the next WAITING period
                # starts fresh
                if prev.state == SessionState.WAITING:
                    prev.waiting_since = None
                    prev.notified_waiting = False

            prev.state = current_state

    def _send_state_notification(self, session: SessionInfo, state: SessionState):
        """Send notification for session state"""
        project = (
            os.path.basename(session.directory) if session.directory else "unknown"
        )

        if state == SessionState.WAITING:
            title = "⏳ OpenCode Waiting"
            message = f"[{project}] Waiting for your input"
            priority = 1  # High priority
            sound = "magic"

        elif state == SessionState.ERROR:
            title = "❌ OpenCode Error"
            message = f"[{project}] Session encountered an error"
            priority = 1
            sound = "siren"

        elif state == SessionState.IDLE:
            idle_min = int(self.idle_threshold.total_seconds() // 60)
            title = "😴 OpenCode Idle"
            message = f"[{project}] Session idle for >{idle_min} minutes"
            priority = 0
            sound = "none"

        else:
            return

        success = self.notifier.send_notification(
            message=message, title=title, priority=priority, sound=sound
        )

        if success:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Sent {state.value} notification for session {session.id}"
            )
        else:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Failed to send notification"
            )

    def run(self):
        """Main monitoring loop"""
        print(f"🔍 OpenCode Pushover Monitor Started")
        print(f"   Checking every {self.check_interval} seconds")
        print(f"   Idle threshold: {self.idle_threshold}")
        print(f"   Wait threshold: {self.wait_threshold}")
        print(f"   Max session age: {self.max_age_hours}h")
        print(f"   Press Ctrl+C to stop\n")

        # Seed state from current sessions without sending notifications.
        # For sessions already in WAITING, start the timer at ``now`` so
        # a notification fires after ``--wait-minutes`` if the session is
        # still waiting.  This avoids an immediate flood on startup while
        # still catching sessions that were genuinely stuck before the
        # monitor started.
        print("📋 Seeding initial session state...")
        sessions = self.get_sessions()
        now = time.time()
        for session in sessions:
            state = self._check_idle_status(session)
            ts = TrackedSession(state=state, last_updated_ms=session.updated_ms)
            if state == SessionState.WAITING:
                ts.waiting_since = now
                # notified_waiting stays False -- will fire after threshold
            elif state == SessionState.IDLE:
                ts.notified_idle = True
            elif state == SessionState.ERROR:
                ts.notified_error = True
            self.tracked[session.id] = ts
            project = (
                os.path.basename(session.directory) if session.directory else "unknown"
            )
            print(f"   [{project}] state={state.value}")
        print(f"   Tracking {len(sessions)} sessions (notifications start next poll)\n")

        try:
            while True:
                self.check_and_notify()
                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            print("\n👋 Monitor stopped")


def main():
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Monitor OpenCode sessions and send Pushover notifications"
    )
    parser.add_argument(
        "--app-token",
        required=True,
        help="Pushover application token (from pushover.net/apps/build)",
    )
    parser.add_argument(
        "--user-key", required=True, help="Pushover user key (from pushover.net)"
    )
    parser.add_argument(
        "--idle-minutes",
        type=int,
        default=10,
        help="Minutes before considering a session idle (default: 10)",
    )
    parser.add_argument(
        "--wait-minutes",
        type=int,
        default=2,
        help="Minutes a session must be waiting before sending a notification (default: 2)",
    )
    parser.add_argument(
        "--interval", type=int, default=30, help="Seconds between checks (default: 30)"
    )
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=24,
        help="Only monitor sessions updated within this many hours (default: 24)",
    )

    args = parser.parse_args()

    monitor = OpenCodeSessionMonitor(
        pushover_app_token=args.app_token,
        pushover_user_key=args.user_key,
        idle_threshold_minutes=args.idle_minutes,
        wait_threshold_minutes=args.wait_minutes,
        check_interval_seconds=args.interval,
        max_age_hours=args.max_age_hours,
    )

    monitor.run()


if __name__ == "__main__":
    main()
