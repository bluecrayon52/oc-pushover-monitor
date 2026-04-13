#!/usr/bin/env python3
"""
OpenCode Pushover Notifier
Monitors OpenCode sessions and sends iPhone notifications via Pushover
when sessions are idle, waiting for user input, or encounter errors.
"""

import subprocess
import json
import time
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List
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
    updated: str
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
        sound: str = "pushover"
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
                    "timestamp": int(time.time())
                },
                timeout=10
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
        check_interval_seconds: int = 30
    ):
        self.notifier = PushoverNotifier(pushover_app_token, pushover_user_key)
        self.idle_threshold = timedelta(minutes=idle_threshold_minutes)
        self.check_interval = check_interval_seconds
        self.known_sessions: Dict[str, datetime] = {}
        self.last_states: Dict[str, SessionState] = {}
    
    def get_sessions(self) -> List[SessionInfo]:
        """Fetch current OpenCode sessions"""
        try:
            # Try JSON format first
            result = subprocess.run(
                ["opencode", "session", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                # Parse JSON output
                data = json.loads(result.stdout)
                sessions = []
                for session_data in data:
                    session = SessionInfo(
                        id=session_data.get("id", "unknown"),
                        title=session_data.get("title", "Untitled"),
                        updated=session_data.get("updated", ""),
                        state=self._determine_state(session_data)
                    )
                    sessions.append(session)
                return sessions
            else:
                # JSON failed, run again without --json flag
                result = subprocess.run(
                    ["opencode", "session", "list"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    return self._parse_text_output(result.stdout)
                else:
                    print(f"Error: opencode session list failed with code {result.returncode}")
                    return []
            
        except Exception as e:
            print(f"Error fetching sessions: {e}")
            return []
    
    def _parse_text_output(self, output: str) -> List[SessionInfo]:
        """Parse text output from opencode session list"""
        sessions = []
        lines = output.strip().split("\n")
        
        # Skip header lines
        for line in lines[2:]:  # Skip header and separator
            if not line.strip() or line.startswith("-"):
                continue
            
            # Fixed-width column parsing based on actual output format
            # Format: Session ID (30 chars) | Title (78 chars) | Updated (rest)
            if len(line) >= 32:
                session_id = line[0:30].strip()
                title = line[32:110].strip()  # Title column
                updated = line[111:].strip() if len(line) > 111 else ""
                
                if session_id and title:
                    sessions.append(SessionInfo(
                        id=session_id,
                        title=title,
                        updated=updated,
                        state=SessionState.UNKNOWN
                    ))
        
        return sessions
    
    def _determine_state(self, session_data: dict) -> SessionState:
        """Determine session state from data"""
        # This is a heuristic - you might need to adjust based on actual opencode output
        title = session_data.get("title", "").lower()
        
        if "waiting" in title or "input" in title or "question" in title:
            return SessionState.WAITING
        elif "error" in title or "failed" in title:
            return SessionState.ERROR
        elif "idle" in title:
            return SessionState.IDLE
        
        return SessionState.ACTIVE
    
    def _check_idle_status(self, session: SessionInfo) -> SessionState:
        """Check session state from title keywords and idle time"""
        title = session.title.lower()
        
        # First check for explicit state keywords in title
        if "waiting" in title or "input" in title or "question" in title:
            return SessionState.WAITING
        elif "error" in title or "failed" in title or "fail" in title:
            return SessionState.ERROR
        
        # Then check for idle time
        try:
            updated_str = session.updated
            formats = ["%I:%M %p", "%H:%M", "%Y-%m-%d %H:%M:%S"]
            
            updated_time = None
            for fmt in formats:
                try:
                    updated_time = datetime.strptime(updated_str, fmt)
                    break
                except ValueError:
                    continue
            
            if updated_time:
                if updated_time.year == 1900:
                    updated_time = datetime.now().replace(
                        hour=updated_time.hour,
                        minute=updated_time.minute,
                        second=updated_time.second
                    )
                
                if datetime.now() - updated_time > self.idle_threshold:
                    return SessionState.IDLE
                    
        except Exception:
            pass
        
        # Return original state if set, otherwise ACTIVE
        return session.state if session.state != SessionState.UNKNOWN else SessionState.ACTIVE
    
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
                SessionState.WAITING, SessionState.ERROR, SessionState.IDLE
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
            message=message,
            title=title,
            priority=priority,
            sound=sound
        )
        
        if success:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Sent {state.value} notification for session {session.id}")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] Failed to send notification")
    
    def run(self):
        """Main monitoring loop"""
        print(f"🔍 OpenCode Pushover Monitor Started")
        print(f"   Checking every {self.check_interval} seconds")
        print(f"   Idle threshold: {self.idle_threshold}")
        print(f"   Press Ctrl+C to stop\n")
        
        # On startup, check for any currently waiting/error sessions and notify
        print("📋 Checking current sessions on startup...")
        sessions = self.get_sessions()
        for session in sessions:
            current_state = self._check_idle_status(session)
            if current_state in [SessionState.WAITING, SessionState.ERROR]:
                print(f"   Found {current_state.value} session: {session.title[:50]}...")
                self._send_state_notification(session, current_state)
                self.last_states[session.id] = current_state
        print(f"   Found {len(sessions)} sessions\n")
        
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
        help="Pushover application token (from pushover.net/apps/build)"
    )
    parser.add_argument(
        "--user-key",
        required=True,
        help="Pushover user key (from pushover.net)"
    )
    parser.add_argument(
        "--idle-minutes",
        type=int,
        default=10,
        help="Minutes before considering a session idle (default: 10)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Seconds between checks (default: 30)"
    )
    
    args = parser.parse_args()
    
    monitor = OpenCodeSessionMonitor(
        pushover_app_token=args.app_token,
        pushover_user_key=args.user_key,
        idle_threshold_minutes=args.idle_minutes,
        check_interval_seconds=args.interval
    )
    
    monitor.run()


if __name__ == "__main__":
    main()
