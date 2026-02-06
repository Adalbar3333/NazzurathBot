from flask import Flask, request
import os

app = Flask(__name__)

@app.route("/github", methods=["POST"])
def github_webhook():
    data = request.json

    if "pusher" not in data:
        return "ignored", 200

    repo = data["repository"]["full_name"]
    pusher = data["pusher"]["name"]
    commits = data["commits"]

    messages = []
    for c in commits:
        messages.append(f"- **{c['message']}**")

    payload = {
        "repo": repo,
        "pusher": pusher,
        "messages": messages
    }

    # write to a file your discord bot can read
    with open("latest_push.json", "w") as f:
        import json
        json.dump(payload, f)

    return "ok", 200

app.run(port=5000)
