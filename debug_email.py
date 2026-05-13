"""
Run this on your server: python debug_flow.py
This simulates the EXACT offer_response accept flow and shows where it breaks.
"""
import os, json, uuid, base64
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

LINE = "─" * 60

BASE_URL     = os.environ.get("BASE_URL", "")
BREVO_API_KEY= os.environ.get("BREVO_API_KEY", "")
SENDER_NAME  = os.environ.get("SENDER_NAME", "")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")

DATA_DIR  = "/tmp/data"
CANDS_F   = DATA_DIR + "/candidates.json"
VERIFS_F  = DATA_DIR + "/verifications.json"
USERS_F   = DATA_DIR + "/users.json"

def load(path):
    return json.load(open(path)) if os.path.exists(path) else {}

print(f"\n{LINE}")
print("  OfferFlow — BG Email Flow Debugger")
print(LINE)

# ── Pick a candidate ──────────────────────────────────────────────────────────
cands  = load(CANDS_F)
verifs = load(VERIFS_F)
users  = load(USERS_F)

accepted = [(cid, c) for cid, c in cands.items() if c.get('offer_status') == 'accepted']
print(f"\nAccepted candidates: {len(accepted)}")
for i, (cid, c) in enumerate(accepted):
    existing = next((v for v in verifs.values() if v.get('candidate_id') == cid), None)
    sent = existing.get('bg_email_sent') if existing else 'N/A'
    print(f"  [{i}] {c['name']} | {c['email']} | verif={bool(existing)} | bg_email_sent={sent}")

idx = input("\nEnter number to test (or press Enter for 0): ").strip()
idx = int(idx) if idx else 0
cid, c = accepted[idx]

print(f"\n{LINE}")
print(f"  Testing: {c['name']} ({c['email']})")
print(LINE)

# ── Step 1: HR user lookup ────────────────────────────────────────────────────
print(f"\n[1] HR user lookup")
print(f"    c['hr_id'] = {c.get('hr_id')}")
hr = users.get(c.get('hr_id', ''), {})
print(f"    hr found   = {bool(hr)}")
company = hr.get('company_name', 'the company')
print(f"    company    = {company}")

# ── Step 2: Existing verif ────────────────────────────────────────────────────
print(f"\n[2] Verif record check")
existing = next((v for v in verifs.values() if v.get('candidate_id') == cid), None)
if existing:
    vid = existing['id']
    print(f"    existing   = YES (vid={vid})")
    print(f"    bg_email_sent = {existing.get('bg_email_sent')}")
else:
    vid = str(uuid.uuid4())
    print(f"    existing   = NO — would create vid={vid}")

# ── Step 3: Build bg_link ─────────────────────────────────────────────────────
print(f"\n[3] Build bg_link")
print(f"    BASE_URL   = '{BASE_URL}'")
if not BASE_URL:
    print("    ❌ BASE_URL is empty — this is the bug!")
    exit(1)
bg_link = f"{BASE_URL.rstrip('/')}/background-verification/{vid}"
print(f"    bg_link    = {bg_link}")

# ── Step 4: Build email HTML ──────────────────────────────────────────────────
print(f"\n[4] Build email HTML")
name = c.get('name', '')
role = c.get('role', '')
html = f"""<html><body style="font-family:Arial;padding:32px">
<h2>Hello {name},</h2>
<p>Please complete background verification for <b>{role}</b> at <b>{company}</b>.</p>
<a href="{bg_link}" style="background:#1a56db;color:#fff;padding:12px 28px;
   border-radius:8px;text-decoration:none;font-weight:700;display:inline-block;margin-top:16px">
  Start Background Verification
</a>
<p style="color:#94a3b8;font-size:11px;margin-top:20px">Link: {bg_link}</p>
</body></html>"""
print(f"    HTML length = {len(html)} chars ✅")

# ── Step 5: Actually send ─────────────────────────────────────────────────────
print(f"\n[5] Send email via Brevo")
print(f"    TO      : {c['email']}")
print(f"    SUBJECT : Next Step: Complete Your Background Verification — {company}")
print(f"    FROM    : {SENDER_NAME} <{SENDER_EMAIL}>")

try:
    import sib_api_v3_sdk
    from sib_api_v3_sdk.rest import ApiException

    configuration = sib_api_v3_sdk.Configuration()
    configuration.api_key['api-key'] = BREVO_API_KEY
    api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
        sib_api_v3_sdk.ApiClient(configuration)
    )
    send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
        to=[{"email": c['email']}],
        sender={"name": SENDER_NAME, "email": SENDER_EMAIL},
        subject=f"Next Step: Complete Your Background Verification — {company}",
        html_content=html
    )
    result = api_instance.send_transac_email(send_smtp_email)
    print(f"\n    ✅ EMAIL SENT! Message ID: {result.message_id}")
    print(f"    Check {c['email']} inbox + spam folder now.")

except ApiException as e:
    body = json.loads(e.body) if e.body else {}
    print(f"\n    ❌ Brevo API error: HTTP {e.status}")
    print(f"    Message: {body.get('message', str(e.body))}")

except Exception as e:
    print(f"\n    ❌ Unexpected error: {type(e).__name__}: {e}")

# ── Step 6: Check what bg_verification_email_html does ───────────────────────
print(f"\n{LINE}")
print("  [6] Checking your app.py bg_verification_email_html function")
print(LINE)
print("""
  If email sent above but NOT from the app, the bug is in how app.py
  calls bg_verification_email_html. Add this line temporarily to app.py
  inside the offer_response accept block, right before send_email():

      print(f"[DEBUG] about to send BG email to {c['email']}, company={company}, bg_link={bg_link}")

  Then accept an offer and check your server terminal for that line.
  If you NEVER see it printed, the code is not reaching send_email() at all.
""")