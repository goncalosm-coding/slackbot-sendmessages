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

admin_session = {
    "selected_startup_ids": None,
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
            text=f"Done! Sent {total_sent} messages as {source}."
        )
    except Exception as e:
        print(f"Could not notify admin: {e}")

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
                        "text": {"type": "plain_text", "text": "Send Messages", "emoji": False},
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
                        "text": "You can also trigger a send via `/sendmessages` from any channel."
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
        if payload["view"]["callback_id"] == "message_editor_modal":
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

    # ── Block actions ──────────────────────────────────────────────────
    if payload_type == "block_actions":
        actions = payload.get("actions", [])
        if not actions:
            return "", 200

        action_id = actions[0]["action_id"]

        # ── Open modal — return immediately ────────────────────────────
        if action_id == "open_message_editor":
            try:
                bot_client.views_open(
                    trigger_id=payload["trigger_id"],
                    view=build_message_editor_modal()
                )
            except Exception as e:
                print(f"Failed to open modal: {e}")
            return "", 200

        # ── Startup selector ───────────────────────────────────────────
        if action_id == "startup_selector":
            selected = actions[0].get("selected_options", [])
            admin_session["selected_startup_ids"] = (
                {opt["value"] for opt in selected} if selected else set()
            )

        # ── Send button ────────────────────────────────────────────────
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

        # ── Reset button ───────────────────────────────────────────────
        elif action_id == "reset_defaults_button":
            admin_session["selected_startup_ids"] = None
            admin_session["message_template"] = DEFAULT_MESSAGE_TEMPLATE

        # Refresh Home tab
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