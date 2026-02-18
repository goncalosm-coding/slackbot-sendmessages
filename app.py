from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time

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
    Triggered by the Slack slash command.
    Sends messages to all founders listed in the CSV.
    """
    # Immediately acknowledge Slack to avoid timeout
    channel_id = request.form.get("channel_id")
    if channel_id:
        try:
            client.chat_postMessage(
                channel=channel_id, text="✅ Mensagens estão sendo enviadas em segundo plano..."
            )
        except Exception as e:
            print(f"❌ Failed to notify user: {e}")

    results = []

    # Iterate founders and send messages
    for _, row in startups.iterrows():
        slack_id = row.get("slack_user_id")
        if pd.isna(slack_id) or not slack_id:
            results.append(f"⚠️ Missing Slack ID for {row.get('founder_name', 'Unknown')}")
            continue

        message = MESSAGE_TEMPLATE.format(
            founder_name=row.get("founder_name", "Founders"),
            startup_name=row.get("startup_name", "Startup")
        )

        try:
            client.chat_postMessage(channel=slack_id, text=message)
            results.append(f"✅ Message sent to {row.get('founder_name', 'Unknown')}")
            time.sleep(1)  # avoid Slack rate limits
        except Exception as e:
            error_msg = f"❌ Failed to send to {row.get('founder_name', 'Unknown')}: {e}"
            print(error_msg)
            results.append(error_msg)

    return jsonify({"text": "✅ Todas as mensagens processadas!", "results": results})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)