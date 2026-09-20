"""Optional email channel, same simulated/live pattern as Stripe and Quo.

Uses Python's built-in smtplib -- no extra install. Works with any normal
SMTP provider (Gmail app password, Zoho, Fastmail, SendGrid's SMTP relay,
etc). Until SMTP settings are filled in on the Settings page, emails are
just logged instead of sent, so nothing breaks if you never use this.
"""
import smtplib
import ssl
from email.message import EmailMessage

from db import get_setting, execute


def is_configured():
    return bool(get_setting("smtp_host", "")) and bool(get_setting("owner_email", ""))


def send_email(subject, body, to_addr=None):
    to_addr = to_addr or get_setting("owner_email", "")
    if not to_addr:
        return {"skipped": "no owner_email configured"}

    if not is_configured():
        execute(
            "INSERT INTO call_events (from_number, event_type, raw_payload) "
            "VALUES (?, 'simulated_outbound_email', ?)",
            (to_addr, f"SUBJECT: {subject}\n{body}"),
        )
        return {"simulated": True, "to": to_addr, "subject": subject}

    host = get_setting("smtp_host")
    port = int(get_setting("smtp_port", "587"))
    user = get_setting("smtp_user", "")
    password = get_setting("smtp_password", "")
    from_addr = get_setting("smtp_from", user or to_addr)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=15) as server:
        server.starttls(context=context)
        if user:
            server.login(user, password)
        server.send_message(msg)
    return {"simulated": False, "to": to_addr}
