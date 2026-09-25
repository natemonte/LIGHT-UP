#!/usr/bin/env python3
"""End-to-end smoke test for Lights CRM.

Runs entirely against a throwaway temporary database (never touches your
real data/crm.db), spins up the app in-process with Flask's test client,
and clicks through the core flows: setup, customers, quotes, the
customer-facing pay link, CSV exports, the schedule/jobs pages, and lead
conversion. It also checks that CSRF protection actually blocks a forged
request.

Run this after making any code change, before trusting it:

    python3 scripts/smoke_test.py

Exits non-zero (and prints exactly what failed) if anything's broken.
Intentionally has zero dependencies beyond what the app itself needs.
"""
import os
import re
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# Point the DB at a throwaway temp file BEFORE importing app/db.
_tmp_dir = tempfile.mkdtemp(prefix="lights_crm_smoketest_")
import db  # noqa: E402
db.DB_PATH = os.path.join(_tmp_dir, "test.db")

import app as appmodule  # noqa: E402

failures = []
passed = 0


def check(label, condition):
    global passed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}")


def extract_csrf(html_bytes):
    m = re.search(rb'name="csrf_token" value="([a-f0-9]+)"', html_bytes)
    return m.group(1).decode() if m else None


def extract_quote_token(html_bytes):
    m = re.search(rb'/q/([a-f0-9]{32})', html_bytes)
    return m.group(1).decode() if m else None


def main():
    db.init_db()
    appmodule.app.config["TESTING"] = True
    client = appmodule.app.test_client()

    print("Setup + auth")
    r = client.get("/setup")
    check("GET /setup is 200", r.status_code == 200)
    token = extract_csrf(r.data)
    check("setup page has a csrf token", token is not None)

    r = client.post("/setup", data={
        "email": "test@example.com", "password": "testpass123",
        "owner_phone": "+15551112222", "csrf_token": token,
    })
    check("POST /setup redirects (account created)", r.status_code == 302)

    print("CSRF protection")
    r = client.post("/numbers", data={"number": "+15559990000", "label": "X"})
    check("POST without csrf_token is rejected (400)", r.status_code == 400)
    r = client.post("/numbers", data={"number": "+15559990000", "label": "X", "csrf_token": "wrong"})
    check("POST with wrong csrf_token is rejected (400)", r.status_code == 400)

    print("Numbers + customers + jobs")
    r = client.get("/numbers")
    token = extract_csrf(r.data)
    r = client.post("/numbers", data={
        "number": "+15559990001", "label": "Blue Yard Sign", "campaign": "West side",
        "monthly_cost": "25", "csrf_token": token,
    })
    check("add phone number redirects", r.status_code == 302)

    r = client.get("/customers/new")
    token = extract_csrf(r.data)
    r = client.post("/customers/new", data={
        "first_name": "Jane", "last_name": "Doe", "phone": "+15551230000",
        "lead_source": "missed_call", "csrf_token": token,
    })
    check("add customer redirects", r.status_code == 302)
    customer_url = r.headers.get("Location", "")
    check("redirected to a customer detail page", "/customers/" in customer_url)

    r = client.get("/customers/1/jobs/new")
    token = extract_csrf(r.data)
    r = client.post("/customers/1/jobs/new", data={
        "title": "Roofline Lights", "materials_cost": "200", "labor_cost": "150",
        "price_quoted": "800", "csrf_token": token,
    })
    check("create quote redirects", r.status_code == 302)

    r = client.get("/jobs/1")
    check("job detail page loads", r.status_code == 200)
    check("job detail shows margin", b"$450.00" in r.data)
    quote_token = extract_quote_token(r.data)
    check("job has a public quote token", quote_token is not None)

    print("Manual payment recording (Venmo/cash/check)")
    r = client.get("/settings")
    token = extract_csrf(r.data)
    r = client.post("/settings", data={"venmo_handle": "@test-business", "csrf_token": token})
    check("saving venmo handle redirects", r.status_code == 302)
    r = client.get(f"/q/{quote_token}")
    check("public quote shows the venmo handle", b"@test-business" in r.data)
    r = client.get("/jobs/1")
    token = extract_csrf(r.data)
    r = client.post("/jobs/1/record_payment", data={
        "method": "venmo", "amount": "300", "note": "test venmo payment", "csrf_token": token,
    })
    check("recording a venmo payment redirects", r.status_code == 302)
    r = client.get("/jobs/1")
    check("payment history shows the venmo payment", b"venmo" in r.data and b"$300.00" in r.data)
    check("balance due dropped after the venmo payment", b"$500.00" in r.data)

    print("Install/takedown scheduling")
    from datetime import date, timedelta
    install_date = (date.today() + timedelta(days=5)).isoformat()
    takedown_date = (date.today() + timedelta(days=10)).isoformat()
    r = client.get("/jobs/1/edit")
    token = extract_csrf(r.data)
    r = client.post("/jobs/1/edit", data={
        "title": "Roofline Lights", "materials_cost": "200", "labor_cost": "150",
        "price_quoted": "800", "scheduled_date": install_date, "takedown_date": takedown_date,
        "csrf_token": token,
    })
    check("saving install/takedown dates redirects", r.status_code == 302)
    r = client.get("/jobs/1")
    check("job detail shows takedown date", takedown_date.encode() in r.data)
    r = client.get("/schedule?days=30")
    check("schedule page loads with days param", r.status_code == 200)
    check("schedule shows the install badge", b"badge-install" in r.data)
    check("schedule shows the takedown badge", b"badge-takedown" in r.data)
    token = extract_csrf(r.data)
    check("schedule page has a csrf token for the mark-done form", token is not None)
    r = client.post("/jobs/1/takedown_done", data={"csrf_token": token})
    check("marking takedown done redirects", r.status_code == 302)
    r = client.get("/schedule?days=30")
    check("completed takedown drops off the schedule", b"badge-takedown" not in r.data)

    print("Customer-facing quote link (no login)")
    anon_client = appmodule.app.test_client()  # separate client = no session cookie
    r = anon_client.get(f"/q/{quote_token}")
    check("public quote page loads without login", r.status_code == 200)
    check("shows balance due", b"800.00" in r.data)

    r = anon_client.post(f"/q/{quote_token}/pay", data={"method": "ach"})
    check("public pay kicks off simulated checkout", r.status_code == 302)
    sim_path = r.headers.get("Location", "").split("?")[0]  # drop ?session=...&method=... query string
    r = anon_client.post(sim_path + "/confirm", data={"method": "ach"})
    check("simulated payment confirms", r.status_code == 302)
    r = anon_client.get(f"/q/{quote_token}")
    check("quote now shows paid in full", b"paid in full" in r.data)

    print("Missed call -> auto-text -> lead -> owner alert")
    r = client.get("/calls")
    token = extract_csrf(r.data)
    r = client.post("/calls/simulate", data={
        "phone_number_id": "1", "fake_caller": "+15559998888", "csrf_token": token,
    })
    check("simulate missed call redirects", r.status_code == 302)
    r = client.get("/calls")
    check("call log shows the missed call", b"missed_call" in r.data)
    r = client.get("/leads")
    check("a lead was created from the missed call", b"+15559998888" in r.data)

    print("Lead conversion")
    r = client.get("/leads")
    token = extract_csrf(r.data)
    lead_id_match = re.search(rb'/leads/(\d+)/convert', r.data)
    check("a convertible lead exists", lead_id_match is not None)
    if lead_id_match:
        lead_id = lead_id_match.group(1).decode()
        r = client.post(f"/leads/{lead_id}/convert", data={"csrf_token": token})
        check("convert lead to customer redirects", r.status_code == 302)

    print("Email alerts (simulated mode)")
    r = client.get("/settings")
    token = extract_csrf(r.data)
    r = client.post("/settings", data={
        "owner_email": "owner@example.com", "csrf_token": token,
        "smtp_port": "587",
    })
    check("saving owner_email in settings redirects", r.status_code == 302)
    r = client.get("/leads")
    token = extract_csrf(r.data)
    r = client.post("/leads/simulate", data={"source": "website", "csrf_token": token})
    check("simulate lead with email configured redirects", r.status_code == 302)
    r = client.get("/calls")
    check("simulated outbound email got logged", b"simulated_outbound_email" in r.data)

    print("Lists, filters, exports")
    check("jobs list loads", client.get("/jobs").status_code == 200)
    check("jobs list filter loads", client.get("/jobs?status=quote").status_code == 200)
    check("schedule page loads", client.get("/schedule").status_code == 200)
    check("reports page loads", client.get("/reports").status_code == 200)
    r = client.get("/export/customers.csv")
    check("customers CSV export works", r.status_code == 200 and b"Jane" in r.data)
    check("jobs CSV export works", client.get("/export/jobs.csv").status_code == 200)
    check("leads CSV export works", client.get("/export/leads.csv").status_code == 200)

    print("Not-found handling")
    check("unknown customer is a real 404", client.get("/customers/9999").status_code == 404)
    check("unknown job is a real 404", client.get("/jobs/9999").status_code == 404)
    check("unknown quote link is a real 404", anon_client.get("/q/doesnotexist").status_code == 404)

    print()
    print(f"{passed} passed, {len(failures)} failed")
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All good.")


if __name__ == "__main__":
    main()
