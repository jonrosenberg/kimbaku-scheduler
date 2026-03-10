#!/usr/bin/env python3
"""GCalPoller — incremental sync of Google Calendar events for Telegram notifications."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default path where @cocal/google-calendar-mcp stores OAuth tokens
DEFAULT_TOKEN_PATH = os.path.expanduser("~/.config/google-calendar-mcp/tokens.json")
DEFAULT_SYNC_STATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "gcal_sync.json"
)


class GCalPoller:
    def __init__(
        self,
        oauth_creds_path: str,
        token_path: str = DEFAULT_TOKEN_PATH,
        sync_state_path: str = DEFAULT_SYNC_STATE_PATH,
        calendar_id: str = "primary",
    ):
        self.oauth_creds_path = oauth_creds_path
        self.token_path = token_path
        self.sync_state_path = sync_state_path
        self.calendar_id = calendar_id

    def _load_credentials(self):
        """Load OAuth credentials and build a google.oauth2.credentials.Credentials object."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        # Load client ID/secret from the Google Cloud Console JSON file
        with open(self.oauth_creds_path) as f:
            raw = json.load(f)

        # The file may be "installed" or "web" type
        client_info = raw.get("installed") or raw.get("web") or raw
        client_id = client_info["client_id"]
        client_secret = client_info["client_secret"]
        token_uri = client_info.get("token_uri", "https://oauth2.googleapis.com/token")

        # Load access/refresh tokens from the MCP server's token file
        with open(self.token_path) as f:
            tok = json.load(f)

        access_token = tok.get("access_token")
        refresh_token = tok.get("refresh_token")

        # Node.js stores expiry as epoch ms; convert to datetime
        expiry = None
        expiry_date = tok.get("expiry_date") or tok.get("expiryDate")
        if expiry_date:
            expiry = datetime.fromtimestamp(expiry_date / 1000, tz=timezone.utc)

        creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            expiry=expiry,
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Write refreshed token back so the MCP server can also use it
            tok["access_token"] = creds.token
            if creds.expiry:
                tok["expiry_date"] = int(creds.expiry.timestamp() * 1000)
            with open(self.token_path, "w") as f:
                json.dump(tok, f, indent=2)

        return creds

    def _load_sync_state(self) -> dict:
        try:
            with open(self.sync_state_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_sync_state(self, state: dict) -> None:
        Path(self.sync_state_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.sync_state_path, "w") as f:
            json.dump(state, f, indent=2)

    def poll(self) -> list[dict]:
        """
        Fetch new/updated Google Calendar events using incremental sync.

        First call: fetches all events from now forward, saves nextSyncToken.
        Subsequent calls: uses syncToken — GCal returns only changed events (very efficient).
        Returns list of new/updated events (cancelled events excluded).
        """
        from googleapiclient.discovery import build

        creds = self._load_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        state = self._load_sync_state()
        sync_token = state.get("nextSyncToken")

        new_events: list[dict] = []
        page_token = None

        try:
            while True:
                if sync_token and not page_token:
                    # Incremental sync — only changed events since last poll
                    kwargs = {
                        "calendarId": self.calendar_id,
                        "syncToken": sync_token,
                        "singleEvents": True,
                    }
                else:
                    # Full (initial) sync — events from now forward
                    kwargs = {
                        "calendarId": self.calendar_id,
                        "timeMin": datetime.now(timezone.utc).isoformat(),
                        "singleEvents": True,
                        "orderBy": "startTime",
                    }

                if page_token:
                    kwargs["pageToken"] = page_token

                result = service.events().list(**kwargs).execute()

                for event in result.get("items", []):
                    if event.get("status") != "cancelled":
                        new_events.append(event)

                page_token = result.get("nextPageToken")
                if not page_token:
                    # Save the sync token for next poll
                    new_sync_token = result.get("nextSyncToken")
                    if new_sync_token:
                        state["nextSyncToken"] = new_sync_token
                        self._save_sync_state(state)
                    break

        except Exception as exc:
            # 410 Gone means sync token expired — reset and do a full sync next time
            if "410" in str(exc) or "Gone" in str(exc):
                logger.warning("Sync token expired — resetting sync state for full re-sync")
                self._save_sync_state({})
            else:
                raise

        # On first poll, don't notify about existing events — just establish the baseline
        if not sync_token:
            logger.info("Initial GCal sync complete — %d existing events indexed", len(new_events))
            return []

        logger.info("GCal poll returned %d new/updated event(s)", len(new_events))
        return new_events

    def register_watch(self, webhook_url: str, token: str = "") -> dict:
        """Register a GCal push notification channel. Returns channel info."""
        import uuid
        from googleapiclient.discovery import build

        creds = self._load_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        channel_id = str(uuid.uuid4())
        body = {"id": channel_id, "type": "web_hook", "address": webhook_url}
        if token:
            body["token"] = token
        result = service.events().watch(calendarId=self.calendar_id, body=body).execute()
        state = self._load_sync_state()
        state["channel"] = {
            "id": result["id"],
            "resourceId": result["resourceId"],
            "expiration": int(result["expiration"]),  # Unix ms
        }
        self._save_sync_state(state)
        return result

    def stop_watch(self) -> None:
        """Unregister the active GCal push notification channel."""
        from googleapiclient.discovery import build

        state = self._load_sync_state()
        channel = state.get("channel")
        if not channel:
            return
        creds = self._load_credentials()
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        service.channels().stop(
            body={"id": channel["id"], "resourceId": channel["resourceId"]}
        ).execute()
        state.pop("channel", None)
        self._save_sync_state(state)

    def watch_expires_within(self, seconds: int) -> bool:
        """Returns True if the watch expires within `seconds` from now (or no watch exists)."""
        import time

        state = self._load_sync_state()
        expiration = state.get("channel", {}).get("expiration")
        if not expiration:
            return True  # No watch registered — needs one
        return (expiration / 1000 - time.time()) < seconds
