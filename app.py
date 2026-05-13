from flask import (Flask, render_template, request, jsonify,
                   session, redirect, send_file)
import json, os, uuid, hashlib, re, base64, io
from datetime import datetime, timedelta
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
import pandas as pd
from werkzeug.utils import secure_filename
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, HRFlowable)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId
import cloudinary
import cloudinary.uploader
import cloudinary.api

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "fallback-secret-key")

BREVO_API_KEY  = os.environ.get("BREVO_API_KEY")
SENDER_NAME    = os.environ.get("SENDER_NAME")
SENDER_EMAIL   = os.environ.get("SENDER_EMAIL")
BASE_URL       = os.environ.get("BASE_URL", "http://localhost:5000").rstrip("/")
MONGODB_URI    = os.environ.get("MONGODB_URI")

# ── Cloudinary config ─────────────────────────────────────────────────────────
cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key    = os.environ.get("CLOUDINARY_API_KEY"),
    api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
    secure     = True
)

# ── MongoDB ───────────────────────────────────────────────────────────────────
_mongo_client = None
from pymongo import MongoClient

client = MongoClient(MONGODB_URI)

try:
    client.admin.command('ping')
    print("MongoDB Connected")
except Exception as e:
    print(e)
def get_db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    return _mongo_client["offerflow"]

def col(name):
    return get_db()[name]

# ── Local temp dir (for PDF generation only — not for persistent storage) ─────
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
LETTER_DIR = os.path.join("/tmp", "generated_letters")
os.makedirs(LETTER_DIR, exist_ok=True)

# ── MongoDB helpers ───────────────────────────────────────────────────────────
def _clean(doc):
    """Convert MongoDB _id to string id field."""
    if doc is None:
        return None
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    return doc

def get_users():
    return {str(u["_id"]): _clean(u) for u in col("users").find()}

def get_user_by_id(uid):
    try:
        return _clean(col("users").find_one({"_id": uid}))
    except Exception:
        return None

def save_user(uid, data):
    data_copy = {k: v for k, v in data.items() if k != "id"}
    col("users").replace_one({"_id": uid}, {**data_copy, "_id": uid}, upsert=True)

def get_cands():
    return {str(c["_id"]): _clean(c) for c in col("candidates").find()}

def get_cand_by_id(cid):
    return _clean(col("candidates").find_one({"_id": cid}))

def save_cand(cid, data):
    data_copy = {k: v for k, v in data.items() if k != "id"}
    col("candidates").replace_one({"_id": cid}, {**data_copy, "_id": cid}, upsert=True)

def get_verifs():
    return {str(v["_id"]): _clean(v) for v in col("verifications").find()}

def get_verif_by_id(vid):
    return _clean(col("verifications").find_one({"_id": vid}))

def get_verif_by_candidate(cid):
    return _clean(col("verifications").find_one({"candidate_id": cid}))

def save_verif(vid, data):
    data_copy = {k: v for k, v in data.items() if k != "id"}
    col("verifications").replace_one({"_id": vid}, {**data_copy, "_id": vid}, upsert=True)

def get_tokens():
    return {str(t["_id"]): _clean(t) for t in col("tokens").find()}

def get_token_by_id(token):
    return _clean(col("tokens").find_one({"_id": token}))

def save_token(token, data):
    data_copy = {k: v for k, v in data.items() if k != "id"}
    col("tokens").replace_one({"_id": token}, {**data_copy, "_id": token}, upsert=True)

def delete_token(token):
    col("tokens").delete_one({"_id": token})

# ── Auth helpers ──────────────────────────────────────────────────────────────
def hash_pw(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def validate_password(pw):
    return (len(pw) >= 8
            and re.search(r'[A-Z]', pw)
            and re.search(r'[a-z]', pw)
            and re.search(r'[^A-Za-z0-9]', pw))

def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return get_user_by_id(uid)

# ── Cloudinary upload helper ──────────────────────────────────────────────────
def upload_letterhead_to_cloudinary(file_stream, filename):
    """Upload a PDF letterhead to Cloudinary and return the secure URL."""
    try:
        result = cloudinary.uploader.upload(
            file_stream,
            resource_type="raw",
            folder="offerflow/letterheads",
            public_id=f"lh_{uuid.uuid4().hex}",
            format="pdf"
        )
        return result.get("secure_url")
    except Exception as e:
        print(f"[CLOUDINARY ERROR] {e}")
        return None

def download_letterhead_to_tmp(url):
    """Download a Cloudinary-hosted letterhead PDF to /tmp and return local path."""
    import urllib.request
    tmp_path = os.path.join("/tmp", f"lh_{uuid.uuid4().hex}.pdf")
    try:
        urllib.request.urlretrieve(url, tmp_path)
        return tmp_path
    except Exception as e:
        print(f"[DOWNLOAD LETTERHEAD ERROR] {e}")
        return None

# ── Email via Brevo ───────────────────────────────────────────────────────────
def send_email(to, subject, html_body, attach_path=None, attach_name=None):
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = BREVO_API_KEY

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
            attachment=attachments if attachments else None
        )
        api_instance.send_transac_email(send_smtp_email)
        print(f"[BREVO EMAIL SENT] → {to}")
        return True
    except ApiException as e:
        print(f"[BREVO ERROR] {e}")
        return False
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False

# ── PDF letterhead merge ──────────────────────────────────────────────────────
def _merge_letterhead(lh_path, content_bytes, out_path):
    """
    Properly overlays content PDF onto letterhead PDF using pypdf.
    The letterhead is the background; content is rendered on top.
    """
    try:
        lh_reader      = PdfReader(lh_path)
        content_reader = PdfReader(io.BytesIO(content_bytes))
        writer         = PdfWriter()

        num_lh_pages = len(lh_reader.pages)

        for i, content_page in enumerate(content_reader.pages):
            # Clone the letterhead page so we don't mutate the original
            lh_page_idx = min(i, num_lh_pages - 1)
            lh_page     = lh_reader.pages[lh_page_idx]

            # Create a fresh writer page from letterhead, then merge content on top
            page_writer = PdfWriter()
            page_writer.add_page(lh_page)
            merged_page = page_writer.pages[0]
            merged_page.merge_page(content_page)
            writer.add_page(merged_page)

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            writer.write(f)

        print(f"[PDF MERGED WITH LETTERHEAD] → {out_path}")
        return out_path
    except Exception as e:
        print(f"[MERGE LETTERHEAD ERROR] {e}")
        # Fallback: save content-only PDF
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(content_bytes)
        return out_path

# ── PDF offer letter generator ────────────────────────────────────────────────
def generate_offer_pdf(candidate, hr_user, pattern='standard'):
    cid      = candidate['id']
    out_path = os.path.join(LETTER_DIR, f"offer_{cid}.pdf")
    today    = datetime.now().strftime('%d %B %Y')
    company  = hr_user['company_name']
    name     = candidate.get('name', '')
    role     = candidate.get('role', '')
    joining  = candidate.get('joining_date', '')
    salary   = candidate.get('salary', '')
    emp_type = candidate.get('employment_type', 'full_time').replace('_', ' ').title()
    custom_body = candidate.get('custom_body', '')

    lh_url  = hr_user.get('letterhead_url', '')
    lh_path = None
    has_letterhead = False

    if lh_url:
        lh_path = download_letterhead_to_tmp(lh_url)
        has_letterhead = bool(lh_path and os.path.exists(lh_path))

    # ── Measure letterhead top/bottom margins by detecting blank space ────────
    # Default margins — override based on letterhead detection
    top_margin    = 62 * mm   # space for letterhead header
    bottom_margin = 32 * mm   # space for letterhead footer

    if has_letterhead:
        try:
            top_margin, bottom_margin = _detect_letterhead_margins(lh_path)
        except Exception:
            top_margin    = 62 * mm
            bottom_margin = 32 * mm

    styles = getSampleStyleSheet()

    def style(name, **kw):
        return ParagraphStyle(name, parent=styles['Normal'], **kw)

    co_style   = style('co',  fontName='Helvetica-Bold', fontSize=18,
                       textColor=colors.HexColor('#0d1b3e'), alignment=TA_CENTER, spaceAfter=2)
    sub_style  = style('sub', fontName='Helvetica', fontSize=9,
                       textColor=colors.HexColor('#64748b'), alignment=TA_CENTER, spaceAfter=10)
    h2_style   = style('h2',  fontName='Helvetica-Bold', fontSize=14,
                       textColor=colors.HexColor('#0d1b3e'), alignment=TA_CENTER, spaceAfter=14)
    body       = style('b',   fontName='Helvetica', fontSize=10, leading=16,
                       textColor=colors.HexColor('#1e293b'), spaceAfter=7)
    bold_body  = style('bb',  fontName='Helvetica-Bold', fontSize=10, leading=16,
                       textColor=colors.HexColor('#0d1b3e'), spaceAfter=5)
    small_gray = style('sg',  fontName='Helvetica', fontSize=8,
                       textColor=colors.HexColor('#94a3b8'), alignment=TA_CENTER)

    tbl_data = [
        ['Field', 'Details'],
        ['Candidate Name',     name],
        ['Designation / Role', role],
        ['Joining Date',       joining],
        ['Annual CTC',         f'Rs. {salary}'],
        ['Employment Type',    emp_type],
        ['Reporting Location', 'As communicated by HR'],
    ]
    tbl = Table(tbl_data, colWidths=[68 * mm, 102 * mm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0), (-1, 0),  colors.HexColor('#0d1b3e')),
        ('TEXTCOLOR',     (0, 0), (-1, 0),  colors.white),
        ('FONTNAME',      (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0), (-1, 0),  10),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1),
         [colors.HexColor('#f0f4ff'), colors.HexColor('#ffffff')]),
        ('FONTNAME',      (0, 1), (0, -1),  'Helvetica-Bold'),
        ('FONTNAME',      (1, 1), (1, -1),  'Helvetica'),
        ('FONTSIZE',      (0, 1), (-1, -1), 10),
        ('TEXTCOLOR',     (0, 1), (0, -1),  colors.HexColor('#1a56db')),
        ('GRID',          (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('ROWPADDING',    (0, 0), (-1, -1), 8),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
    ]))

    # ── Body text by pattern ──────────────────────────────────────────────────
    if pattern == 'custom' and custom_body:
        body_text = custom_body
    elif pattern == 'formal':
        body_text = (
            f'This is to formally notify that <b>{name}</b> has been selected for the '
            f'position of <b>{role}</b> at <b>{company}</b>. '
            f'The appointment is subject to the terms and conditions of employment as '
            f'set forth by the organisation, including but not limited to the Code of Conduct, '
            f'Non-Disclosure Agreement, and applicable labour laws.'
        )
    elif pattern == 'modern':
        body_text = (
            f'Hey <b>{name}</b> — we\'re thrilled to welcome you aboard! '
            f'You\'ll be joining us as <b>{role}</b> starting <b>{joining}</b>. '
            f'We can\'t wait to have you on the team.'
        )
    elif pattern == 'detailed':
        body_text = (
            f'We are pleased to offer <b>{name}</b> the position of <b>{role}</b> '
            f'at <b>{company}</b>. The compensation package includes an Annual CTC of '
            f'<b>Rs.{salary}</b>, which encompasses base salary, statutory benefits, '
            f'performance-linked incentives, and applicable allowances as per company policy.'
        )
    else:
        body_text = (
            f'We are delighted to extend this offer of employment for the position of '
            f'<b>{role}</b> at <b>{company}</b>. We believe your skills and experience '
            f'are an excellent fit for our team.'
        )

    # ── Build story ───────────────────────────────────────────────────────────
    # When letterhead is present, skip company header (it's on the letterhead)
    if has_letterhead:
        story_header = [
            Spacer(1, 4),
            HRFlowable(width='100%', thickness=2, color=colors.HexColor('#1a56db'), spaceAfter=8),
            Paragraph('OFFER OF EMPLOYMENT', h2_style),
        ]
    else:
        story_header = [
            Paragraph(company, co_style),
            Paragraph('Human Resources Department', sub_style),
            HRFlowable(width='100%', thickness=2.5, color=colors.HexColor('#1a56db'), spaceAfter=10),
            Paragraph('OFFER OF EMPLOYMENT', h2_style),
        ]

    story_body = [
        Paragraph(f'Date: {today}',
                  style('dt', fontName='Helvetica', fontSize=9,
                        textColor=colors.HexColor('#64748b'), spaceAfter=12)),
        Paragraph(f'Dear <b>{name}</b>,', body),
        Spacer(1, 4),
        Paragraph(body_text, body),
        Spacer(1, 8),
        Paragraph('Your offer details:', bold_body),
        Spacer(1, 4),
        tbl,
        Spacer(1, 12),
        Paragraph('This offer is contingent upon:', bold_body),
        Paragraph('• Successful completion of background verification and reference checks.', body),
        Paragraph('• Submission of all required documents before joining.', body),
        Paragraph('• Acceptance of the company\'s Code of Conduct and employment terms.', body),
        Spacer(1, 10),
        Paragraph(
            'Please confirm your acceptance via the <b>Accept Offer</b> button in your email. '
            'We look forward to welcoming you to our team.', body),
        Spacer(1, 20),
        HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#e2e8f0'), spaceAfter=10),
        Paragraph('Warm regards,', body),
        Paragraph(f'<b>HR Department — {company}</b>', bold_body),
        Spacer(1, 16),
        Paragraph(f'Generated by OfferFlow on {today}.', small_gray),
    ]

    full_story = story_header + story_body

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=22 * mm, leftMargin=22 * mm,
        topMargin=top_margin, bottomMargin=bottom_margin
    )
    doc.build(full_story)
    content_bytes = buf.getvalue()

    if has_letterhead:
        _merge_letterhead(lh_path, content_bytes, out_path)
        # Clean up tmp letterhead
        try:
            os.remove(lh_path)
        except Exception:
            pass
    else:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'wb') as f:
            f.write(content_bytes)

    return out_path


def _detect_letterhead_margins(lh_path):
    """
    Attempts to auto-detect the usable content area of a letterhead by
    checking where graphical content ends at the top and bottom.
    Falls back to safe defaults if detection fails.
    Returns (top_margin_pts, bottom_margin_pts).
    """
    try:
        import pdfplumber
        with pdfplumber.open(lh_path) as pdf:
            page   = pdf.pages[0]
            height = float(page.height)
            objs   = page.objects.get('rect', []) + page.objects.get('line', []) + page.objects.get('image', [])

            if not objs:
                return 62 * mm, 32 * mm

            # Find lowest y of top elements and highest y of bottom elements
            top_bound    = max((float(o.get('y1', o.get('y0', 0))) for o in objs), default=80)
            bottom_bound = min((float(o.get('y0', height)) for o in objs), default=height - 60)

            top_margin    = max(top_bound + 8,    50 * mm / mm) * mm / mm
            bottom_margin = max(height - bottom_bound + 8, 25 * mm / mm) * mm / mm
            return top_margin, bottom_margin
    except Exception:
        return 62 * mm, 32 * mm


# ── HTML preview (letterhead background via iframe) ───────────────────────────
def letterhead_preview_html(hr_user, candidate, pattern='standard'):
    company     = hr_user['company_name']
    lh_url      = hr_user.get('letterhead_url', '')
    today       = datetime.now().strftime('%d %B %Y')
    name        = candidate.get('name', '')
    role        = candidate.get('role', '')
    joining     = candidate.get('joining_date', '')
    salary      = candidate.get('salary', '')
    emp_type    = candidate.get('employment_type', 'full_time').replace('_', ' ').title()
    email       = candidate.get('email', '')
    custom_body = candidate.get('custom_body', '')

    # ── Letterhead preview block ──────────────────────────────────────────────
    lh_block = ''
    if lh_url:
        lh_block = f'''
        <div style="margin-bottom:16px">
          <div style="background:#fff8e1;border:1px solid #f59e0b;border-radius:8px;
                      padding:10px 16px;margin-bottom:10px;font-size:12px;color:#92400e;
                      display:flex;align-items:center;gap:8px">
            <span style="font-size:16px">&#128196;</span>
            <div><b>Company Letterhead attached</b> — the offer letter PDF will be printed on this letterhead</div>
          </div>
          <iframe src="{lh_url}" width="100%" height="160"
            style="border:1px solid #e2e8f0;border-radius:8px;display:block"
            title="Company Letterhead Preview"></iframe>
        </div>'''

    rows = ''.join(
        f'<tr style="background:{"#f0f4ff" if i % 2 == 0 else "#fff"}">'
        f'<td style="padding:10px 14px;border:1px solid #e2e8f0;font-weight:700;'
        f'color:#1a56db;width:42%;font-size:12px">{k}</td>'
        f'<td style="padding:10px 14px;border:1px solid #e2e8f0;color:#1e293b;font-size:12px">{v}</td></tr>'
        for i, (k, v) in enumerate([
            ('Candidate Name',     name),
            ('Designation / Role', role),
            ('Joining Date',       joining),
            ('Annual CTC',         f'&#8377; {salary}'),
            ('Employment Type',    emp_type),
            ('Reporting Location', 'As communicated by HR'),
        ])
    )

    # ── Body text per pattern ─────────────────────────────────────────────────
    if pattern == 'custom' and custom_body:
        body_html = (f'<p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">'
                     f'{custom_body}</p>')
    elif pattern == 'formal':
        body_html = f'''<p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">
            This is to formally notify that <b>{name}</b> has been selected for the position of
            <b style="color:#1a56db">{role}</b> at <b>{company}</b>.
            The appointment is subject to the terms and conditions of employment as set forth by
            the organisation, including but not limited to the Code of Conduct, Non-Disclosure
            Agreement, and applicable labour laws.
        </p>
        <p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">
            The candidate is required to report to duty on the date specified herein, failing which
            this offer shall stand null and void.
        </p>'''
    elif pattern == 'modern':
        body_html = f'''<p style="font-size:13px;color:#0d1b3e;line-height:1.9;
            margin-bottom:14px;font-weight:600">
            Hey {name} &#8212; we&#8217;re absolutely thrilled to have you on board! &#127881;
        </p>
        <p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">
            You&#8217;ll be joining us as <b style="color:#1a56db">{role}</b> starting
            <b>{joining}</b>. We can&#8217;t wait to see what you&#8217;ll build with us.
        </p>'''
    elif pattern == 'detailed':
        body_html = f'''<p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">
            We are pleased to offer <b>{name}</b> the position of
            <b style="color:#1a56db">{role}</b> at <b>{company}</b>.
        </p>
        <div style="background:#f8faff;border-radius:8px;padding:14px 16px;
                    margin-bottom:14px;border-left:3px solid #1a56db">
            <div style="font-size:11px;font-weight:700;color:#0d1b3e;margin-bottom:8px;
                        text-transform:uppercase;letter-spacing:.05em">Compensation Breakdown</div>
            <div style="display:flex;justify-content:space-between;font-size:11.5px;
                        color:#475569;margin-bottom:4px">
                <span>Annual CTC</span>
                <span style="font-weight:700;color:#1a56db">&#8377; {salary}</span>
            </div>
            <div style="font-size:11px;color:#94a3b8">
                Includes base salary, PF, gratuity &amp; applicable allowances
            </div>
        </div>'''
    else:
        body_html = f'''<p style="font-size:12px;color:#475569;line-height:1.8;margin-bottom:14px">
            We are delighted to extend this offer of employment for the position of
            <b style="color:#1a56db">{role}</b> at <b>{company}</b>. We believe your skills and
            experience are an excellent fit for our team.
        </p>'''

    # ── Header style per pattern ──────────────────────────────────────────────
    if pattern == 'modern':
        header_style = 'background:linear-gradient(135deg,#0ea5e9,#6366f1);'
        title_text   = '&#10022; OFFER OF EMPLOYMENT &#10022;'
    elif pattern == 'formal':
        header_style = 'background:#0d1b3e;'
        title_text   = 'FORMAL OFFER OF EMPLOYMENT'
    elif pattern == 'detailed':
        header_style = 'background:linear-gradient(135deg,#0d1b3e,#1a56db);'
        title_text   = 'COMPREHENSIVE OFFER LETTER'
    else:
        header_style = 'background:linear-gradient(135deg,#0d1b3e,#1a56db);'
        title_text   = 'OFFER OF EMPLOYMENT'

    # If there's a letterhead, show the letter content as it would appear
    # positioned inside the letterhead's blank area
    letterhead_wrapper_open  = ''
    letterhead_wrapper_close = ''
    if lh_url:
        letterhead_wrapper_open = f'''
        <div style="position:relative;background:white;border:1px solid #e2e8f0;
                    border-radius:8px;overflow:hidden;margin-bottom:16px">
          <!-- Letterhead background -->
          <iframe src="{lh_url}" style="position:absolute;top:0;left:0;width:100%;
                  height:100%;border:none;z-index:0;pointer-events:none"
                  title="Letterhead Background"></iframe>
          <!-- Content overlay -->
          <div style="position:relative;z-index:1;padding:68px 28px 36px 28px;
                      background:rgba(255,255,255,0.0)">
        '''
        letterhead_wrapper_close = '</div></div>'

def generate_offer_html(
    company,
    name,
    email,
    today,
    body_html,
    rows,
    title_text,
    header_style,
    lh_block,
    lh_url,
    letterhead_wrapper_open,
    letterhead_wrapper_close
):

    header_html = ""

    if not lh_url:
        header_html = f"""
        <div style="{header_style}padding:24px 28px;border-radius:8px;margin-bottom:20px">

          <div style="font-family:Arial,sans-serif;
                      font-size:20px;
                      font-weight:900;
                      color:#fff;
                      text-align:center">

            {company}

          </div>

          <div style="font-size:10px;
                      color:rgba(255,255,255,.65);
                      margin-top:3px;
                      text-align:center">

            Human Resources Department

          </div>

          <div style="margin-top:12px;
                      padding-top:12px;
                      border-top:1px solid rgba(255,255,255,.2);
                      text-align:center;
                      font-size:14px;
                      font-weight:700;
                      color:#fff;
                      letter-spacing:.08em">

            {title_text}

          </div>

        </div>
        """

    return f"""
<div style="font-family:Georgia,serif;
            background:#fff;
            border:1px solid #e2e8f0;
            border-radius:12px;
            overflow:hidden">

  <div style="padding:24px 28px">

    {lh_block}

    {header_html}

    {letterhead_wrapper_open}

    <div style="font-size:10px;
                color:#64748b;
                margin-bottom:12px">

      Date: {today}

    </div>

    <p style="font-size:13px;
              color:#1e293b;
              margin-bottom:10px">

      Dear <b>{name}</b>,

    </p>

    {body_html}

    <p style="font-size:11.5px;
              font-weight:700;
              color:#0d1b3e;
              margin-bottom:8px">

      Offer Details:

    </p>

    <table style="width:100%;
                  border-collapse:collapse;
                  margin-bottom:16px">

      {rows}

    </table>

    <p style="font-size:11.5px;
              font-weight:700;
              color:#0d1b3e;
              margin-bottom:6px">

      This offer is subject to:

    </p>

    <ul style="font-size:11.5px;
               color:#475569;
               line-height:2;
               margin-left:18px;
               margin-bottom:14px">

      <li>Successful completion of background verification.</li>

      <li>Submission of all required documents before joining.</li>

      <li>
        Acceptance of the company&#8217;s Code of Conduct
        and employment terms.
      </li>

    </ul>

    <p style="font-size:12px;
              color:#475569;
              line-height:1.8;
              margin-bottom:20px">

      Please accept or decline via the buttons in the email.

    </p>

    <div style="border-top:1px solid #e2e8f0;
                padding-top:16px">

      <p style="font-size:11.5px;
                color:#1e293b;
                margin:0">

        Warm regards,

      </p>

      <p style="font-size:12px;
                font-weight:700;
                color:#0d1b3e;
                margin:4px 0 0">

        HR Department &#8212; {company}

      </p>

    </div>

    <div style="margin-top:16px;
                padding:10px;
                background:#f8faff;
                border-radius:6px;
                font-size:10px;
                color:#94a3b8;
                text-align:center">

      &#128231; PDF offer letter will be sent as an attachment to
      <b>{email}</b>

    </div>

    {letterhead_wrapper_close}

  </div>

</div>
"""

# ── Email HTML builders ───────────────────────────────────────────────────────
def offer_email_html(c, hr, accept_link, decline_link):
    company  = hr['company_name']
    name     = c.get('name', '')
    role     = c.get('role', '')
    joining  = c.get('joining_date', '')
    salary   = c.get('salary', '')
    emp_type = c.get('employment_type', 'full_time').replace('_', ' ').title()
    rows = ''.join(
        f'<tr style="background:{"#f8faff" if i % 2 == 0 else "#fff"}">'
        f'<td style="padding:11px 16px;font-weight:700;color:#1a56db;font-size:13px;width:38%">{k}</td>'
        f'<td style="padding:11px 16px;color:#1e293b;font-size:13px">{v}</td></tr>'
        for i, (k, v) in enumerate([
            ('Role',          role),
            ('Joining Date',  joining),
            ('Annual CTC',    '&#8377; ' + str(salary)),
            ('Employment Type', emp_type)
        ])
    )
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e 0%,#1a56db 100%);
                 padding:36px 40px;text-align:center">
    <div style="font-size:26px;font-weight:900;color:#fff;letter-spacing:1px">{company}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.7);margin-top:6px">Official Offer Letter</div>
  </td></tr>
  <tr><td style="padding:36px 40px">
    <h2 style="font-size:22px;color:#0d1b3e;margin:0 0 8px">&#127881; Congratulations, {name}!</h2>
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
                    padding:14px 34px;border-radius:9px;text-decoration:none;
                    font-weight:700;font-size:15px;letter-spacing:.3px;
                    font-family:Arial,sans-serif">&#10003; Accept Offer</a>
        </td>
        <td>
          <a href="{decline_link}"
             style="display:inline-block;background:#ef4444;color:#fff;
                    padding:14px 34px;border-radius:9px;text-decoration:none;
                    font-weight:700;font-size:15px;letter-spacing:.3px;
                    font-family:Arial,sans-serif">&#10005; Decline Offer</a>
        </td>
      </tr>
    </table>
    <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:22px;line-height:1.7">
      &#9200; Offer expires automatically after 48 hours if no response.<br>
      &#128206; Your offer letter PDF is attached to this email.
    </p>
  </td></tr>
  <tr><td style="background:#f8faff;padding:14px 40px;text-align:center;
                 border-top:1px solid #e2e8f0">
    <p style="color:#94a3b8;font-size:11px;margin:0">
      Sent via OfferFlow &middot; {company} HR Portal
    </p>
  </td></tr>
</table></td></tr></table></body></html>"""


def bg_verification_email_html(candidate, company, bg_link):
    name = candidate.get('name', '')
    role = candidate.get('role', '')
    items = [
        'Personal details (Name, Aadhaar, PAN)',
        'Educational qualifications',
        'Previous employment details (if experienced)'
    ]
    items_html = ''.join(
        f'<div style="display:flex;align-items:center;gap:10px;font-size:13px;'
        f'color:#475569;margin-bottom:6px">'
        f'<span style="color:#10b981;font-size:16px">&#10003;</span>{item}</div>'
        for item in items
    )
    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:32px 0">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0"
  style="background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e 0%,#1a56db 100%);
                 padding:32px 40px;text-align:center">
    <div style="width:64px;height:64px;background:rgba(255,255,255,.15);border-radius:50%;
                margin:0 auto 16px;font-size:28px;line-height:64px;text-align:center">
      &#128737;
    </div>
    <div style="font-size:22px;font-weight:900;color:#fff">{company}</div>
    <div style="font-size:12px;color:rgba(255,255,255,.65);margin-top:4px">
      Background Verification Request
    </div>
  </td></tr>
  <tr><td style="padding:36px 40px">
    <h2 style="font-size:20px;color:#0d1b3e;margin:0 0 12px">Hello {name},</h2>
    <p style="color:#475569;font-size:13.5px;line-height:1.9;margin:0 0 16px">
      Congratulations on accepting the offer for
      <b style="color:#1a56db">{role}</b> at <b>{company}</b>! &#127881;
    </p>
    <p style="color:#475569;font-size:13px;line-height:1.8;margin:0 0 24px">
      As the next step in your onboarding journey, we need you to complete a
      <b>background verification</b>. This is a standard process for all new hires.
    </p>
    <div style="background:#f0f4ff;border-radius:10px;padding:20px 24px;
                margin-bottom:28px;border-left:4px solid #1a56db">
      <div style="font-size:12px;font-weight:700;color:#0d1b3e;margin-bottom:10px;
                  text-transform:uppercase;letter-spacing:.06em">What you'll need to provide</div>
      {items_html}
    </div>
    <div style="text-align:center;margin-bottom:24px">
      <a href="{bg_link}"
         style="display:inline-block;background:linear-gradient(135deg,#1a56db,#0ea5e9);
                color:#fff;padding:15px 40px;border-radius:10px;text-decoration:none;
                font-weight:700;font-size:15px;letter-spacing:.3px;font-family:Arial,sans-serif;
                box-shadow:0 4px 12px rgba(26,86,219,.3)">
        &#128737; Start Background Verification
      </a>
    </div>
    <p style="color:#94a3b8;font-size:11px;text-align:center;line-height:1.7">
      This link is unique to you. Please do not share it with others.
    </p>
  </td></tr>
  <tr><td style="background:#f8faff;padding:14px 40px;text-align:center;
                 border-top:1px solid #e2e8f0">
    <p style="color:#94a3b8;font-size:11px;margin:0">
      Sent via OfferFlow &middot; {company} HR Portal
    </p>
  </td></tr>
</table></td></tr></table></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login_page():
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json or {}
    user = col("users").find_one({
        "username": data.get('username'),
        "password": hash_pw(data.get('password', ''))
    })
    if not user:
        return jsonify({'success': False, 'message': 'Invalid credentials'}), 401
    session['user_id'] = str(user['_id'])
    return jsonify({'success': True})

@app.route('/api/register', methods=['POST'])
def api_register():
    username     = request.form.get('username', '').strip()
    email        = request.form.get('email', '').strip()
    password     = request.form.get('password', '')
    confirm      = request.form.get('confirm_password', '')
    company_name = request.form.get('company_name', '').strip()

    if not all([username, email, password, confirm, company_name]):
        return jsonify({'success': False, 'message': 'All fields are required'}), 400
    if col("users").find_one({"username": username}):
        return jsonify({'success': False, 'message': 'Username already exists'}), 400
    if col("users").find_one({"email": email}):
        return jsonify({'success': False, 'message': 'Email already registered'}), 400
    if password != confirm:
        return jsonify({'success': False, 'message': 'Passwords do not match'}), 400
    if not validate_password(password):
        return jsonify({'success': False, 'message':
            'Password needs 8+ chars, uppercase, lowercase & special character'}), 400

    lh = request.files.get('letterhead')
    if not lh or not lh.filename:
        return jsonify({'success': False, 'message': 'Company letterhead (PDF) is required'}), 400
    if not lh.filename.lower().endswith('.pdf'):
        return jsonify({'success': False, 'message': 'Letterhead must be a PDF file'}), 400

    # Upload letterhead to Cloudinary
    lh_url = upload_letterhead_to_cloudinary(lh.stream, lh.filename)
    if not lh_url:
        return jsonify({'success': False, 'message': 'Failed to upload letterhead. Please try again.'}), 500

    uid = str(uuid.uuid4())
    col("users").insert_one({
        "_id":          uid,
        "username":     username,
        "email":        email,
        "password":     hash_pw(password),
        "company_name": company_name,
        "letterhead_url": lh_url,        # Cloudinary URL instead of local path
        "created_at":   str(datetime.now())
    })
    return jsonify({'success': True})

@app.route('/api/forgot-password', methods=['POST'])
def api_forgot_password():
    email = (request.json or {}).get('email', '').strip()
    user  = col("users").find_one({"email": email})
    if not user:
        return jsonify({'success': False, 'message': 'Email not found'}), 404

    token    = str(uuid.uuid4())
    expires  = str(datetime.now() + timedelta(hours=1))
    col("tokens").insert_one({"_id": token, "user_id": str(user["_id"]), "expires": expires})

    link = f"{BASE_URL}/reset-password/{token}"
    html = f"""<!DOCTYPE html><html>
<body style="margin:0;padding:32px 0;background:#f0f4fa;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="480" style="background:#fff;border-radius:16px;overflow:hidden;
       box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e,#1a56db);
                 padding:28px;text-align:center">
    <div style="font-size:22px;font-weight:900;color:#fff">OfferFlow</div>
    <div style="color:rgba(255,255,255,.7);font-size:12px;margin-top:4px">Password Reset</div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <div style="font-size:44px;margin-bottom:14px">&#128273;</div>
    <h2 style="font-size:19px;color:#0d1b3e;margin:0 0 10px">Reset Your Password</h2>
    <p style="color:#64748b;font-size:13px;margin:0 0 26px;line-height:1.7">
      Click the button below. This link expires in <b>1 hour</b>.
    </p>
    <a href="{link}"
       style="display:inline-block;background:#1a56db;color:#fff;
              padding:13px 34px;border-radius:8px;text-decoration:none;
              font-weight:700;font-size:14px">Reset Password</a>
    <p style="color:#94a3b8;font-size:11px;margin-top:20px">
      If you did not request this, ignore this email.
    </p>
  </td></tr>
</table></td></tr></table></body></html>"""
    send_email(email, 'Reset Your OfferFlow Password', html)
    return jsonify({'success': True, 'message': 'Password reset link sent to your email'})

@app.route('/reset-password/<token>')
def reset_password_page(token):
    if not col("tokens").find_one({"_id": token}):
        return redirect('/login')
    return render_template('reset_password.html', token=token)

@app.route('/api/reset-password', methods=['POST'])
def api_reset_password():
    data    = request.json or {}
    token   = data.get('token', '')
    pw      = data.get('password', '')
    confirm = data.get('confirm_password', '')

    tok = col("tokens").find_one({"_id": token})
    if not tok:
        return jsonify({'success': False, 'message': 'Invalid or expired link'}), 400
    if pw != confirm:
        return jsonify({'success': False, 'message': 'Passwords do not match'}), 400
    if not validate_password(pw):
        return jsonify({'success': False, 'message': 'Password requirements not met'}), 400

    col("users").update_one({"_id": tok["user_id"]}, {"$set": {"password": hash_pw(pw)}})
    col("tokens").delete_one({"_id": token})
    return jsonify({'success': True})

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route('/dashboard')
def dashboard():
    if not current_user():
        return redirect('/login')
    return render_template('dashboard.html')

@app.route('/api/dashboard-stats')
def api_dashboard_stats():
    u = current_user()
    if not u:
        return jsonify({}), 401
    uid = u['id']

    cands  = list(col("candidates").find({"hr_id": uid}))
    verifs = list(col("verifications").find({"hr_id": uid}))

    return jsonify({
        'total_offers':         len(cands),
        'offer_accepted':       sum(1 for c in cands if c.get('offer_status') == 'accepted'),
        'offer_declined':       sum(1 for c in cands if c.get('offer_status') == 'declined'),
        'action_pending':       sum(1 for c in cands if c.get('offer_status') == 'pending'),
        'cancelled':            sum(1 for c in cands if c.get('offer_status') == 'cancelled'),
        'verified':             sum(1 for v in verifs if v.get('verification_status') == 'verified'),
        'rejected':             sum(1 for v in verifs if v.get('verification_status') == 'rejected'),
        'verification_pending': sum(1 for v in verifs if v.get('verification_status') == 'pending'),
        'company_name':         u['company_name'],
    })

@app.route('/api/offers')
def api_offers():
    u = current_user()
    if not u:
        return jsonify([]), 401
    status = request.args.get('status')
    query  = {"hr_id": u['id']}
    if status:
        query["offer_status"] = status
    result = [
        {
            'id':              str(c['_id']),
            'name':            c.get('name', ''),
            'role':            c.get('role', ''),
            'joining_date':    c.get('joining_date', ''),
            'email':           c.get('email', ''),
            'salary':          c.get('salary', ''),
            'employment_type': c.get('employment_type', ''),
            'offer_status':    c.get('offer_status', 'pending'),
        }
        for c in col("candidates").find(query)
    ]
    return jsonify(result)

@app.route('/api/verifications')
def api_verifications():
    u = current_user()
    if not u:
        return jsonify([]), 401
    status = request.args.get('status')
    query  = {"hr_id": u['id']}
    if status:
        query["verification_status"] = status
    result = [
        {
            'id':                  str(v['_id']),
            'name':                v.get('name', ''),
            'email':               v.get('email', ''),
            'salary':              v.get('salary', ''),
            'phone':               v.get('phone', ''),
            'verification_status': v.get('verification_status', 'pending'),
        }
        for v in col("verifications").find(query)
    ]
    return jsonify(result)

# ── Step pages ────────────────────────────────────────────────────────────────
@app.route('/upload-excel')
def upload_excel_page():
    if not current_user(): return redirect('/login')
    return render_template('step1_upload.html')

@app.route('/step/employment-type')
def step_employment_type():
    if not current_user(): return redirect('/login')
    return render_template('step2_employment.html')

@app.route('/step/pattern')
def step_pattern():
    if not current_user(): return redirect('/login')
    return render_template('step3_pattern.html')

@app.route('/step/preview')
def step_preview():
    if not current_user(): return redirect('/login')
    return render_template('step4_preview.html')

# ── Excel upload ──────────────────────────────────────────────────────────────
@app.route('/api/upload-excel', methods=['POST'])
def api_upload_excel():
    u = current_user()
    if not u:
        return jsonify({'success': False}), 401
    f = request.files.get('file')
    if not f or not f.filename.endswith('.xlsx'):
        return jsonify({'success': False, 'message': 'Please upload a .xlsx file'}), 400
    try:
        df      = pd.read_excel(f.stream)
        required = ['Name', 'Gmail id', 'Role', 'Joining date', 'Salary']
        missing  = [r for r in required if r not in df.columns]
        if missing:
            return jsonify({'success': False,
                'message': f'Missing columns: {", ".join(missing)}'}), 400
        extra_cols = [c for c in df.columns if c not in required]
        return jsonify({
            'success':       True,
            'records':       df.fillna('').to_dict('records'),
            'columns':       list(df.columns),
            'extra_columns': extra_cols
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/save-candidates', methods=['POST'])
def api_save_candidates():
    u = current_user()
    if not u:
        return jsonify({'success': False}), 401
    data    = request.json or {}
    new_ids = []
    docs    = []
    for rec in data.get('candidates', []):
        cid = str(uuid.uuid4())
        docs.append({
            "_id":              cid,
            "hr_id":            u['id'],
            "name":             rec.get('Name', ''),
            "email":            rec.get('Gmail id', ''),
            "role":             rec.get('Role', ''),
            "joining_date":     str(rec.get('Joining date', '')),
            "salary":           str(rec.get('Salary', '')),
            "employment_type":  data.get('employment_type', 'full_time'),
            "pattern":          data.get('pattern', 'standard'),
            "custom_body":      data.get('custom_body', ''),
            "offer_status":     'pending',
            "sent_at":          str(datetime.now()),
            "extra":            {k: v for k, v in rec.items()
                                 if k not in ['Name', 'Gmail id', 'Role', 'Joining date', 'Salary']}
        })
        new_ids.append(cid)
    if docs:
        col("candidates").insert_many(docs)
    return jsonify({'success': True, 'candidate_ids': new_ids})

@app.route('/api/preview-letter', methods=['POST'])
def api_preview_letter():
    u = current_user()
    if not u:
        return jsonify({'success': False}), 401
    data    = request.json or {}
    c       = data.get('candidate', {})
    c['employment_type'] = data.get('employment_type', 'full_time')
    c['id']              = 'preview'
    pattern              = data.get('pattern', 'standard')
    c['custom_body']     = data.get('custom_body', '')
    return jsonify({'success': True, 'html': letterhead_preview_html(u, c, pattern)})

@app.route('/api/send-offer-emails', methods=['POST'])
def api_send_offer_emails():
    u = current_user()
    if not u:
        return jsonify({'success': False}), 401

    data  = request.json or {}
    cids  = data.get('candidate_ids', [])
    sent  = 0

    for cid in cids:
        c = col("candidates").find_one({"_id": cid})
        if not c:
            continue
        c = _clean(c)
        if c.get('email_sent_at'):
            print(f"[SKIP] Email already sent to {c['email']}")
            continue

        accept_link  = f"{BASE_URL}/offer-response/{cid}/accept"
        decline_link = f"{BASE_URL}/offer-response/{cid}/decline"

        try:
            pdf_path = generate_offer_pdf(c, u, c.get('pattern', 'standard'))
        except Exception as e:
            print(f"[PDF FAILED] {cid}: {e}")
            pdf_path = None

        subject = f"Job Offer — {c.get('role', '')} at {u['company_name']}"
        ok = send_email(
            c['email'],
            subject,
            offer_email_html(c, u, accept_link, decline_link),
            attach_path=pdf_path,
            attach_name=f"Offer_Letter_{c['name'].replace(' ', '_')}.pdf"
        )
        if ok:
            col("candidates").update_one(
                {"_id": cid},
                {"$set": {"email_sent_at": str(datetime.now())}}
            )
            sent += 1

    return jsonify({'success': True, 'sent': sent})


@app.route('/offer-response/<cid>/<action>')
def offer_response(cid, action):
    c = col("candidates").find_one({"_id": cid})
    if not c:
        return ("<div style='font-family:sans-serif;text-align:center;margin-top:80px'>"
                "<h2>Invalid or expired link.</h2></div>"), 404
    c = _clean(c)

    current_status = c.get('offer_status', 'pending')
    if current_status in ['accepted', 'declined', 'cancelled']:
        tmpl = 'offer_accepted.html' if current_status == 'accepted' else 'offer_declined.html'
        return render_template(tmpl, candidate=c)

    if action == 'accept':
        # Mark offer accepted
        col("candidates").update_one(
            {"_id": cid},
            {"$set": {"offer_status": "accepted", "responded_at": str(datetime.now())}}
        )
        c['offer_status'] = 'accepted'

        hr = get_user_by_id(c['hr_id']) or {}
        company = hr.get('company_name', 'the company')

        # Find or create verification record — always fresh query (MongoDB persistent)
        existing_verif = col("verifications").find_one({"candidate_id": cid})

        if not existing_verif:
            vid = str(uuid.uuid4())
            verif_doc = {
                "_id":                 vid,
                "candidate_id":        cid,
                "hr_id":               c['hr_id'],
                "name":                c['name'],
                "email":               c['email'],
                "salary":              c['salary'],
                "phone":               '',
                "verification_status": 'pending',
                "created_at":          str(datetime.now()),
                "bg_email_sent":       False
            }
            col("verifications").insert_one(verif_doc)
            existing_verif = verif_doc
            print(f"[VERIF CREATED] vid={vid} for cid={cid}")
        else:
            vid = str(existing_verif["_id"])

        # Send BG verification email (only once)
        if not existing_verif.get('bg_email_sent'):
            bg_link = f"{BASE_URL}/background-verification/{vid}"
            print(f"[BG EMAIL] Sending to {c['email']} | link={bg_link}")
            ok = send_email(
                c['email'],
                f'Next Step: Complete Your Background Verification — {company}',
                bg_verification_email_html(c, company, bg_link)
            )
            if ok:
                col("verifications").update_one(
                    {"_id": vid},
                    {"$set": {"bg_email_sent": True}}
                )
                print(f"[BG EMAIL] ✅ Sent to {c['email']}")
            else:
                print(f"[BG EMAIL] ❌ Failed for {c['email']}")
        else:
            print(f"[BG EMAIL SKIP] Already sent for vid={vid}")

        return render_template('offer_accepted.html', candidate=c)

    elif action == 'decline':
        col("candidates").update_one(
            {"_id": cid},
            {"$set": {"offer_status": "declined", "responded_at": str(datetime.now())}}
        )
        c['offer_status'] = 'declined'
        return render_template('offer_declined.html', candidate=c)

    return "<div style='font-family:sans-serif;text-align:center;margin-top:80px'><h2>Invalid action.</h2></div>"


@app.route('/api/resend-bg-email/<cid>', methods=['POST'])
def resend_bg_email(cid):
    try:
        u = current_user()
        if not u:
            return jsonify({'success': False, 'message': 'Not logged in'}), 401

        c = col("candidates").find_one({"_id": cid})
        if not c:
            return jsonify({'success': False, 'message': f'Candidate {cid} not found'}), 404
        c = _clean(c)
        if c.get('hr_id') != u['id']:
            return jsonify({'success': False, 'message': 'Unauthorized'}), 403
        if c.get('offer_status') != 'accepted':
            return jsonify({'success': False,
                'message': f'Offer status is {c.get("offer_status")}, not accepted'}), 400

        existing = col("verifications").find_one({"candidate_id": cid})
        if not existing:
            vid = str(uuid.uuid4())
            col("verifications").insert_one({
                "_id":                 vid,
                "candidate_id":        cid,
                "hr_id":               c['hr_id'],
                "name":                c['name'],
                "email":               c['email'],
                "salary":              c['salary'],
                "phone":               '',
                "verification_status": 'pending',
                "created_at":          str(datetime.now()),
                "bg_email_sent":       False
            })
        else:
            vid = str(existing["_id"])

        hr      = get_user_by_id(c['hr_id']) or {}
        company = hr.get('company_name', 'the company')
        bg_link = f"{BASE_URL}/background-verification/{vid}"

        ok = send_email(
            c['email'],
            f'Next Step: Complete Your Background Verification — {company}',
            bg_verification_email_html(c, company, bg_link)
        )
        if ok:
            col("verifications").update_one(
                {"_id": vid},
                {"$set": {"bg_email_sent": True}}
            )
            return jsonify({'success': True, 'message': f'BG email sent to {c["email"]}'})
        else:
            return jsonify({'success': False,
                'message': 'send_email() returned False — check Brevo config'}), 500

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"[RESEND ERROR] {err}")
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/background-verification/<vid>')
def background_verification_page(vid):
    v = col("verifications").find_one({"_id": vid})
    if not v:
        return ("<div style='font-family:sans-serif;text-align:center;margin-top:80px'>"
                "<h2>Invalid verification link.</h2></div>"), 404
    return render_template('background_verification.html', verification=_clean(v))


@app.route('/api/submit-verification', methods=['POST'])
def api_submit_verification():
    vid = request.form.get('verification_id')
    v   = col("verifications").find_one({"_id": vid})
    if not v:
        return jsonify({'success': False}), 404
    v   = _clean(v)
    step = request.form.get('step')

    if step == '1':
        fn = request.form.get('first_name', '')
        ln = request.form.get('last_name', '')
        col("verifications").update_one({"_id": vid}, {"$set": {
            "first_name": fn, "last_name": ln, "name": f"{fn} {ln}",
            "phone":    request.form.get('phone', ''),
            "aadhaar":  request.form.get('aadhaar', ''),
            "pan":      request.form.get('pan', ''),
        }})

    elif step == '2':
        col("verifications").update_one({"_id": vid}, {"$set": {
            "college":        request.form.get('college', ''),
            "specialization": request.form.get('specialization', ''),
            "percentage":     request.form.get('percentage', ''),
        }})

    elif step == '3':
        ctype   = request.form.get('candidate_type', 'fresher')
        hr      = get_user_by_id(v.get('hr_id', '')) or {}
        company = hr.get('company_name', 'the company')

        col("verifications").update_one({"_id": vid}, {"$set": {"candidate_type": ctype}})

        if ctype == 'experienced':
            prev_co   = request.form.get('prev_company', '')
            prev_role = request.form.get('prev_role', '')
            co_email  = request.form.get('company_email', '')
            duration  = request.form.get('duration', '')
            col("verifications").update_one({"_id": vid}, {"$set": {
                "prev_company":  prev_co,
                "prev_role":     prev_role,
                "company_email": co_email,
                "duration":      duration,
                "verification_status": "pending",
                "submitted_at":  str(datetime.now()),
            }})
            vlink = f"{BASE_URL}/company-verify/{vid}/verify"
            rlink = f"{BASE_URL}/company-verify/{vid}/reject"
            send_email(co_email, f"Employment Verification — {v['name']}",
                f"""<!DOCTYPE html><html><body
  style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="540" style="background:#fff;border-radius:16px;overflow:hidden;
       box-shadow:0 4px 20px rgba(0,0,0,.08)">
  <tr><td style="background:linear-gradient(135deg,#0d1b3e,#1a56db);
                 padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">
      Employment Verification Request
    </div>
  </td></tr>
  <tr><td style="padding:36px">
    <p style="font-size:14px;color:#1e293b;line-height:1.8">
      We are conducting a background check for <b>{v['name']}</b>, who has indicated
      employment at your organisation as <b>{prev_role}</b> for <b>{duration}</b>.
    </p>
    <p style="font-size:13px;color:#64748b;margin-bottom:24px">
      Please respond using the buttons below:
    </p>
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="padding-right:12px">
        <a href="{vlink}"
           style="display:inline-block;background:#10b981;color:#fff;
                  padding:12px 28px;border-radius:8px;text-decoration:none;
                  font-weight:700;font-size:14px">&#10003; Verify Employment</a>
      </td>
      <td>
        <a href="{rlink}"
           style="display:inline-block;background:#ef4444;color:#fff;
                  padding:12px 28px;border-radius:8px;text-decoration:none;
                  font-weight:700;font-size:14px">&#10005; Cannot Verify</a>
      </td>
    </tr></table>
  </td></tr>
</table></td></tr></table></body></html>""")
        else:
            col("verifications").update_one({"_id": vid}, {"$set": {
                "verification_status": "verified",
                "submitted_at":        str(datetime.now()),
            }})
            send_email(v['email'], '&#127881; Background Verification Complete!',
                f"""<!DOCTYPE html><html><body
  style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="500" style="background:#fff;border-radius:16px;overflow:hidden">
  <tr><td style="background:linear-gradient(135deg,#10b981,#059669);
                 padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">
      Verification Successful &#10003;
    </div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <div style="font-size:44px;margin-bottom:14px">&#127882;</div>
    <h2 style="font-size:20px;color:#065f46">Congratulations, {v['name']}!</h2>
    <p style="color:#64748b;font-size:13px;line-height:1.8">
      Your background verification is complete. Welcome to <b>{company}</b>!
    </p>
  </td></tr>
</table></td></tr></table></body></html>""")

    # Re-fetch updated doc for status return
    updated = col("verifications").find_one({"_id": vid}) or {}
    return jsonify({'success': True, 'status': updated.get('verification_status', 'pending')})


@app.route('/company-verify/<vid>/<action>')
def company_verify(vid, action):
    v = col("verifications").find_one({"_id": vid})
    if not v:
        return ("<div style='font-family:sans-serif;text-align:center;margin-top:80px'>"
                "<h2>Invalid link.</h2></div>"), 404
    v       = _clean(v)
    hr      = get_user_by_id(v.get('hr_id', '')) or {}
    company = hr.get('company_name', 'the company')

    if action == 'verify':
        col("verifications").update_one(
            {"_id": vid}, {"$set": {"verification_status": "verified"}}
        )
        send_email(v['email'], '&#10003; Employment Verified!',
            f"""<!DOCTYPE html><html><body
  style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="500" style="background:#fff;border-radius:16px;overflow:hidden">
  <tr><td style="background:linear-gradient(135deg,#10b981,#059669);
                 padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">
      Background Verified &#10003;
    </div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <div style="font-size:44px;margin-bottom:14px">&#127881;</div>
    <h2 style="font-size:18px;color:#065f46">Congratulations, {v['name']}!</h2>
    <p style="color:#64748b;font-size:13px;line-height:1.8">
      Your employment has been verified. Welcome to <b>{company}</b>!
    </p>
  </td></tr>
</table></td></tr></table></body></html>""")

    elif action == 'reject':
        col("verifications").update_one(
            {"_id": vid}, {"$set": {"verification_status": "rejected"}}
        )
        send_email(v['email'], 'Update on Your Application',
            f"""<!DOCTYPE html><html><body
  style="font-family:Arial;margin:0;padding:32px 0;background:#f0f4fa">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table width="500" style="background:#fff;border-radius:16px;overflow:hidden">
  <tr><td style="background:#ef4444;padding:28px;text-align:center">
    <div style="font-size:20px;font-weight:900;color:#fff">Verification Update</div>
  </td></tr>
  <tr><td style="padding:36px;text-align:center">
    <h2 style="font-size:18px;color:#991b1b">Dear {v['name']},</h2>
    <p style="color:#64748b;font-size:13px;line-height:1.8">
      We were unable to verify your employment history.
      We regret we cannot proceed at this time.
    </p>
  </td></tr>
</table></td></tr></table></body></html>""")

    updated = col("verifications").find_one({"_id": vid}) or {}
    return render_template('company_verify_done.html',
                           action=action, candidate=_clean(updated))


@app.route('/api/download-template')
def download_template():
    u = current_user()
    if not u:
        return redirect('/login')
    cands = list(col("candidates").find({"hr_id": u['id']}))
    rows = [
        {
            'Name':            c.get('name', ''),
            'Gmail id':        c.get('email', ''),
            'Role':            c.get('role', ''),
            'Joining date':    c.get('joining_date', ''),
            'Salary':          c.get('salary', ''),
            'Employment Type': c.get('employment_type', ''),
            'Status':          c.get('offer_status', ''),
        }
        for c in cands
    ] or [{'Name': '', 'Gmail id': '', 'Role': '', 'Joining date': '',
           'Salary': '', 'Employment Type': '', 'Status': ''}]

    df   = pd.DataFrame(rows)
    path = os.path.join("/tmp", "offers_export.xlsx")
    df.to_excel(path, index=False)
    return send_file(path, as_attachment=True, download_name='offers_export.xlsx')


if __name__ == '__main__':
    app.run(debug=True)