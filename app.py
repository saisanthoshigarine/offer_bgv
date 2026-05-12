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
import threading
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

# ── Email via Brevo API ───────────────────────────────────────
def send_email(to, subject, html_body, attach_path=None, attach_name=None):
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = os.environ.get("BREVO_API_KEY")
        configuration.timeout = 10
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
            sender={
                "name": SENDER_NAME,
                "email": SENDER_EMAIL
            },
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


# ── PDF offer letter generator ─────────────────────────────────────────────────
def generate_offer_pdf(candidate, hr_user):
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
        [f'Annual CTC', f'\u20b9 {salary}'],
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
            f'<b>{role}</b> at <b>{company}</b>. We believe your skills and experience '
            f'are an excellent fit for our team.', body),
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
        Paragraph(
            'Please confirm your acceptance via the <b>Accept Offer</b> button in your email. '
            'We look forward to welcoming you to our team.', body),
        Spacer(1,24),
        HRFlowable(width='100%', thickness=0.5,
                   color=colors.HexColor('#e2e8f0'), spaceAfter=12),
        Paragraph('Warm regards,', body),
        Paragraph(f'<b>HR Department — {company}</b>', bold_body),
        Spacer(1,20),
        Paragraph(f'Generated by OfferFlow on {today}.', small_gray),
    ]

    # If letterhead PDF exists, add it as background on first page
    lh_path = hr_user.get('letterhead','')
    if lh_path and os.path.exists(lh_path):
        # Increase top margin to avoid overlapping letterhead header
        doc2 = SimpleDocTemplate(buf, pagesize=A4,
                                 rightMargin=22*mm, leftMargin=22*mm,
                                 topMargin=52*mm, bottomMargin=22*mm)
        buf2 = io.BytesIO()
        doc2 = SimpleDocTemplate(buf2, pagesize=A4,
                                 rightMargin=22*mm, leftMargin=22*mm,
                                 topMargin=52*mm, bottomMargin=22*mm)

        # Remove company header from story (letterhead has it)
        story_no_header = story[3:]   # skip co_style, sub_style, hrule
        doc2.build(story_no_header)
        buf2.seek(0)

        # Merge: draw letterhead PDF as background, overlay content
        try:
            _merge_letterhead(lh_path, buf2.read(), out_path)
        except Exception as e:
            print(f"Merge failed: {e}; using plain PDF")
            doc.build(story)
            buf.seek(0)
            open(out_path,'wb').write(buf.read())
    else:
        doc.build(story)
        buf.seek(0)
        open(out_path,'wb').write(buf.read())

    return out_path


def _merge_letterhead(lh_path, content_bytes, out_path):
    """
    Vercel-safe letterhead merge function.

    Since Vercel serverless functions do not support Poppler/pdf2image
    properly, this function safely writes the generated PDF content
    directly without attempting PDF background merging.

    Parameters:
        lh_path (str): Path to company letterhead PDF
        content_bytes (bytes): Generated offer letter PDF bytes
        out_path (str): Final output PDF path

    Returns:
        str: Output PDF path
    """

    try:
        # Ensure output directory exists
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        # Write generated PDF directly
        with open(out_path, "wb") as f:
            f.write(content_bytes)

        print(f"[PDF GENERATED] → {out_path}")

        return out_path

    except Exception as e:
        print(f"[MERGE LETTERHEAD ERROR] → {e}")
        return None


def letterhead_preview_html(hr_user, candidate):
    """HTML preview of offer letter rendered on letterhead."""
    company  = hr_user['company_name']
    lh_path  = hr_user.get('letterhead','')
    today    = datetime.now().strftime('%d %B %Y')
    name     = candidate.get('name','')
    role     = candidate.get('role','')
    joining  = candidate.get('joining_date','')
    salary   = candidate.get('salary','')
    emp_type = candidate.get('employment_type','full_time').replace('_',' ').title()
    email    = candidate.get('email','')

    lh_block = ''
    if lh_path and os.path.exists(lh_path):
        with open(lh_path,'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
        lh_block = f'''
        <div style="margin-bottom:0">
          <div style="background:#fff8e1;border:1px solid #f59e0b;border-radius:6px;
                      padding:8px 14px;margin-bottom:10px;font-size:11px;color:#92400e;">
            📄 <b>Company Letterhead</b> — this PDF is attached to the offer email sent to the candidate.
          </div>
          <iframe src="data:application/pdf;base64,{b64}" width="100%" height="180"
            style="border:1px solid #e2e8f0;border-radius:6px;display:block;margin-bottom:12px"
            title="Company Letterhead Preview"></iframe>
        </div>'''

    rows = ''.join(
        f'<tr style="background:{"#f0f4ff" if i%2==0 else "#fff"}">'
        f'<td style="padding:10px 14px;border:1px solid #e2e8f0;font-weight:700;color:#1a56db;width:42%">{k}</td>'
        f'<td style="padding:10px 14px;border:1px solid #e2e8f0;color:#1e293b">{v}</td></tr>'
        for i,(k,v) in enumerate([
            ('Candidate Name', name),('Designation / Role', role),
            ('Joining Date', joining),(f'Annual CTC', f'₹ {salary}'),
            ('Employment Type', emp_type),('Reporting Location','As communicated by HR'),
        ])
    )

    return f'''
<div style="font-family:Georgia,serif;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden">
  {lh_block}
  <div style="padding:30px 34px">
    <div style="text-align:center;margin-bottom:20px">
      <div style="font-family:Arial,sans-serif;font-size:22px;font-weight:900;color:#0d1b3e">{company}</div>
      <div style="font-size:11px;color:#64748b;margin-top:3px">Human Resources Department</div>
      <div style="height:3px;background:linear-gradient(90deg,#1a56db,#0ea5e9);width:70px;margin:10px auto 0;border-radius:2px"></div>
    </div>
    <div style="text-align:center;font-size:18px;font-weight:700;color:#0d1b3e;margin-bottom:18px;border-bottom:1.5px solid #e2e8f0;padding-bottom:14px">
      OFFER OF EMPLOYMENT
    </div>
    <div style="font-size:11px;color:#64748b;margin-bottom:10px">Date: {today}</div>
    <p style="font-size:13px;color:#1e293b;margin-bottom:10px">Dear <b>{name}</b>,</p>
    <p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">
      We are delighted to extend this offer of employment for the position of
      <b style="color:#1a56db">{role}</b> at <b>{company}</b>.
    </p>
    <p style="font-size:11.5px;font-weight:700;color:#0d1b3e;margin-bottom:8px">Offer Details:</p>
    <table style="width:100%;border-collapse:collapse;margin-bottom:16px;font-size:11.5px">
      {rows}
    </table>
    <p style="font-size:11.5px;font-weight:700;color:#0d1b3e;margin-bottom:6px">This offer is subject to:</p>
    <ul style="font-size:11.5px;color:#475569;line-height:2;margin-left:18px;margin-bottom:14px">
      <li>Successful completion of background verification.</li>
      <li>Submission of all required documents before joining.</li>
      <li>Acceptance of the company's Code of Conduct and employment terms.</li>
    </ul>
    <p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:20px">
      Please accept or decline via the buttons in the email. We look forward to welcoming you.
    </p>
    <div style="border-top:1px solid #e2e8f0;padding-top:16px">
      <p style="font-size:11.5px;color:#1e293b;margin:0">Warm regards,</p>
      <p style="font-size:12px;font-weight:700;color:#0d1b3e;margin:4px 0 0">HR Department — {company}</p>
    </div>
    <div style="margin-top:16px;padding:10px;background:#f8faff;border-radius:6px;
                font-size:10px;color:#94a3b8;text-align:center">
      📧 PDF offer letter will be sent as an attachment to <b>{email}</b>
    </div>
  </div>
</div>'''


# ── Email HTML builders ────────────────────────────────────────────────────────
def offer_email_html(c, hr, accept_link, decline_link):
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
        for i,(k,v) in enumerate([('Role',role),('Joining Date',joining),
            ('Annual CTC','₹ '+str(salary)),('Employment Type',emp_type)]))
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e 0%,#1a56db 100%);padding:36px 40px;text-align:center">
    <div style="font-size:26px;font-weight:900;color:#fff;letter-spacing:1px">{company}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.7);margin-top:6px">Official Offer Letter</div>
  </td></tr>
  <tr><td style="padding:36px 40px">
    <h2 style="font-size:22px;color:#0d1b3e;margin:0 0 8px">🎉 Congratulations, {name}!</h2>
    <p style="color:#64748b;font-size:13.5px;line-height:1.9;margin:0 0 22px">
      We are pleased to extend an offer for the role of <b style="color:#1a56db">{role}</b> at <b>{company}</b>.
      Please review the details below and <b>respond within 48 hours</b>.
    </p>
    <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;margin-bottom:28px">
      {rows}
    </table>
    <p style="color:#475569;font-size:13px;margin-bottom:20px;text-align:center">
      Click a button below to respond to this offer:
    </p>
    <table cellpadding="0" cellspacing="0" style="margin:0 auto">
      <tr>
        <td style="padding-right:14px">
          <a href="{accept_link}" style="display:inline-block;background:#10b981;color:#fff;
             padding:14px 34px;border-radius:9px;text-decoration:none;font-weight:700;
             font-size:15px;letter-spacing:.3px;font-family:Arial,sans-serif">✓ Accept Offer</a>
        </td>
        <td>
          <a href="{decline_link}" style="display:inline-block;background:#ef4444;color:#fff;
             padding:14px 34px;border-radius:9px;text-decoration:none;font-weight:700;
             font-size:15px;letter-spacing:.3px;font-family:Arial,sans-serif">✕ Decline Offer</a>
        </td>
      </tr>
    </table>
    <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:22px;line-height:1.7">
      ⏰ Offer expires automatically after 48 hours if no response.<br>
      📎 Your offer letter PDF is attached to this email.
    </p>
  </td></tr>
  <tr><td style="background:#f8faff;padding:14px 40px;text-align:center;border-top:1px solid #e2e8f0">
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
    return jsonify({'success':True,'html': letterhead_preview_html(u, c)})

@app.route('/api/send-offer-emails', methods=['POST'])
def api_send_offer_emails():

    u = current_user()

    if not u:
        return jsonify({'success':False}), 401

    data  = request.json or {}
    cids  = data.get('candidate_ids', [])
    cands = get_cands()

    sent = 0

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

        try:
            pdf_path = generate_offer_pdf(c, u)
        except Exception as e:
            print(f"PDF failed for {cid}: {e}")
            pdf_path = None

        subject = f"Job Offer — {c.get('role','')} at {u['company_name']}"

        send_email(
            c['email'],
            subject,
            offer_email_html(c, u, accept_link, decline_link),
            attach_path=pdf_path,
            attach_name=f"Offer_Letter_{c['name'].replace(' ','_')}.pdf"
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

        changed = any(
            c2.get(i, {}).get('offer_status') == 'pending'
            for i in ids
        )

        for i in ids:
            if c2.get(i, {}).get('offer_status') == 'pending':
                c2[i]['offer_status'] = 'cancelled'

        if changed:
            save_cands(c2)

    threading.Thread(
        target=auto_cancel,
        args=(cids,),
        daemon=True
    ).start()

    return jsonify({
        'success': True,
        'sent': sent
    })
@app.route('/offer-response/<cid>/<action>')
def offer_response(cid, action):
    cands = get_cands()
    c = cands.get(cid)

    if not c:
        return """
        <div style='font-family:sans-serif;text-align:center;margin-top:80px'>
            <h2>Invalid or expired link.</h2>
        </div>
        """, 404

    current_status = c.get('offer_status', 'pending')

    # Prevent multiple processing
    if current_status in ['accepted', 'declined', 'cancelled']:
        return render_template(
            'offer_accepted.html' if current_status == 'accepted'
            else 'offer_declined.html',
            candidate=c
        )

    # ACCEPT OFFER
    if action == 'accept':

        c['offer_status'] = 'accepted'
        c['responded_at'] = str(datetime.now())
        save_cands(cands)

        verifs = get_verifs()

        # Avoid duplicate verification creation
        existing = next(
            (v for v in verifs.values()
             if v.get('candidate_id') == cid),
            None
        )

        if not existing:

            vid = str(uuid.uuid4())

            verifs[vid] = {
                'id': vid,
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

            bg_link = f"{BASE_URL}/background-verification/{vid}"

            users = get_users()
            hr = users.get(c['hr_id'], {})
            company = hr.get('company_name', 'the company')

            # Send verification mail in background
            def send_bg_mail():

                    send_email(
                     c['email'],
                    'Next Step: Complete Background Verification',
                    f"""
                    <html>
                    <body style="font-family:Arial;padding:40px">
                        <h2>Background Verification</h2>

                        <p>Hello {c['name']},</p>

                        <p>
                            Thank you for accepting the offer for
                            <b>{c.get('role','')}</b>.
                        </p>

                        <p>
                            Please complete your background verification.
                        </p>

            <a href="{bg_link}"
            style="
                background:#1a56db;
                color:white;
                padding:12px 24px;
                text-decoration:none;
                border-radius:8px;
                display:inline-block;
            ">
                Start Verification
            </a>
        </body>
        </html>
        """
    )

            threading.Thread(
                target=send_bg_mail,
                daemon=True
            ).start()

        return render_template('offer_accepted.html', candidate=c)

    # DECLINE OFFER
    elif action == 'decline':

        c['offer_status'] = 'declined'
        c['responded_at'] = str(datetime.now())

        save_cands(cands)

        return render_template('offer_declined.html', candidate=c)

    return """
    <div style='font-family:sans-serif;text-align:center;margin-top:80px'>
        <h2>Invalid action.</h2>
    </div>
    """
@app.route('/background-verification/<vid>')
def background_verification_page(vid):

    verifs = get_verifs()
    v = verifs.get(vid)

    if not v:
        return """
        <div style='font-family:sans-serif;text-align:center;margin-top:80px'>
            <h2>Invalid verification link.</h2>
        </div>
        """, 404

    return render_template(
        'background_verification.html',
        verification=v
    )
@app.route('/api/submit-verification', methods=['POST'])
def api_submit_verification():
    vid    = request.form.get('verification_id')
    verifs = get_verifs()
    v      = verifs.get(vid)
    if not v: return jsonify({'success':False}), 404
    step   = request.form.get('step')

    if step=='1':
        fn = request.form.get('first_name','')
        ln = request.form.get('last_name','')
        v.update({'first_name':fn,'last_name':ln,'name':f"{fn} {ln}",
                  'phone':request.form.get('phone',''),
                  'aadhaar':request.form.get('aadhaar',''),
                  'pan':request.form.get('pan','')})
    elif step=='2':
        v.update({'college':request.form.get('college',''),
                  'specialization':request.form.get('specialization',''),
                  'percentage':request.form.get('percentage','')})
    elif step=='3':
        ctype = request.form.get('candidate_type','fresher')
        v['candidate_type'] = ctype
        users = get_users(); hr = users.get(v.get('hr_id',''),{}); company=hr.get('company_name','the company')
        if ctype=='experienced':
            prev_co    = request.form.get('prev_company','')
            prev_role  = request.form.get('prev_role','')
            co_email   = request.form.get('company_email','')
            duration   = request.form.get('duration','')
            v.update({'prev_company':prev_co,'prev_role':prev_role,
                      'company_email':co_email,'duration':duration})
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
          ✓ Verify Employment
        </a>
      </td>
      <td>
        <a href="{rlink}" style="display:inline-block;background:#ef4444;color:#fff;
           padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px">
          ✕ Cannot Verify
        </a>
      </td>
    </tr></table>
  </td></tr>
</table></td></tr></table></body></html>""")
        else:
            v['verification_status'] = 'verified'
            send_email(v['email'],'🎉 Background Verification Complete!',
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
    verifs = get_verifs()
    v = verifs.get(vid)
    if not v:
        return "<div style='font-family:sans-serif;text-align:center;margin-top:80px'><h2>Invalid link.</h2></div>",404
    users = get_users(); hr = users.get(v.get('hr_id',''),{}); company=hr.get('company_name','the company')
    if action=='verify':
        v['verification_status']='verified'
        send_email(v['email'],'✅ Employment Verified — Offer Letter Coming!',
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
    elif action=='reject':
        v['verification_status']='rejected'
        send_email(v['email'],'Update on Your Application',
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