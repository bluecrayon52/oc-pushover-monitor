#!/usr/bin/env python3
"""
OpenCode Pushover Notifier
Monitors OpenCode (v2) sessions and sends iPhone notifications via Pushover
when sessions are idle, waiting for user input, or encounter errors.
"""

import os
import re
import subprocess
import json
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote
import requests
from dataclasses import dataclass
from enum import Enum

# macOS Keychain service name used to store Pushover credentials.
KEYCHAIN_SERVICE = "oc-pushover-monitor"
KEYCHAIN_ACCOUNT_APP_TOKEN = "app-token"
KEYCHAIN_ACCOUNT_USER_KEY = "user-key"


def read_keychain_secret(account: str, service: str = KEYCHAIN_SERVICE) -> str:
    """Return a generic password from the macOS login Keychain, or "".

    Uses the ``security`` CLI so no third-party dependency is needed.  The
    secret is read from stdout and never appears in any process's argv.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def resolve_credential(
    cli_value: Optional[str], env_var: str, keychain_account: str
) -> Tuple[str, str]:
    """Resolve a credential: CLI flag > environment variable > macOS Keychain.

    Returns ``(value, source)`` where ``source`` describes where the value
    came from (``""`` when not found).
    """
    if cli_value:
        return cli_value, "command line"
    env_value = os.environ.get(env_var, "")
    if env_value:
        return env_value, f"${env_var}"
    keychain_value = read_keychain_secret(keychain_account)
    if keychain_value:
        return keychain_value, "Keychain"
    return "", ""


class SessionState(Enum):
    ACTIVE = "active"
    IDLE = "idle"
    WAITING = "waiting"
    ERROR = "error"
    STALLED = "stalled"
    UNKNOWN = "unknown"


@dataclass
class SessionInfo:
    id: str
    title: str
    updated_ms: int  # epoch milliseconds (session time.updated)
    directory: str  # absolute path of the session's working directory
    last_message_role: str  # "assistant", "user", or "" if unknown
    last_message_finish: str  # "stop", "tool-calls", or "" if unknown/null
    last_message_error: str  # v2 error type e.g. "provider.error", or ""
    last_message_text: str  # text of last text part in the latest message
    has_pending_question: bool  # True if a question/form/permission awaits input
    state: SessionState
    is_idle: bool = False  # True once OpenCode has recorded the turn as ended

    # OpenCode v2 error types that indicate a real failure worth notifying
    # about.  "aborted" is excluded because it's user-initiated (cancel).
    NOTIFIABLE_ERRORS = frozenset(
        {"provider.error", "provider.content-filter", "unknown", "output-limit"}
    )

    # Message types that describe the conversation turn.  Bookkeeping
    # messages (system, agent/model/location switches) are ignored.
    TURN_MESSAGE_TYPES = frozenset(
        {"user", "assistant", "idle", "shell", "synthetic", "skill", "compaction"}
    )


@dataclass
class TrackedSession:
    """Per-session tracking state for the monitor."""

    state: SessionState
    last_updated_ms: int = 0  # session's time_updated from last poll
    waiting_since: Optional[float] = None  # time.time() when WAITING started
    idle_since: Optional[float] = None  # time.time() when IDLE started
    notified_waiting: bool = False  # already sent a notification for this wait?
    notified_idle: bool = False  # already sent a notification for this idle period?
    notified_error: bool = False  # already sent a notification for this error?
    notified_stalled: bool = False  # already sent a notification for this stall?


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
        stall_threshold_minutes: int = 10,
        check_interval_seconds: int = 30,
        max_age_hours: int = 24,
    ):
        self.notifier = PushoverNotifier(pushover_app_token, pushover_user_key)
        self.idle_threshold = timedelta(minutes=idle_threshold_minutes)
        self.wait_threshold = timedelta(minutes=wait_threshold_minutes)
        self.stall_threshold = timedelta(minutes=stall_threshold_minutes)
        self.check_interval = check_interval_seconds
        self.max_age_hours = max_age_hours
        self.tracked: Dict[str, TrackedSession] = {}

    # Recent top-level sessions to fetch per poll.  The v2 API returns
    # sessions across all projects, newest first.
    session_limit = 50
    # Messages to scan from the end when looking for the latest turn
    # message (skips trailing system/agent-switch bookkeeping messages).
    MESSAGE_PAGE_SIZE = 5
    # ``opencode`` subcommands that are not interactive clients.
    NON_CLIENT_SUBCOMMANDS = frozenset(
        {
            "serve", "service", "api", "acp", "debug", "auth", "mcp", "plugin",
            "models", "stats", "run", "session", "reload", "pair", "upgrade",
            "update", "uninstall",
        }
    )  # fmt: skip

    @classmethod
    def _is_opencode_client(cls, command: str) -> bool:
        """True if a ``ps`` command line is an interactive opencode client."""
        argv = command.split()
        if not argv or os.path.basename(argv[0]) != "opencode":
            return False
        return len(argv) == 1 or argv[1] not in cls.NON_CLIENT_SUBCOMMANDS

    def _get_active_directories(self) -> Set[str]:
        """Return working directories of running ``opencode`` client processes.

        Uses ``ps`` to find opencode client PIDs (excluding the background
        server and one-shot subcommands such as ``opencode api``), then
        ``lsof -d cwd`` to resolve each process's working directory.  Works
        on macOS and Linux.
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
                if len(parts) == 2 and self._is_opencode_client(parts[1]):
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

    def _api_get(self, path: str) -> Optional[dict]:
        """GET a path from the OpenCode v2 server via ``opencode api``.

        ``opencode api`` handles service discovery and authentication, so
        the monitor never needs the server URL or credentials.  Query
        parameters must be embedded in ``path`` (``--param`` only applies
        to OpenAPI path parameters).  Returns ``None`` on any failure.
        """
        try:
            result = subprocess.run(
                ["opencode", "api", "get", path],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"Error: opencode api request failed: {e}")
            return None
        if result.returncode != 0:
            print(f"Error: opencode api get {path} failed (rc={result.returncode})")
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            print(f"Error: opencode api get {path} returned invalid JSON")
            return None

    def _list_root_sessions(self) -> List[dict]:
        """Return recent top-level sessions (subagent sessions excluded)."""
        resp = self._api_get(f"/api/session?parentID=null&limit={self.session_limit}")
        return (resp or {}).get("data", [])

    def _pending_input_session_ids(self, directories: Set[str]) -> Set[str]:
        """Session IDs with a pending form or permission request.

        Both endpoints are location-scoped, so query each directory.
        """
        ids: Set[str] = set()
        for directory in directories:
            loc = quote(directory, safe="")
            for path in ("/api/form", "/api/permission/request"):
                resp = self._api_get(f"{path}?location[directory]={loc}")
                for item in (resp or {}).get("data", []):
                    if item.get("sessionID"):
                        ids.add(item["sessionID"])
        return ids

    def _latest_turn_message(self, session_id: str) -> dict:
        """Return the newest message that describes the conversation turn."""
        resp = self._api_get(
            f"/api/session/{session_id}/message?limit={self.MESSAGE_PAGE_SIZE}&order=desc"
        )
        for msg in (resp or {}).get("data", []):
            if msg.get("type") in SessionInfo.TURN_MESSAGE_TYPES:
                return msg
        return {}

    @staticmethod
    def _session_from_api(
        session: dict, latest: dict, pending_input: bool
    ) -> SessionInfo:
        """Map a v2 ``Session.Info`` and its latest message onto ``SessionInfo``.

        An ``idle`` message marks the end of a turn; its outcome/error
        override the preceding assistant message's ``finish``.
        """
        msg_type = latest.get("type", "")
        finish = ""
        error = ""
        text = ""
        role = ""
        is_idle = False

        if msg_type == "idle":
            is_idle = True
            role = "assistant"
            outcome = latest.get("outcome", "")
            error = (latest.get("error") or {}).get("type", "")
            if outcome == "succeeded":
                finish = "stop"
        elif msg_type == "assistant":
            role = "assistant"
            finish = latest.get("finish") or ""
            error = (latest.get("error") or {}).get("type", "")
            content = latest.get("content") or []
            for part in content:
                if (
                    part.get("type") == "tool"
                    and part.get("name") == "question"
                    and (part.get("state") or {}).get("status")
                    in ("streaming", "running")
                ):
                    pending_input = True
        elif msg_type:
            role = "user"

        return SessionInfo(
            id=session.get("id", "unknown"),
            title=session.get("title", "Untitled"),
            updated_ms=int((session.get("time") or {}).get("updated", 0)),
            directory=(session.get("location") or {}).get("directory", ""),
            last_message_role=role,
            last_message_finish=finish,
            last_message_error=error,
            last_message_text=text,
            has_pending_question=pending_input,
            state=SessionState.UNKNOWN,
            is_idle=is_idle,
        )

    def get_sessions(self) -> List[SessionInfo]:
        """Fetch the current session for each running OpenCode instance.

        Lists recent top-level sessions from the OpenCode v2 server, then
        keeps the most recently updated one per working directory of a live
        ``opencode`` client process.  Falls back to every session updated
        within ``max_age_hours`` if process detection finds nothing.

        For each kept session, fetches the latest turn message plus any
        pending forms/permission requests to classify its state.
        """
        try:
            all_sessions = self._list_root_sessions()
            if not all_sessions:
                return []

            active_dirs = self._get_active_directories()
            selected: List[dict] = []
            if active_dirs:
                latest_by_dir: Dict[str, dict] = {}
                for s in all_sessions:
                    if (s.get("time") or {}).get("archived"):
                        continue
                    d = (s.get("location") or {}).get("directory", "")
                    if d not in active_dirs:
                        continue
                    cur = latest_by_dir.get(d)
                    if cur is None or s["time"]["updated"] > cur["time"]["updated"]:
                        latest_by_dir[d] = s
                selected = list(latest_by_dir.values())
            else:
                cutoff_ms = int((time.time() - self.max_age_hours * 3600) * 1000)
                selected = [
                    s
                    for s in all_sessions
                    if not (s.get("time") or {}).get("archived")
                    and (s.get("time") or {}).get("updated", 0) > cutoff_ms
                ]

            selected.sort(key=lambda s: s["time"]["updated"], reverse=True)
            dirs = {(s.get("location") or {}).get("directory", "") for s in selected}
            pending_ids = self._pending_input_session_ids({d for d in dirs if d})

            sessions: List[SessionInfo] = []
            for s in selected:
                latest = self._latest_turn_message(s["id"])
                info = self._session_from_api(s, latest, s["id"] in pending_ids)
                if latest.get("type") in ("assistant", "idle"):
                    info.last_message_text = self._last_assistant_text(s["id"], latest)
                sessions.append(info)
            return sessions

        except Exception as e:
            print(f"Error fetching sessions: {e}")
            return []

    def _last_assistant_text(self, session_id: str, latest: dict) -> str:
        """Return the last text block of the most recent assistant message.

        When the latest message is an ``idle`` marker, look back for the
        assistant message that precedes it.
        """
        msg = latest
        if latest.get("type") != "assistant":
            resp = self._api_get(
                f"/api/session/{session_id}/message?limit=1&order=desc&type=assistant"
            )
            data = (resp or {}).get("data", [])
            msg = data[0] if data else {}
        texts = [
            p.get("text", "")
            for p in (msg.get("content") or [])
            if p.get("type") == "text"
        ]
        return texts[-1] if texts else ""

    @staticmethod
    def _has_prose_question(text: str) -> bool:
        """Check if the last paragraph of text contains a question mark.

        Strips fenced code blocks (``` ... ```) before checking so that
        question marks inside code samples don't cause false positives.
        Splits on blank lines and checks the last non-empty paragraph.
        """
        if not text:
            return False
        # Strip fenced code blocks
        stripped = re.sub(r"```[\s\S]*?```", "", text)
        # Split into paragraphs on blank lines
        paragraphs = [p.strip() for p in stripped.split("\n\n") if p.strip()]
        if not paragraphs:
            return False
        return "?" in paragraphs[-1]

    def _check_idle_status(self, session: SessionInfo) -> SessionState:
        """Determine session state from the latest message and idle time.

        Detection logic (checked in order):

        1. **Waiting** -- a ``question`` tool call is pending/running, OR
           the assistant finished (``finish = 'stop'``) and the last
           paragraph of its text contains a ``?`` (prose question).
        2. **Error** -- the latest assistant/idle message has a notifiable
           v2 error type (``provider.error``, ``unknown``, ...).
           User-initiated aborts (``aborted``) are excluded.
        3. **Idle** -- ``finish = 'stop'`` with no prose question: the
           assistant completed its turn without asking anything.
        4. **Stalled** -- ``finish`` is ``tool-calls`` or ``null`` (no
           error) and ``time_updated`` exceeds the stall threshold:
           the assistant was working but stopped making progress.
        5. **Active** -- none of the above matched.
        """
        # 1. Waiting: question tool blocking, or assistant asked a prose
        #    question (finish=stop with '?' in last paragraph)
        if session.has_pending_question:
            return SessionState.WAITING
        if (
            session.last_message_role == "assistant"
            and session.last_message_finish == "stop"
        ):
            if self._has_prose_question(session.last_message_text):
                return SessionState.WAITING
            # finish=stop with no prose question -> idle (step 3)
            return SessionState.IDLE

        # 2. Error: latest assistant message has a notifiable error
        if session.last_message_error in SessionInfo.NOTIFIABLE_ERRORS:
            return SessionState.ERROR

        # A turn that OpenCode marked idle (e.g. interrupted by the user)
        # is not in flight, so it can't be stalled.
        if session.is_idle:
            return SessionState.ACTIVE

        # 4. Stalled: assistant was working but time_updated is stale
        if session.updated_ms > 0:
            age = timedelta(milliseconds=time.time() * 1000 - session.updated_ms)
            if age > self.stall_threshold:
                return SessionState.STALLED

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
                elif current_state == SessionState.IDLE:
                    ts.idle_since = now
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
                if prev.state != SessionState.IDLE or activity:
                    # Just entered IDLE, or the session had activity while
                    # we kept seeing IDLE.  Start a fresh timer.
                    prev.idle_since = now
                    prev.notified_idle = False
                elif prev.idle_since is None:
                    prev.idle_since = now

                # Check if we've been idle long enough to notify
                elapsed = now - prev.idle_since
                if (
                    elapsed >= self.idle_threshold.total_seconds()
                    and not prev.notified_idle
                ):
                    self._send_state_notification(session, current_state)
                    prev.notified_idle = True

            elif current_state == SessionState.ERROR:
                if prev.state != SessionState.ERROR:
                    prev.notified_error = False
                if not prev.notified_error:
                    self._send_state_notification(session, current_state)
                    prev.notified_error = True

            elif current_state == SessionState.STALLED:
                if prev.state != SessionState.STALLED:
                    prev.notified_stalled = False
                if not prev.notified_stalled:
                    self._send_state_notification(session, current_state)
                    prev.notified_stalled = True

            else:
                # ACTIVE -- reset timers so the next WAITING/IDLE period
                # starts fresh
                if prev.state == SessionState.WAITING:
                    prev.waiting_since = None
                    prev.notified_waiting = False
                if prev.state == SessionState.IDLE:
                    prev.idle_since = None
                    prev.notified_idle = False

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
            error_name = session.last_message_error or "Unknown error"
            message = f"[{project}] {error_name}"
            priority = 1
            sound = "siren"

        elif state == SessionState.IDLE:
            title = "😴 OpenCode Idle"
            message = f"[{project}] Assistant finished"
            priority = 0
            sound = "none"

        elif state == SessionState.STALLED:
            stall_min = int(self.stall_threshold.total_seconds() // 60)
            title = "⚠️ OpenCode Stalled"
            message = f"[{project}] No progress for >{stall_min} minutes"
            priority = 1
            sound = "siren"

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
        print("🔍 OpenCode Pushover Monitor Started")
        print(f"   Checking every {self.check_interval} seconds")
        print(f"   Wait threshold: {self.wait_threshold}")
        print(f"   Stall threshold: {self.stall_threshold}")
        print(f"   Max session age: {self.max_age_hours}h")
        print("   Press Ctrl+C to stop\n")

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
                ts.idle_since = now
                # notified_idle stays False -- will fire after threshold
            elif state == SessionState.ERROR:
                ts.notified_error = True
            elif state == SessionState.STALLED:
                ts.notified_stalled = True
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


def setup_keychain() -> int:
    """Prompt for Pushover credentials and store them in the macOS Keychain.

    Secrets are passed to ``security`` on stdin (interactive mode) so they
    never appear in process arguments visible to ``ps``.
    """
    import getpass

    if sys.platform != "darwin":
        print("Error: --setup-keychain requires macOS.", file=sys.stderr)
        return 1

    for account, label in (
        (KEYCHAIN_ACCOUNT_APP_TOKEN, "Pushover app token"),
        (KEYCHAIN_ACCOUNT_USER_KEY, "Pushover user key"),
    ):
        value = getpass.getpass(f"{label}: ").strip()
        # Pushover tokens/keys are 30 alphanumeric characters.  Rejecting
        # anything else also keeps the value safe to quote for ``security``.
        if not re.fullmatch(r"[A-Za-z0-9]{20,64}", value):
            print(f"Error: {label} doesn't look valid.", file=sys.stderr)
            return 1
        cmd = (
            f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" '
            f'-a "{account}" -l "{KEYCHAIN_SERVICE} {account}" -w "{value}"\n'
        )
        result = subprocess.run(
            ["security", "-i"], input=cmd, capture_output=True, text=True
        )
        if result.returncode != 0 or read_keychain_secret(account) != value:
            print(f"Error: failed to store {label} in Keychain.", file=sys.stderr)
            return 1
        print(f"✓ Stored {label} (service={KEYCHAIN_SERVICE}, account={account})")
    return 0


def main():
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Monitor OpenCode sessions and send Pushover notifications"
    )
    parser.add_argument(
        "--app-token",
        help="Pushover application token. Prefer the Keychain (see --setup-keychain); "
        "falls back to $PUSHOVER_APP_TOKEN, then the Keychain.",
    )
    parser.add_argument(
        "--user-key",
        help="Pushover user key. Falls back to $PUSHOVER_USER_KEY, then the Keychain.",
    )
    parser.add_argument(
        "--setup-keychain",
        action="store_true",
        help="Interactively store Pushover credentials in the macOS Keychain and exit",
    )
    parser.add_argument(
        "--idle-minutes",
        type=int,
        default=10,
        help="Minutes before notifying that a completed session is idle (default: 10)",
    )
    parser.add_argument(
        "--wait-minutes",
        type=int,
        default=2,
        help="Minutes a session must be waiting before sending a notification (default: 2)",
    )
    parser.add_argument(
        "--stall-minutes",
        type=int,
        default=10,
        help="Minutes of no progress before considering a session stalled (default: 10)",
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

    if args.setup_keychain:
        sys.exit(setup_keychain())

    app_token, app_src = resolve_credential(
        args.app_token, "PUSHOVER_APP_TOKEN", KEYCHAIN_ACCOUNT_APP_TOKEN
    )
    user_key, user_src = resolve_credential(
        args.user_key, "PUSHOVER_USER_KEY", KEYCHAIN_ACCOUNT_USER_KEY
    )
    if not app_token or not user_key:
        print("Error: Pushover credentials not found.", file=sys.stderr)
        print(
            f"Store them in the Keychain with:\n  {sys.argv[0]} --setup-keychain",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"🔑 Credentials: app token from {app_src}, user key from {user_src}")

    monitor = OpenCodeSessionMonitor(
        pushover_app_token=app_token,
        pushover_user_key=user_key,
        idle_threshold_minutes=args.idle_minutes,
        wait_threshold_minutes=args.wait_minutes,
        stall_threshold_minutes=args.stall_minutes,
        check_interval_seconds=args.interval,
        max_age_hours=args.max_age_hours,
    )

    monitor.run()


if __name__ == "__main__":
    main()
