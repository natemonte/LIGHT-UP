import csv
import io
import os
import secrets
import uuid
from datetime import datetime, date
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort, Response
from werkzeug.security import generate_password_hash, check_password_hash

import db
from db import init_db, query, execute, get_setting, set_setting
from integrations import stripe_client, quo_client, email_client
from notifications import notify_owner_new_lead

SECRET_FILE = os.path.join(db.DATA_DIR, ".flask_secret")

DEFAULT_LEASE_TERMS = (
    "All lighting, wiring, timers, and related materials installed by us remain the property of the "
    "Company at all times. This agreement is a seasonal lease of that equipment, not a sale -- the "
    "customer does not own the installed lighting. The Company is responsible for installation, "
    "routine maintenance during the lease term, and removal (takedown) of all equipment at the end of "
    "the season. The customer agrees not to alter, relocate, or attempt to repair the equipment "
    "themselves during the lease term."
)


def get_flask_secret():
    os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE).read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_FILE, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.secret_key = get_flask_secret()


# ---------------------------------------------------------------- helpers --

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def has_any_user():
    return query("SELECT id FROM users LIMIT 1", one=True) is not None


def get_or_404(sql, params, message="Not found"):
    row = query(sql, params, one=True)
    if row is None:
        abort(404, description=message)
    return row


# --------------------------------------------------------------- security --
# Minimal, dependency-free CSRF protection (no flask-wtf needed). Every
# authenticated form includes {{ csrf_token() }} as a hidden field; we
# reject any POST to a non-exempt route whose token doesn't match the
# one issued to that browser session.
CSRF_EXEMPT_PREFIXES = ("/webhooks/", "/leads/intake", "/q/", "/pay/simulate/")


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def enforce_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    if request.path.startswith(CSRF_EXEMPT_PREFIXES):
        return
    sent = request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, description="Your session expired or this form was out of date -- please go back and try again.")


# Simple login lockout: after too many wrong passwords for an email, make
# them wait. In-memory only (resets on restart), which is fine for a
# single-owner app -- the point is slowing down a brute-force script, not
# building a full audit system.
_failed_logins = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300


def _login_is_locked(email):
    import time
    rec = _failed_logins.get(email)
    if not rec:
        return False
    count, last = rec
    if count >= LOGIN_MAX_ATTEMPTS and (time.time() - last) < LOGIN_LOCKOUT_SECONDS:
        return True
    return False


def _record_failed_login(email):
    import time
    count, _ = _failed_logins.get(email, (0, 0))
    _failed_logins[email] = (count + 1, time.time())


def _clear_failed_login(email):
    _failed_logins.pop(email, None)


@app.context_processor
def inject_globals():
    return {
        "stripe_live": stripe_client.is_live(),
        "quo_live": quo_client.is_live(),
        "email_configured": email_client.is_configured(),
        "current_year": datetime.now().year,
        "venmo_handle": get_setting("venmo_handle", ""),
        "lease_terms": get_setting("lease_terms", DEFAULT_LEASE_TERMS),
    }


def money(v):
    try:
        return f"${float(v or 0):,.2f}"
    except (ValueError, TypeError):
        return "$0.00"


app.jinja_env.filters["money"] = money


# ------------------------------------------------------------- auth/setup --

@app.route("/setup", methods=["GET", "POST"])
def setup():
    if has_any_user():
        return redirect(url_for("login"))
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        owner_phone = request.form.get("owner_phone", "").strip()
        uid = execute(
            "INSERT INTO users (email, password_hash, owner_phone) VALUES (?, ?, ?)",
            (email, generate_password_hash(password), owner_phone),
        )
        if owner_phone:
            set_setting("owner_phone", owner_phone)
        session["user_id"] = uid
        flash("Welcome! Your CRM is set up.", "success")
        return redirect(url_for("dashboard"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_any_user():
        return redirect(url_for("setup"))
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if _login_is_locked(email):
            flash(f"Too many failed attempts. Please wait {LOGIN_LOCKOUT_SECONDS // 60} minutes and try again.", "error")
            return render_template("login.html")
        user = query("SELECT * FROM users WHERE email = ?", (email,), one=True)
        if user and check_password_hash(user["password_hash"], password):
            _clear_failed_login(email)
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        _record_failed_login(email)
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------------------------------------------------- dashboard --

@app.route("/")
@login_required
def dashboard():
    today = date.today().isoformat()

    jobs_by_status = [
        dict(row)
        for row in query(
            "SELECT status, COUNT(*) as n, COALESCE(SUM(price_quoted),0) as total "
            "FROM jobs GROUP BY status"
        )
    ]

    upcoming_renewals = query(
        "SELECT jobs.*, customers.first_name, customers.last_name "
        "FROM jobs JOIN customers ON customers.id = jobs.customer_id "
        "WHERE next_service_date IS NOT NULL AND next_service_date >= ? "
        "ORDER BY next_service_date ASC LIMIT 10",
        (today,),
    )

    recent_leads = query(
        "SELECT leads.*, phone_numbers.label as number_label FROM leads "
        "LEFT JOIN phone_numbers ON phone_numbers.id = leads.source_number_id "
        "ORDER BY leads.created_at DESC LIMIT 10"
    )

    roi_by_number = query(
        """
        SELECT pn.id, pn.label, pn.campaign, pn.monthly_cost,
               COUNT(DISTINCT c.id) as lead_count,
               COALESCE(SUM(j.price_quoted), 0) as revenue
        FROM phone_numbers pn
        LEFT JOIN customers c ON c.source_number_id = pn.id
        LEFT JOIN jobs j ON j.customer_id = c.id AND j.status IN ('completed','paid')
        GROUP BY pn.id
        ORDER BY revenue DESC
        """
    )

    open_jobs = query(
        "SELECT jobs.*, customers.first_name, customers.last_name FROM jobs "
        "JOIN customers ON customers.id = jobs.customer_id "
        "WHERE jobs.status IN ('quote','scheduled','in_progress') "
        "ORDER BY jobs.created_at DESC LIMIT 10"
    )

    return render_template(
        "dashboard.html",
        jobs_by_status=jobs_by_status,
        upcoming_renewals=upcoming_renewals,
        recent_leads=recent_leads,
        roi_by_number=roi_by_number,
        open_jobs=open_jobs,
    )


# --------------------------------------------------------------- customers --

@app.route("/customers")
@login_required
def customers_list():
    q = request.args.get("q", "").strip()
    if q:
        rows = query(
            "SELECT * FROM customers WHERE first_name LIKE ? OR last_name LIKE ? "
            "OR phone LIKE ? OR email LIKE ? ORDER BY created_at DESC",
            (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"),
        )
    else:
        rows = query("SELECT * FROM customers ORDER BY created_at DESC")
    return render_template("customers_list.html", customers=rows, q=q)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def customer_new():
    numbers = query("SELECT * FROM phone_numbers WHERE active = 1")
    if request.method == "POST":
        cid = execute(
            "INSERT INTO customers (first_name, last_name, phone, email, address, notes, "
            "source_number_id, lead_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.form["first_name"],
                request.form.get("last_name", ""),
                request.form.get("phone", ""),
                request.form.get("email", ""),
                request.form.get("address", ""),
                request.form.get("notes", ""),
                request.form.get("source_number_id") or None,
                request.form.get("lead_source", "other"),
            ),
        )
        flash("Customer added.", "success")
        return redirect(url_for("customer_detail", customer_id=cid))
    return render_template("customer_form.html", numbers=numbers, customer=None)


@app.route("/customers/<int:customer_id>")
@login_required
def customer_detail(customer_id):
    customer = get_or_404("SELECT * FROM customers WHERE id = ?", (customer_id,), "Customer not found")
    jobs = query(
        "SELECT * FROM jobs WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,)
    )
    return render_template("customer_detail.html", customer=customer, jobs=jobs)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def customer_edit(customer_id):
    customer = get_or_404("SELECT * FROM customers WHERE id = ?", (customer_id,), "Customer not found")
    numbers = query("SELECT * FROM phone_numbers WHERE active = 1")
    if request.method == "POST":
        execute(
            "UPDATE customers SET first_name=?, last_name=?, phone=?, email=?, address=?, "
            "notes=?, source_number_id=?, lead_source=? WHERE id=?",
            (
                request.form["first_name"],
                request.form.get("last_name", ""),
                request.form.get("phone", ""),
                request.form.get("email", ""),
                request.form.get("address", ""),
                request.form.get("notes", ""),
                request.form.get("source_number_id") or None,
                request.form.get("lead_source", "other"),
                customer_id,
            ),
        )
        flash("Customer updated.", "success")
        return redirect(url_for("customer_detail", customer_id=customer_id))
    return render_template("customer_form.html", numbers=numbers, customer=customer)


# -------------------------------------------------------------------- jobs --

@app.route("/jobs")
@login_required
def jobs_list():
    status = request.args.get("status", "")
    sql = (
        "SELECT jobs.*, customers.first_name, customers.last_name FROM jobs "
        "JOIN customers ON customers.id = jobs.customer_id "
    )
    params = ()
    if status:
        sql += "WHERE jobs.status = ? "
        params = (status,)
    sql += "ORDER BY jobs.created_at DESC"
    rows = query(sql, params)
    return render_template("jobs_list.html", jobs=rows, status=status)


@app.route("/customers/<int:customer_id>/jobs/new", methods=["GET", "POST"])
@login_required
def job_new(customer_id):
    customer = get_or_404("SELECT * FROM customers WHERE id = ?", (customer_id,), "Customer not found")
    if request.method == "POST":
        jid = execute(
            "INSERT INTO jobs (customer_id, title, status, scheduled_date, takedown_date, materials_cost, "
            "labor_cost, price_quoted, footage, price_per_foot, next_service_date, next_estimated_price, notes, public_token) "
            "VALUES (?, ?, 'quote', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                request.form["title"],
                request.form.get("scheduled_date") or None,
                request.form.get("takedown_date") or None,
                float(request.form.get("materials_cost") or 0),
                float(request.form.get("labor_cost") or 0),
                float(request.form.get("price_quoted") or 0),
                float(request.form.get("footage") or 0) or None,
                float(request.form.get("price_per_foot") or 0) or None,
                request.form.get("next_service_date") or None,
                float(request.form.get("next_estimated_price") or 0) or None,
                request.form.get("notes", ""),
                uuid.uuid4().hex,
            ),
        )
        flash("Quote created.", "success")
        return redirect(url_for("job_detail", job_id=jid))
    return render_template("job_form.html", customer=customer, job=None)


def ensure_public_token(job):
    """Older jobs created before the customer-facing quote link existed
    won't have a token yet -- generate one the first time it's needed."""
    if job["public_token"]:
        return job["public_token"]
    token = uuid.uuid4().hex
    execute("UPDATE jobs SET public_token = ? WHERE id = ?", (token, job["id"]))
    return token


@app.route("/jobs/<int:job_id>")
@login_required
def job_detail(job_id):
    job = get_or_404("SELECT * FROM jobs WHERE id = ?", (job_id,), "Job not found")
    customer = query("SELECT * FROM customers WHERE id = ?", (job["customer_id"],), one=True)
    payments = query("SELECT * FROM payments WHERE job_id = ? ORDER BY created_at DESC", (job_id,))
    margin = float(job["price_quoted"] or 0) - float(job["materials_cost"] or 0) - float(job["labor_cost"] or 0)
    public_token = ensure_public_token(job)
    return render_template(
        "job_detail.html", job=job, customer=customer, payments=payments, margin=margin, public_token=public_token
    )


@app.route("/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required
def job_edit(job_id):
    job = get_or_404("SELECT * FROM jobs WHERE id = ?", (job_id,), "Job not found")
    customer = query("SELECT * FROM customers WHERE id = ?", (job["customer_id"],), one=True)
    if request.method == "POST":
        execute(
            "UPDATE jobs SET title=?, scheduled_date=?, takedown_date=?, materials_cost=?, labor_cost=?, "
            "price_quoted=?, footage=?, price_per_foot=?, next_service_date=?, next_estimated_price=?, notes=? WHERE id=?",
            (
                request.form["title"],
                request.form.get("scheduled_date") or None,
                request.form.get("takedown_date") or None,
                float(request.form.get("materials_cost") or 0),
                float(request.form.get("labor_cost") or 0),
                float(request.form.get("price_quoted") or 0),
                float(request.form.get("footage") or 0) or None,
                float(request.form.get("price_per_foot") or 0) or None,
                request.form.get("next_service_date") or None,
                float(request.form.get("next_estimated_price") or 0) or None,
                request.form.get("notes", ""),
                job_id,
            ),
        )
        flash("Job updated.", "success")
        return redirect(url_for("job_detail", job_id=job_id))
    return render_template("job_form.html", customer=customer, job=job)


@app.route("/jobs/<int:job_id>/takedown_done", methods=["POST"])
@login_required
def job_takedown_done(job_id):
    get_or_404("SELECT id FROM jobs WHERE id = ?", (job_id,), "Job not found")
    execute(
        "UPDATE jobs SET takedown_completed_date = ? WHERE id = ?",
        (date.today().isoformat(), job_id),
    )
    flash("Takedown marked done.", "success")
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<int:job_id>/status", methods=["POST"])
@login_required
def job_update_status(job_id):
    get_or_404("SELECT id FROM jobs WHERE id = ?", (job_id,), "Job not found")
    new_status = request.form["status"]
    if new_status == "completed":
        execute(
            "UPDATE jobs SET status = ?, completed_date = ? WHERE id = ?",
            (new_status, date.today().isoformat(), job_id),
        )
    else:
        execute("UPDATE jobs SET status = ? WHERE id = ?", (new_status, job_id))
    flash("Job updated.", "success")
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/jobs/<int:job_id>/pay", methods=["POST"])
@login_required
def job_pay(job_id):
    job = get_or_404("SELECT * FROM jobs WHERE id = ?", (job_id,), "Job not found")
    method = request.form.get("method", "card")
    session_data = stripe_client.create_checkout_session(job, method=method)
    execute("UPDATE jobs SET stripe_checkout_id = ? WHERE id = ?", (session_data["id"], job_id))
    execute(
        "INSERT INTO payments (job_id, amount, method, status, stripe_ref) VALUES (?, ?, ?, 'pending', ?)",
        (job_id, session_data["amount_cents"] / 100.0, method, session_data["id"]),
    )
    return redirect(session_data["url"])


@app.route("/jobs/<int:job_id>/record_payment", methods=["POST"])
@login_required
def job_record_payment(job_id):
    """For payment methods with no live API to confirm automatically --
    Venmo, cash, check. You collect the money outside the app (e.g. a
    customer sends Venmo to your business handle), then log it here so the
    job's balance and payment history stay accurate."""
    job = get_or_404("SELECT * FROM jobs WHERE id = ?", (job_id,), "Job not found")
    method = request.form.get("method", "venmo")
    amount = float(request.form.get("amount") or 0)
    note = request.form.get("note", "").strip()
    if amount <= 0:
        flash("Enter an amount greater than $0.", "error")
        return redirect(url_for("job_detail", job_id=job_id))
    execute(
        "INSERT INTO payments (job_id, amount, method, status, stripe_ref) VALUES (?, ?, ?, 'paid', ?)",
        (job_id, amount, method, note or None),
    )
    new_paid = float(job["amount_paid"] or 0) + amount
    new_status = "paid" if new_paid >= float(job["price_quoted"] or 0) else job["status"]
    execute(
        "UPDATE jobs SET amount_paid = ?, payment_method = ?, status = ? WHERE id = ?",
        (new_paid, method, new_status, job_id),
    )
    flash(f"Recorded {method} payment of {money(amount)}.", "success")
    return redirect(url_for("job_detail", job_id=job_id))


@app.route("/q/<token>")
def public_quote(token):
    """Customer-facing quote/invoice page -- no login needed. This is the
    link you text or email to a customer so THEY can review and pay,
    instead of you having to take a card over the phone."""
    job = query("SELECT * FROM jobs WHERE public_token = ?", (token,), one=True)
    if not job:
        abort(404)
    customer = query("SELECT * FROM customers WHERE id = ?", (job["customer_id"],), one=True)
    balance = float(job["price_quoted"] or 0) - float(job["amount_paid"] or 0)
    ach_amount = round(balance * (1 - stripe_client.ACH_DISCOUNT_PCT), 2)
    return render_template(
        "public_quote.html", job=job, customer=customer, balance=balance, ach_amount=ach_amount, token=token
    )


@app.route("/q/<token>/pay", methods=["POST"])
def public_quote_pay(token):
    job = query("SELECT * FROM jobs WHERE public_token = ?", (token,), one=True)
    if not job:
        abort(404)
    method = request.form.get("method", "card")
    session_data = stripe_client.create_checkout_session(job, method=method)
    execute("UPDATE jobs SET stripe_checkout_id = ? WHERE id = ?", (session_data["id"], job["id"]))
    execute(
        "INSERT INTO payments (job_id, amount, method, status, stripe_ref) VALUES (?, ?, ?, 'pending', ?)",
        (job["id"], session_data["amount_cents"] / 100.0, method, session_data["id"]),
    )
    return redirect(session_data["url"])


@app.route("/pay/simulate/<int:job_id>")
def pay_simulate(job_id):
    """Stand-in for the real Stripe-hosted checkout page, used until a real
    Stripe test key is configured. Lets you click through the full flow."""
    job = get_or_404("SELECT * FROM jobs WHERE id = ?", (job_id,), "Job not found")
    method = request.args.get("method", "card")
    return render_template("pay_simulate.html", job=job, method=method)


@app.route("/pay/simulate/<int:job_id>/confirm", methods=["POST"])
def pay_simulate_confirm(job_id):
    job = get_or_404("SELECT * FROM jobs WHERE id = ?", (job_id,), "Job not found")
    method = request.form.get("method", "card")
    balance = float(job["price_quoted"]) - float(job["amount_paid"] or 0)
    discount = balance * stripe_client.ACH_DISCOUNT_PCT if method == "ach" else 0
    amount = round(balance - discount, 2)
    execute(
        "UPDATE jobs SET amount_paid = amount_paid + ?, payment_method = ?, status = 'paid' WHERE id = ?",
        (amount, method, job_id),
    )
    execute(
        "UPDATE payments SET status = 'paid' WHERE job_id = ? AND status = 'pending'",
        (job_id,),
    )
    flash(f"Simulated {method.upper()} payment of {money(amount)} recorded. (Test mode -- no real money moved.)", "success")
    if session.get("user_id"):
        return redirect(url_for("job_detail", job_id=job_id))
    token = ensure_public_token(job)
    return redirect(url_for("public_quote", token=token))


# ---------------------------------------------------------- phone numbers --

@app.route("/numbers", methods=["GET", "POST"])
@login_required
def numbers():
    if request.method == "POST":
        execute(
            "INSERT INTO phone_numbers (number, label, campaign, monthly_cost) VALUES (?, ?, ?, ?)",
            (
                request.form["number"].strip(),
                request.form["label"].strip(),
                request.form.get("campaign", ""),
                float(request.form.get("monthly_cost") or 0),
            ),
        )
        flash("Number added.", "success")
        return redirect(url_for("numbers"))
    rows = query("SELECT * FROM phone_numbers ORDER BY created_at DESC")
    return render_template("numbers.html", numbers=rows)


# ------------------------------------------------------------------ calls --

@app.route("/calls")
@login_required
def calls_list():
    rows = query(
        "SELECT call_events.*, phone_numbers.label as number_label FROM call_events "
        "LEFT JOIN phone_numbers ON phone_numbers.id = call_events.phone_number_id "
        "ORDER BY call_events.created_at DESC LIMIT 100"
    )
    numbers = query("SELECT * FROM phone_numbers WHERE active = 1")
    return render_template("calls.html", calls=rows, numbers=numbers)


def _find_number_row(e164_number):
    if not e164_number:
        return None
    return query("SELECT * FROM phone_numbers WHERE number = ?", (e164_number,), one=True)


def handle_missed_call(from_number, to_number, raw_payload=""):
    number_row = _find_number_row(to_number)
    number_id = number_row["id"] if number_row else None

    reply = quo_client.missed_call_reply_text()
    quo_client.send_text(to_number=from_number, from_number=to_number, body=reply)

    event_id = execute(
        "INSERT INTO call_events (phone_number_id, from_number, event_type, auto_reply_sent, raw_payload) "
        "VALUES (?, ?, 'missed_call', 1, ?)",
        (number_id, from_number, raw_payload),
    )

    existing_customer = query("SELECT * FROM customers WHERE phone = ?", (from_number,), one=True)
    lead_id = execute(
        "INSERT INTO leads (name, phone, source, source_number_id, message, customer_id) "
        "VALUES (?, ?, 'missed_call', ?, ?, ?)",
        (
            existing_customer["first_name"] if existing_customer else None,
            from_number,
            number_id,
            f"Missed call on {number_row['label'] if number_row else to_number}",
            existing_customer["id"] if existing_customer else None,
        ),
    )
    lead = query("SELECT * FROM leads WHERE id = ?", (lead_id,), one=True)
    notify_owner_new_lead(lead)
    return event_id


@app.route("/webhooks/quo", methods=["POST"])
def webhook_quo():
    """Public endpoint you paste into Quo's webhook settings once live."""
    payload = request.get_json(silent=True) or {}
    event = quo_client.parse_webhook_event(payload)
    if event["type"] == "missed_call":
        handle_missed_call(event["from_number"], event["to_number"], raw_payload=str(payload)[:2000])
    return jsonify({"ok": True})


@app.route("/calls/simulate", methods=["POST"])
@login_required
def calls_simulate():
    """Demo button: pretend a missed call just came in, so you can see the
    whole auto-text + lead + owner-notification flow without a real call."""
    number_id = request.form.get("phone_number_id")
    number_row = query("SELECT * FROM phone_numbers WHERE id = ?", (number_id,), one=True)
    to_number = number_row["number"] if number_row else "+15550000000"
    fake_caller = request.form.get("fake_caller", "+15559998888")
    handle_missed_call(fake_caller, to_number, raw_payload="simulated from dashboard")
    flash("Simulated missed call handled: auto-text sent + you were notified.", "success")
    return redirect(url_for("calls_list"))


# ------------------------------------------------------------------ leads --

@app.route("/leads")
@login_required
def leads_list():
    rows = query(
        "SELECT leads.*, phone_numbers.label as number_label FROM leads "
        "LEFT JOIN phone_numbers ON phone_numbers.id = leads.source_number_id "
        "ORDER BY leads.created_at DESC LIMIT 200"
    )
    return render_template("leads.html", leads=rows)


@app.route("/leads/intake", methods=["POST"])
def leads_intake():
    """Generic public endpoint for a future website contact form or
    Facebook Lead Ads integration (e.g. via Zapier). Send JSON:
    {"name": "...", "phone": "...", "email": "...", "source": "website", "message": "..."}
    """
    data = request.get_json(silent=True) or request.form
    lead_id = execute(
        "INSERT INTO leads (name, phone, email, source, message) VALUES (?, ?, ?, ?, ?)",
        (
            data.get("name"),
            data.get("phone"),
            data.get("email"),
            data.get("source", "website"),
            data.get("message"),
        ),
    )
    lead = query("SELECT * FROM leads WHERE id = ?", (lead_id,), one=True)
    notify_owner_new_lead(lead)
    return jsonify({"ok": True, "lead_id": lead_id})


@app.route("/leads/<int:lead_id>/convert", methods=["POST"])
@login_required
def lead_convert(lead_id):
    """Turn a lead into a full customer record, one click. Splits a plain
    'name' into first/last as a best guess -- you can fix it on the
    customer's edit page afterward."""
    lead = get_or_404("SELECT * FROM leads WHERE id = ?", (lead_id,), "Lead not found")
    if lead["customer_id"]:
        flash("This lead is already linked to a customer.", "error")
        return redirect(url_for("leads_list"))

    name_parts = (lead["name"] or "New Lead").split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    cid = execute(
        "INSERT INTO customers (first_name, last_name, phone, email, source_number_id, lead_source, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            first_name,
            last_name,
            lead["phone"],
            lead["email"],
            lead["source_number_id"],
            lead["source"],
            lead["message"] or "",
        ),
    )
    execute("UPDATE leads SET customer_id = ? WHERE id = ?", (cid, lead_id))
    flash("Lead converted to a customer. Edit their info below if needed.", "success")
    return redirect(url_for("customer_detail", customer_id=cid))


@app.route("/leads/simulate", methods=["POST"])
@login_required
def leads_simulate():
    source = request.form.get("source", "website")
    lead_id = execute(
        "INSERT INTO leads (name, phone, email, source, message) VALUES (?, ?, ?, ?, ?)",
        ("Test Lead", "+15551230000", "test@example.com", source, "Simulated lead for testing"),
    )
    lead = query("SELECT * FROM leads WHERE id = ?", (lead_id,), one=True)
    notify_owner_new_lead(lead)
    flash("Simulated lead created and owner notification sent (check Calls/Settings for simulated-text log if Quo isn't live yet).", "success")
    return redirect(url_for("leads_list"))


# -------------------------------------------------------------- schedule --

@app.route("/schedule")
@login_required
def schedule():
    from datetime import timedelta

    today = date.today()
    days = int(request.args.get("days", 30))
    horizon = (today + timedelta(days=days)).isoformat()

    installs = query(
        "SELECT jobs.*, customers.first_name, customers.last_name, customers.phone, customers.address "
        "FROM jobs JOIN customers ON customers.id = jobs.customer_id "
        "WHERE jobs.scheduled_date IS NOT NULL AND jobs.scheduled_date >= ? AND jobs.scheduled_date <= ? "
        "AND jobs.status NOT IN ('cancelled') "
        "ORDER BY jobs.scheduled_date ASC",
        (today.isoformat(), horizon),
    )
    takedowns = query(
        "SELECT jobs.*, customers.first_name, customers.last_name, customers.phone, customers.address "
        "FROM jobs JOIN customers ON customers.id = jobs.customer_id "
        "WHERE jobs.takedown_date IS NOT NULL AND jobs.takedown_date >= ? AND jobs.takedown_date <= ? "
        "AND jobs.takedown_completed_date IS NULL AND jobs.status NOT IN ('cancelled') "
        "ORDER BY jobs.takedown_date ASC",
        (today.isoformat(), horizon),
    )

    by_date = {}
    for r in installs:
        by_date.setdefault(r["scheduled_date"], []).append({"type": "install", "job": r})
    for r in takedowns:
        by_date.setdefault(r["takedown_date"], []).append({"type": "takedown", "job": r})
    by_date = dict(sorted(by_date.items()))

    return render_template(
        "schedule.html", by_date=by_date, today=today.isoformat(), days=days
    )


# ---------------------------------------------------------------- export --

def _csv_response(rows, filename):
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/export/customers.csv")
@login_required
def export_customers():
    rows = query("SELECT * FROM customers ORDER BY created_at")
    return _csv_response(rows, "customers.csv")


@app.route("/export/jobs.csv")
@login_required
def export_jobs():
    rows = query(
        "SELECT jobs.*, customers.first_name, customers.last_name, customers.phone "
        "FROM jobs JOIN customers ON customers.id = jobs.customer_id ORDER BY jobs.created_at"
    )
    return _csv_response(rows, "jobs.csv")


@app.route("/export/leads.csv")
@login_required
def export_leads():
    rows = query("SELECT * FROM leads ORDER BY created_at")
    return _csv_response(rows, "leads.csv")


# --------------------------------------------------------------- reports --

@app.route("/reports")
@login_required
def reports():
    roi_by_number = query(
        """
        SELECT pn.id, pn.label, pn.campaign, pn.monthly_cost,
               COUNT(DISTINCT c.id) as lead_count,
               COUNT(DISTINCT j.id) as job_count,
               COALESCE(SUM(CASE WHEN j.status IN ('completed','paid') THEN j.price_quoted ELSE 0 END), 0) as revenue
        FROM phone_numbers pn
        LEFT JOIN customers c ON c.source_number_id = pn.id
        LEFT JOIN jobs j ON j.customer_id = c.id
        GROUP BY pn.id
        ORDER BY revenue DESC
        """
    )
    job_margins = query(
        "SELECT jobs.*, customers.first_name, customers.last_name FROM jobs "
        "JOIN customers ON customers.id = jobs.customer_id "
        "WHERE jobs.status IN ('completed','paid') ORDER BY jobs.completed_date DESC"
    )
    max_revenue = max([r["revenue"] for r in roi_by_number], default=0) or 1
    return render_template(
        "reports.html", roi_by_number=roi_by_number, job_margins=job_margins, max_revenue=max_revenue
    )


# -------------------------------------------------------------- settings --

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        set_setting("stripe_secret_key", request.form.get("stripe_secret_key", "").strip())
        set_setting("venmo_handle", request.form.get("venmo_handle", "").strip())
        set_setting("lease_terms", request.form.get("lease_terms", "").strip())
        set_setting("quo_api_key", request.form.get("quo_api_key", "").strip())
        set_setting("quo_api_base", request.form.get("quo_api_base", "").strip() or quo_client.QUO_API_BASE_DEFAULT)
        set_setting("owner_phone", request.form.get("owner_phone", "").strip())
        set_setting("missed_call_message", request.form.get("missed_call_message", "").strip())
        set_setting("public_base_url", request.form.get("public_base_url", "").strip())
        set_setting("owner_email", request.form.get("owner_email", "").strip())
        set_setting("smtp_host", request.form.get("smtp_host", "").strip())
        set_setting("smtp_port", request.form.get("smtp_port", "587").strip())
        set_setting("smtp_user", request.form.get("smtp_user", "").strip())
        if request.form.get("smtp_password"):
            set_setting("smtp_password", request.form.get("smtp_password").strip())
        set_setting("smtp_from", request.form.get("smtp_from", "").strip())
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))

    ctx = {
        "stripe_secret_key": get_setting("stripe_secret_key", ""),
        "venmo_handle": get_setting("venmo_handle", ""),
        "lease_terms": get_setting("lease_terms", DEFAULT_LEASE_TERMS),
        "quo_api_key": get_setting("quo_api_key", ""),
        "quo_api_base": get_setting("quo_api_base", quo_client.QUO_API_BASE_DEFAULT),
        "owner_phone": get_setting("owner_phone", ""),
        "missed_call_message": quo_client.missed_call_reply_text(),
        "public_base_url": get_setting("public_base_url", ""),
        "owner_email": get_setting("owner_email", ""),
        "smtp_host": get_setting("smtp_host", ""),
        "smtp_port": get_setting("smtp_port", "587"),
        "smtp_user": get_setting("smtp_user", ""),
        "smtp_from": get_setting("smtp_from", ""),
        "email_live": email_client.is_configured(),
    }
    return render_template("settings.html", **ctx)


# Runs on import, not just under `python3 app.py` -- this matters because a
# real host runs this app via gunicorn ("gunicorn app:app", see Procfile),
# which imports this module directly and never executes the __main__ block
# below. Without this line here, a fresh deploy would have no database
# tables at all and every page would crash.
init_db()


if __name__ == "__main__":
    # Only reached when you run `python3 app.py` yourself (local testing).
    # A real deploy uses gunicorn via the Procfile and never hits this.
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
