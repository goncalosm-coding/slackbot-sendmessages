from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time
import requests

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

@app.route("/sendmessages", methods=["POST"])
def send_messages():
    """
    Slack slash command handler.
    Uses response_url to avoid timeout.
    """

    response_url = request.form.get("response_url")

    # 1️⃣ Immediate response (Slack requires <3s)
    immediate_response = {
        "response_type": "ephemeral",
        "text": "✅ Slack acknowledged! Sending messages now..."
    }

    # Send immediate acknowledgement
    requests.post(response_url, json=immediate_response)

    total_sent = 0

    # 2️⃣ Process CSV synchronously
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
            time.sleep(1)  # Slack rate limit
        except Exception as e:
            print(f"❌ Failed to send to {slack_id}: {e}")

    # 3️⃣ Final completion message
    final_response = {
        "response_type": "ephemeral",
        "text": f"✅ Finished sending {total_sent} messages."
    }

    requests.post(response_url, json=final_response)

    return "", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)