from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time
import threading
import json
import datetime

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
    "scheduled_time": None,   # datetime object or None (None = send now)
    "schedule_mode": "now",   # "now" | "scheduled"
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

    try:
        client.chat_postMessage(
            channel=user_id,
            text=f"✅ Done! Sent {total_sent} messages as {source}."
        )
    except Exception as e:
        print(f"Could not notify admin: {e}")


def schedule_and_send(user_id, client_type, selected_ids, message_template, send_at: datetime.datetime):
    """Wait until send_at (UTC), then fire messages."""
    now = datetime.datetime.utcnow()
    delay = (send_at - now).total_seconds()
    if delay > 0:
        try:
            bot_client.chat_postMessage(
                channel=user_id,
                text=(
                    f"⏰ Got it! Your message to "
                    f"*{len(selected_ids) if selected_ids is not None else len(startups)} founder(s)* "
                    f"is scheduled for *{send_at.strftime('%Y-%m-%d %H:%M')} UTC*."
                )
            )
        except Exception:
            pass
        time.sleep(delay)
    process_messages(user_id, client_type, selected_ids, message_template)

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
            "text": "🚫 You are not allowed to use this command."
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
        "text": "🚀 Blasting messages now. Check your DMs for a confirmation when it's done."
    }), 200

# =========================
# HOME TAB VIEWS
# =========================

def _schedule_summary_text():
    """Returns a human-readable description of the current schedule setting."""
    mode = admin_session.get("schedule_mode", "now")
    if mode == "now":
        return "🟢  *Send immediately* — messages go out the moment you hit launch."
    dt = admin_session.get("scheduled_time")
    if dt:
        return f"🕐  *Scheduled for {dt.strftime('%b %d, %Y  ·  %H:%M')} UTC*"
    return "🟢  *Send immediately*"


def build_admin_home_view():
    startup_count = len(startups)
    selected_ids = admin_session["selected_startup_ids"]
    current_template = admin_session["message_template"]
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

    preview_text = current_template.format(founder_name="Maria", startup_name="Acme")

    return {
        "type": "home",
        "blocks": [

            # ── HERO ──────────────────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🦄  UnicornFactory Outreach",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Outreach that hits different.*\n"
                        "Pick your founders, write your message, choose your moment — then launch. 🚀"
                    )
                }
            },
            {"type": "divider"},

            # ── STATS BAND ────────────────────────────────────────────
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"📋  *{startup_count}*\nFounders in roster"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"✅  *{selected_count}*\nSelected"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"⏭️  *{skipped_count}*\nSkipped"
                    },
                    {
                        "type": "mrkdwn",
                        "text": "🔑  *Admin*\nFull access"
                    }
                ]
            },
            {"type": "divider"},

            # ── SECTION 1: RECIPIENTS ─────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "👥  Step 1 — Who's getting this?",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Uncheck anyone you want to skip this round. Everyone else is in. ✔️"
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

            # ── SECTION 2: MESSAGE ────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "✍️  Step 2 — Craft your message",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Current message:*\n```{current_template}```"
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️  Edit", "emoji": True},
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
                            f"👁️  *Live preview:*  {preview_text}\n"
                            "Use `{{founder_name}}` and `{{startup_name}}` as dynamic placeholders."
                        )
                    }
                ]
            },
            {"type": "divider"},

            # ── SECTION 3: SCHEDULE ───────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🗓️  Step 3 — Choose your moment",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _schedule_summary_text()
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⚡  Send now", "emoji": True},
                        "action_id": "schedule_now",
                        "style": "primary" if admin_session["schedule_mode"] == "now" else "default"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⏱️  In 1 hour", "emoji": True},
                        "action_id": "schedule_1h"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "⏱️  In 3 hours", "emoji": True},
                        "action_id": "schedule_3h"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🌅  Tomorrow 9 AM UTC", "emoji": True},
                        "action_id": "schedule_tomorrow"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "📅  Custom date & time", "emoji": True},
                        "action_id": "open_schedule_editor"
                    }
                ]
            },
            {"type": "divider"},

            # ── SECTION 4: LAUNCH ─────────────────────────────────────
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚀  Step 4 — Launch",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"You're about to reach *{selected_count} founder(s)*.\n"
                        f"{_schedule_summary_text()}\n"
                        "You'll get a DM the moment it's done. 📬"
                    )
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🚀  Launch outreach", "emoji": True},
                        "style": "primary",
                        "action_id": "send_messages_button",
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Ready to launch?", "emoji": False},
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"This sends your message to *{selected_count} founder(s)*.\n"
                                    + (
                                        "Messages go out *right now*. No take-backs. ⚡"
                                        if admin_session["schedule_mode"] == "now"
                                        else f"Scheduled for *{admin_session['scheduled_time'].strftime('%b %d, %Y · %H:%M')} UTC*. 🕐"
                                    )
                                )
                            },
                            "confirm": {"type": "plain_text", "text": "Let's go 🚀"},
                            "deny": {"type": "plain_text", "text": "Not yet"}
                        }
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "🔄  Reset everything", "emoji": True},
                        "action_id": "reset_defaults_button"
                    }
                ]
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡  You can also trigger an immediate send via `/sendmessages` from any channel."
                    }
                ]
            }
        ]
    }


def build_message_editor_modal():
    return {
        "type": "modal",
        "callback_id": "message_editor_modal",
        "title": {"type": "plain_text", "text": "✍️  Edit message", "emoji": True},
        "submit": {"type": "plain_text", "text": "💾  Save", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": False},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "✨  *Make it yours.*\n"
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
                        "text": "Write something worth reading... ✍️"
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


def build_schedule_editor_modal():
    """Modal for picking a custom date + time to schedule the send."""
    return {
        "type": "modal",
        "callback_id": "schedule_editor_modal",
        "title": {"type": "plain_text", "text": "📅  Schedule send", "emoji": True},
        "submit": {"type": "plain_text", "text": "✅  Schedule", "emoji": True},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": False},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🕐  *Pick an exact date and time (UTC).*\n"
                        "Your message will be held and sent automatically at that moment."
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
                    "placeholder": {"type": "plain_text", "text": "Pick a date"}
                },
                "label": {"type": "plain_text", "text": "📆  Date (UTC)", "emoji": True}
            },
            {
                "type": "input",
                "block_id": "schedule_time_block",
                "element": {
                    "type": "timepicker",
                    "action_id": "schedule_time",
                    "placeholder": {"type": "plain_text", "text": "Pick a time"}
                },
                "label": {"type": "plain_text", "text": "🕐  Time (UTC)", "emoji": True}
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
                    "text": "🦄  UnicornFactory Outreach",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Outreach that hits different.* 🚀"
                }
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "🔒  *This area is admin-only.*\n"
                        "You don't have access to the outreach dashboard.\n"
                        "If you think that's a mistake, ping your admin."
                    )
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "🦄  UnicornFactory Outreach Bot  ·  Admin access required"
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

        # Message editor saved
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

            threading.Thread(
                target=lambda: bot_client.views_publish(
                    user_id=user_id,
                    view=build_admin_home_view()
                )
            ).start()
            return "", 200

        # Schedule editor saved
        if callback_id == "schedule_editor_modal":
            values = payload.get("view", {}).get("state", {}).get("values", {})
            date_str = values.get("schedule_date_block", {}).get("schedule_date", {}).get("selected_date", "")
            time_str = values.get("schedule_time_block", {}).get("schedule_time", {}).get("selected_time", "")

            if date_str and time_str:
                try:
                    dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                    if dt > datetime.datetime.utcnow():
                        admin_session["scheduled_time"] = dt
                        admin_session["schedule_mode"] = "scheduled"
                        print(f"[DEBUG] Scheduled for: {dt}")
                    else:
                        # Past time — fall back to now and notify
                        admin_session["schedule_mode"] = "now"
                        admin_session["scheduled_time"] = None
                        try:
                            bot_client.chat_postMessage(
                                channel=user_id,
                                text="⚠️ That time is in the past — I've reset to *send immediately* mode."
                            )
                        except Exception:
                            pass
                except ValueError:
                    pass

            threading.Thread(
                target=lambda: bot_client.views_publish(
                    user_id=user_id,
                    view=build_admin_home_view()
                )
            ).start()
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
                print(f"Failed to open message modal: {e}")
            return "", 200

        # ── Open schedule editor modal ──────────────────────────────────
        if action_id == "open_schedule_editor":
            try:
                bot_client.views_open(
                    trigger_id=payload["trigger_id"],
                    view=build_schedule_editor_modal()
                )
            except Exception as e:
                print(f"Failed to open schedule modal: {e}")
            return "", 200

        # ── Quick schedule presets ──────────────────────────────────────
        if action_id == "schedule_now":
            admin_session["schedule_mode"] = "now"
            admin_session["scheduled_time"] = None

        elif action_id == "schedule_1h":
            admin_session["scheduled_time"] = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
            admin_session["schedule_mode"] = "scheduled"

        elif action_id == "schedule_3h":
            admin_session["scheduled_time"] = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
            admin_session["schedule_mode"] = "scheduled"

        elif action_id == "schedule_tomorrow":
            tomorrow = datetime.datetime.utcnow() + datetime.timedelta(days=1)
            admin_session["scheduled_time"] = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
            admin_session["schedule_mode"] = "scheduled"

        # ── Startup selector ────────────────────────────────────────────
        elif action_id == "startup_selector":
            selected = actions[0].get("selected_options", [])
            admin_session["selected_startup_ids"] = (
                {opt["value"] for opt in selected} if selected else set()
            )

        # ── Send / schedule button ──────────────────────────────────────
        elif action_id == "send_messages_button":
            selected_ids = admin_session["selected_startup_ids"]
            template = admin_session["message_template"]

            if admin_session["schedule_mode"] == "scheduled" and admin_session["scheduled_time"]:
                threading.Thread(
                    target=schedule_and_send,
                    args=(user_id, "bot", selected_ids, template, admin_session["scheduled_time"])
                ).start()
            else:
                threading.Thread(
                    target=process_messages,
                    args=(user_id, "bot", selected_ids, template)
                ).start()

        # ── Reset button ────────────────────────────────────────────────
        elif action_id == "reset_defaults_button":
            admin_session["selected_startup_ids"] = None
            admin_session["message_template"] = DEFAULT_MESSAGE_TEMPLATE
            admin_session["scheduled_time"] = None
            admin_session["schedule_mode"] = "now"

        # Refresh Home tab after any action (except modal opens which already returned)
        try:
            bot_client.views_publish(
                user_id=user_id,
                view=build_admin_home_view()
            )
        except Exception as e:
            print(f"Failed to refresh Home tab: {e}")

    return "", 200

# =========================
# RUN APP
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)