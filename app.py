from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time
import threading
import json

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

# In-memory session state per admin interaction
# Stores: selected startup IDs and custom message template
admin_session = {
    "selected_startup_ids": None,   # None = all selected
    "message_template": DEFAULT_MESSAGE_TEMPLATE
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

    print(f"[DEBUG] Sending messages as {source}")

    for _, row in startups.iterrows():
        slack_id = row.get("slack_user_id")
        if pd.isna(slack_id) or not slack_id:
            continue

        # Filter by selected startups if provided
        if selected_ids is not None and str(slack_id) not in selected_ids:
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
            text=f"Done! Sent {total_sent} messages as {source}."
        )
    except Exception as e:
        print(f"Could not notify admin: {e}")

# =========================
# ROOT / HEALTH CHECK
# =========================

@app.route("/")
def home():
    return "Slack bot is running."

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
        args=(user_id, "user", admin_session["selected_startup_ids"], admin_session["message_template"])
    )
    thread.start()

    return jsonify({
        "response_type": "ephemeral",
        "text": "Sending messages now. You'll receive a DM when it's done."
    }), 200

# =========================
# HOME TAB VIEWS
# =========================

def build_admin_home_view():
    startup_count = len(startups)
    selected_ids = admin_session["selected_startup_ids"]
    current_template = admin_session["message_template"]
    selected_count = startup_count if selected_ids is None else len(selected_ids)

    # Build multi-select options from CSV
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
        # Pre-select all by default, or restore saved selection
        if selected_ids is None or slack_id in selected_ids:
            initial_options.append(option)

    return {
        "type": "home",
        "blocks": [

            # ── HERO BAND ──────────────────────────────────────────────
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*SendMessagesBot*\n"
                        "Outreach dashboard — send personalized messages to your founders at scale."
                    )
                }
            },
            {"type": "divider"},

            # ── LIVE STATS ─────────────────────────────────────────────
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Total founders*\n{startup_count}"},
                    {"type": "mrkdwn", "text": f"*Currently selected*\n{selected_count}"},
                    {"type": "mrkdwn", "text": "*Access level*\nAdmin"},
                    {"type": "mrkdwn", "text": f"*Trigger*\nHome tab  ·  `/sendmessages`"}
                ]
            },
            {"type": "divider"},

            # ── RECIPIENT SELECTOR ─────────────────────────────────────
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Recipients*\nChoose which founders will receive the message. Deselect any you want to skip."}
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

            # ── MESSAGE EDITOR ─────────────────────────────────────────
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Message template*\n"
                        "Edit the message below. Use `{founder_name}` and `{startup_name}` as dynamic placeholders."
                    )
                }
            },
            {
                "type": "input",
                "dispatch_action": True,
                "block_id": "message_editor_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "message_editor",
                    "multiline": True,
                    "initial_value": current_template,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Write your message here..."
                    }
                },
                "label": {"type": "plain_text", "text": "Message body", "emoji": False},
                "hint": {
                    "type": "plain_text",
                    "text": "Changes are saved automatically as you type and confirmed when you hit Enter or click outside the field.",
                    "emoji": False
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*Preview:*  "
                            + current_template.format(founder_name="Maria", startup_name="Acme")
                        )
                    }
                ]
            },
            {"type": "divider"},

            # ── SEND BUTTON ────────────────────────────────────────────
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Ready to send?*\nThis will dispatch your message to *{selected_count} founder(s)*. You'll get a DM confirmation when it completes."
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Send Messages", "emoji": False},
                        "style": "primary",
                        "action_id": "send_messages_button",
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Are you sure?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": f"This will send a message to *{selected_count} founder(s)*. This cannot be undone."
                            },
                            "confirm": {"type": "plain_text", "text": "Yes, send"},
                            "deny": {"type": "plain_text", "text": "Cancel"}
                        }
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reset to defaults", "emoji": False},
                        "action_id": "reset_defaults_button"
                    }
                ]
            }
        ]
    }


def build_guest_home_view():
    return {
        "type": "home",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*SendMessagesBot*\nPersonalized outreach, delivered to every founder in your workspace."
                }
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Access restricted*\nThis dashboard is available to workspace admins only.\nIf you believe you should have access, reach out to your admin."
                }
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": "SendMessagesBot  ·  Admin-only outreach tool"}
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
    user_id = payload["user"]["id"]

    if user_id != ADMIN_USER_ID:
        return "", 200

    # Handle block actions
    for action in payload.get("actions", []):
        action_id = action["action_id"]

        # ── Startup multi-select changed ───────────────────────────────
        if action_id == "startup_selector":
            selected = action.get("selected_options", [])
            admin_session["selected_startup_ids"] = (
                {opt["value"] for opt in selected} if selected else set()
            )

        # ── Message editor updated ─────────────────────────────────────
        elif action_id == "message_editor":
            new_text = action.get("value", "").strip()
            if new_text:
                admin_session["message_template"] = new_text

        # ── Send button ────────────────────────────────────────────────
        elif action_id == "send_messages_button":
            thread = threading.Thread(
                target=process_messages,
                args=(
                    user_id,
                    "bot",
                    admin_session["selected_startup_ids"],
                    admin_session["message_template"]
                )
            )
            thread.start()

        # ── Reset button ───────────────────────────────────────────────
        elif action_id == "reset_defaults_button":
            admin_session["selected_startup_ids"] = None
            admin_session["message_template"] = DEFAULT_MESSAGE_TEMPLATE

    # Refresh the Home tab after any interaction
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