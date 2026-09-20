"""Minimal Stripe integration, talking straight to Stripe's REST API
(no `stripe` SDK needed -- it's just HTTPS + your secret key as the
Basic-auth username, per Stripe's own docs).

WORKS IN TWO MODES:
  1. "Simulated" mode (default, no real key set): nothing ever touches the
     internet. Checkout "sessions" are fake local records so you can click
     through the entire quote -> pay -> paid flow end to end and see it work.
  2. "Live test" mode: as soon as a real Stripe TEST secret key (starts with
     sk_test_...) is saved in Settings, every call below starts hitting the
     real Stripe sandbox and returns real Stripe Checkout links. No code
     changes needed to flip this on -- and nothing here will ever run with a
     live (sk_live_...) key without you explicitly saving one.

Massachusetts note: surcharging (charging MORE for card) is illegal here.
So instead we apply a small automatic DISCOUNT for paying by bank transfer
(ACH), which is legal and nets you the same effect. See ACH_DISCOUNT_PCT.
"""
import time
import uuid

import requests

from db import get_setting

STRIPE_API_BASE = "https://api.stripe.com/v1"
ACH_DISCOUNT_PCT = 0.03  # 3% off for paying by bank transfer instead of card


def _secret_key():
    return get_setting("stripe_secret_key", "")


def is_live():
    key = _secret_key()
    return bool(key) and key.startswith("sk_")


def _simulated_checkout(job, amount_cents, method):
    """Fake Stripe session used until a real key is configured."""
    fake_id = f"sim_{uuid.uuid4().hex[:16]}"
    return {
        "id": fake_id,
        "url": f"/pay/simulate/{job['id']}?session={fake_id}&method={method}",
        "amount_cents": amount_cents,
        "simulated": True,
    }


def create_checkout_session(job, method="card"):
    """
    Create a Stripe Checkout Session for a job's remaining balance.
    method: 'card' or 'ach' (bank transfer, gets the MA-legal discount).
    """
    balance = float(job["price_quoted"]) - float(job["amount_paid"] or 0)
    discount = balance * ACH_DISCOUNT_PCT if method == "ach" else 0
    amount = round(balance - discount, 2)
    amount_cents = int(round(amount * 100))

    if not is_live():
        return _simulated_checkout(job, amount_cents, method)

    payment_method_types = ["us_bank_account"] if method == "ach" else ["card"]
    payload = {
        "mode": "payment",
        "success_url": get_setting("public_base_url", "http://localhost:5000") + "/jobs/" + str(job["id"]) + "?paid=1",
        "cancel_url": get_setting("public_base_url", "http://localhost:5000") + "/jobs/" + str(job["id"]),
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][product_data][name]": job["title"],
        "line_items[0][price_data][unit_amount]": amount_cents,
        "line_items[0][quantity]": 1,
        "metadata[job_id]": job["id"],
    }
    for i, pmt in enumerate(payment_method_types):
        payload[f"payment_method_types[{i}]"] = pmt

    resp = requests.post(
        f"{STRIPE_API_BASE}/checkout/sessions",
        auth=(_secret_key(), ""),
        data=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return {"id": data["id"], "url": data["url"], "amount_cents": amount_cents, "simulated": False}


def mark_simulated_paid(job_id):
    """Used by the /pay/simulate route to pretend a payment succeeded."""
    return {"status": "paid", "job_id": job_id, "at": time.time()}
