from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time
import queue
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

# Thread-safe queue for messages
message_queue = queue.Queue()

def worker():
    """Worker thread to send Slack messages one by one."""
    while True:
        slack_id, message = message_queue.get()
        try:
            client.chat_postMessage(channel=slack_id, text=message)
            print(f"✅ Message sent to {slack_id}")
        except Exception as e:
            print(f"❌ Failed to send to {slack_id}: {e}")
        time.sleep(1)  # Slack rate limit
        message_queue.task_done()

# Start workers once per Gunicorn worker
@app.before_first_request
def start_workers():
    for _ in range(3):  # number of threads
        t = threading.Thread(target=worker, daemon=True)
        t.start()
    print("🟢 Slack message worker threads started!")

@app.route("/")
def home():
    return "✅ Slack bot is running! Use /sendmessages in Slack."

@app.route("/sendmessages", methods=["POST"])
def send_messages():
    """
    Triggered by Slack slash command.
    Queues messages in the background.
    """
    channel_id = request.form.get("channel_id")

    # Immediate response to Slack
    if channel_id:
        try:
            client.chat_postMessage(
                channel=channel_id,
                text="✅ Slack acknowledged! Messages are being queued in the background..."
            )
        except Exception as e:
            print(f"❌ Failed to notify Slack user: {e}")

    # Queue messages for all founders
    for _, row in startups.iterrows():
        slack_id = row.get("slack_user_id")
        if pd.isna(slack_id) or not slack_id:
            print(f"⚠️ Missing Slack ID for {row.get('founder_name', 'Unknown')}")
            continue

        message = MESSAGE_TEMPLATE.format(
            founder_name=row.get("founder_name", "Founder"),
            startup_name=row.get("startup_name", "Startup")
        )
        message_queue.put((slack_id, message))

    return jsonify({"text": "✅ Messages are queued and being sent in the background!"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)