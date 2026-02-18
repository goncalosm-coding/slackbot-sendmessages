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

# Slack tokens
USER_TOKEN = os.environ.get("SLACK_TOKEN")  # user token
if not USER_TOKEN:
    raise ValueError("SLACK_TOKEN environment variable not set!")

BOT_TOKEN = os.environ.get("BOT_TOKEN")  # bot token
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set!")

user_client = WebClient(token=USER_TOKEN)
bot_client = WebClient(token=BOT_TOKEN)

# Admin Slack user ID (only this user can send messages)
ADMIN_USER_ID = "U0AFL5S3R0A"  # <--- replace with actual admin user ID

# Load CSV once
CSV_PATH = "workspace_users.csv"
if not os.path.exists(CSV_PATH):
    raise FileNotFoundError(f"{CSV_PATH} not found. Make sure your CSV is uploaded.")

startups = pd.read_csv(CSV_PATH)
required_columns = {"startup_name", "founder_name", "slack_user_id"}
missing_columns = required_columns - set(startups.columns)
if missing_columns:
    raise Exception(f"CSV is missing required columns: {missing_columns}")

MESSAGE_TEMPLATE = (
    "Olá {founder_name}, tenho acompanhado a {startup_name} "
    "e queria compartilhar algo com você!"
)

# =========================
# CORE MESSAGE PROCESSING
# =========================

def process_messages(user_id, client_type="user"):
    if user_id != ADMIN_USER_ID:
        print(f"❌ User {user_id} is not allowed to send messages.")
        return

    total_sent = 0
    client = user_client if client_type == "user" else bot_client
    source = "USER" if client_type == "user" else "BOT"

    print(f"[DEBUG] Sending messages as {source}")

    for _, row in startups.iterrows():
        slack_id = row.get("slack_user_id")
        if pd.isna(slack_id) or not slack_id:
            continue

        message = MESSAGE_TEMPLATE.format(
            founder_name=row.get("founder_name", "Founder"),
            startup_name=row.get("startup_name", "Startup")
        )

        try:
            client.chat_postMessage(channel=slack_id, text=message)
            total_sent += 1
            time.sleep(1)
        except Exception as e:
            print(f"❌ Failed to send to {slack_id}: {e}")

    # Notify admin when done
    try:
        client.chat_postMessage(
            channel=user_id,
            text=f"✅ Finished sending {total_sent} messages as {source}."
        )
    except Exception as e:
        print(f"❌ Could not notify {source}: {e}")

# =========================
# ROOT / HEALTH CHECK
# =========================

@app.route("/")
def home():
    return "✅ Slack bot is running! Use /sendmessages or open the Home tab."

# =========================
# SLASH COMMAND (user messages)
# =========================

@app.route("/sendmessages", methods=["POST"])
def send_messages():
    user_id = request.form.get("user_id")

    if user_id != ADMIN_USER_ID:
        return jsonify({
            "response_type": "ephemeral",
            "text": "❌ You are not allowed to use this command."
        }), 200

    response = {
        "response_type": "ephemeral",
        "text": "✅ Slack acknowledged! Sending messages now..."
    }

    # Send messages as the admin user
    thread = threading.Thread(target=process_messages, args=(user_id, "user"))
    thread.start()

    return jsonify(response), 200

# =========================
# SLACK EVENTS (HOME TAB + URL VERIFICATION)
# =========================

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json

    # URL verification
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    # Home tab opened
    if data.get("event", {}).get("type") == "app_home_opened":
        user_id = data["event"]["user"]

        try:
            if user_id == ADMIN_USER_ID:
                # Admin sees interactive button
                bot_client.views_publish(
                    user_id=user_id,
                    view={
                        "type": "home",
                        "blocks": [
                            {"type": "section", "text": {"type": "mrkdwn", "text": "*🚀 SendMessagesBot Dashboard*"}},
                            {"type": "section", "text": {"type": "mrkdwn", "text": "Click the button below to send messages to all startups as BOT."}},
                            {"type": "actions", "elements": [
                                {"type": "button", "text": {"type": "plain_text", "text": "🚀 Send Messages"}, "action_id": "send_messages_button"}
                            ]}
                        ]
                    }
                )
            else:
                # Regular users see info only
                bot_client.views_publish(
                    user_id=user_id,
                    view={
                        "type": "home",
                        "blocks": [
                            {"type": "section", "text": {"type": "mrkdwn", "text": "👋 Hi! Only the admin can send messages."}}
                        ]
                    }
                )
        except Exception as e:
            print(f"❌ Failed to publish Home tab: {e}")

    return "", 200

# =========================
# BUTTON INTERACTIONS (bot messages)
# =========================

@app.route("/slack/interactions", methods=["POST"])
def slack_interactions():
    payload = json.loads(request.form["payload"])
    action_id = payload["actions"][0]["action_id"]
    user_id = payload["user"]["id"]

    if action_id == "send_messages_button":
        if user_id != ADMIN_USER_ID:
            print(f"❌ User {user_id} tried to use the button without permission.")
            return "", 200

        thread = threading.Thread(target=process_messages, args=(user_id, "bot"))
        thread.start()

    return "", 200

# =========================
# RUN APP
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)