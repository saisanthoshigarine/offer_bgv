from flask import (Flask, render_template, request, jsonify,
                   session, redirect, send_file)
import json, os, uuid, hashlib, re, smtplib, threading, base64, io
from datetime import datetime, timedelta
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
import pandas as pd
from werkzeug.utils import secure_filename
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, HRFlowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fallback-secret-key")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
SENDER_NAME = os.environ.get("SENDER_NAME")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL")
BASE_URL = os.environ.get("BASE_URL")

# ── Folders ───────────────────────────────────────────────────────────────────
BASE_DIR = "/tmp"
UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads')
DATA_DIR = os.path.join(BASE_DIR, 'data')
LETTER_DIR = os.path.join(BASE_DIR, 'generated_letters')
for d in [UPLOAD_DIR+'/letterheads', UPLOAD_DIR+'/excel',
          UPLOAD_DIR+'/documents', DATA_DIR, LETTER_DIR]:
    os.makedirs(d, exist_ok=True)

# ── JSON helpers ──────────────────────────────────────────────────────────────
def load_json(path):
    return json.load(open(path)) if os.path.exists(path) else {}

def save_json(path, data):
    json.dump(data, open(path,'w'), indent=2, default=str)

USERS_F  = DATA_DIR+'/users.json'
CANDS_F  = DATA_DIR+'/candidates.json'
VERIFS_F = DATA_DIR+'/verifications.json'
TOKENS_F = DATA_DIR+'/tokens.json'

def get_users():  return load_json(USERS_F)
def get_cands():  return load_json(CANDS_F)
def get_verifs(): return load_json(VERIFS_F)
def get_tokens(): return load_json(TOKENS_F)
def save_users(d):  save_json(USERS_F,  d)
def save_cands(d):  save_json(CANDS_F,  d)
def save_verifs(d): save_json(VERIFS_F, d)
def save_tokens(d): save_json(TOKENS_F, d)

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def validate_password(pw):
    return (len(pw)>=8 and re.search(r'[A-Z]',pw)
            and re.search(r'[a-z]',pw) and re.search(r'[^A-Za-z0-9]',pw))

def current_user():
    uid = session.get('user_id')
    return get_users().get(uid) if uid else None

# ── Email via Brevo API ───────────────────────────────────────────────────────
def send_email(to, subject, html_body, attach_path=None, attach_name=None):
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = os.environ.get("BREVO_API_KEY")
        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )
        attachments = []
        if attach_path and os.path.exists(attach_path):
            with open(attach_path, "rb") as f:
                encoded_file = base64.b64encode(f.read()).decode()
            attachments.append({
                "content": encoded_file,
                "name": attach_name or "offer_letter.pdf"
            })
        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": to}],
            sender={"name": SENDER_NAME, "email": SENDER_EMAIL},
            subject=subject,
            html_content=html_body,
            attachment=attachments
        )
        api_instance.send_transac_email(send_smtp_email)
        print(f"[BREVO EMAIL SENT] → {to}")
        return True
    except ApiException as e:
        print(f"[BREVO ERROR] → {e}")
        return False
    except Exception as e:
        print(f"[GENERAL EMAIL ERROR] → {e}")
        return False


# ── Letter Pattern Templates ──────────────────────────────────────────────────
# Each pattern returns an HTML string that overlays content ON the letterhead.
# The letterhead PDF (if present) is shown as background via base64 iframe/img.

LETTER_PATTERNS = {
    'classic': {
        'name': 'Classic Formal',
        'description': 'Traditional corporate structure with centered header, ruled lines, and formal table layout.',
        'preview_color': '#0d1b3e',
    },
    'modern': {
        'name': 'Modern Minimal',
        'description': 'Clean left-aligned design with accent sidebar stripe and bold typography.',
        'preview_color': '#1a56db',
    },
    'elegant': {
        'name': 'Elegant Executive',
        'description': 'Luxury look with serif-inspired headers, gold accents, and refined spacing.',
        'preview_color': '#7c5f1e',
    },
    'bold': {
        'name': 'Bold Impact',
        'description': 'High-contrast dark header block, modern sans-serif, vibrant green CTA styling.',
        'preview_color': '#064e3b',
    },
    'custom': {
        'name': 'Custom Pattern',
        'description': 'Write your own letter structure. Use placeholders like {name}, {role}, {company}, {salary}, {joining_date}.',
        'preview_color': '#6d28d9',
    },
}


def _letterhead_base64(lh_path):
    """Return base64-encoded PDF string if letterhead exists, else empty string."""
    if lh_path and os.path.exists(lh_path):
        with open(lh_path, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    return ''


def _lh_bg_block(lh_b64, height='220px'):
    """HTML block that renders letterhead PDF as background on top of letter content."""
    if not lh_b64:
        return ''
    return f'''
    <div style="position:relative;width:100%;margin-bottom:0;border-bottom:2px solid #e2e8f0;">
      <iframe
        src="data:application/pdf;base64,{lh_b64}"
        style="width:100%;height:{height};border:none;display:block;background:#fff;"
        title="Company Letterhead"
      ></iframe>
      <div style="position:absolute;bottom:6px;right:10px;font-size:9px;
                  color:#94a3b8;background:rgba(255,255,255,.7);padding:2px 6px;border-radius:4px;">
        Company Letterhead
      </div>
    </div>'''


def build_letter_html(pattern, candidate, hr_user, custom_text='', editable=False):
    """
    Build the offer letter HTML for a given pattern.
    editable=True adds contenteditable spans for inline editing in preview.
    """
    company   = hr_user.get('company_name', '')
    lh_path   = hr_user.get('letterhead', '')
    lh_b64    = _letterhead_base64(lh_path)
    today     = datetime.now().strftime('%d %B %Y')
    name      = candidate.get('name', '')
    role      = candidate.get('role', '')
    joining   = candidate.get('joining_date', '')
    salary    = candidate.get('salary', '')
    emp_type  = candidate.get('employment_type', 'full_time').replace('_', ' ').title()
    email     = candidate.get('email', '')

    lh_block  = _lh_bg_block(lh_b64)

    def e(field, val, tag='span'):
        """Wrap in contenteditable span if editable mode."""
        if editable:
            return (f'<{tag} contenteditable="true" data-field="{field}" '
                    f'style="border-bottom:1.5px dashed #1a56db;outline:none;'
                    f'min-width:40px;display:inline-block;cursor:text;">{val}</{tag}>')
        return val

    # ── Pattern: Classic ────────────────────────────────────────────────────
    if pattern == 'classic':
        rows = ''.join(
            f'<tr style="background:{"#f0f4ff" if i%2==0 else "#fff"}">'
            f'<td style="padding:9px 14px;border:1px solid #e2e8f0;font-weight:700;color:#1a56db;width:42%;font-size:12px">{k}</td>'
            f'<td style="padding:9px 14px;border:1px solid #e2e8f0;color:#1e293b;font-size:12px">{v}</td></tr>'
            for i,(k,v) in enumerate([
                ('Candidate Name', e('name', name)),
                ('Designation / Role', e('role', role)),
                ('Joining Date', e('joining_date', joining)),
                ('Annual CTC', f'₹ {e("salary", salary)}'),
                ('Employment Type', e('employment_type', emp_type)),
                ('Reporting Location', 'As communicated by HR'),
            ])
        )
        return f'''
<div style="font-family:Georgia,serif;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.06)">
  {lh_block}
  <div style="padding:28px 36px">
    {"" if lh_b64 else f'<div style="text-align:center;margin-bottom:16px"><div style="font-size:22px;font-weight:900;color:#0d1b3e;font-family:Georgia,serif">{company}</div><div style="font-size:11px;color:#64748b;margin-top:3px">Human Resources Department</div></div>'}
    <hr style="border:none;border-top:2.5px solid #1a56db;margin:0 0 14px"/>
    <div style="text-align:center;font-size:17px;font-weight:700;color:#0d1b3e;letter-spacing:.5px;margin-bottom:16px">OFFER OF EMPLOYMENT</div>
    <div style="font-size:11px;color:#64748b;margin-bottom:12px">Date: {today}</div>
    <p style="font-size:13px;color:#1e293b;margin-bottom:8px">Dear <b>{e('name', name)}</b>,</p>
    <p style="font-size:12px;color:#475569;line-height:1.9;margin-bottom:14px">
      We are delighted to extend this offer of employment for the position of
      <b style="color:#1a56db">{e('role', role)}</b> at <b>{company}</b>. We believe your skills are an excellent fit.
    </p>
    <p style="font-size:12px;font-weight:700;color:#0d1b3e;margin-bottom:8px">Offer Details:</p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:16px">{rows}</table>
    <p style="font-size:12px;font-weight:700;color:#0d1b3e;margin-bottom:6px">This offer is subject to:</p>
    <ul style="font-size:12px;color:#475569;line-height:2;margin-left:18px;margin-bottom:14px">
      <li>Successful completion of background verification.</li>
      <li>Submission of all required documents before joining.</li>
      <li>Acceptance of the Code of Conduct and employment terms.</li>
    </ul>
    <p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:20px">
      Please confirm your acceptance or decline via the buttons in the email we have sent to <b>{email}</b>.
    </p>
    <hr style="border:none;border-top:1px solid #e2e8f0;margin-bottom:12px"/>
    <p style="font-size:12px;color:#1e293b;margin:0">Warm regards,</p>
    <p style="font-size:13px;font-weight:700;color:#0d1b3e;margin:4px 0 0">HR Department — {company}</p>
    <p style="font-size:10px;color:#94a3b8;margin-top:16px;text-align:center">Generated by OfferFlow · {today}</p>
  </div>
</div>'''

    # ── Pattern: Modern ──────────────────────────────────────────────────────
    elif pattern == 'modern':
        items = [
            ('Role', e('role', role)),
            ('Joining Date', e('joining_date', joining)),
            ('Annual CTC', f'₹ {e("salary", salary)}'),
            ('Employment Type', e('employment_type', emp_type)),
        ]
        detail_rows = ''.join(
            f'<div style="display:flex;align-items:center;padding:10px 0;border-bottom:1px solid #f1f5f9">'
            f'<span style="width:140px;font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.5px">{k}</span>'
            f'<span style="font-size:13px;color:#0f172a;font-weight:700">{v}</span>'
            f'</div>'
            for k,v in items
        )
        return f'''
<div style="font-family:\'Segoe UI\',Arial,sans-serif;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(0,0,0,.08);display:flex;flex-direction:column">
  {lh_block}
  <div style="display:flex;min-height:520px">
    <div style="width:8px;background:linear-gradient(180deg,#1a56db,#0ea5e9);flex-shrink:0"></div>
    <div style="flex:1;padding:30px 32px">
      {"" if lh_b64 else f'<div style="font-size:20px;font-weight:900;color:#0d1b3e;margin-bottom:2px">{company}</div><div style="font-size:11px;color:#94a3b8;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px">Human Resources</div>'}
      <div style="display:inline-block;background:#dbeafe;color:#1a56db;font-size:10px;font-weight:700;padding:4px 12px;border-radius:20px;letter-spacing:.8px;text-transform:uppercase;margin-bottom:16px">Official Offer Letter</div>
      <div style="font-size:22px;font-weight:900;color:#0f172a;margin-bottom:6px">Congratulations, {e('name', name)}! 🎉</div>
      <p style="font-size:13px;color:#64748b;line-height:1.9;margin-bottom:20px">
        We're thrilled to offer you the role of <b style="color:#1a56db">{e('role', role)}</b> at <b>{company}</b>.
        Please review the details below and respond at your earliest convenience.
      </p>
      <div style="background:#f8faff;border-radius:10px;padding:18px 22px;margin-bottom:20px">
        <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px">Offer Details</div>
        {detail_rows}
      </div>
      <p style="font-size:12px;color:#64748b;line-height:1.8;margin-bottom:14px">
        This offer is contingent on background verification, document submission, and acceptance of employment terms.
      </p>
      <p style="font-size:12px;color:#475569">A confirmation link has been sent to <b>{email}</b>. Please respond within 48 hours.</p>
      <div style="margin-top:24px;padding-top:16px;border-top:1px solid #f1f5f9">
        <p style="font-size:12px;color:#0f172a;margin:0">Best regards,</p>
        <p style="font-size:13px;font-weight:700;color:#1a56db;margin:4px 0 0">{company} — HR Team</p>
        <p style="font-size:10px;color:#94a3b8;margin-top:10px">{today} · OfferFlow</p>
      </div>
    </div>
  </div>
</div>'''

    # ── Pattern: Elegant ────────────────────────────────────────────────────
    elif pattern == 'elegant':
        rows = ''.join(
            f'<tr><td style="padding:10px 16px;font-size:12px;color:#7c5f1e;font-weight:600;border-bottom:1px solid #fef3c7;width:44%">{k}</td>'
            f'<td style="padding:10px 16px;font-size:12px;color:#1e293b;border-bottom:1px solid #fef3c7">{v}</td></tr>'
            for k,v in [
                ('Candidate Name', e('name', name)),
                ('Position', e('role', role)),
                ('Commencement Date', e('joining_date', joining)),
                ('Annual Remuneration', f'₹ {e("salary", salary)}'),
                ('Nature of Employment', e('employment_type', emp_type)),
            ]
        )
        return f'''
<div style="font-family:Garamond,Georgia,serif;background:#fffdf5;border:1px solid #f59e0b;border-radius:12px;overflow:hidden;box-shadow:0 2px 16px rgba(245,158,11,.12)">
  {lh_block}
  <div style="padding:32px 38px">
    {"" if lh_b64 else f'<div style="text-align:center;margin-bottom:20px"><div style="font-size:24px;font-weight:700;color:#7c5f1e;letter-spacing:1.5px">{company.upper()}</div><div style="width:60px;height:2px;background:linear-gradient(90deg,#f59e0b,#d97706);margin:8px auto;border-radius:2px"></div><div style="font-size:11px;color:#a16207;letter-spacing:2px;text-transform:uppercase">Human Resources</div></div>'}
    <div style="text-align:center;font-size:16px;font-weight:700;color:#7c5f1e;letter-spacing:3px;text-transform:uppercase;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #fde68a">
      ✦ Offer of Employment ✦
    </div>
    <div style="font-size:11px;color:#a16207;margin-bottom:14px;font-style:italic">Date: {today}</div>
    <p style="font-size:13px;color:#1e293b;margin-bottom:10px">Dear <b>{e('name', name)}</b>,</p>
    <p style="font-size:12.5px;color:#57534e;line-height:2;margin-bottom:18px">
      It is our distinct pleasure to extend a formal offer of employment for the distinguished position of
      <em><b style="color:#7c5f1e">{e('role', role)}</b></em> at <b>{company}</b>.
      We have reviewed your credentials with great admiration and are confident you will be an invaluable addition.
    </p>
    <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;overflow:hidden;margin-bottom:18px">
      <div style="background:#fef3c7;padding:8px 16px;font-size:11px;font-weight:700;color:#7c5f1e;letter-spacing:1px;text-transform:uppercase">Terms of Engagement</div>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
    </div>
    <p style="font-size:12px;color:#57534e;line-height:1.9;margin-bottom:14px">
      This offer is subject to the successful conclusion of background verification procedures, submission of requisite documentation, and your agreement to the terms of employment.
    </p>
    <p style="font-size:12px;color:#57534e">Kindly confirm your acceptance via the correspondence addressed to <b>{email}</b>.</p>
    <div style="margin-top:24px;padding-top:16px;border-top:1px solid #fde68a">
      <p style="font-size:12px;color:#1e293b;margin:0">Yours sincerely,</p>
      <p style="font-size:13px;font-weight:700;color:#7c5f1e;margin:4px 0 0">Office of Human Resources — {company}</p>
      <p style="font-size:10px;color:#a3a3a3;margin-top:14px;text-align:center;font-style:italic">Issued via OfferFlow · {today}</p>
    </div>
  </div>
</div>'''

    # ── Pattern: Bold ───────────────────────────────────────────────────────
    elif pattern == 'bold':
        cards = ''.join(
            f'<div style="background:rgba(255,255,255,.08);border-radius:8px;padding:12px 16px;margin-bottom:8px">'
            f'<div style="font-size:10px;color:#6ee7b7;font-weight:700;text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px">{k}</div>'
            f'<div style="font-size:14px;color:#fff;font-weight:700">{v}</div>'
            f'</div>'
            for k,v in [
                ('Role', e('role', role)),
                ('Start Date', e('joining_date', joining)),
                ('Annual CTC', f'₹ {e("salary", salary)}'),
                ('Employment Type', e('employment_type', emp_type)),
            ]
        )
        return f'''
<div style="font-family:\'Segoe UI\',Arial,sans-serif;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 20px rgba(0,0,0,.1)">
  {lh_block}
  <div style="background:linear-gradient(135deg,#064e3b 0%,#065f46 50%,#047857 100%);padding:32px 36px;color:#fff">
    {"" if lh_b64 else f'<div style="font-size:13px;font-weight:700;color:#6ee7b7;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px">{company}</div>'}
    <div style="font-size:28px;font-weight:900;line-height:1.2;margin-bottom:8px">You're In! 🚀</div>
    <div style="font-size:14px;color:rgba(255,255,255,.8);margin-bottom:24px">Official Offer — {today}</div>
    <div style="font-size:15px;margin-bottom:20px">Hello <b>{e('name', name)}</b>, we're excited to have you join us as <b style="color:#6ee7b7">{e('role', role)}</b>.</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">{cards}</div>
  </div>
  <div style="padding:24px 36px">
    <p style="font-size:12px;color:#64748b;line-height:1.9;margin-bottom:14px">
      This offer is valid for <b>48 hours</b>. It is contingent on background verification and document submission.
      Please click the response buttons in the email sent to <b>{email}</b>.
    </p>
    <div style="background:#f0fdf4;border-left:4px solid #10b981;border-radius:4px;padding:12px 16px;margin-bottom:16px">
      <div style="font-size:11px;font-weight:700;color:#065f46;text-transform:uppercase;letter-spacing:.5px">What happens next?</div>
      <ul style="font-size:12px;color:#475569;margin:8px 0 0;padding-left:16px;line-height:2">
        <li>Accept the offer via the email link</li>
        <li>Complete background verification</li>
        <li>Submit required documents</li>
        <li>Get your onboarding schedule</li>
      </ul>
    </div>
    <p style="font-size:12px;color:#1e293b;margin:0">All the best,</p>
    <p style="font-size:13px;font-weight:700;color:#065f46;margin:4px 0 0">{company} HR Team</p>
    <p style="font-size:10px;color:#94a3b8;margin-top:12px">OfferFlow · {today}</p>
  </div>
</div>'''

    # ── Pattern: Custom ─────────────────────────────────────────────────────
    elif pattern == 'custom':
        # Replace placeholders in user-supplied custom_text
        filled = (custom_text
            .replace('{name}', e('name', name))
            .replace('{role}', e('role', role))
            .replace('{company}', company)
            .replace('{salary}', e('salary', salary))
            .replace('{joining_date}', e('joining_date', joining))
            .replace('{employment_type}', e('employment_type', emp_type))
            .replace('{email}', email)
            .replace('{date}', today)
            .replace('\n', '<br>')
        )
        return f'''
<div style="font-family:\'Segoe UI\',Arial,sans-serif;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.06)">
  {lh_block}
  <div style="padding:28px 36px">
    {"" if lh_b64 else f'<div style="font-size:20px;font-weight:900;color:#6d28d9;margin-bottom:4px">{company}</div><div style="font-size:11px;color:#94a3b8;margin-bottom:16px">Human Resources Department</div>'}
    <div style="background:#f5f3ff;border-left:4px solid #6d28d9;border-radius:4px;padding:8px 14px;margin-bottom:18px;font-size:11px;color:#5b21b6;font-weight:600">
      📝 Custom Letter Template — {today}
    </div>
    <div style="font-size:13px;color:#1e293b;line-height:2">{filled}</div>
    <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0 12px"/>
    <p style="font-size:12px;color:#94a3b8;text-align:center">Generated by OfferFlow · {today}</p>
  </div>
</div>'''

    # Fallback to classic
    return build_letter_html('classic', candidate, hr_user, editable=editable)


# ── Preview HTML builder (with edit overlay) ──────────────────────────────────
def letterhead_preview_html(hr_user, candidate, pattern='classic', custom_text=''):
    """Returns the full preview HTML with edit controls and live letter."""
    letter_html = build_letter_html(pattern, candidate, hr_user, custom_text, editable=True)

    edit_bar = '''
    <div id="edit-bar" style="background:#1a56db;color:#fff;padding:10px 18px;
         border-radius:8px;margin-bottom:14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      <span style="font-size:12px;font-weight:700">✏️ Inline Edit Mode</span>
      <span style="font-size:11px;opacity:.8">Click any underlined field to edit it directly</span>
      <button onclick="collectEdits()" style="margin-left:auto;background:#fff;color:#1a56db;
              border:none;padding:7px 18px;border-radius:6px;font-weight:700;font-size:12px;cursor:pointer">
        💾 Save Edits
      </button>
      <button onclick="resetEdits()" style="background:rgba(255,255,255,.15);color:#fff;
              border:1px solid rgba(255,255,255,.3);padding:7px 14px;border-radius:6px;
              font-size:12px;cursor:pointer">
        ↺ Reset
      </button>
    </div>
    <script>
    function collectEdits() {
      const edits = {};
      document.querySelectorAll('[contenteditable][data-field]').forEach(el => {
        edits[el.dataset.field] = el.innerText.trim();
      });
      // Dispatch to parent page if inside an iframe, or call window callback
      if (window.onPreviewEdits) window.onPreviewEdits(edits);
      else if (window.parent && window.parent.onPreviewEdits) window.parent.onPreviewEdits(edits);
      else {
        const ev = new CustomEvent('previewEdits', {detail: edits, bubbles: true});
        document.dispatchEvent(ev);
      }
      const btn = event.target;
      btn.textContent = '✅ Saved!';
      setTimeout(() => btn.textContent = '💾 Save Edits', 1500);
    }
    function resetEdits() {
      location.reload();
    }
    </script>
    '''
    return edit_bar + letter_html


# ── PDF offer letter generator (kept for legacy; no longer auto-attached) ────
def generate_offer_pdf(candidate, hr_user, pattern='classic', custom_text=''):
    """
    Generates a PDF offer letter. Note: PDF is NO LONGER auto-attached to
    accept/decline emails. This function is retained for manual download use.
    """
    cid      = candidate['id']
    out_path = os.path.join(LETTER_DIR, f"offer_{cid}.pdf")
    today    = datetime.now().strftime('%d %B %Y')
    company  = hr_user['company_name']
    name     = candidate.get('name','')
    role     = candidate.get('role','')
    joining  = candidate.get('joining_date','')
    salary   = candidate.get('salary','')
    emp_type = candidate.get('employment_type','full_time').replace('_',' ').title()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=22*mm, leftMargin=22*mm,
                            topMargin=18*mm, bottomMargin=22*mm)
    styles = getSampleStyleSheet()

    def style(name,**kw):
        s = ParagraphStyle(name, parent=styles['Normal'], **kw)
        return s

    co_style  = style('co', fontName='Helvetica-Bold', fontSize=20,
                      textColor=colors.HexColor('#0d1b3e'), alignment=TA_CENTER, spaceAfter=2)
    sub_style = style('sub', fontName='Helvetica', fontSize=10,
                      textColor=colors.HexColor('#64748b'), alignment=TA_CENTER, spaceAfter=14)
    h2_style  = style('h2', fontName='Helvetica-Bold', fontSize=15,
                      textColor=colors.HexColor('#0d1b3e'), alignment=TA_CENTER, spaceAfter=16)
    body      = style('b', fontName='Helvetica', fontSize=10.5, leading=17,
                      textColor=colors.HexColor('#1e293b'), spaceAfter=8)
    bold_body = style('bb', fontName='Helvetica-Bold', fontSize=10.5, leading=17,
                      textColor=colors.HexColor('#0d1b3e'), spaceAfter=6)
    small_gray= style('sg', fontName='Helvetica', fontSize=9,
                      textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)

    tbl_data = [
        ['Field','Details'],
        ['Candidate Name', name],
        ['Designation / Role', role],
        ['Joining Date', joining],
        ['Annual CTC', f'₹ {salary}'],
        ['Employment Type', emp_type],
        ['Reporting Location', 'As communicated by HR'],
    ]
    tbl = Table(tbl_data, colWidths=[68*mm, 102*mm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',  (0,0),(-1,0), colors.HexColor('#0d1b3e')),
        ('TEXTCOLOR',   (0,0),(-1,0), colors.white),
        ('FONTNAME',    (0,0),(-1,0), 'Helvetica-Bold'),
        ('FONTSIZE',    (0,0),(-1,0), 10),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),
         [colors.HexColor('#f0f4ff'), colors.HexColor('#ffffff')]),
        ('FONTNAME',    (0,1),(0,-1), 'Helvetica-Bold'),
        ('FONTNAME',    (1,1),(1,-1), 'Helvetica'),
        ('FONTSIZE',    (0,1),(-1,-1), 10),
        ('TEXTCOLOR',   (0,1),(0,-1), colors.HexColor('#1a56db')),
        ('GRID',        (0,0),(-1,-1), 0.5, colors.HexColor('#e2e8f0')),
        ('ROWPADDING',  (0,0),(-1,-1), 9),
        ('VALIGN',      (0,0),(-1,-1), 'MIDDLE'),
    ]))

    story = [
        Paragraph(company, co_style),
        Paragraph('Human Resources Department', sub_style),
        HRFlowable(width='100%', thickness=2.5,
                   color=colors.HexColor('#1a56db'), spaceAfter=10),
        Paragraph('OFFER OF EMPLOYMENT', h2_style),
        Paragraph(f'Date: {today}', style('dt', fontName='Helvetica',
                  fontSize=10, textColor=colors.HexColor('#64748b'), spaceAfter=14)),
        Paragraph(f'Dear <b>{name}</b>,', body),
        Spacer(1,4),
        Paragraph(
            f'We are delighted to extend this offer of employment for the position of '
            f'<b>{role}</b> at <b>{company}</b>.', body),
        Spacer(1,10),
        Paragraph('Your offer details:', bold_body),
        Spacer(1,6),
        tbl,
        Spacer(1,14),
        Paragraph('This offer is contingent upon:', bold_body),
        Paragraph('• Successful completion of background verification and reference checks.', body),
        Paragraph('• Submission of all required documents before joining.', body),
        Paragraph('• Acceptance of the company\'s Code of Conduct and employment terms.', body),
        Spacer(1,12),
        Paragraph('Warm regards,', body),
        Paragraph(f'<b>HR Department — {company}</b>', bold_body),
        Spacer(1,20),
        Paragraph(f'Generated by OfferFlow on {today}.', small_gray),
    ]

    doc.build(story)
    buf.seek(0)
    open(out_path,'wb').write(buf.read())
    return out_path


# ── Email HTML builders ────────────────────────────────────────────────────────
def offer_email_html(c, hr, accept_link, decline_link):
    """
    Offer email sent to candidate.
    NOTE: No PDF is attached — PDF attachment removed as requested.
    The email contains Accept/Decline buttons only.
    """
    company  = hr['company_name']
    name     = c.get('name','')
    role     = c.get('role','')
    joining  = c.get('joining_date','')
    salary   = c.get('salary','')
    emp_type = c.get('employment_type','full_time').replace('_',' ').title()
    rows     = ''.join(
        f'<tr style="background:{"#f8faff" if i%2==0 else "#fff"}">'
        f'<td style="padding:11px 16px;font-weight:700;color:#1a56db;font-size:13px;width:38%">{k}</td>'
        f'<td style="padding:11px 16px;color:#1e293b;font-size:13px">{v}</td></tr>'
        for i,(k,v) in enumerate([
            ('Role', role),
            ('Joining Date', joining),
            ('Annual CTC', '₹ '+str(salary)),
            ('Employment Type', emp_type)
        ])
    )
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e 0%,#1a56db 100%);padding:36px 40px;text-align:center">
    <div style="font-size:26px;font-weight:900;color:#fff;letter-spacing:1px">{company}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.7);margin-top:6px">Official Offer Letter</div>
  </td></tr>
  <tr><td style="padding:36px 40px">
    <h2 style="font-size:22px;color:#0d1b3e;margin:0 0 8px">🎉 Congratulations, {name}!</h2>
    <p style="color:#64748b;font-size:13.5px;line-height:1.9;margin:0 0 22px">
      We are pleased to extend an offer for the role of
      <b style="color:#1a56db">{role}</b> at <b>{company}</b>.
      Please review the details below and <b>respond within 48 hours</b>.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:28px">
      {rows}
    </table>
    <p style="color:#475569;font-size:13px;margin-bottom:20px;text-align:center">
      Click a button below to respond to this offer:
    </p>
    <table cellpadding="0" cellspacing="0" style="margin:0 auto">
      <tr>
        <td style="padding-right:14px">
          <a href="{accept_link}"
            style="display:inline-block;background:#10b981;color:#fff;
               padding:14px 34px;border-radius:9px;text-decoration:none;font-weight:700;
               font-size:15px;letter-spacing:.3px;font-family:Arial,sans-serif">✓ Accept Offer</a>
        </td>
        <td>
          <a href="{decline_link}"
            style="display:inline-block;background:#ef4444;color:#fff;
               padding:14px 34px;border-radius:9px;text-decoration:none;font-weight:700;
               font-size:15px;letter-spacing:.3px;font-family:Arial,sans-serif">✕ Decline Offer</a>
        </td>
      </tr>
    </table>
    <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:22px;line-height:1.7">
      ⏰ Offer expires automatically after 48 hours if no response.
    </p>
  </td></tr>
  <tr><td style="background:#f8faff;padding:14px 40px;text-align:center;border-top:1px solid #e2e8f0">
    <p style="color:#94a3b8;font-size:11px;margin:0">Sent via OfferFlow · {company} HR Portal</p>
  </td></tr>
</table></td></tr></table></body></html>"""


def verification_email_html(c, bg_link, company):
    """Immediate verification email sent right after candidate accepts."""
    name = c.get('name', '')
    role = c.get('role', '')
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e,#1a56db);padding:30px 40px;text-align:center">
    <div style="font-size:22px;font-weight:900;color:#fff">{company}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.7);margin-top:5px">Background Verification</div>
  </td></tr>
  <tr><td style="padding:36px 40px">
    <div style="font-size:40px;text-align:center;margin-bottom:14px">📋</div>
    <h2 style="font-size:20px;color:#0d1b3e;text-align:center;margin:0 0 10px">
      Next Step: Background Verification
    </h2>
    <p style="color:#64748b;font-size:13.5px;line-height:1.9;text-align:center;margin:0 0 24px">
      Hello <b>{name}</b>, thank you for accepting the offer for <b style="color:#1a56db">{role}</b>!<br>
      Please complete your background verification to proceed with onboarding.
    </p>
    <div style="text-align:center;margin-bottom:24px">
      <a href="{bg_link}"
        style="display:inline-block;background:#1a56db;color:#fff;
               padding:15px 40px;border-radius:10px;text-decoration:none;
               font-weight:700;font-size:15px;letter-spacing:.3px">
        🚀 Start Background Verification
      </a>
    </div>
    <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:14px 18px">
      <div style="font-size:12px;font-weight:700;color:#0369a1;margin-bottom:8px">You will need to provide:</div>
      <ul style="font-size:12px;color:#475569;line-height:2;margin:0;padding-left:18px">
        <li>Personal details (Aadhaar, PAN)</li>
        <li>Educational qualifications</li>
        <li>Previous employment details (if applicable)</li>
        <li>Your digital signature</li>
      </ul>
    </div>
    <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:18px">
      This link is unique to you. Do not share it.
    </p>
  </td></tr>
  <tr><td style="background:#f8faff;padding:12px 40px;text-align:center;border-top:1px solid #e2e8f0">
    <p style="color:#94a3b8;font-size:11px;margin:0">Sent via OfferFlow · {company} HR Portal</p>
  </td></tr>
</table></td></tr></table></body></html>"""


# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    user = next((u for u in get_users().values()
                 if u['username']==data.get('username')
                 and u['password']==hash_pw(data.get('password',''))), None)
    if not user:
        return jsonify({'success':False,'message':'Invalid credentials'}), 401
    session['user_id'] = user['id']
    return jsonify({'success':True})

@app.route('/api/register', methods=['POST'])
def api_register():
    users        = get_users()
    username     = request.form.get('username','').strip()
    email        = request.form.get('email','').strip()
    password     = request.form.get('password','')
    confirm      = request.form.get('confirm_password','')
    company_name = request.form.get('company_name','').strip()

    if not all([username,email,password,confirm,company_name]):
        return jsonify({'success':False,'message':'All fields are required'}), 400
    if any(u['username']==username for u in users.values()):
        return jsonify({'success':False,'message':'Username already exists'}), 400
    if any(u['email']==email for u in users.values()):
        return jsonify({'success':False,'message':'Email already registered'}), 400
    if password != confirm:
        return jsonify({'success':False,'message':'Passwords do not match'}), 400
    if not validate_password(password):
        return jsonify({'success':False,'message':
            'Password needs 8+ chars, uppercase, lowercase & special character'}), 400

    lh = request.files.get('letterhead')
    if not lh or not lh.filename:
        return jsonify({'success':False,'message':'Company letterhead (PDF) is required'}), 400
    if not lh.filename.lower().endswith('.pdf'):
        return jsonify({'success':False,'message':'Letterhead must be a PDF file'}), 400
    fname   = secure_filename(lh.filename)
    lh_path = os.path.join(UPLOAD_DIR,'letterheads',fname)
    lh.save(lh_path)

    uid = str(uuid.uuid4())
    users[uid] = {'id':uid,'username':username,'email':email,
                  'password':hash_pw(password),'company_name':company_name,
                  'letterhead':lh_path,'created_at':str(datetime.now())}
    save_users(users)
    return jsonify({'success':True})

@app.route('/api/forgot-password', methods=['POST'])
def api_forgot_password():
    email = (request.json or {}).get('email','').strip()
    user  = next((u for u in get_users().values() if u['email']==email), None)
    if not user:
        return jsonify({'success':False,'message':'Email not found'}), 404
    token  = str(uuid.uuid4())
    tokens = get_tokens()
    tokens[token] = {'user_id':user['id'],
                     'expires':str(datetime.now()+timedelta(hours=1))}
    save_tokens(tokens)
    link = f"{BASE_URL}/reset-password/{token}"
    html = f"""<!DOCTYPE html><html><body style="margin:0;padding:32px 0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="480" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e,#1a56db);padding:28px;text-align:center">
    <div style="font-size:22px;font-weight:900;color:#fff">OfferFlow</div>
    <div style="color:rgba(255,255,255,.7);font-size:12px;margin-top:4px">Password Reset</div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <div style="font-size:44px;margin-bottom:14px">🔑</div>
    <h2 style="font-size:19px;color:#0d1b3e;margin:0 0 10px">Reset Your Password</h2>
    <p style="color:#64748b;font-size:13px;margin:0 0 26px;line-height:1.7">
      Click the button below. This link expires in <b>1 hour</b>.
    </p>
    <a href="{link}" style="display:inline-block;background:#1a56db;color:#fff;
       padding:13px 34px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">
      Reset Password
    </a>
    <p style="color:#94a3b8;font-size:11px;margin-top:20px">If you did not request this, ignore this email.</p>
  </td></tr>
</table></td></tr></table></body></html>"""
    send_email(email, 'Reset Your OfferFlow Password', html)
    return jsonify({'success':True,'message':'Password reset link sent to your email'})

@app.route('/reset-password/<token>')
def reset_password_page(token):
    if token not in get_tokens(): return redirect('/login')
    return render_template('reset_password.html', token=token)

@app.route('/api/reset-password', methods=['POST'])
def api_reset_password():
    data    = request.json or {}
    token   = data.get('token','')
    pw      = data.get('password','')
    confirm = data.get('confirm_password','')
    tokens  = get_tokens()
    if token not in tokens:
        return jsonify({'success':False,'message':'Invalid or expired link'}), 400
    if pw != confirm:
        return jsonify({'success':False,'message':'Passwords do not match'}), 400
    if not validate_password(pw):
        return jsonify({'success':False,'message':'Password requirements not met'}), 400
    users = get_users()
    uid   = tokens[token]['user_id']
    users[uid]['password'] = hash_pw(pw)
    save_users(users)
    del tokens[token]; save_tokens(tokens)
    return jsonify({'success':True})

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

# Dashboard
@app.route('/dashboard')
def dashboard():
    if not current_user(): return redirect('/login')
    return render_template('dashboard.html')

@app.route('/api/dashboard-stats')
def api_dashboard_stats():
    u = current_user()
    if not u: return jsonify({}), 401
    uid    = u['id']
    cands  = {k:v for k,v in get_cands().items() if v.get('hr_id')==uid}
    verifs = {k:v for k,v in get_verifs().items() if v.get('hr_id')==uid}
    return jsonify({
        'total_offers':         len(cands),
        'offer_accepted':       sum(1 for c in cands.values() if c.get('offer_status')=='accepted'),
        'offer_declined':       sum(1 for c in cands.values() if c.get('offer_status')=='declined'),
        'action_pending':       sum(1 for c in cands.values() if c.get('offer_status')=='pending'),
        'cancelled':            sum(1 for c in cands.values() if c.get('offer_status')=='cancelled'),
        'verified':             sum(1 for v in verifs.values() if v.get('verification_status')=='verified'),
        'rejected':             sum(1 for v in verifs.values() if v.get('verification_status')=='rejected'),
        'verification_pending': sum(1 for v in verifs.values() if v.get('verification_status')=='pending'),
        'company_name': u['company_name'],
    })

@app.route('/api/offers')
def api_offers():
    u = current_user()
    if not u: return jsonify([]), 401
    status = request.args.get('status')
    result = [{'id':cid,'name':c['name'],'role':c.get('role',''),
               'joining_date':c.get('joining_date',''),'email':c.get('email',''),
               'salary':c.get('salary',''),'employment_type':c.get('employment_type',''),
               'offer_status':c.get('offer_status','pending')}
              for cid,c in get_cands().items() if c.get('hr_id')==u['id']]
    if status: result = [r for r in result if r['offer_status']==status]
    return jsonify(result)

@app.route('/api/verifications')
def api_verifications():
    u = current_user()
    if not u: return jsonify([]), 401
    status = request.args.get('status')
    result = [{'id':vid,'name':v['name'],'email':v.get('email',''),
               'salary':v.get('salary',''),'phone':v.get('phone',''),
               'verification_status':v.get('verification_status','pending')}
              for vid,v in get_verifs().items() if v.get('hr_id')==u['id']]
    if status: result = [r for r in result if r['verification_status']==status]
    return jsonify(result)

# ── Letter Patterns API ───────────────────────────────────────────────────────
@app.route('/api/letter-patterns')
def api_letter_patterns():
    """Return list of available letter patterns."""
    return jsonify([
        {'id': pid, **info}
        for pid, info in LETTER_PATTERNS.items()
    ])

# Upload Excel
@app.route('/upload-excel')
def upload_excel_page():
    if not current_user(): return redirect('/login')
    return render_template('upload_excel.html')

@app.route('/api/upload-excel', methods=['POST'])
def api_upload_excel():
    u = current_user()
    if not u: return jsonify({'success':False}), 401
    f = request.files.get('file')
    if not f or not f.filename.endswith('.xlsx'):
        return jsonify({'success':False,'message':'Please upload a .xlsx file'}), 400
    path = os.path.join(UPLOAD_DIR,'excel',secure_filename(f.filename))
    f.save(path)
    try:
        df = pd.read_excel(path)
        required = ['Name','Gmail id','Role','Joining date','Salary']
        missing  = [r for r in required if r not in df.columns]
        if missing:
            return jsonify({'success':False,
                'message':f'Missing columns: {", ".join(missing)}'}), 400
        return jsonify({'success':True,'records':df.fillna('').to_dict('records'),
                        'columns':list(df.columns)})
    except Exception as e:
        return jsonify({'success':False,'message':str(e)}), 500

@app.route('/api/save-candidates', methods=['POST'])
def api_save_candidates():
    u = current_user()
    if not u: return jsonify({'success':False}), 401
    data = request.json or {}
    cands = get_cands()
    new_ids = []
    for rec in data.get('candidates',[]):
        cid = str(uuid.uuid4())
        cands[cid] = {
            'id':cid,'hr_id':u['id'],
            'name':rec.get('Name',''),'email':rec.get('Gmail id',''),
            'role':rec.get('Role',''),'joining_date':str(rec.get('Joining date','')),
            'salary':str(rec.get('Salary','')),'employment_type':data.get('employment_type','full_time'),
            'letter_pattern': data.get('letter_pattern','classic'),
            'custom_letter_text': data.get('custom_letter_text',''),
            'offer_status':'pending','sent_at':str(datetime.now()),
            'extra':{k:v for k,v in rec.items() if k not in ['Name','Gmail id','Role','Joining date','Salary']}
        }
        new_ids.append(cid)
    save_cands(cands)
    return jsonify({'success':True,'candidate_ids':new_ids})

@app.route('/api/preview-letter', methods=['POST'])
def api_preview_letter():
    u = current_user()
    if not u: return jsonify({'success':False}), 401
    data = request.json or {}
    c = data.get('candidate',{})
    c['employment_type'] = data.get('employment_type','full_time')
    c['id'] = 'preview'
    pattern     = data.get('letter_pattern','classic')
    custom_text = data.get('custom_letter_text','')
    html = letterhead_preview_html(u, c, pattern=pattern, custom_text=custom_text)
    return jsonify({'success':True,'html': html})

@app.route('/api/send-offer-emails', methods=['POST'])
def api_send_offer_emails():
    u = current_user()
    if not u:
        return jsonify({'success':False}), 401

    data  = request.json or {}
    cids  = data.get('candidate_ids', [])
    cands = get_cands()
    sent  = 0

    for cid in cids:
        c = cands.get(cid)
        if not c:
            continue

        # Prevent duplicate sending
        if c.get('email_sent_at'):
            print(f"Email already sent to {c['email']}")
            continue

        accept_link  = f"{BASE_URL}/offer-response/{cid}/accept"
        decline_link = f"{BASE_URL}/offer-response/{cid}/decline"

        # Send offer email WITHOUT PDF attachment
        subject = f"Job Offer — {c.get('role','')} at {u['company_name']}"
        send_email(
            c['email'],
            subject,
            offer_email_html(c, u, accept_link, decline_link)
            # No attach_path — PDF removed from offer email
        )

        # Mark email as sent
        cands[cid]['email_sent_at'] = str(datetime.now())
        sent += 1

    save_cands(cands)

    # 48h auto-cancel
    def auto_cancel(ids):
        import time
        time.sleep(172800)
        c2 = get_cands()
        changed = any(c2.get(i, {}).get('offer_status') == 'pending' for i in ids)
        for i in ids:
            if c2.get(i, {}).get('offer_status') == 'pending':
                c2[i]['offer_status'] = 'cancelled'
        if changed:
            save_cands(c2)

    threading.Thread(target=auto_cancel, args=(cids,), daemon=True).start()

    return jsonify({'success':True,'sent':sent})


# ── Offer Response (Accept / Decline) ─────────────────────────────────────────
@app.route('/offer-response/<cid>/<action>')
def offer_response(cid, action):
    cands = get_cands()
    c     = cands.get(cid)

    if not c:
        return """<div style='font-family:sans-serif;text-align:center;margin-top:80px'>
                    <h2>Invalid or expired link.</h2></div>""", 404

    current_status = c.get('offer_status', 'pending')

    # Already responded
    if current_status in ['accepted', 'declined', 'cancelled']:
        return render_template(
            'offer_accepted.html' if current_status == 'accepted' else 'offer_declined.html',
            candidate=c
        )

    # ── ACCEPT ───────────────────────────────────────────────────────────────
    if action == 'accept':
        c['offer_status'] = 'accepted'
        c['responded_at'] = str(datetime.now())
        save_cands(cands)

        verifs = get_verifs()

        # Avoid duplicate verification entry
        existing = next((v for v in verifs.values() if v.get('candidate_id') == cid), None)

        if not existing:
            vid = str(uuid.uuid4())
            verifs[vid] = {
                'id':  vid,
                'candidate_id': cid,
                'hr_id': c['hr_id'],
                'name': c['name'],
                'email': c['email'],
                'salary': c['salary'],
                'phone': '',
                'verification_status': 'pending',
                'created_at': str(datetime.now())
            }
            save_verifs(verifs)

            # ── Send verification email IMMEDIATELY ──────────────────────────
            users   = get_users()
            hr      = users.get(c['hr_id'], {})
            company = hr.get('company_name', 'the company')
            bg_link = f"{BASE_URL}/background-verification/{vid}"

            send_email(
                c['email'],
                f'Next Step: Complete Background Verification — {company}',
                verification_email_html(c, bg_link, company)
            )

        return render_template('offer_accepted.html', candidate=c)

    # ── DECLINE ──────────────────────────────────────────────────────────────
    elif action == 'decline':
        c['offer_status'] = 'declined'
        c['responded_at'] = str(datetime.now())
        save_cands(cands)
        return render_template('offer_declined.html', candidate=c)

    return """<div style='font-family:sans-serif;text-align:center;margin-top:80px'>
                <h2>Invalid action.</h2></div>"""


# ── Background Verification ────────────────────────────────────────────────────
@app.route('/background-verification/<vid>')
def background_verification_page(vid):
    verifs = get_verifs()
    v = verifs.get(vid)
    if not v:
        return """<div style='font-family:sans-serif;text-align:center;margin-top:80px'>
                    <h2>Invalid verification link.</h2></div>""", 404
    return render_template('background_verification.html', verification=v)


@app.route('/api/submit-verification', methods=['POST'])
def api_submit_verification():
    """
    Steps:
      step=1  → Personal info (Aadhaar, PAN, phone)
      step=2  → Education + DIGITAL SIGNATURE (uploaded as base64 image)
      step=3  → Experience / fresher
    """
    vid    = request.form.get('verification_id')
    verifs = get_verifs()
    v      = verifs.get(vid)
    if not v: return jsonify({'success':False}), 404

    step = request.form.get('step')

    if step == '1':
        fn = request.form.get('first_name','')
        ln = request.form.get('last_name','')
        v.update({
            'first_name': fn, 'last_name': ln, 'name': f"{fn} {ln}",
            'phone':   request.form.get('phone',''),
            'aadhaar': request.form.get('aadhaar',''),
            'pan':     request.form.get('pan',''),
        })

    elif step == '2':
        # ── Education + Digital Signature ──────────────────────────────────
        v.update({
            'college':        request.form.get('college',''),
            'specialization': request.form.get('specialization',''),
            'percentage':     request.form.get('percentage',''),
        })

        # Digital signature: sent as base64 data URL from canvas
        sig_data = request.form.get('digital_signature','')
        if sig_data and sig_data.startswith('data:image'):
            v['digital_signature'] = sig_data  # store full data URL
        else:
            v['digital_signature'] = ''

    elif step == '3':
        ctype = request.form.get('candidate_type','fresher')
        v['candidate_type'] = ctype
        users   = get_users()
        hr      = users.get(v.get('hr_id',''), {})
        company = hr.get('company_name','the company')

        if ctype == 'experienced':
            prev_co   = request.form.get('prev_company','')
            prev_role = request.form.get('prev_role','')
            co_email  = request.form.get('company_email','')
            duration  = request.form.get('duration','')
            v.update({
                'prev_company': prev_co, 'prev_role': prev_role,
                'company_email': co_email, 'duration': duration,
            })
            v['verification_status'] = 'pending'

            vlink = f"{BASE_URL}/company-verify/{vid}/verify"
            rlink = f"{BASE_URL}/company-verify/{vid}/reject"

            send_email(co_email,
                f"Employment Verification — {v['name']}",
                f"""<html><body style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="540" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e,#1a56db);padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">Employment Verification Request</div>
  </td></tr>
  <tr><td style="padding:36px">
    <p style="font-size:14px;color:#1e293b;line-height:1.8">
      We are conducting a background check for <b>{v['name']}</b>, who has indicated
      employment at your organisation as <b>{prev_role}</b> for <b>{duration}</b>.
    </p>
    <p style="font-size:13px;color:#64748b;margin-bottom:24px">Please respond using the buttons below:</p>
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="padding-right:12px">
        <a href="{vlink}" style="display:inline-block;background:#10b981;color:#fff;
           padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">
          ✓ Verify Employment</a>
      </td>
      <td>
        <a href="{rlink}" style="display:inline-block;background:#ef4444;color:#fff;
           padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">
          ✕ Cannot Verify</a>
      </td>
    </tr></table>
  </td></tr>
</table></td></tr></table></body></html>""")

        else:
            # Fresher → auto-verified
            v['verification_status'] = 'verified'
            send_email(v['email'], '🎉 Background Verification Complete!',
                f"""<html><body style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="500" style="background:#fff;border-radius:16px;overflow:hidden">
  <tr><td style="background:linear-gradient(135deg,#10b981,#059669);padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">Verification Successful ✅</div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <div style="font-size:44px;margin-bottom:14px">🎊</div>
    <h2 style="font-size:20px;color:#065f46">Congratulations, {v['name']}!</h2>
    <p style="color:#64748b;font-size:13px;line-height:1.8">
      Your background verification is complete. Welcome to <b>{company}</b>!
      Our HR team will be in touch with onboarding details shortly.
    </p>
  </td></tr>
</table></td></tr></table></body></html>""")

        v['submitted_at'] = str(datetime.now())

    save_verifs(verifs)
    return jsonify({'success':True,'status':v.get('verification_status')})


@app.route('/company-verify/<vid>/<action>')
def company_verify(vid, action):
    verifs  = get_verifs()
    v       = verifs.get(vid)
    if not v:
        return "<div style='font-family:sans-serif;text-align:center;margin-top:80px'><h2>Invalid link.</h2></div>", 404
    users   = get_users()
    hr      = users.get(v.get('hr_id',''), {})
    company = hr.get('company_name','the company')

    if action == 'verify':
        v['verification_status'] = 'verified'
        send_email(v['email'], '✅ Employment Verified — Welcome Aboard!',
            f"""<html><body style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="500" style="background:#fff;border-radius:16px;overflow:hidden">
  <tr><td style="background:linear-gradient(135deg,#10b981,#059669);padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">Background Verified ✅</div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <div style="font-size:44px;margin-bottom:14px">🎉</div>
    <h2 style="font-size:18px;color:#065f46">Congratulations, {v['name']}!</h2>
    <p style="color:#64748b;font-size:13px;line-height:1.8">
      Your employment has been verified. You are now a verified candidate at <b>{company}</b>.
      HR will send your final offer letter and onboarding schedule shortly.
    </p>
  </td></tr>
</table></td></tr></table></body></html>""")

    elif action == 'reject':
        v['verification_status'] = 'rejected'
        send_email(v['email'], 'Update on Your Application',
            f"""<html><body style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="500" style="background:#fff;border-radius:16px;overflow:hidden">
  <tr><td style="background:#ef4444;padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">Verification Update</div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <h2 style="font-size:18px;color:#991b1b">Dear {v['name']},</h2>
    <p style="color:#64748b;font-size:13px;line-height:1.8">
      We were unable to verify your employment history as provided.
      We regret we cannot proceed with your application at this time.
      Thank you for your interest and we wish you the very best.
    </p>
  </td></tr>
</table></td></tr></table></body></html>""")

    save_verifs(verifs)
    return render_template('company_verify_done.html', action=action, candidate=v)


@app.route('/api/download-template')
def download_template():
    u = current_user()
    if not u: return redirect('/login')
    cands = {k:v for k,v in get_cands().items() if v.get('hr_id')==u['id']}
    rows  = [{'Name':c['name'],'Gmail id':c['email'],'Role':c['role'],
              'Joining date':c['joining_date'],'Salary':c['salary'],
              'Employment Type':c['employment_type'],'Status':c['offer_status']}
             for c in cands.values()] or \
            [{'Name':'','Gmail id':'','Role':'','Joining date':'','Salary':'',
              'Employment Type':'','Status':''}]
    df = pd.DataFrame(rows)
    path = '/tmp/offers_export.xlsx'
    df.to_excel(path, index=False)
    return send_file(path, as_attachment=True, download_name='offers_export.xlsx')


if __name__ == '__main__':
    app.run(debug=True, port=5000)