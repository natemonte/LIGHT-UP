# Lights CRM

A CRM built specifically for a seasonal lighting/landscaping business:
customers, quotes/invoices with payments, job scheduling, job cost vs. price
tracking, next-year renewal tracking, missed-call auto-text, per-number
(yard sign) lead/ROI tracking, and a text alert to you the moment a new lead
comes in.

## What this is built with (and why)

Plain **Python + Flask + SQLite**, server-rendered pages, no build step, no
JavaScript framework. Nothing to compile, nothing to `npm install`. One
Python process, one database file. This was a deliberate choice for
reliability and easy hosting -- it runs comfortably on the cheapest tier of
basically any host (Render, Railway, PythonAnywhere, Fly.io, a $5 DigitalOcean
droplet, etc).

## Running it yourself right now (to try it before deploying anywhere)

```bash
cd lights-crm
pip install -r requirements.txt
python3 app.py
```

Then open `http://localhost:5000` in a browser. First visit will ask you to
create your login (this is YOUR account, stored only in your own database).

## What's real vs. simulated out of the box

This app is **fully usable right now with zero outside accounts** -- you can
add customers, create quotes, mark jobs paid, simulate missed calls and
leads, and see the whole flow work. Two things run in "simulated" mode until
you add real credentials in Settings:

- **Payments (Stripe):** until you paste a Stripe key into Settings, "Charge
  card" / "Pay by bank transfer" show a stand-in checkout page so you can
  test the whole flow. Paste in a Stripe **test** key (`sk_test_...`) and it
  switches to real Stripe Checkout automatically -- no code changes.
- **Texting (Quo):** until you paste your Quo API key into Settings, missed
  call auto-replies and "new lead" alerts to you just get logged instead of
  actually sent, visible on the Calls page. Paste in your real Quo API key
  and they start actually sending.

This means you (or I) can safely click every button and see exactly how it
behaves before any money or real texts are involved.

## To make it "real" (in the order I'd do it)

1. **Create a Stripe account** (free, ~2 minutes, just email + password --
   no bank info needed until you flip it to live mode). Dashboard → Developers
   → API keys → copy the **test** secret key → paste into this app's Settings
   page. Try a payment. When ready for real money, flip Stripe to live mode
   and paste the live key in instead.
2. **Get your Quo API key.** In Quo: Settings → API (or similar; Quo's admin
   UI changes over time, so if you don't see it, search Quo's help docs for
   "API key"). Paste it into Settings here.
   - **Important:** I built the webhook parser (`integrations/quo_client.py`,
     function `parse_webhook_event`) against Quo's publicly documented
     webhook format, but I could not test it against your real account. The
     very first time a real missed call comes in, check the Calls page to
     confirm it logged correctly. If it doesn't, forward me (or whoever's
     helping you next) the raw webhook payload from Quo's dashboard logs and
     that one function is the only thing that needs adjusting.
   - In Quo, set the webhook URL to: `https://<your-deployed-domain>/webhooks/quo`
3. **Add your phone numbers** on the Numbers page -- one row per yard-sign
   number, with a label so reports make sense (e.g. "Blue Yard Sign").
4. **Deploy it somewhere it can run 24/7.** Recommended: Railway
   (usage-based, roughly $5-10/month for an app this size, no cold-start
   delay). Render works too but its free tier sleeps after 15 minutes idle,
   which would make a customer's pay link or a missed-call webhook hang for
   up to a minute -- fine for testing, not for real use.
   - Push this folder to a GitHub repo (private is fine).
   - Railway: "New Project" → "Deploy from GitHub repo" → point at the repo.
     It already has a `Procfile` telling Railway how to run it
     (`gunicorn app:app`) -- no extra config needed.
   - **Add a persistent volume** (Railway: your service → Settings → Volumes
     → mount path `/data`), then set an environment variable
     `CRM_DATA_DIR` = `/data` on the service. This matters: without it, your
     customer list and every quote gets wiped the next time you redeploy,
     because a container's own disk doesn't survive that -- only a real
     volume does. This one setting is what makes the difference. (I tested
     this exact env-var override end to end before handing this off, so it's
     confirmed working, not just documented.)
   - Once deployed, put the live URL into Settings → "Public URL of this app"
     so payment links redirect correctly.
5. **Massachusetts payment note:** surcharging (charging extra for card) is
   illegal in MA. Instead, paying by bank transfer (ACH) automatically shows
   a 3% discount (`ACH_DISCOUNT_PCT` in `integrations/stripe_client.py`,
   change it any time) -- same effect, fully legal.

## Where things are, if you want to change something later

- `app.py` -- every page/route in the app.
- `schema.sql` -- the database tables (customers, jobs, payments, calls, leads, numbers).
- `integrations/stripe_client.py` -- all Stripe logic, isolated here.
- `integrations/quo_client.py` -- all Quo logic, isolated here. This is the
  one file most likely to need a tweak once tested against your real account.
- `integrations/email_client.py` -- optional email alerts via plain SMTP.
- `notifications.py` -- the "tell me about a new lead" logic (text + optional email).
- `templates/` -- every page's HTML (plain Jinja2 templates, no frontend build).
- `static/style.css` -- all the styling, one file.
- `scripts/smoke_test.py` -- an automated test that clicks through every
  major flow (setup, customers, quotes, the customer pay link, missed-call
  automation, lead conversion, exports, security checks) against a
  throwaway database. **Run this after any code change** (yours, mine, or a
  hired developer's) before trusting it: `python3 scripts/smoke_test.py`.
  It prints exactly what broke if something did.
- `scripts/backup.py` -- database backup (see below).

## Connecting a website form or Facebook Lead Ads later

Anything (a website contact form, a Zapier "New Facebook Lead" trigger, etc.)
can create a lead and text you automatically by sending a POST request to:

```
POST https://<your-domain>/leads/intake
Content-Type: application/json

{"name": "Jane Doe", "phone": "+15551234567", "email": "jane@example.com",
 "source": "facebook_ad", "message": "Interested in roofline lights"}
```

**If what you actually get today is an EMAIL** (Facebook sends you an email
per lead, or your website's contact form emails you instead of texting) --
that's exactly what this is for too, you just need one small piece of
plumbing in between, since this app can't read your inbox directly. The
standard, no-code way: a Zapier "Zap" with trigger **New Email Matching
Search** (or Facebook's own **New Lead** trigger, which is cleaner if you
run Facebook Lead Ads) and action **Webhooks by Zapier -> POST**, pointed at
`https://<your-domain>/leads/intake` with the fields above mapped from the
email/lead fields. Once that's set up once, every future lead -- by email or
by Facebook form -- turns into a text to you automatically, same as a missed
call does today. I can build out the exact Zap step-by-step with you once
you're ready to set it up.

## Since the first version: what got added

- **Optional email alerts**, alongside texts. Fill in your email + SMTP
  details on the Settings page (a Gmail account + free "app password" works
  well) and you'll get emailed on every new lead too, not just texted.
  Leave it blank and nothing changes -- texting alone still works exactly
  as before. See `integrations/email_client.py`.
- **Security hardening:** CSRF protection on every form, and a login lockout
  after 5 wrong password attempts.
- **One-click "convert lead to customer"** on the Leads page.
- **A revenue-by-number bar chart** on Reports, so the best/worst yard sign
  jumps out visually instead of needing to read a table.
- **Automated test suite** (`scripts/smoke_test.py`) covering every major
  flow, so future changes can be verified in seconds -- see below.
- **Customer-facing quote/pay links.** Every job now has its own link
  (`/q/<token>`, shown on the job's page) that you can text or email a
  customer directly -- they can review the quote and pay (card or discounted
  bank transfer) without ever logging in. This is the actual way you'd get
  paid day to day; the "Charge card" buttons on the job page are the
  business-owner-does-it-for-them alternative.
- **Jobs list with status filter** (`/jobs`) -- see everything at a glance,
  filter to just quotes, just scheduled, etc.
- **CSV exports** (Settings page) for customers, jobs, and leads -- opens
  straight in Excel/Google Sheets, and doubles as a quick manual backup.
- **Edit screens** for customers and jobs (previously add-only).
- **Proper "not found" pages** instead of the app crashing on a bad link.
- **"Add to Home Screen" support** -- once this is deployed with a real
  domain, opening it in your phone's browser and choosing "Add to Home
  Screen" (Safari) or "Install app" (Chrome) gives you an icon that opens
  full-screen like a real app, no browser bar. That's the "daily on my
  phone" feel without needing a native app store listing.

## Honest limitations right now (roadmap, not excuses)

- Single login (just you). Multi-user/staff logins would be a follow-up.
- No CSRF protection on forms yet -- low risk for a single-owner internal
  tool behind a login, but worth adding before this handles a lot of traffic.
- No login rate-limiting yet -- someone could brute-force guess your
  password with enough attempts. Worth adding before going live.
- `scripts/backup.py` copies the database to a timestamped file and keeps
  the last 30. It's not scheduled anywhere yet -- once deployed, set your
  host's cron/scheduled-job feature to run `python3 scripts/backup.py` daily
  (Railway and Render both support this). Until then, run it by hand
  occasionally, or ask me to set up the scheduled check-in to do it for you.
- The Quo webhook field names should be confirmed against a real payload
  (see step 2 above) before fully trusting the missed-call automation.
