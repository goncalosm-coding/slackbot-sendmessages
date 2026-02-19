from flask import Flask, request, jsonify
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

RUNWAY_ALERT_THRESHOLD = 6      # months
MRR_DROP_ALERT = True           # alert if MRR dropped vs previous month

# =========================
# NOTION HELPERS
# =========================

def get_previous_mrr(company_name):
    """Fetch the most recent MRR entry for a company from Notion."""
    try:
        results = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={
                "property": "Company",
                "title": {
                    "equals": company_name
                }
            },
            sorts=[
                {
                    "property": "Date",
                    "direction": "descending"
                }
            ],
            page_size=1
        )
        pages = results.get("results", [])
        if not pages:
            return None
        props = pages[0]["properties"]
        mrr_prop = props.get("MRR (€)", {})
        return mrr_prop.get("number", None)
    except Exception as e:
        print(f"[ERROR] Failed to fetch previous MRR for {company_name}: {e}")
        return None


def write_to_notion(data):
    """Write a health check response to the Notion database."""
    company = data.get("company", "Unknown")
    founder = data.get("founder", "Unknown")
    mrr = data.get("mrr", 0)
    runway = data.get("runway", 0)
    headcount = data.get("headcount", 0)
    biggest_win = data.get("biggest_win", "")
    biggest_blocker = data.get("biggest_blocker", "")
    help_needed = data.get("help_needed", "")

    previous_mrr = get_previous_mrr(company)

    # Determine alerts
    alert = False
    alert_reasons = []

    if runway < RUNWAY_ALERT_THRESHOLD:
        alert = True
        alert_reasons.append(f"Runway is only {runway} months")

    if MRR_DROP_ALERT and previous_mrr is not None and mrr < previous_mrr:
        alert = True
        alert_reasons.append(f"MRR dropped from €{previous_mrr:,.0f} to €{mrr:,.0f}")

    if help_needed and help_needed.strip():
        alert = True
        alert_reasons.append(f"Founder requested help: {help_needed}")

    alert_reason_text = " | ".join(alert_reasons) if alert_reasons else ""

    try:
        notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={
                "Company": {
                    "title": [{"text": {"content": company}}]
                },
                "Founder": {
                    "rich_text": [{"text": {"content": founder}}]
                },
                "Date": {
                    "date": {"start": datetime.now().strftime("%Y-%m-%d")}
                },
                "MRR (€)": {
                    "number": mrr
                },
                "Previous MRR (€)": {
                    "number": previous_mrr if previous_mrr is not None else 0
                },
                "Runway (months)": {
                    "number": runway
                },
                "Headcount": {
                    "number": headcount
                },
                "Biggest Win": {
                    "rich_text": [{"text": {"content": biggest_win}}]
                },
                "Biggest Blocker": {
                    "rich_text": [{"text": {"content": biggest_blocker}}]
                },
                "Help Needed": {
                    "rich_text": [{"text": {"content": help_needed}}]
                },
                "Alert": {
                    "checkbox": alert
                },
                "Alert Reason": {
                    "rich_text": [{"text": {"content": alert_reason_text}}]
                },
                "Responded": {
                    "checkbox": True
                }
            }
        )
        print(f"[DEBUG] Notion row created for {company}")
        return alert, alert_reasons
    except Exception as e:
        print(f"[ERROR] Failed to write to Notion: {e}")
        return False, []


# =========================
# ALERT HELPERS
# =========================

def send_alert(company, founder, alert_reasons):
    """Send a Slack alert to the admin channel."""
    reasons_text = "\n".join([f"• {r}" for r in alert_reasons])
    message = (
        f"🚨 *Health Check Alert — {company}*\n"
        f"Founder: {founder}\n\n"
        f"{reasons_text}\n\n"
        f"Check the Notion dashboard for full details."
    )
    try:
        bot_client.chat_postMessage(
            channel=ALERT_SLACK_CHANNEL,
            text=message
        )
        print(f"[DEBUG] Alert sent for {company}")
    except Exception as e:
        print(f"[ERROR] Failed to send alert: {e}")


# =========================
# TYPEFORM WEBHOOK
# =========================

def parse_typeform_response(payload):
    """Extract answers from a Typeform webhook payload."""
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
        elif "headcount" in field_title or "team" in field_title:
            data["headcount"] = int(value) if value else 0
        elif "win" in field_title:
            data["biggest_win"] = value or ""
        elif "blocker" in field_title:
            data["biggest_blocker"] = value or ""
        elif "help" in field_title or "unicorn" in field_title:
            data["help_needed"] = value or ""

    # Try to match founder name from CSV using company name
    company_name = data.get("company", "")
    match = startups[startups["startup_name"].str.lower() == company_name.lower()]
    if not match.empty:
        data["founder"] = match.iloc[0]["founder_name"]
    else:
        data["founder"] = "Unknown"

    return data


@app.route("/typeform/webhook", methods=["POST"])
def typeform_webhook():
    # Verify the secret
    secret = request.args.get("secret", "")
    if secret != TYPEFORM_WEBHOOK_SECRET:
        print("[WARN] Unauthorized webhook attempt")
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.json
    if not payload:
        return jsonify({"error": "No payload"}), 400

    data = parse_typeform_response(payload)
    print(f"[DEBUG] Parsed Typeform response: {data}")

    # Write to Notion and check for alerts
    alert, alert_reasons = write_to_notion(data)

    # Send alert if needed
    if alert:
        send_alert(data.get("company", "Unknown"), data.get("founder", "Unknown"), alert_reasons)

    return jsonify({"status": "ok"}), 200


# =========================
# HEALTH CHECK BLAST
# =========================

def send_health_check_pings(user_id):
    """DM every founder in the CSV with the health check Typeform link."""
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


@app.route("/healthcheck", methods=["POST"])
def trigger_health_check():
    """Slash command to manually trigger health check pings."""
    user_id = request.form.get("user_id")

    if user_id != ADMIN_USER_ID:
        return jsonify({
            "response_type": "ephemeral",
            "text": "You are not allowed to use this command."
        }), 200

    threading.Thread(target=send_health_check_pings, args=(user_id,)).start()

    return jsonify({
        "response_type": "ephemeral",
        "text": "Sending health check pings now. You'll get a DM when it's done."
    }), 200


# =========================
# MONTHLY CRON SCHEDULER
# =========================

def schedule_monthly_health_check():
    """Schedule the health check ping for the 1st of every month at 09:00 Lisbon time."""
    tz = pytz.timezone("Europe/Lisbon")

    def next_run_time():
        now = datetime.now(tz)
        # Next 1st of month at 09:00
        if now.day == 1 and now.hour < 9:
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        else:
            # Roll to next month
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
    """Query Notion for this month's responses and post a digest to Slack."""
    tz = pytz.timezone("Europe/Lisbon")
    now = datetime.now(tz)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d")

    try:
        results = notion.databases.query(
            database_id=NOTION_DATABASE_ID,
            filter={
                "property": "Date",
                "date": {
                    "on_or_after": month_start
                }
            },
            sorts=[{"property": "Date", "direction": "descending"}]
        )
        pages = results.get("results", [])
    except Exception as e:
        print(f"[ERROR] Failed to query Notion for digest: {e}")
        return

    if not pages:
        bot_client.chat_postMessage(
            channel=ALERT_SLACK_CHANNEL,
            text="📋 *Weekly Portfolio Digest* — No health check responses received this month yet."
        )
        return

    responded = []
    alerts = []

    for page in pages:
        props = page["properties"]
        company = props.get("Company", {}).get("title", [{}])[0].get("text", {}).get("content", "Unknown")
        founder = props.get("Founder", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "Unknown")
        mrr = props.get("MRR (€)", {}).get("number", 0) or 0
        runway = props.get("Runway (months)", {}).get("number", 0) or 0
        alert = props.get("Alert", {}).get("checkbox", False)
        alert_reason = props.get("Alert Reason", {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")

        responded.append(f"• *{company}* ({founder}) — MRR: €{mrr:,.0f} | Runway: {runway}mo")
        if alert:
            alerts.append(f"• 🚨 *{company}* — {alert_reason}")

    total_startups = len(startups)
    responded_count = len(pages)
    missing_count = total_startups - responded_count

    digest = f"📋 *Weekly Portfolio Digest — {now.strftime('%B %Y')}*\n\n"
    digest += f"*{responded_count}/{total_startups} founders have responded* ({missing_count} still pending)\n\n"

    if responded:
        digest += "*Responses this month:*\n" + "\n".join(responded) + "\n\n"

    if alerts:
        digest += "*⚠️ Active alerts:*\n" + "\n".join(alerts) + "\n\n"

    if missing_count > 0:
        digest += f"_Tip: Run `/healthcheck` to re-ping founders who haven't responded yet._"

    try:
        bot_client.chat_postMessage(channel=ALERT_SLACK_CHANNEL, text=digest)
        print("[DEBUG] Weekly digest sent.")
    except Exception as e:
        print(f"[ERROR] Failed to send weekly digest: {e}")


def schedule_weekly_digest():
    """Schedule the weekly digest every Monday at 08:00 Lisbon time."""
    tz = pytz.timezone("Europe/Lisbon")

    def next_monday():
        now = datetime.now(tz)
        days_until_monday = (7 - now.weekday()) % 7 or 7
        target = (now + timedelta(days=days_until_monday)).replace(
            hour=8, minute=0, second=0, microsecond=0
        )
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
        print(f"User {user_id} is not allowed to send messages.")
        return

    total_sent = 0
    client = user_client if client_type == "user" else bot_client
    source = "USER" if client_type == "user" else "BOT"
    template = message_template or DEFAULT_MESSAGE_TEMPLATE

    print(f"[DEBUG] Sending as {source} with template: {template}")

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
        client.chat_postMessage(
            channel=user_id,
            text=f"Done! Sent {total_sent} messages as {source}."
        )
    except Exception as e:
        print(f"Could not notify admin: {e}")

    try:
        bot_client.views_publish(
            user_id=user_id,
            view=build_admin_home_view()
        )
    except Exception as e:
        print(f"Failed to refresh Home tab post-send: {e}")


def schedule_messages(user_id, scheduled_dt_utc, client_type="bot", selected_ids=None, message_template=None):
    cancel_scheduled_send(notify=False)

    delay_seconds = (scheduled_dt_utc - datetime.now(pytz.utc)).total_seconds()
    if delay_seconds < 0:
        delay_seconds = 0

    timer = threading.Timer(
        delay_seconds,
        process_messages,
        args=(user_id, client_type, selected_ids, message_template)
    )
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
            bot_client.chat_postMessage(
                channel=ADMIN_USER_ID,
                text="⏹️ Scheduled send cancelled."
            )
        except Exception as e:
            print(f"Could not notify admin of cancellation: {e}")


# =========================
# ROOT / HEALTH CHECK
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
        return jsonify({
            "response_type": "ephemeral",
            "text": "You are not allowed to use this command."
        }), 200

    thread = threading.Thread(
        target=process_messages,
        args=(
            user_id,
            "user",
            admin_session["selected_startup_ids"],
            admin_session["message_template"]
        )
    )
    thread.start()

    return jsonify({
        "response_type": "ephemeral",
        "text": "Blasting messages now. Check your DMs for a confirmation when it's done."
    }), 200


@app.route("/healthcheck", methods=["POST"])
def trigger_health_check_slash():
    user_id = request.form.get("user_id")

    if user_id != ADMIN_USER_ID:
        return jsonify({
            "response_type": "ephemeral",
            "text": "You are not allowed to use this command."
        }), 200

    threading.Thread(target=send_health_check_pings, args=(user_id,)).start()

    return jsonify({
        "response_type": "ephemeral",
        "text": "Sending health check pings now. You'll get a DM when it's done."
    }), 200


@app.route("/digest", methods=["POST"])
def trigger_digest_slash():
    """Slash command to manually trigger the weekly digest."""
    user_id = request.form.get("user_id")

    if user_id != ADMIN_USER_ID:
        return jsonify({
            "response_type": "ephemeral",
            "text": "You are not allowed to use this command."
        }), 200

    threading.Thread(target=send_weekly_digest).start()

    return jsonify({
        "response_type": "ephemeral",
        "text": "Generating digest now. Check your Slack channel in a moment."
    }), 200


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
        option = {
            "text": {"type": "plain_text", "text": label[:75], "emoji": False},
            "value": slack_id
        }
        startup_options.append(option)
        if selected_ids is None or slack_id in selected_ids:
            initial_options.append(option)

    scheduled_status_blocks = []
    if scheduled_time:
        scheduled_status_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⏰ *Scheduled send active* — "
                        f"Messages will go out on *{format_scheduled_time_local(scheduled_time)}*."
                    )
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel schedule", "emoji": False},
                    "action_id": "cancel_schedule_button",
                    "style": "danger",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Cancel scheduled send?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": "This will cancel the upcoming scheduled send. No messages will be sent."
                        },
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
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "UnicornFactory", "emoji": False}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Outreach that hits different. Select your founders, craft your message, blast it out."
                }
            },
            {"type": "divider"},
            *scheduled_status_blocks,
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*{startup_count}*\nFounders in the roster"},
                    {"type": "mrkdwn", "text": f"*{selected_count}*\nSelected to receive"},
                    {"type": "mrkdwn", "text": f"*{skipped_count}*\nSkipped this round"},
                    {"type": "mrkdwn", "text": "*Admin*\nFull access"}
                ]
            },
            {"type": "divider"},
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Who's getting this?", "emoji": False}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Uncheck anyone you want to skip. Everyone else gets the message."}
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "checkboxes",
                        "action_id": "startup_selector",
                        "options": startup_options,
                        "initial_options": initial_options
                    }
                ]
            },
            {"type": "divider"},
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "The message", "emoji": False}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"_{current_template}_"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit", "emoji": False},
                    "action_id": "open_message_editor",
                    "style": "primary"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Live preview  —  " + current_template.format(founder_name="Maria", startup_name="Acme")
                    }
                ]
            },
            {"type": "divider"},
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Ready to launch?", "emoji": False}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"You're about to reach *{selected_count} founder(s)*. You'll get a DM the moment it's done."
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Send Now", "emoji": False},
                        "style": "primary",
                        "action_id": "send_messages_button",
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Launch outreach?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": f"This sends your message to *{selected_count} founder(s)* right now. No take-backs."
                            },
                            "confirm": {"type": "plain_text", "text": "Let's go"},
                            "deny": {"type": "plain_text", "text": "Not yet"}
                        }
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Schedule Send", "emoji": False},
                        "action_id": "open_schedule_modal"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reset everything", "emoji": False},
                        "action_id": "reset_defaults_button"
                    }
                ]
            },
            {
                "type": "divider"
            },
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Portfolio Health Checks", "emoji": False}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Ping all founders with the monthly health check form, or generate the portfolio digest on demand."
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Send Health Check Pings", "emoji": False},
                        "action_id": "send_health_check_button",
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Send health check pings?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": "This will DM every founder in your roster with the monthly health check form link."
                            },
                            "confirm": {"type": "plain_text", "text": "Send it"},
                            "deny": {"type": "plain_text", "text": "Not yet"}
                        }
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Generate Digest", "emoji": False},
                        "action_id": "send_digest_button"
                    }
                ]
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Health checks go out automatically on the 1st of each month. "
                            "Digest posts every Monday at 08:00. "
                            "Use `/healthcheck` or `/digest` from any channel too."
                        )
                    }
                ]
            }
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
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Make it yours.*\n"
                        "Use `{founder_name}` and `{startup_name}` as dynamic placeholders — "
                        "they'll be swapped out for each founder automatically."
                    )
                }
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "message_editor_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "message_editor",
                    "multiline": True,
                    "initial_value": admin_session["message_template"],
                    "placeholder": {"type": "plain_text", "text": "Write something worth reading..."}
                },
                "label": {"type": "plain_text", "text": "Message body", "emoji": False},
                "hint": {
                    "type": "plain_text",
                    "text": "Click Save when you're happy with it. The Home tab will update with a live preview.",
                    "emoji": False
                }
            }
        ]
    }


def build_schedule_modal():
    tz_label = admin_session.get("timezone", "Europe/Lisbon")
    tz = pytz.timezone(tz_label)
    tomorrow_local = (datetime.now(tz) + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )

    return {
        "type": "modal",
        "callback_id": "schedule_modal",
        "title": {"type": "plain_text", "text": "Schedule send", "emoji": False},
        "submit": {"type": "plain_text", "text": "Schedule", "emoji": False},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": False},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Pick a date and time* ({tz_label}).\nThe outreach will fire automatically at the moment you choose."
                }
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "schedule_date_block",
                "element": {
                    "type": "datepicker",
                    "action_id": "schedule_date",
                    "initial_date": tomorrow_local.strftime("%Y-%m-%d"),
                    "placeholder": {"type": "plain_text", "text": "Select a date"}
                },
                "label": {"type": "plain_text", "text": "Date", "emoji": False}
            },
            {
                "type": "input",
                "block_id": "schedule_time_block",
                "element": {
                    "type": "timepicker",
                    "action_id": "schedule_time",
                    "initial_time": tomorrow_local.strftime("%H:%M"),
                    "placeholder": {"type": "plain_text", "text": "Select a time"}
                },
                "label": {"type": "plain_text", "text": "Time", "emoji": False}
            },
            {
                "type": "input",
                "block_id": "schedule_tz_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "schedule_tz",
                    "initial_value": tz_label,
                    "placeholder": {"type": "plain_text", "text": "e.g. Europe/Lisbon, America/New_York"}
                },
                "label": {"type": "plain_text", "text": "Timezone (IANA format)", "emoji": False},
                "hint": {
                    "type": "plain_text",
                    "text": "Enter a valid IANA timezone name. Your last used timezone is pre-filled.",
                    "emoji": False
                }
            }
        ]
    }


def build_guest_home_view():
    return {
        "type": "home",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "UnicornFactory", "emoji": False}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Outreach that hits different."}
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*This area is admin-only.*\nYou don't have access to the outreach dashboard. If you think that's a mistake, ping your admin."
                }
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "UnicornFactory Outreach Bot  ·  Admin access required"}
                ]
            }
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

            threading.Thread(target=lambda: bot_client.views_publish(
                user_id=user_id, view=build_admin_home_view()
            )).start()
            return "", 200

        if callback_id == "schedule_modal":
            values = payload.get("view", {}).get("state", {}).get("values", {})
            date_str = values.get("schedule_date_block", {}).get("schedule_date", {}).get("selected_date", "")
            time_str = values.get("schedule_time_block", {}).get("schedule_time", {}).get("selected_time", "")
            tz_str = values.get("schedule_tz_block", {}).get("schedule_tz", {}).get("value", "Europe/Lisbon").strip()

            try:
                tz = pytz.timezone(tz_str)
            except pytz.exceptions.UnknownTimeZoneError:
                return jsonify({
                    "response_action": "errors",
                    "errors": {"schedule_tz_block": f"'{tz_str}' is not a valid IANA timezone."}
                }), 200

            try:
                local_dt = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
                utc_dt = local_dt.astimezone(pytz.utc)
            except Exception as e:
                print(f"[ERROR] Failed to parse schedule datetime: {e}")
                return "", 200

            if utc_dt <= datetime.now(pytz.utc):
                return jsonify({
                    "response_action": "errors",
                    "errors": {"schedule_date_block": "The scheduled time must be in the future."}
                }), 200

            admin_session["timezone"] = tz_str
            schedule_messages(
                user_id=user_id,
                scheduled_dt_utc=utc_dt,
                client_type="bot",
                selected_ids=admin_session["selected_startup_ids"],
                message_template=admin_session["message_template"]
            )

            try:
                bot_client.chat_postMessage(
                    channel=user_id,
                    text=f"✅ Scheduled! Your message goes out on *{format_scheduled_time_local(utc_dt)}*."
                )
            except Exception as e:
                print(f"Could not send schedule confirmation: {e}")

            threading.Thread(target=lambda: bot_client.views_publish(
                user_id=user_id, view=build_admin_home_view()
            )).start()
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
            admin_session["selected_startup_ids"] = (
                {opt["value"] for opt in selected} if selected else set()
            )

        elif action_id == "send_messages_button":
            threading.Thread(
                target=process_messages,
                args=(user_id, "bot", admin_session["selected_startup_ids"], admin_session["message_template"])
            ).start()

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

        try:
            bot_client.views_publish(user_id=user_id, view=build_admin_home_view())
        except Exception as e:
            print(f"Failed to refresh Home tab: {e}")

    return "", 200


# =========================
# STARTUP CRON JOBS
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
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)