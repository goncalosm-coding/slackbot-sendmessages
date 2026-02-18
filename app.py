from flask import Flask, request, jsonify
from slack_sdk import WebClient
import pandas as pd
import os
import time


app = Flask(__name__)

SLACK_TOKEN = os.environ.get("SLACK_TOKEN")
client = WebClient(token=SLACK_TOKEN)

# Load CSV once at startup
CSV_PATH = "workspace_users.csv"
startups = pd.read_csv(CSV_PATH)

MESSAGE_TEMPLATE = "Olá {founder_name}, tenho acompanhado a {startup_name} e queria compartilhar algo com você!"

@app.route("/sendmessages", methods=["POST"])
def send_messages():
    data = request.form
    user_id = data.get("user_id")
    channel_id = data.get("channel_id")

    # Notify user
    client.chat_postMessage(channel=channel_id, text=f"✅ Enviando mensagens...")

    for _, row in startups.iterrows():
        slack_id = row['slack_user_id']
        if pd.isna(slack_id):
            continue

        message = MESSAGE_TEMPLATE.format(
            founder_name=row['founder_name'],
            startup_name=row['startup_name']
        )

        try:
            client.chat_postMessage(channel=slack_id, text=message)
            time.sleep(1)
        except Exception as e:
            print(f"❌ Failed to send to {row['founder_name']}: {e}")

    return jsonify({"text": "✅ Todas as mensagens foram enviadas!"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)