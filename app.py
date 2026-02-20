from flask import Flask, request, jsonify, send_file
from slack_sdk import WebClient
import pandas as pd
import os
import time
import threading
import json
from datetime import datetime, timedelta
import pytz
from notion_client import Client as NotionClient
import requests
import openai
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak
)
from reportlab.graphics.shapes import Drawing, Rect
from reportlab.graphics import renderPDF
import io

app = Flask(__name__)

# =========================
# CONFIGURATION
# =========================

USER_TOKEN = os.environ.get("SLACK_TOKEN")
if not USER_TOKEN:
    raise ValueError("SLACK_TOKEN environment variable not set!")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
if not NOTION_TOKEN:
    raise ValueError("NOTION_TOKEN environment variable not set!")

NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
if not NOTION_DATABASE_ID:
    raise ValueError("NOTION_DATABASE_ID environment variable not set!")

TYPEFORM_WEBHOOK_SECRET = os.environ.get("TYPEFORM_WEBHOOK_SECRET")
if not TYPEFORM_WEBHOOK_SECRET:
    raise ValueError("TYPEFORM_WEBHOOK_SECRET environment variable not set!")

ALERT_SLACK_CHANNEL = os.environ.get("ALERT_SLACK_CHANNEL")
if not ALERT_SLACK_CHANNEL:
    raise ValueError("ALERT_SLACK_CHANNEL environment variable not set!")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable not set!")

REPORT_OUTPUT_PATH = os.environ.get("REPORT_OUTPUT_PATH", "reports")
os.makedirs(REPORT_OUTPUT_PATH, exist_ok=True)

openai.api_key = OPENAI_API_KEY

user_client = WebClient(token=USER_TOKEN)
bot_client = WebClient(token=BOT_TOKEN)
notion = NotionClient(auth=NOTION_TOKEN)

ADMIN_USER_ID = "U0AFL5S3R0A"

CSV_PATH = "workspace_users.csv"
if not os.path.exists(CSV_PATH):
    raise FileNotFoundError(f"{CSV_PATH} not found.")

startups = pd.read_csv(CSV_PATH)
required_columns = {"startup_name", "founder_name", "slack_user_id"}
missing_columns = required_columns - set(startups.columns)
if missing_columns:
    raise Exception(f"CSV is missing required columns: {missing_columns}")

DEFAULT_MESSAGE_TEMPLATE = (
    "Olá {founder_name}, tenho acompanhado a {startup_name} "
    "e queria compartilhar algo com você!"
)

HEALTH_CHECK_MESSAGE_TEMPLATE = (
    "Olá {founder_name}! 👋 É hora do check-in mensal da {startup_name}.\n\n"
    "Leva menos de 3 minutos e ajuda-nos a acompanhar o vosso progresso "
    "e a perceber onde podemos ajudar.\n\n"
    "👉 {typeform_url}"
)

TYPEFORM_URL = os.environ.get("TYPEFORM_URL", "https://your-typeform-url.typeform.com/to/xxxxxx")

admin_session = {
    "selected_startup_ids": None,
    "message_template": DEFAULT_MESSAGE_TEMPLATE,
    "scheduled_time": None,
    "scheduled_timer": None,
    "timezone": "Europe/Lisbon"
}

# =========================
# ALERT THRESHOLDS
# =========================

RUNWAY_ALERT_THRESHOLD = 6
MRR_DROP_ALERT = True

# =========================
# BRAND COLORS
# =========================

UF_BLACK = colors.HexColor("#0A0A0A")
UF_WHITE = colors.HexColor("#FFFFFF")
UF_ACCENT = colors.HexColor("#6C3CF5")       # purple — change to match UF brand
UF_LIGHT = colors.HexColor("#F4F2FF")
UF_GRAY = colors.HexColor("#6B7280")
UF_GREEN = colors.HexColor("#10B981")
UF_YELLOW = colors.HexColor("#F59E0B")
UF_RED = colors.HexColor("#EF4444")

# =========================
# NOTION DATA SOURCE HELPER
# =========================

_data_source_id_cache = None

def get_data_source_id():
    global _data_source_id_cache
    if _data_source_id_cache:
        return _data_source_id_cache
    try:
        response = requests.get(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2025-09-03"
            }
        )
        data = response.json()
        data_sources = data.get("data_sources", [])
        if not data_sources:
            raise Exception("No data sources found for this database.")
        _data_source_id_cache = data_sources[0]["id"]
        print(f"[DEBUG] Fetched data_source_id: {_data_source_id_cache}")
        return _data_source_id_cache
    except Exception as e:
        print(f"[ERROR] Failed to fetch data_source_id: {e}")
        return None


def notion_query(filters=None, sorts=None, page_size=100):
    data_source_id = get_data_source_id()
    if not data_source_id:
        return []
    body = {}
    if filters:
        body["filter"] = filters
    if sorts:
        body["sorts"] = sorts
    if page_size:
        body["page_size"] = page_size
    try:
        response = requests.post(
            f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2025-09-03",
                "Content-Type": "application/json"
            },
            json=body
        )
        return response.json().get("results", [])
    except Exception as e:
        print(f"[ERROR] Notion query failed: {e}")
        return []


def notion_create_page(properties):
    data_source_id = get_data_source_id()
    if not data_source_id:
        return False
    try:
        response = requests.post(
            "https://api.notion.com/v1/pages",
            headers={
                "Authorization": f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2025-09-03",
                "Content-Type": "application/json"
            },
            json={
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": data_source_id
                },
                "properties": properties
            }
        )
        if response.status_code == 200:
            return True
        print(f"[ERROR] Notion page creation failed: {response.text}")
        return False
    except Exception as e:
        print(f"[ERROR] Notion page creation exception: {e}")
        return False

# =========================
# AI COMPANY MATCHING
# =========================

def match_company_with_ai(raw_name):
    company_list = startups["startup_name"].tolist()
    companies_str = "\n".join(company_list)
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a company name matcher for a startup portfolio. "
                        "Given a name typed by a user and a list of known company names, "
                        "return the single best match from the list even if it is only a partial match. "
                        "For example 'Gamma' should match 'Gamma FinTech'. "
                        "Return ONLY the exact company name from the list, nothing else. "
                        "Only return 'Unknown' if there is truly no reasonable match at all."
                    )
                },
                {
                    "role": "user",
                    "content": f"Typed name: {raw_name}\n\nKnown companies:\n{companies_str}"
                }
            ],
            temperature=0
        )
        matched = response.choices[0].message.content.strip()
        print(f"[DEBUG] AI matched '{raw_name}' → '{matched}'")
        return matched if matched != "Unknown" else None
    except Exception as e:
        print(f"[ERROR] AI matching failed: {e}")
        return None

# =========================
# NOTION HELPERS
# =========================

def get_previous_mrr(company_name):
    pages = notion_query(
        filters={"property": "Company", "title": {"equals": company_name}},
        sorts=[{"property": "Date", "direction": "descending"}],
        page_size=1
    )
    if not pages:
        return None
    return pages[0]["properties"].get("MRR (€)", {}).get("number", None)


def get_latest_entry_per_company(month_start):
    pages = notion_query(
        filters={"property": "Date", "date": {"on_or_after": month_start}},
        sorts=[{"property": "Date", "direction": "descending"}]
    )
    seen = set()
    latest = []
    for page in pages:
        props = page["properties"]
        company = (
            props.get("Company", {})
            .get("title", [{}])[0]
            .get("text", {})
            .get("content", "Unknown")
        )
        if company not in seen:
            seen.add(company)
            latest.append(page)
    return latest


def write_to_notion(data):
    company = data.get("company", "Unknown")
    founder = data.get("founder", "Unknown")
    mrr = data.get("mrr", 0)
    runway = data.get("runway", 0)
    headcount = data.get("headcount", 0)
    biggest_win = data.get("biggest_win", "")
    biggest_blocker = data.get("biggest_blocker", "")
    help_needed = data.get("help_needed", "")

    previous_mrr = get_previous_mrr(company)

    alert = False
    alert_reasons = []

    if runway < RUNWAY_ALERT_THRESHOLD:
        alert = True
        alert_reasons.append(f"Runway is only {runway} months")

    if MRR_DROP_ALERT and previous_mrr is not None and mrr < previous_mrr:
        alert = True
        alert_reasons.append(f"MRR dropped from €{previous_mrr:,.0f} to €{mrr:,.0f}")

    if help_needed and help_needed.strip().lower() not in ["no", "não", "nao", "n/a", "-", ""]:
        alert = True
        alert_reasons.append(f"Founder requested help: {help_needed}")

    alert_reason_text = " | ".join(alert_reasons) if alert_reasons else ""

    properties = {
        "Company": {"title": [{"text": {"content": company}}]},
        "Founder": {"rich_text": [{"text": {"content": founder}}]},
        "Date": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
        "MRR (€)": {"number": mrr},
        "Previous MRR (€)": {"number": previous_mrr if previous_mrr is not None else 0},
        "Runway (months)": {"number": runway},
        "Headcount": {"number": headcount},
        "Biggest Win": {"rich_text": [{"text": {"content": biggest_win}}]},
        "Biggest Blocker": {"rich_text": [{"text": {"content": biggest_blocker}}]},
        "Help Needed": {"rich_text": [{"text": {"content": help_needed}}]},
        "Alert": {"checkbox": alert},
        "Alert Reason": {"rich_text": [{"text": {"content": alert_reason_text}}]},
        "Responded": {"checkbox": True}
    }

    success = notion_create_page(properties)
    if success:
        print(f"[DEBUG] Notion row created for {company}")
        return alert, alert_reasons
    return False, []

# =========================
# ALERT HELPERS
# =========================

def send_alert(company, founder, alert_reasons):
    reasons_text = "\n".join([f"• {r}" for r in alert_reasons])
    message = (
        f"🚨 *Health Check Alert — {company}*\n"
        f"Founder: {founder}\n\n"
        f"{reasons_text}\n\n"
        f"Check the Notion dashboard for full details."
    )
    try:
        bot_client.chat_postMessage(channel=ALERT_SLACK_CHANNEL, text=message)
        print(f"[DEBUG] Alert sent for {company}")
    except Exception as e:
        print(f"[ERROR] Failed to send alert: {e}")

# =========================
# PDF REPORT GENERATOR
# =========================

def get_status_indicator(runway, mrr, previous_mrr, help_needed):
    """Return a status string based on company health."""
    if runway < 3:
        return "🔴 Critical"
    if runway < 6:
        return "🟡 Watch"
    if previous_mrr and mrr < previous_mrr:
        return "🟡 Watch"
    if help_needed and help_needed.strip().lower() not in ["no", "não", "nao", "n/a", "-", ""]:
        return "🟡 Watch"
    return "🟢 Healthy"


def generate_ai_narrative(portfolio_data, month_str, total_mrr, avg_runway, total_headcount, pending_companies):
    """Use GPT-4o to write the executive summary narrative."""
    companies_summary = ""
    for d in portfolio_data:
        companies_summary += (
            f"- {d['company']} (Founder: {d['founder']}): "
            f"MRR €{d['mrr']:,.0f}, Runway {d['runway']} months, "
            f"Headcount {d['headcount']}, "
            f"Win: {d['biggest_win']}, "
            f"Blocker: {d['biggest_blocker']}, "
            f"Status: {d['status']}\n"
        )

    pending_str = ", ".join(pending_companies) if pending_companies else "None"

    prompt = f"""You are writing the executive summary for Unicorn Factory Lisboa's monthly investor update for {month_str}.

Portfolio data:
{companies_summary}

Aggregate metrics:
- Combined portfolio MRR: €{total_mrr:,.0f}
- Average runway: {avg_runway:.1f} months
- Total headcount across portfolio: {total_headcount}
- Companies yet to report: {pending_str}

Write a professional, confident, and honest 3-paragraph executive summary for investors. 
Paragraph 1: Overall portfolio health and momentum this month.
Paragraph 2: Standout wins and concerning signals, named specifically.
Paragraph 3: How Unicorn Factory Lisboa is actively supporting the portfolio and what to expect next month.

Tone: warm but professional, like a top European VC fund. Do not use bullet points. Do not be overly positive — be candid about challenges while remaining constructive. Keep it under 250 words."""

    try:
        response = openai.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert VC fund manager writing investor updates."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[ERROR] GPT narrative generation failed: {e}")
        return "Executive summary unavailable this month."


def build_pdf_report(month_str, portfolio_data, pending_companies):
    """Generate the full investor update PDF and return the file path."""

    tz = pytz.timezone("Europe/Lisbon")
    now = datetime.now(tz)
    filename = f"UnicornFactory_InvestorUpdate_{now.strftime('%Y_%m')}.pdf"
    filepath = os.path.join(REPORT_OUTPUT_PATH, filename)

    # Aggregate metrics
    total_mrr = sum(d["mrr"] for d in portfolio_data)
    avg_runway = sum(d["runway"] for d in portfolio_data) / len(portfolio_data) if portfolio_data else 0
    total_headcount = sum(d["headcount"] for d in portfolio_data)
    total_companies = len(startups)
    responded_count = len(portfolio_data)

    # Generate AI narrative
    narrative = generate_ai_narrative(
        portfolio_data, month_str, total_mrr,
        avg_runway, total_headcount, pending_companies
    )

    # ── Document setup ──────────────────────────────────────────────
    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=20*mm,
        bottomMargin=20*mm
    )

    W = A4[0] - 40*mm  # usable width

    # ── Styles ──────────────────────────────────────────────────────
    styles = getSampleStyleSheet()

    style_cover_title = ParagraphStyle(
        "CoverTitle",
        fontName="Helvetica-Bold",
        fontSize=32,
        leading=38,
        textColor=UF_WHITE,
        alignment=TA_LEFT,
        spaceAfter=4*mm
    )
    style_cover_sub = ParagraphStyle(
        "CoverSub",
        fontName="Helvetica",
        fontSize=14,
        textColor=colors.HexColor("#CCBBFF"),
        alignment=TA_LEFT,
        spaceAfter=2*mm
    )
    style_cover_date = ParagraphStyle(
        "CoverDate",
        fontName="Helvetica",
        fontSize=11,
        textColor=colors.HexColor("#999999"),
        alignment=TA_LEFT,
    )
    style_section_header = ParagraphStyle(
        "SectionHeader",
        fontName="Helvetica-Bold",
        fontSize=16,
        textColor=UF_BLACK,
        spaceBefore=6*mm,
        spaceAfter=3*mm
    )
    style_body = ParagraphStyle(
        "Body",
        fontName="Helvetica",
        fontSize=10,
        leading=16,
        textColor=colors.HexColor("#374151"),
        spaceAfter=3*mm
    )
    style_company_name = ParagraphStyle(
        "CompanyName",
        fontName="Helvetica-Bold",
        fontSize=13,
        textColor=UF_BLACK,
        spaceAfter=1*mm
    )
    style_label = ParagraphStyle(
        "Label",
        fontName="Helvetica-Bold",
        fontSize=8,
        textColor=UF_GRAY,
        spaceAfter=0
    )
    style_value = ParagraphStyle(
        "Value",
        fontName="Helvetica",
        fontSize=10,
        textColor=UF_BLACK,
        spaceAfter=2*mm
    )
    style_small = ParagraphStyle(
        "Small",
        fontName="Helvetica",
        fontSize=8,
        textColor=UF_GRAY,
        spaceAfter=1*mm
    )
    style_footer = ParagraphStyle(
        "Footer",
        fontName="Helvetica",
        fontSize=8,
        textColor=UF_GRAY,
        alignment=TA_CENTER
    )

    story = []

    # ── COVER PAGE ───────────────────────────────────────────────────
    # Dark background cover using a table
    cover_data = [[
        Paragraph("Unicorn Factory<br/>Lisboa", style_cover_title),
    ]]
    cover_table = Table(cover_data, colWidths=[W])
    cover_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), UF_BLACK),
        ("TOPPADDING", (0, 0), (-1, -1), 40*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10*mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 12*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12*mm),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [UF_BLACK]),
    ]))
    story.append(cover_table)

    # Accent bar
    accent_data = [[""]]
    accent_table = Table(accent_data, colWidths=[W], rowHeights=[3*mm])
    accent_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), UF_ACCENT),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(accent_table)

    # Cover subtitle block
    cover_info_data = [[
        Paragraph(f"Portfolio Update", style_cover_sub),
    ], [
        Paragraph(f"{month_str}", style_cover_title),
    ], [
        Paragraph(f"Prepared {now.strftime('%d %B %Y')}  ·  Confidential", style_cover_date),
    ]]
    cover_info_table = Table(cover_info_data, colWidths=[W])
    cover_info_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), UF_BLACK),
        ("TOPPADDING", (0, 0), (0, 0), 6*mm),
        ("TOPPADDING", (0, 1), (-1, -1), 1*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -2), 1*mm),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 40*mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 12*mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12*mm),
    ]))
    story.append(cover_info_table)
    story.append(PageBreak())

    # ── PAGE 2 — EXECUTIVE SUMMARY ───────────────────────────────────
    story.append(Paragraph("Executive Summary", style_section_header))
    story.append(HRFlowable(width=W, thickness=1, color=UF_ACCENT, spaceAfter=4*mm))

    # Aggregate stats band
    stats_data = [[
        Paragraph(f"€{total_mrr:,.0f}<br/><font size=8 color='#6B7280'>Combined MRR</font>", style_body),
        Paragraph(f"{responded_count}/{total_companies}<br/><font size=8 color='#6B7280'>Companies Reported</font>", style_body),
        Paragraph(f"{avg_runway:.1f} mo<br/><font size=8 color='#6B7280'>Avg. Runway</font>", style_body),
        Paragraph(f"{total_headcount}<br/><font size=8 color='#6B7280'>Total Headcount</font>", style_body),
    ]]
    stats_table = Table(stats_data, colWidths=[W/4]*4)
    stats_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), UF_LIGHT),
        ("TOPPADDING", (0, 0), (-1, -1), 5*mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5*mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 5*mm),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 14),
        ("TEXTCOLOR", (0, 0), (-1, -1), UF_BLACK),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [UF_LIGHT]),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
    ]))
    story.append(stats_table)
    story.append(Spacer(1, 5*mm))

    # AI narrative
    for para in narrative.split("\n\n"):
        if para.strip():
            story.append(Paragraph(para.strip(), style_body))

    story.append(PageBreak())

    # ── PAGE 3+ — STARTUP SNAPSHOTS ──────────────────────────────────
    story.append(Paragraph("Portfolio Snapshots", style_section_header))
    story.append(HRFlowable(width=W, thickness=1, color=UF_ACCENT, spaceAfter=4*mm))

    for i, d in enumerate(portfolio_data):
        status = d["status"]
        if "🔴" in status:
            status_color = UF_RED
        elif "🟡" in status:
            status_color = UF_YELLOW
        else:
            status_color = UF_GREEN

        # Company header row
        header_data = [[
            Paragraph(d["company"], style_company_name),
            Paragraph(status, ParagraphStyle(
                "Status", fontName="Helvetica-Bold", fontSize=10,
                textColor=status_color, alignment=TA_RIGHT
            ))
        ]]
        header_table = Table(header_data, colWidths=[W*0.7, W*0.3])
        header_table.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1*mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(header_table)
        story.append(Paragraph(f"Founder: {d['founder']}", style_small))

        # Metrics row
        metrics_data = [[
            Paragraph(f"<b>€{d['mrr']:,.0f}</b><br/><font size=7 color='#6B7280'>MRR</font>", style_body),
            Paragraph(f"<b>{d['runway']} mo</b><br/><font size=7 color='#6B7280'>Runway</font>", style_body),
            Paragraph(f"<b>{d['headcount']}</b><br/><font size=7 color='#6B7280'>Headcount</font>", style_body),
            Paragraph(
                f"<b>{'↑' if not d['previous_mrr'] or d['mrr'] >= d['previous_mrr'] else '↓'} {abs(((d['mrr'] - d['previous_mrr']) / d['previous_mrr'] * 100)) if d['previous_mrr'] else 0:.0f}%</b><br/><font size=7 color='#6B7280'>MRR Change</font>",
                ParagraphStyle(
                    "MrrChange",
                    fontName="Helvetica-Bold",
                    fontSize=10,
                    textColor=UF_GREEN if not d['previous_mrr'] or d['mrr'] >= d['previous_mrr'] else UF_RED,
                    spaceAfter=2*mm
                )
            ),
        ]]
        metrics_table = Table(metrics_data, colWidths=[W/4]*4)
        metrics_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), UF_LIGHT),
            ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ]))
        story.append(metrics_table)
        story.append(Spacer(1, 2*mm))

        # Win / Blocker
        wb_data = [[
            Paragraph(f"<b>🏆 Biggest Win</b><br/>{d['biggest_win']}", style_body),
            Paragraph(f"<b>🚧 Current Blocker</b><br/>{d['biggest_blocker']}", style_body),
        ]]
        wb_table = Table(wb_data, colWidths=[W/2, W/2])
        wb_table.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3*mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
        ]))
        story.append(wb_table)

        # Help needed (only if flagged)
        if d["help_needed"] and d["help_needed"].strip().lower() not in ["no", "não", "nao", "n/a", "-", ""]:
            story.append(Spacer(1, 1*mm))
            help_data = [[
                Paragraph(f"<b>💬 Support Requested:</b> {d['help_needed']}", style_body)
            ]]
            help_table = Table(help_data, colWidths=[W])
            help_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
                ("TOPPADDING", (0, 0), (-1, -1), 2*mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2*mm),
                ("LEFTPADDING", (0, 0), (-1, -1), 3*mm),
                ("BOX", (0, 0), (-1, -1), 0.5, UF_YELLOW),
            ]))
            story.append(help_table)

        story.append(Spacer(1, 5*mm))
        story.append(HRFlowable(width=W, thickness=0.5, color=colors.HexColor("#E5E7EB"), spaceAfter=4*mm))

    # ── LAST PAGE — PENDING + CLOSING ────────────────────────────────
    if pending_companies:
        story.append(PageBreak())
        story.append(Paragraph("Awaiting Updates", style_section_header))
        story.append(HRFlowable(width=W, thickness=1, color=UF_ACCENT, spaceAfter=3*mm))
        story.append(Paragraph(
            "The following portfolio companies have not yet submitted their monthly update. "
            "Unicorn Factory Lisboa is actively following up.",
            style_body
        ))
        for company in sorted(pending_companies):
            story.append(Paragraph(f"• {company}", style_body))
        story.append(Spacer(1, 6*mm))

    # Closing note
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width=W, thickness=1, color=UF_ACCENT, spaceAfter=4*mm))
    story.append(Paragraph(
        f"Next update expected in {(now.replace(day=1) + timedelta(days=32)).replace(day=1).strftime('%B %Y')}. "
        f"For questions, contact the Unicorn Factory Lisboa portfolio team.",
        style_footer
    ))
    story.append(Paragraph(
        "This document is confidential and intended solely for the named recipients.",
        style_footer
    ))

    doc.build(story)
    print(f"[DEBUG] PDF generated: {filepath}")
    return filepath


def fetch_portfolio_data_for_report():
    """Pull this month's data from Notion and format it for the PDF."""
    tz = pytz.timezone("Europe/Lisbon")
    now = datetime.now(tz)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")

    pages = get_latest_entry_per_company(month_start)

    portfolio_data = []
    responded_companies = set()

    for page in pages:
        props = page["properties"]
        company = props.get("Company", {}).get("title", [{}])[0].get("text", {}).get("content", "Unknown")
        founder = props.get("Founder", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "Unknown")
        mrr = props.get("MRR (€)", {}).get("number", 0) or 0
        previous_mrr = props.get("Previous MRR (€)", {}).get("number", 0) or 0
        runway = props.get("Runway (months)", {}).get("number", 0) or 0
        headcount = props.get("Headcount", {}).get("number", 0) or 0
        biggest_win = props.get("Biggest Win", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "—")
        biggest_blocker = props.get("Biggest Blocker", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "—")
        help_needed = props.get("Help Needed", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")

        status = get_status_indicator(runway, mrr, previous_mrr, help_needed)

        responded_companies.add(company)
        portfolio_data.append({
            "company": company,
            "founder": founder,
            "mrr": mrr,
            "previous_mrr": previous_mrr,
            "runway": runway,
            "headcount": headcount,
            "biggest_win": biggest_win,
            "biggest_blocker": biggest_blocker,
            "help_needed": help_needed,
            "status": status
        })

    all_companies = set(startups["startup_name"].tolist())
    pending_companies = sorted(all_companies - responded_companies)

    return portfolio_data, pending_companies


def generate_and_send_report(user_id):
    """Generate the PDF and send it to the admin via Slack with an approve button."""
    tz = pytz.timezone("Europe/Lisbon")
    now = datetime.now(tz)
    month_str = now.strftime("%B %Y")

    try:
        bot_client.chat_postMessage(
            channel=user_id,
            text=f"⏳ Generating investor update for *{month_str}*... this takes about 30 seconds."
        )
    except Exception as e:
        print(f"[ERROR] Could not notify admin: {e}")

    portfolio_data, pending_companies = fetch_portfolio_data_for_report()

    if not portfolio_data:
        try:
            bot_client.chat_postMessage(
                channel=user_id,
                text="⚠️ No health check data found for this month. Ask founders to fill the form first."
            )
        except Exception as e:
            print(f"[ERROR] {e}")
        return

    filepath = build_pdf_report(month_str, portfolio_data, pending_companies)

    # Upload PDF to Slack
    try:
        # Open a DM channel with the admin first to get a valid channel ID
        dm = bot_client.conversations_open(users=user_id)
        dm_channel_id = dm["channel"]["id"]

        with open(filepath, "rb") as f:
            bot_client.files_upload_v2(
                channel=dm_channel_id,
                file=f,
                filename=os.path.basename(filepath),
                initial_comment=(
                    f"📊 *Investor Update — {month_str}* is ready for your review.\n\n"
                    f"*{len(portfolio_data)}/{len(startups)} companies* reported this month. "
                    f"{'*⚠️ ' + str(len(pending_companies)) + ' companies pending.*' if pending_companies else '✅ All companies reported.'}\n\n"
                    f"Review the PDF above, then forward it to your investors when ready."
                )
            )
        print(f"[DEBUG] PDF sent to admin via Slack.")
    except Exception as e:
        print(f"[ERROR] Failed to upload PDF to Slack: {e}")


# =========================
# MONTHLY INVESTOR UPDATE SCHEDULER
# =========================

def schedule_monthly_investor_update():
    """Auto-generate investor update on the 10th of every month at 09:00 Lisbon time."""
    tz = pytz.timezone("Europe/Lisbon")

    def next_run_time():
        now = datetime.now(tz)
        if now.day == 10 and now.hour < 9:
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            if now.month == 12:
                target = now.replace(year=now.year + 1, month=1, day=10, hour=9, minute=0, second=0, microsecond=0)
            else:
                target = now.replace(month=now.month + 1, day=10, hour=9, minute=0, second=0, microsecond=0)
        return target

    def run():
        while True:
            target = next_run_time()
            now = datetime.now(tz)
            delay = (target - now).total_seconds()
            print(f"[CRON] Next investor update scheduled for {target.strftime('%d %b %Y at %H:%M %Z')} (in {delay:.0f}s)")
            time.sleep(delay)
            generate_and_send_report(ADMIN_USER_ID)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

# =========================
# TYPEFORM WEBHOOK
# =========================

def parse_typeform_response(payload):
    answers = payload.get("form_response", {}).get("answers", [])
    definition = payload.get("form_response", {}).get("definition", {})
    fields = {f["id"]: f["title"] for f in definition.get("fields", [])}

    data = {}
    for answer in answers:
        field_id = answer.get("field", {}).get("id")
        field_title = fields.get(field_id, "").lower()
        answer_type = answer.get("type")

        value = None
        if answer_type == "text":
            value = answer.get("text", "")
        elif answer_type == "number":
            value = answer.get("number", 0)

        if "company" in field_title:
            data["company"] = value
        elif "mrr" in field_title:
            data["mrr"] = float(value) if value else 0
        elif "runway" in field_title:
            data["runway"] = float(value) if value else 0
        elif "headcount" in field_title or "team" in field_title or "people" in field_title:
            data["headcount"] = int(value) if value else 0
        elif "win" in field_title:
            data["biggest_win"] = value or ""
        elif "blocker" in field_title:
            data["biggest_blocker"] = value or ""
        elif "help" in field_title or "unicorn" in field_title:
            data["help_needed"] = value or ""

    raw_company = data.get("company", "")
    matched_company = match_company_with_ai(raw_company) if raw_company else None

    if matched_company:
        data["company"] = matched_company
        match = startups[startups["startup_name"] == matched_company]
        if not match.empty:
            data["founder"] = match.iloc[0]["founder_name"]
        else:
            data["founder"] = "Unknown"
    else:
        data["founder"] = "Unknown"

    return data


@app.route("/typeform/webhook", methods=["POST"])
def typeform_webhook():
    secret = request.args.get("secret", "")
    if secret != TYPEFORM_WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.json
    if not payload:
        return jsonify({"error": "No payload"}), 400

    data = parse_typeform_response(payload)
    print(f"[DEBUG] Parsed Typeform response: {data}")

    alert, alert_reasons = write_to_notion(data)
    if alert:
        send_alert(data.get("company", "Unknown"), data.get("founder", "Unknown"), alert_reasons)

    return jsonify({"status": "ok"}), 200

# =========================
# HEALTH CHECK BLAST
# =========================

def send_health_check_pings(user_id):
    if user_id != ADMIN_USER_ID:
        return

    total_sent = 0
    for _, row in startups.iterrows():
        slack_id = str(row.get("slack_user_id", ""))
        if not slack_id or slack_id == "nan":
            continue

        message = HEALTH_CHECK_MESSAGE_TEMPLATE.format(
            founder_name=row.get("founder_name", "Founder"),
            startup_name=row.get("startup_name", "Startup"),
            typeform_url=TYPEFORM_URL
        )

        try:
            bot_client.chat_postMessage(channel=slack_id, text=message)
            total_sent += 1
            time.sleep(1)
        except Exception as e:
            print(f"[ERROR] Failed to send health check ping to {slack_id}: {e}")

    try:
        bot_client.chat_postMessage(
            channel=user_id,
            text=f"✅ Health check pings sent to {total_sent} founders."
        )
    except Exception as e:
        print(f"[ERROR] Could not notify admin: {e}")

# =========================
# MONTHLY CRON SCHEDULER
# =========================

def schedule_monthly_health_check():
    tz = pytz.timezone("Europe/Lisbon")

    def next_run_time():
        now = datetime.now(tz)
        if now.day == 1 and now.hour < 9:
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            if now.month == 12:
                target = now.replace(year=now.year + 1, month=1, day=1, hour=9, minute=0, second=0, microsecond=0)
            else:
                target = now.replace(month=now.month + 1, day=1, hour=9, minute=0, second=0, microsecond=0)
        return target

    def run():
        while True:
            target = next_run_time()
            now = datetime.now(tz)
            delay = (target - now).total_seconds()
            print(f"[CRON] Next health check ping scheduled for {target.strftime('%d %b %Y at %H:%M %Z')} (in {delay:.0f}s)")
            time.sleep(delay)
            send_health_check_pings(ADMIN_USER_ID)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

# =========================
# WEEKLY DIGEST
# =========================

def send_weekly_digest():
    tz = pytz.timezone("Europe/Lisbon")
    now = datetime.now(tz)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")

    pages = get_latest_entry_per_company(month_start)

    if not pages:
        try:
            bot_client.chat_postMessage(
                channel=ALERT_SLACK_CHANNEL,
                text="📋 *Weekly Portfolio Digest* — No health check responses received this month yet."
            )
        except Exception as e:
            print(f"[ERROR] Failed to send empty digest: {e}")
        return

    responded = []
    alerts = []
    responded_companies = set()

    for page in pages:
        props = page["properties"]
        company = props.get("Company", {}).get("title", [{}])[0].get("text", {}).get("content", "Unknown")
        founder = props.get("Founder", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "Unknown")
        mrr = props.get("MRR (€)", {}).get("number", 0) or 0
        runway = props.get("Runway (months)", {}).get("number", 0) or 0
        alert = props.get("Alert", {}).get("checkbox", False)
        alert_reason = props.get("Alert Reason", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")

        responded_companies.add(company)
        responded.append(f"• *{company}* ({founder}) — MRR: €{mrr:,.0f} | Runway: {runway}mo")
        if alert:
            alerts.append(f"• 🚨 *{company}* — {alert_reason}")

    all_companies = set(startups["startup_name"].tolist())
    pending_companies = all_companies - responded_companies
    total_startups = len(all_companies)
    responded_count = len(responded_companies)
    missing_count = len(pending_companies)

    digest = f"📋 *Weekly Portfolio Digest — {now.strftime('%B %Y')}*\n\n"
    digest += f"*{responded_count}/{total_startups} founders have responded* ({missing_count} still pending)\n\n"
    if responded:
        digest += "*Responses this month:*\n" + "\n".join(responded) + "\n\n"
    if alerts:
        digest += "*⚠️ Active alerts:*\n" + "\n".join(alerts) + "\n\n"
    if pending_companies:
        pending_list = "\n".join([f"• {c}" for c in sorted(pending_companies)])
        digest += f"*Still waiting on:*\n{pending_list}\n\n"
        digest += "_Run `/healthcheck` to re-ping founders who haven't responded yet._"

    try:
        bot_client.chat_postMessage(channel=ALERT_SLACK_CHANNEL, text=digest)
        print("[DEBUG] Weekly digest sent.")
    except Exception as e:
        print(f"[ERROR] Failed to send weekly digest: {e}")


def schedule_weekly_digest():
    tz = pytz.timezone("Europe/Lisbon")

    def next_monday():
        now = datetime.now(tz)
        days_until_monday = (7 - now.weekday()) % 7 or 7
        target = (now + timedelta(days=days_until_monday)).replace(hour=8, minute=0, second=0, microsecond=0)
        return target

    def run():
        while True:
            target = next_monday()
            now = datetime.now(tz)
            delay = (target - now).total_seconds()
            print(f"[CRON] Next weekly digest scheduled for {target.strftime('%d %b %Y at %H:%M %Z')} (in {delay:.0f}s)")
            time.sleep(delay)
            send_weekly_digest()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

# =========================
# CORE MESSAGE PROCESSING
# =========================

def process_messages(user_id, client_type="user", selected_ids=None, message_template=None):
    if user_id != ADMIN_USER_ID:
        return

    total_sent = 0
    client = user_client if client_type == "user" else bot_client
    source = "USER" if client_type == "user" else "BOT"
    template = message_template or DEFAULT_MESSAGE_TEMPLATE

    for _, row in startups.iterrows():
        slack_id = str(row.get("slack_user_id", ""))
        if not slack_id or slack_id == "nan":
            continue
        if selected_ids is not None and slack_id not in selected_ids:
            continue
        message = template.format(
            founder_name=row.get("founder_name", "Founder"),
            startup_name=row.get("startup_name", "Startup")
        )
        try:
            client.chat_postMessage(channel=slack_id, text=message)
            total_sent += 1
            time.sleep(1)
        except Exception as e:
            print(f"Failed to send to {slack_id}: {e}")

    admin_session["scheduled_time"] = None
    admin_session["scheduled_timer"] = None

    try:
        client.chat_postMessage(channel=user_id, text=f"Done! Sent {total_sent} messages as {source}.")
    except Exception as e:
        print(f"Could not notify admin: {e}")

    try:
        bot_client.views_publish(user_id=user_id, view=build_admin_home_view())
    except Exception as e:
        print(f"Failed to refresh Home tab post-send: {e}")


def schedule_messages(user_id, scheduled_dt_utc, client_type="bot", selected_ids=None, message_template=None):
    cancel_scheduled_send(notify=False)
    delay_seconds = (scheduled_dt_utc - datetime.now(pytz.utc)).total_seconds()
    if delay_seconds < 0:
        delay_seconds = 0
    timer = threading.Timer(delay_seconds, process_messages, args=(user_id, client_type, selected_ids, message_template))
    timer.daemon = True
    timer.start()
    admin_session["scheduled_time"] = scheduled_dt_utc
    admin_session["scheduled_timer"] = timer


def cancel_scheduled_send(notify=True):
    timer = admin_session.get("scheduled_timer")
    if timer:
        timer.cancel()
    admin_session["scheduled_timer"] = None
    admin_session["scheduled_time"] = None
    if notify:
        try:
            bot_client.chat_postMessage(channel=ADMIN_USER_ID, text="⏹️ Scheduled send cancelled.")
        except Exception as e:
            print(f"Could not notify admin of cancellation: {e}")

# =========================
# ROOT
# =========================

@app.route("/")
def home():
    return "UnicornFactory bot is running."

# =========================
# SLASH COMMANDS
# =========================

@app.route("/sendmessages", methods=["POST"])
def send_messages():
    user_id = request.form.get("user_id")
    if user_id != ADMIN_USER_ID:
        return jsonify({"response_type": "ephemeral", "text": "You are not allowed to use this command."}), 200
    threading.Thread(target=process_messages, args=(user_id, "user", admin_session["selected_startup_ids"], admin_session["message_template"])).start()
    return jsonify({"response_type": "ephemeral", "text": "Blasting messages now. Check your DMs for a confirmation when it's done."}), 200


@app.route("/healthcheck", methods=["POST"])
def trigger_health_check_slash():
    user_id = request.form.get("user_id")
    if user_id != ADMIN_USER_ID:
        return jsonify({"response_type": "ephemeral", "text": "You are not allowed to use this command."}), 200
    threading.Thread(target=send_health_check_pings, args=(user_id,)).start()
    return jsonify({"response_type": "ephemeral", "text": "Sending health check pings now. You'll get a DM when it's done."}), 200


@app.route("/digest", methods=["POST"])
def trigger_digest_slash():
    user_id = request.form.get("user_id")
    if user_id != ADMIN_USER_ID:
        return jsonify({"response_type": "ephemeral", "text": "You are not allowed to use this command."}), 200
    threading.Thread(target=send_weekly_digest).start()
    return jsonify({"response_type": "ephemeral", "text": "Generating digest now. Check your Slack channel in a moment."}), 200


@app.route("/investorupdate", methods=["POST"])
def trigger_investor_update_slash():
    user_id = request.form.get("user_id")
    if user_id != ADMIN_USER_ID:
        return jsonify({"response_type": "ephemeral", "text": "You are not allowed to use this command."}), 200
    threading.Thread(target=generate_and_send_report, args=(user_id,)).start()
    return jsonify({"response_type": "ephemeral", "text": "⏳ Generating investor update PDF... you'll receive it as a DM in about 30 seconds."}), 200

# =========================
# HOME TAB VIEWS
# =========================

def format_scheduled_time_local(dt_utc):
    tz = pytz.timezone(admin_session.get("timezone", "Europe/Lisbon"))
    local_dt = dt_utc.astimezone(tz)
    return local_dt.strftime("%d %b %Y at %H:%M (%Z)")


def build_admin_home_view():
    startup_count = len(startups)
    selected_ids = admin_session["selected_startup_ids"]
    current_template = admin_session["message_template"]
    scheduled_time = admin_session["scheduled_time"]
    selected_count = startup_count if selected_ids is None else len(selected_ids)
    skipped_count = startup_count - selected_count

    startup_options = []
    initial_options = []
    for _, row in startups.iterrows():
        slack_id = str(row.get("slack_user_id", ""))
        label = f"{row.get('founder_name', '?')}  —  {row.get('startup_name', '?')}"
        option = {"text": {"type": "plain_text", "text": label[:75], "emoji": False}, "value": slack_id}
        startup_options.append(option)
        if selected_ids is None or slack_id in selected_ids:
            initial_options.append(option)

    scheduled_status_blocks = []
    if scheduled_time:
        scheduled_status_blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"⏰ *Scheduled send active* — Messages will go out on *{format_scheduled_time_local(scheduled_time)}*."},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel schedule", "emoji": False},
                    "action_id": "cancel_schedule_button",
                    "style": "danger",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Cancel scheduled send?"},
                        "text": {"type": "mrkdwn", "text": "This will cancel the upcoming scheduled send."},
                        "confirm": {"type": "plain_text", "text": "Yes, cancel it"},
                        "deny": {"type": "plain_text", "text": "Keep it"}
                    }
                }
            },
            {"type": "divider"}
        ]

    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "UnicornFactory", "emoji": False}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Outreach that hits different. Select your founders, craft your message, blast it out."}},
            {"type": "divider"},
            *scheduled_status_blocks,
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*{startup_count}*\nFounders in the roster"},
                {"type": "mrkdwn", "text": f"*{selected_count}*\nSelected to receive"},
                {"type": "mrkdwn", "text": f"*{skipped_count}*\nSkipped this round"},
                {"type": "mrkdwn", "text": "*Admin*\nFull access"}
            ]},
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "Who's getting this?", "emoji": False}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Uncheck anyone you want to skip. Everyone else gets the message."}},
            {"type": "actions", "elements": [{"type": "checkboxes", "action_id": "startup_selector", "options": startup_options, "initial_options": initial_options}]},
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "The message", "emoji": False}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{current_template}_"},
                "accessory": {"type": "button", "text": {"type": "plain_text", "text": "Edit", "emoji": False}, "action_id": "open_message_editor", "style": "primary"}
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Live preview  —  " + current_template.format(founder_name="Maria", startup_name="Acme")}]},
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "Ready to launch?", "emoji": False}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"You're about to reach *{selected_count} founder(s)*. You'll get a DM the moment it's done."}},
            {"type": "actions", "elements": [
                {
                    "type": "button", "text": {"type": "plain_text", "text": "Send Now", "emoji": False},
                    "style": "primary", "action_id": "send_messages_button",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Launch outreach?"},
                        "text": {"type": "mrkdwn", "text": f"This sends your message to *{selected_count} founder(s)* right now."},
                        "confirm": {"type": "plain_text", "text": "Let's go"},
                        "deny": {"type": "plain_text", "text": "Not yet"}
                    }
                },
                {"type": "button", "text": {"type": "plain_text", "text": "Schedule Send", "emoji": False}, "action_id": "open_schedule_modal"},
                {"type": "button", "text": {"type": "plain_text", "text": "Reset everything", "emoji": False}, "action_id": "reset_defaults_button"}
            ]},
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "Portfolio Health Checks", "emoji": False}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Ping all founders with the monthly health check form, or generate the portfolio digest on demand."}},
            {"type": "actions", "elements": [
                {
                    "type": "button", "text": {"type": "plain_text", "text": "Send Health Check Pings", "emoji": False},
                    "action_id": "send_health_check_button",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Send health check pings?"},
                        "text": {"type": "mrkdwn", "text": "This will DM every founder in your roster with the monthly health check form link."},
                        "confirm": {"type": "plain_text", "text": "Send it"},
                        "deny": {"type": "plain_text", "text": "Not yet"}
                    }
                },
                {"type": "button", "text": {"type": "plain_text", "text": "Generate Digest", "emoji": False}, "action_id": "send_digest_button"}
            ]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Health checks go out automatically on the 1st of each month. Digest posts every Monday at 08:00."}]},
            {"type": "divider"},
            {"type": "header", "text": {"type": "plain_text", "text": "Investor Update", "emoji": False}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Generate a professional PDF investor update from this month's health check data. Auto-runs on the 10th of each month."}},
            {"type": "actions", "elements": [
                {
                    "type": "button", "text": {"type": "plain_text", "text": "Generate Investor Update PDF", "emoji": False},
                    "style": "primary", "action_id": "generate_investor_update_button",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Generate investor update?"},
                        "text": {"type": "mrkdwn", "text": "This will generate a PDF report from this month's data and send it to you as a DM for review."},
                        "confirm": {"type": "plain_text", "text": "Generate it"},
                        "deny": {"type": "plain_text", "text": "Not yet"}
                    }
                }
            ]},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Use `/investorupdate` from any channel too. You'll receive the PDF as a DM for review before forwarding."}]}
        ]
    }


def build_message_editor_modal():
    return {
        "type": "modal",
        "callback_id": "message_editor_modal",
        "title": {"type": "plain_text", "text": "Edit message", "emoji": False},
        "submit": {"type": "plain_text", "text": "Save", "emoji": False},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": False},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Make it yours.*\nUse `{founder_name}` and `{startup_name}` as dynamic placeholders."}},
            {"type": "divider"},
            {
                "type": "input", "block_id": "message_editor_block",
                "element": {
                    "type": "plain_text_input", "action_id": "message_editor",
                    "multiline": True, "initial_value": admin_session["message_template"],
                    "placeholder": {"type": "plain_text", "text": "Write something worth reading..."}
                },
                "label": {"type": "plain_text", "text": "Message body", "emoji": False},
                "hint": {"type": "plain_text", "text": "Click Save when you're happy with it.", "emoji": False}
            }
        ]
    }


def build_schedule_modal():
    tz_label = admin_session.get("timezone", "Europe/Lisbon")
    tz = pytz.timezone(tz_label)
    tomorrow_local = (datetime.now(tz) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return {
        "type": "modal", "callback_id": "schedule_modal",
        "title": {"type": "plain_text", "text": "Schedule send", "emoji": False},
        "submit": {"type": "plain_text", "text": "Schedule", "emoji": False},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": False},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Pick a date and time* ({tz_label})."}},
            {"type": "divider"},
            {"type": "input", "block_id": "schedule_date_block", "element": {"type": "datepicker", "action_id": "schedule_date", "initial_date": tomorrow_local.strftime("%Y-%m-%d"), "placeholder": {"type": "plain_text", "text": "Select a date"}}, "label": {"type": "plain_text", "text": "Date", "emoji": False}},
            {"type": "input", "block_id": "schedule_time_block", "element": {"type": "timepicker", "action_id": "schedule_time", "initial_time": tomorrow_local.strftime("%H:%M"), "placeholder": {"type": "plain_text", "text": "Select a time"}}, "label": {"type": "plain_text", "text": "Time", "emoji": False}},
            {"type": "input", "block_id": "schedule_tz_block", "element": {"type": "plain_text_input", "action_id": "schedule_tz", "initial_value": tz_label, "placeholder": {"type": "plain_text", "text": "e.g. Europe/Lisbon"}}, "label": {"type": "plain_text", "text": "Timezone (IANA format)", "emoji": False}, "hint": {"type": "plain_text", "text": "Your last used timezone is pre-filled.", "emoji": False}}
        ]
    }


def build_guest_home_view():
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "UnicornFactory", "emoji": False}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Outreach that hits different."}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*This area is admin-only.*\nYou don't have access to the outreach dashboard."}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "UnicornFactory Outreach Bot  ·  Admin access required"}]}
        ]
    }

# =========================
# SLACK EVENTS
# =========================

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})
    if data.get("event", {}).get("type") == "app_home_opened":
        user_id = data["event"]["user"]
        try:
            view = build_admin_home_view() if user_id == ADMIN_USER_ID else build_guest_home_view()
            bot_client.views_publish(user_id=user_id, view=view)
        except Exception as e:
            print(f"Failed to publish Home tab: {e}")
    return "", 200

# =========================
# INTERACTIONS
# =========================

@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload = json.loads(request.form["payload"])
    payload_type = payload.get("type")
    user_id = payload["user"]["id"]

    if user_id != ADMIN_USER_ID:
        return "", 200

    if payload_type == "view_submission":
        callback_id = payload["view"]["callback_id"]

        if callback_id == "message_editor_modal":
            new_text = (
                payload.get("view", {}).get("state", {}).get("values", {})
                .get("message_editor_block", {}).get("message_editor", {}).get("value", "") or ""
            ).strip()
            if new_text:
                admin_session["message_template"] = new_text
            threading.Thread(target=lambda: bot_client.views_publish(user_id=user_id, view=build_admin_home_view())).start()
            return "", 200

        if callback_id == "schedule_modal":
            values = payload.get("view", {}).get("state", {}).get("values", {})
            date_str = values.get("schedule_date_block", {}).get("schedule_date", {}).get("selected_date", "")
            time_str = values.get("schedule_time_block", {}).get("schedule_time", {}).get("selected_time", "")
            tz_str = values.get("schedule_tz_block", {}).get("schedule_tz", {}).get("value", "Europe/Lisbon").strip()

            try:
                tz = pytz.timezone(tz_str)
            except pytz.exceptions.UnknownTimeZoneError:
                return jsonify({"response_action": "errors", "errors": {"schedule_tz_block": f"'{tz_str}' is not a valid IANA timezone."}}), 200

            try:
                local_dt = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
                utc_dt = local_dt.astimezone(pytz.utc)
            except Exception as e:
                print(f"[ERROR] Failed to parse schedule datetime: {e}")
                return "", 200

            if utc_dt <= datetime.now(pytz.utc):
                return jsonify({"response_action": "errors", "errors": {"schedule_date_block": "The scheduled time must be in the future."}}), 200

            admin_session["timezone"] = tz_str
            schedule_messages(user_id=user_id, scheduled_dt_utc=utc_dt, client_type="bot", selected_ids=admin_session["selected_startup_ids"], message_template=admin_session["message_template"])

            try:
                bot_client.chat_postMessage(channel=user_id, text=f"✅ Scheduled! Your message goes out on *{format_scheduled_time_local(utc_dt)}*.")
            except Exception as e:
                print(f"Could not send schedule confirmation: {e}")

            threading.Thread(target=lambda: bot_client.views_publish(user_id=user_id, view=build_admin_home_view())).start()
            return "", 200

    if payload_type == "block_actions":
        actions = payload.get("actions", [])
        if not actions:
            return "", 200

        action_id = actions[0]["action_id"]

        if action_id == "open_message_editor":
            try:
                bot_client.views_open(trigger_id=payload["trigger_id"], view=build_message_editor_modal())
            except Exception as e:
                print(f"Failed to open modal: {e}")
            return "", 200

        if action_id == "open_schedule_modal":
            try:
                bot_client.views_open(trigger_id=payload["trigger_id"], view=build_schedule_modal())
            except Exception as e:
                print(f"Failed to open schedule modal: {e}")
            return "", 200

        if action_id == "startup_selector":
            selected = actions[0].get("selected_options", [])
            admin_session["selected_startup_ids"] = ({opt["value"] for opt in selected} if selected else set())
        elif action_id == "send_messages_button":
            threading.Thread(target=process_messages, args=(user_id, "bot", admin_session["selected_startup_ids"], admin_session["message_template"])).start()
        elif action_id == "cancel_schedule_button":
            cancel_scheduled_send(notify=True)
        elif action_id == "reset_defaults_button":
            cancel_scheduled_send(notify=False)
            admin_session["selected_startup_ids"] = None
            admin_session["message_template"] = DEFAULT_MESSAGE_TEMPLATE
        elif action_id == "send_health_check_button":
            threading.Thread(target=send_health_check_pings, args=(user_id,)).start()
        elif action_id == "send_digest_button":
            threading.Thread(target=send_weekly_digest).start()
        elif action_id == "generate_investor_update_button":
            threading.Thread(target=generate_and_send_report, args=(user_id,)).start()

        try:
            bot_client.views_publish(user_id=user_id, view=build_admin_home_view())
        except Exception as e:
            print(f"Failed to refresh Home tab: {e}")

    return "", 200

# =========================
# HELPERS
# =========================

def startup_count_for_session():
    selected_ids = admin_session["selected_startup_ids"]
    return len(startups) if selected_ids is None else len(selected_ids)

# =========================
# RUN APP
# =========================

if __name__ == "__main__":
    schedule_monthly_health_check()
    schedule_weekly_digest()
    schedule_monthly_investor_update()
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)