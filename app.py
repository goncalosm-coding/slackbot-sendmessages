from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time
import threading
import json
from datetime import datetime, timedelta
import pytz

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

user_client = WebClient(token=USER_TOKEN)
bot_client = WebClient(token=BOT_TOKEN)

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

admin_session = {
    "selected_startup_ids": None,
    "message_template": DEFAULT_MESSAGE_TEMPLATE,
    "scheduled_time": None,       # datetime object (UTC) or None
    "scheduled_timer": None,      # threading.Timer object or None
    "timezone": "Europe/Lisbon"   # default timezone for display
}

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

    # Clear scheduled state after sending
    admin_session["scheduled_time"] = None
    admin_session["scheduled_timer"] = None

    try:
        client.chat_postMessage(
            channel=user_id,
            text=f"Done! Sent {total_sent} messages as {source}."
        )
    except Exception as e:
        print(f"Could not notify admin: {e}")

    # Refresh Home tab to reflect cleared schedule
    try:
        bot_client.views_publish(
            user_id=user_id,
            view=build_admin_home_view()
        )
    except Exception as e:
        print(f"Failed to refresh Home tab post-send: {e}")


def schedule_messages(user_id, scheduled_dt_utc, client_type="bot", selected_ids=None, message_template=None):
    """Cancel any existing scheduled send and schedule a new one."""
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

    print(f"[DEBUG] Scheduled send in {delay_seconds:.0f}s at {scheduled_dt_utc.isoformat()}")


def cancel_scheduled_send(notify=True):
    """Cancel any pending scheduled send."""
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
# SLASH COMMAND
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

# =========================
# HOME TAB VIEWS
# =========================

def format_scheduled_time_local(dt_utc):
    """Return a human-readable local time string from a UTC datetime."""
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

    # Build the scheduled send status block (shown only when a send is queued)
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

            # ── BRAND HEADER ───────────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "UnicornFactory",
                    "emoji": False
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Outreach that hits different. Select your founders, craft your message, blast it out."
                }
            },
            {"type": "divider"},

            # ── SCHEDULED STATUS (conditional) ─────────────────────────
            *scheduled_status_blocks,

            # ── STATS BAND ─────────────────────────────────────────────
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*{startup_count}*\nFounders in the roster"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*{selected_count}*\nSelected to receive"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*{skipped_count}*\nSkipped this round"
                    },
                    {
                        "type": "mrkdwn",
                        "text": "*Admin*\nFull access"
                    }
                ]
            },
            {"type": "divider"},

            # ── RECIPIENTS ─────────────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Who's getting this?",
                    "emoji": False
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Uncheck anyone you want to skip. Everyone else gets the message."
                }
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

            # ── MESSAGE PREVIEW ────────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "The message",
                    "emoji": False
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"_{current_template}_"
                },
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
                        "text": (
                            "Live preview  —  "
                            + current_template.format(founder_name="Maria", startup_name="Acme")
                        )
                    }
                ]
            },
            {"type": "divider"},

            # ── LAUNCH ─────────────────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Ready to launch?",
                    "emoji": False
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"You're about to reach *{selected_count} founder(s)*. "
                        f"You'll get a DM the moment it's done."
                    )
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
                                "text": (
                                    f"This sends your message to *{selected_count} founder(s)* right now. "
                                    f"No take-backs."
                                )
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
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "You can also trigger an immediate send via `/sendmessages` from any channel."
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
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Write something worth reading..."
                    }
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
    """Modal for picking a date and time to schedule the send."""
    tz_label = admin_session.get("timezone", "Europe/Lisbon")
    # Pre-fill with tomorrow at 09:00 local time as a sensible default
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
                    "text": (
                        f"*Pick a date and time* ({tz_label}).\n"
                        "The outreach will fire automatically at the moment you choose."
                    )
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
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a date"
                    }
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
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Select a time"
                    }
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
                    "placeholder": {
                        "type": "plain_text",
                        "text": "e.g. Europe/Lisbon, America/New_York"
                    }
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
                "text": {
                    "type": "plain_text",
                    "text": "UnicornFactory",
                    "emoji": False
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Outreach that hits different."
                }
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
                    {
                        "type": "mrkdwn",
                        "text": "UnicornFactory Outreach Bot  ·  Admin access required"
                    }
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

    # ── Modal submitted ────────────────────────────────────────────────
    if payload_type == "view_submission":
        callback_id = payload["view"]["callback_id"]

        # Message editor modal
        if callback_id == "message_editor_modal":
            new_text = (
                payload
                .get("view", {})
                .get("state", {})
                .get("values", {})
                .get("message_editor_block", {})
                .get("message_editor", {})
                .get("value", "")
                or ""
            ).strip()

            if new_text:
                admin_session["message_template"] = new_text
                print(f"[DEBUG] Message saved: {new_text}")

            def refresh_home():
                try:
                    bot_client.views_publish(
                        user_id=user_id,
                        view=build_admin_home_view()
                    )
                except Exception as e:
                    print(f"Failed to refresh Home tab: {e}")

            threading.Thread(target=refresh_home).start()
            return "", 200

        # Schedule modal
        if callback_id == "schedule_modal":
            values = payload.get("view", {}).get("state", {}).get("values", {})
            date_str = values.get("schedule_date_block", {}).get("schedule_date", {}).get("selected_date", "")
            time_str = values.get("schedule_time_block", {}).get("schedule_time", {}).get("selected_time", "")
            tz_str = values.get("schedule_tz_block", {}).get("schedule_tz", {}).get("value", "Europe/Lisbon").strip()

            # Validate timezone
            try:
                tz = pytz.timezone(tz_str)
            except pytz.exceptions.UnknownTimeZoneError:
                return jsonify({
                    "response_action": "errors",
                    "errors": {
                        "schedule_tz_block": f"'{tz_str}' is not a valid IANA timezone. Try something like 'Europe/Lisbon' or 'America/New_York'."
                    }
                }), 200

            # Parse the datetime in the chosen timezone and convert to UTC
            try:
                local_dt = tz.localize(datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M"))
                utc_dt = local_dt.astimezone(pytz.utc)
            except Exception as e:
                print(f"[ERROR] Failed to parse schedule datetime: {e}")
                return "", 200

            # Reject past datetimes
            if utc_dt <= datetime.now(pytz.utc):
                return jsonify({
                    "response_action": "errors",
                    "errors": {
                        "schedule_date_block": "The scheduled time must be in the future."
                    }
                }), 200

            # Persist timezone preference and schedule the send
            admin_session["timezone"] = tz_str
            schedule_messages(
                user_id=user_id,
                scheduled_dt_utc=utc_dt,
                client_type="bot",
                selected_ids=admin_session["selected_startup_ids"],
                message_template=admin_session["message_template"]
            )

            # Confirm to admin via DM
            try:
                bot_client.chat_postMessage(
                    channel=user_id,
                    text=(
                        f"✅ Scheduled! Your message will go out to "
                        f"*{startup_count_for_session()} founder(s)* on "
                        f"*{format_scheduled_time_local(utc_dt)}*."
                    )
                )
            except Exception as e:
                print(f"Could not send schedule confirmation: {e}")

            def refresh_home():
                try:
                    bot_client.views_publish(
                        user_id=user_id,
                        view=build_admin_home_view()
                    )
                except Exception as e:
                    print(f"Failed to refresh Home tab: {e}")

            threading.Thread(target=refresh_home).start()
            return "", 200

    # ── Block actions ──────────────────────────────────────────────────
    if payload_type == "block_actions":
        actions = payload.get("actions", [])
        if not actions:
            return "", 200

        action_id = actions[0]["action_id"]

        # ── Open message editor modal ───────────────────────────────────
        if action_id == "open_message_editor":
            try:
                bot_client.views_open(
                    trigger_id=payload["trigger_id"],
                    view=build_message_editor_modal()
                )
            except Exception as e:
                print(f"Failed to open modal: {e}")
            return "", 200

        # ── Open schedule modal ────────────────────────────────────────
        if action_id == "open_schedule_modal":
            try:
                bot_client.views_open(
                    trigger_id=payload["trigger_id"],
                    view=build_schedule_modal()
                )
            except Exception as e:
                print(f"Failed to open schedule modal: {e}")
            return "", 200

        # ── Startup selector ───────────────────────────────────────────
        if action_id == "startup_selector":
            selected = actions[0].get("selected_options", [])
            admin_session["selected_startup_ids"] = (
                {opt["value"] for opt in selected} if selected else set()
            )

        # ── Send Now button ────────────────────────────────────────────
        elif action_id == "send_messages_button":
            threading.Thread(
                target=process_messages,
                args=(
                    user_id,
                    "bot",
                    admin_session["selected_startup_ids"],
                    admin_session["message_template"]
                )
            ).start()

        # ── Cancel schedule button ─────────────────────────────────────
        elif action_id == "cancel_schedule_button":
            cancel_scheduled_send(notify=True)

        # ── Reset button ───────────────────────────────────────────────
        elif action_id == "reset_defaults_button":
            cancel_scheduled_send(notify=False)
            admin_session["selected_startup_ids"] = None
            admin_session["message_template"] = DEFAULT_MESSAGE_TEMPLATE

        # Refresh Home tab for all block actions except modal openers
        try:
            bot_client.views_publish(
                user_id=user_id,
                view=build_admin_home_view()
            )
        except Exception as e:
            print(f"Failed to refresh Home tab: {e}")

    return "", 200


def startup_count_for_session():
    """Return how many startups are currently selected."""
    selected_ids = admin_session["selected_startup_ids"]
    return len(startups) if selected_ids is None else len(selected_ids)

# =========================
# RUN APP
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)