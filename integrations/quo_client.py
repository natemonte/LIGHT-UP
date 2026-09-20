"""Quo (formerly OpenPhone) integration: sending texts + reading webhook
events for missed calls / incoming messages.

WORKS IN TWO MODES, same pattern as stripe_client.py:
  1. "Simulated" mode (default, no API key set): send_text() just logs the
     message to the messages_log table instead of actually texting anyone,
     so you can test the whole missed-call -> auto-text -> owner-alert flow
     safely with fake data.
  2. "Live" mode: as soon as a real Quo API key is saved in Settings, texts
     actually send through Quo's API.

IMPORTANT HONESTY NOTE FOR NATE (and future me):
Quo rebranded from OpenPhone recently and is actively shipping a public API
(quo.com/docs). The exact base URL / auth header / webhook field names below
are my best-documented guess as of this build and are ISOLATED to this one
file on purpose. Before going live: log into your Quo dashboard -> Settings
-> API/Webhooks, grab the real API key + current base URL, and we do a
5-minute test together to confirm the webhook payload shape matches what
`parse_webhook_event()` expects below -- then adjust only this file if needed.
Nothing else in the app needs to change.
"""
import sqlite3

import requests

from db import get_setting, execute

QUO_API_BASE_DEFAULT = "https://api.openphone.com/v1"  # verify against quo.com/docs at go-live


def _api_key():
    return get_setting("quo_api_key", "")


def _api_base():
    return get_setting("quo_api_base", QUO_API_BASE_DEFAULT)


def is_live():
    return bool(_api_key())


def send_text(to_number, from_number, body):
    """Send an SMS. Falls back to a local log entry in simulated mode."""
    if not is_live():
        execute(
            "INSERT INTO call_events (from_number, event_type, raw_payload) "
            "VALUES (?, 'simulated_outbound_text', ?)",
            (to_number, f"FROM {from_number}: {body}"),
        )
        return {"simulated": True, "to": to_number, "body": body}

    resp = requests.post(
        f"{_api_base()}/messages",
        headers={"Authorization": _api_key(), "Content-Type": "application/json"},
        json={"to": [to_number], "from": from_number, "content": body},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def missed_call_reply_text():
    return get_setting(
        "missed_call_message",
        "Hey, sorry we missed your call! We're up on a roof right now and "
        "will call you back as soon as we're down. If you've got details "
        "about the job, feel free to text them here and we'll take a look.",
    )


def parse_webhook_event(payload):
    """
    Normalize an incoming Quo webhook payload into a simple dict:
      {type: 'missed_call' | 'incoming_text' | 'other',
       from_number, to_number, body}

    NOTE: field names here follow OpenPhone's documented webhook shape
    (event "type" + nested "data.object"). Confirm against a real payload
    from Nate's account before relying on this in production -- see the
    module docstring above.
    """
    event_type = payload.get("type", "")
    obj = payload.get("data", {}).get("object", {})

    if "call" in event_type and obj.get("status") in ("missed", "no-answer"):
        return {
            "type": "missed_call",
            "from_number": obj.get("from"),
            "to_number": obj.get("to"),
            "body": None,
        }
    if "message" in event_type and event_type.endswith("received"):
        return {
            "type": "incoming_text",
            "from_number": obj.get("from"),
            "to_number": obj.get("to"),
            "body": obj.get("body") or obj.get("text"),
        }
    return {"type": "other", "from_number": obj.get("from"), "to_number": obj.get("to"), "body": None}
