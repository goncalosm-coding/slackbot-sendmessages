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


def process_messages(user_id):
    """
    Runs in background thread.
    Sends DMs safely and updates the command user.
    """
    total_sent = 0

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
            time.sleep(1)  # rate limit safety
        except Exception as e:
            print(f"❌ Failed to send to {slack_id}: {e}")

    # Notify command executor when done
    try:
        client.chat_postMessage(
            channel=user_id,
            text=f"✅ Finished sending {total_sent} messages."
        )
    except Exception as e:
        print(f"❌ Could not notify admin: {e}")


@app.route("/sendmessages", methods=["POST"])
def send_messages():
    """
    Slack slash command handler.
    MUST respond within 3 seconds.
    """

    user_id = request.form.get("user_id")

    # Immediately respond to Slack (prevents timeout)
    response = {
        "response_type": "ephemeral",
        "text": "✅ Slack acknowledged! Sending messages now..."
    }

    # Start background thread
    thread = threading.Thread(target=process_messages, args=(user_id,))
    thread.start()

    return jsonify(response), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)