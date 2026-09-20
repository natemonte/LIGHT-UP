"""Owner-facing alerts: "hey, you got a new lead" texts.

Kept separate from quo_client so the *channel* used for alerting you (right
now: a text to your own phone via Quo) can change later (e.g. add email)
without touching lead-capture logic anywhere else in the app.
"""
from db import get_setting, execute
from integrations import quo_client, email_client


def notify_owner_new_lead(lead):
    """lead is a sqlite3.Row (or dict) from the `leads` table.

    Texts you by default (that's the channel you actually check on a
    ladder). If you also fill in an owner_email + SMTP settings, you'll
    get an email too -- belt and suspenders. Neither failing blocks the
    other."""
    owner_phone = get_setting("owner_phone", "")

    source_label = {
        "missed_call": "a missed call",
        "website": "your website",
        "facebook_ad": "a Facebook ad",
        "referral": "a referral",
    }.get(lead["source"], lead["source"])

    name = lead["name"] or "Unknown"
    phone = lead["phone"] or "no phone given"
    body = f"New lead! {name} ({phone}) via {source_label}."
    if lead["message"]:
        body += f' Msg: "{lead["message"][:100]}"'

    results = {}

    if owner_phone:
        results["text"] = quo_client.send_text(to_number=owner_phone, from_number=owner_phone, body=body)
    else:
        results["text"] = {"skipped": "no owner_phone configured in Settings"}

    if get_setting("owner_email", ""):
        try:
            results["email"] = email_client.send_email(subject="New lead!", body=body)
        except Exception as e:  # never let an email hiccup block the text alert above
            results["email"] = {"error": str(e)}

    execute("UPDATE leads SET notified = 1 WHERE id = ?", (lead["id"],))
    return results
