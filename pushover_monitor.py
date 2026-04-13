#!/usr/bin/env python3
"""
OpenCode Pushover Notifier
Monitors OpenCode sessions and sends iPhone notifications via Pushover
when sessions are idle, waiting for user input, or encounter errors.
"""

import subprocess
import json
import time
from datetime import datetime, timedelta
from typing import Dict, List, Set
import requests
from dataclasses import dataclass
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
    state: SessionState


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
        check_interval_seconds: int = 30,
        max_age_hours: int = 24,
    ):
        self.notifier = PushoverNotifier(pushover_app_token, pushover_user_key)
        self.idle_threshold = timedelta(minutes=idle_threshold_minutes)
        self.check_interval = check_interval_seconds
        self.max_age_hours = max_age_hours
        self.known_sessions: Dict[str, datetime] = {}
        self.last_states: Dict[str, SessionState] = {}

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

    def get_sessions(self) -> List[SessionInfo]:
        """Fetch the active OpenCode session for each running instance.

        Detects live ``opencode`` processes via ``ps``/``lsof``, then queries
        the SQLite database for the most recently updated session in each
        process's working directory.  This returns exactly one session per
        running OpenCode instance.

        Falls back to a recency-based query (``max_age_hours``) if process
        detection finds nothing (e.g. ``lsof`` unavailable).
        """
        try:
            active_dirs = self._get_active_directories()

            if active_dirs:
                # Build SQL to get the most recent session per active directory
                placeholders = ",".join(f"'{d}'" for d in active_dirs)
                query = (
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
                query = (
                    "SELECT id, title, time_updated, directory FROM session "
                    "WHERE time_archived IS NULL "
                    f"AND time_updated > {cutoff_ms} "
                    "ORDER BY time_updated DESC"
                )

            result = subprocess.run(
                ["opencode", "db", query, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                print(f"Error: opencode db query failed (rc={result.returncode})")
                return []

            data = json.loads(result.stdout)
            sessions: List[SessionInfo] = []
            for row in data:
                sessions.append(
                    SessionInfo(
                        id=row.get("id", "unknown"),
                        title=row.get("title", "Untitled"),
                        updated_ms=int(row.get("time_updated", 0)),
                        state=SessionState.UNKNOWN,
                    )
                )
            return sessions

        except Exception as e:
            print(f"Error fetching sessions: {e}")
            return []

    def _check_idle_status(self, session: SessionInfo) -> SessionState:
        """Determine session state from title keywords and idle time.

        Title keyword matching is checked first (waiting / error), then
        idle time is computed from the epoch-millisecond timestamp stored
        in the database.
        """
        title = session.title.lower()

        # Explicit state keywords in the title
        if "waiting" in title or "input" in title or "question" in title:
            return SessionState.WAITING
        if "error" in title or "failed" in title or "fail" in title:
            return SessionState.ERROR

        # Idle time check using epoch millis from the DB
        if session.updated_ms > 0:
            age = timedelta(milliseconds=time.time() * 1000 - session.updated_ms)
            if age > self.idle_threshold:
                return SessionState.IDLE

        return SessionState.ACTIVE

    def check_and_notify(self):
        """Check sessions and send notifications for state changes"""
        sessions = self.get_sessions()
        current_time = datetime.now()

        for session in sessions:
            session_id = session.id

            # Check for idle status
            current_state = self._check_idle_status(session)

            # Get previous state
            previous_state = self.last_states.get(session_id)

            # Send notification if state changed to a notable state
            if previous_state != current_state and current_state in [
                SessionState.WAITING,
                SessionState.ERROR,
                SessionState.IDLE,
            ]:
                self._send_state_notification(session, current_state)

            # Update tracking
            self.known_sessions[session_id] = current_time
            self.last_states[session_id] = current_state

    def _send_state_notification(self, session: SessionInfo, state: SessionState):
        """Send notification for session state"""
        if state == SessionState.WAITING:
            title = "⏳ OpenCode Waiting"
            message = f"Session '{session.title}' is waiting for your input"
            priority = 1  # High priority
            sound = "magic"

        elif state == SessionState.ERROR:
            title = "❌ OpenCode Error"
            message = f"Session '{session.title}' encountered an error"
            priority = 1
            sound = "siren"

        elif state == SessionState.IDLE:
            title = "😴 OpenCode Idle"
            message = f"Session '{session.title}' has been idle for >10 minutes"
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
        print(f"   Max session age: {self.max_age_hours}h")
        print(f"   Press Ctrl+C to stop\n")

        # Seed state from current sessions without sending notifications.
        # This prevents a flood of alerts for sessions that were already
        # idle/waiting/errored before the monitor started.
        print("📋 Seeding initial session state...")
        sessions = self.get_sessions()
        for session in sessions:
            current_state = self._check_idle_status(session)
            self.last_states[session.id] = current_state
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
        check_interval_seconds=args.interval,
        max_age_hours=args.max_age_hours,
    )

    monitor.run()


if __name__ == "__main__":
    main()
