"""
OfferFlow — Email Debug Script
Run this on your server: python debug_email.py
It will tell you exactly what's broken step by step.
"""

import os, sys, json, base64
from dotenv import load_dotenv
load_dotenv()

LINE = "─" * 60

def check(label, value, secret=False):
    display = ("*" * 8 + value[-4:]) if (secret and value) else value
    status = "✅" if value else "❌ MISSING"
    print(f"  {status}  {label}: {display or '(empty)'}")
    return bool(value)

def section(title):
    print(f"\n{LINE}\n  {title}\n{LINE}")


# ── 1. ENV VARS ───────────────────────────────────────────────────────────────
section("STEP 1 — Environment variables")

api_key     = os.environ.get("BREVO_API_KEY", "")
sender_name = os.environ.get("SENDER_NAME", "")
sender_email= os.environ.get("SENDER_EMAIL", "")
base_url    = os.environ.get("BASE_URL", "")

ok_key    = check("BREVO_API_KEY",  api_key,      secret=True)
ok_name   = check("SENDER_NAME",    sender_name)
ok_email  = check("SENDER_EMAIL",   sender_email)
ok_url    = check("BASE_URL",       base_url)

if base_url.endswith("/"):
    print(f"  ⚠️  BASE_URL has a trailing slash — will cause double-slash links. Remove it.")

if not all([ok_key, ok_name, ok_email, ok_url]):
    print("\n  ❌ Fix the missing env vars above, then re-run this script.")
    sys.exit(1)
else:
    print("\n  ✅ All env vars present.")


# ── 2. IMPORT SDK ─────────────────────────────────────────────────────────────
section("STEP 2 — Import sib_api_v3_sdk")

try:
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException
    print("  ✅ sib_api_v3_sdk imported successfully.")
except ImportError as e:
    print(f"  ❌ Import failed: {e}")
    print("  Fix: pip install sib-api-v3-sdk")
    sys.exit(1)


# ── 3. AUTH — VERIFY API KEY WITH BREVO ──────────────────────────────────────
section("STEP 3 — Verify Brevo API key (live API call)")

try:
    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = api_key
    client = sib_api_v3_sdk.ApiClient(configuration)
    account_api = sib_api_v3_sdk.AccountApi(client)
    account = account_api.get_account()
    print(f"  ✅ API key valid.")
    print(f"     Account email : {account.email}")
    print(f"     Plan          : {account.plan[0].type if account.plan else 'unknown'}")
except ApiException as e:
    body = json.loads(e.body) if e.body else {}
    print(f"  ❌ Brevo API rejected the key: HTTP {e.status}")
    print(f"     Message: {body.get('message', e.body)}")
    if e.status == 401:
        print("  Fix: Your BREVO_API_KEY is wrong or expired. Generate a new one at:")
        print("       https://app.brevo.com/settings/keys/api")
    sys.exit(1)
except Exception as e:
    print(f"  ❌ Unexpected error: {e}")
    sys.exit(1)


# ── 4. CHECK SENDER IS VERIFIED ───────────────────────────────────────────────
section("STEP 4 — Check sender email is verified in Brevo")

try:
    senders_api = sib_api_v3_sdk.SendersApi(client)
    senders = senders_api.get_senders()
    verified = [s.email for s in (senders.senders or []) if s.active]
    print(f"  Verified senders on your account: {verified or ['(none)']}")
    if sender_email in verified:
        print(f"  ✅ '{sender_email}' is verified and active.")
    else:
        print(f"  ❌ '{sender_email}' is NOT in your verified senders list!")
        print("  Fix: Go to https://app.brevo.com/senders and verify this email address.")
        print("       Until it's verified, Brevo will silently reject all sends from it.")
        sys.exit(1)
except Exception as e:
    print(f"  ⚠️  Could not fetch senders list: {e}")
    print("     Continuing anyway — manually verify at https://app.brevo.com/senders")


# ── 5. SEND A REAL TEST EMAIL ─────────────────────────────────────────────────
section("STEP 5 — Send a real test email")

TEST_TO = input(f"\n  Enter the recipient email to test (or press Enter to use {sender_email}): ").strip()
if not TEST_TO:
    TEST_TO = sender_email

print(f"\n  Sending test email to: {TEST_TO} ...")

try:
    transac_api = sib_api_v3_sdk.TransactionalEmailsApi(client)
    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": TEST_TO}],
        sender={"name": sender_name, "email": sender_email},
        subject="OfferFlow — Email Debug Test",
        html_content="""
        <html><body style="font-family:Arial,sans-serif;padding:32px">
          <h2 style="color:#0d1b3e">✅ OfferFlow email is working!</h2>
          <p>If you received this, your Brevo setup is correct and BG verification emails will be delivered.</p>
          <p style="color:#64748b;font-size:12px">Sent by debug_email.py</p>
        </body></html>
        """
    )
    result = transac_api.send_transac_email(send_smtp_email)
    print(f"  ✅ Email sent! Message ID: {result.message_id}")
    print(f"\n  Check {TEST_TO}'s inbox (and spam folder).")
except ApiException as e:
    body = json.loads(e.body) if e.body else {}
    print(f"  ❌ Send failed: HTTP {e.status}")
    print(f"     Message: {body.get('message', str(e.body))}")
    if e.status == 400:
        print("  This usually means the sender is not verified or the TO address is blocked.")
    elif e.status == 402:
        print("  Your Brevo free plan daily limit may be exhausted.")


# ── 6. SIMULATE BG VERIFICATION FLOW ─────────────────────────────────────────
section("STEP 6 — Simulate BG verification email (same code as app.py)")

DATA_DIR = os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
CANDS_F  = DATA_DIR + "/candidates.json"
VERIFS_F = DATA_DIR + "/verifications.json"
USERS_F  = DATA_DIR + "/users.json"
for f in [VERIFS_F, USERS_F]:
    if not os.path.exists(f):
        with open(f, "w") as file:
            json.dump({}, file)
if not os.path.exists(CANDS_F):
    print("  ⚠️  No candidates.json found at /tmp/data — skipping live simulation.")
    print("     This is fine if you haven't run the app yet.")
else:
    with open(CANDS_F) as f:
        cands = json.load(f)
    with open(VERIFS_F) as f:
        verifs = json.load(f) if os.path.exists(VERIFS_F) else {}

    accepted = [(cid, c) for cid, c in cands.items() if c.get('offer_status') == 'accepted']
    print(f"  Found {len(accepted)} accepted candidate(s) in candidates.json")

    for cid, c in accepted[:3]:
        existing = next((v for v in verifs.values() if v.get('candidate_id') == cid), None)
        status = "✅ verif record exists" if existing else "❌ NO verif record — email should have been sent but wasn't"
        print(f"    {c['name']} ({c['email']}): {status}")
        if existing:
            vid = existing['id']
            link = f"{base_url.rstrip('/')}/background-verification/{vid}"
            print(f"       BG link: {link}")

section("DONE")
print("  If Step 5 succeeded but BG emails still don't arrive:")
print("  → Add the debug logging from the fixed offer_response() to your app.py")
print("  → Check your server terminal for [BG EMAIL] log lines when a candidate accepts")
print("  → Confirm the candidate's offer_status is 'accepted' and a verif record exists\n")