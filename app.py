from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time
import threading

app = Flask(__name__)

# Load Slack token from environment
SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
if not SLACK_TOKEN:
    raise ValueError("SLACK_TOKEN environment variable not set!")

client = WebClient(token=SLACK_TOKEN)

# Load CSV once at startup
CSV_PATH = "workspace_users.csv"
if not os.path.exists(CSV_PATH):
    raise FileNotFoundError(f"{CSV_PATH} not found. Make sure your CSV is uploaded.")

try:
    startups = pd.read_csv(CSV_PATH)
except Exception as e:
    raise Exception(f"Failed to read CSV: {e}")

# Ensure required columns exist
required_columns = {"startup_name", "founder_name", "slack_user_id"}
missing_columns = required_columns - set(startups.columns)
if missing_columns:
    raise Exception(f"CSV is missing required columns: {missing_columns}")

MESSAGE_TEMPLATE = (
    "Olá {founder_name}, tenho acompanhado a {startup_name} "
    "e queria compartilhar algo com você!"
)

@app.route("/")
def home():
    return "✅ Slack bot is running! Use /sendmessages in Slack."

def send_messages_background():
    """Send messages to all founders in the CSV, one by one."""
    for _, row in startups.iterrows():
        slack_id = row.get("slack_user_id")
        if pd.isna(slack_id) or not slack_id:
            print(f"⚠️ Missing Slack ID for {row.get('founder_name', 'Unknown')}")
            continue

        message = MESSAGE_TEMPLATE.format(
            founder_name=row.get("founder_name", "Founder"),
            startup_name=row.get("startup_name", "Startup")
        )

        try:
            client.chat_postMessage(channel=slack_id, text=message)
            print(f"✅ Message sent to {row.get('founder_name', 'Unknown')}")
            time.sleep(1)  # avoid Slack rate limits
        except Exception as e:
            print(f"❌ Failed to send to {row.get('founder_name', 'Unknown')}: {e}")

@app.route("/sendmessages", methods=["POST"])
def send_messages():
    """
    Triggered by the Slack slash command.
    Responds immediately to avoid timeout, then sends messages in the background.
    """
    channel_id = request.form.get("channel_id")

    # Respond immediately to Slack
    if channel_id:
        try:
            client.chat_postMessage(
                channel=channel_id, text="✅ Mensagens estão sendo enviadas em segundo plano..."
            )
        except Exception as e:
            print(f"❌ Failed to notify user: {e}")

    # Start background thread to send messages
    threading.Thread(target=send_messages_background, daemon=True).start()

    # Immediate response to Slack to prevent operation_timeout
    return jsonify({"text": "✅ Slack acknowledged! Messages are being sent..."})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)