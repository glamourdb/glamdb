#!/usr/bin/env python3
"""Explicit operator-only Telegram setup calls with token-safe error output."""

import argparse
import getpass
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from transport import NoRedirect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("getme", "chats", "send-test"))
    parser.add_argument("--secrets-dir", type=Path, default=Path("artifacts/alerting/secrets"))
    args = parser.parse_args()
    token_path = args.secrets_dir / "telegram_bot_token"
    token = token_path.read_text().strip() if token_path.is_file() else getpass.getpass("Bot token (hidden): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        parser.exit(1, "invalid token format\n")
    method, payload = "getMe", {}
    if args.command == "chats":
        method, payload = "getUpdates", {"limit": 20, "timeout": 0,
                                         "allowed_updates": ["message", "channel_post", "my_chat_member"]}
    if args.command == "send-test":
        method = "sendMessage"
        payload = {"chat_id": int((args.secrets_dir / "telegram_chat_id").read_text().strip()),
                   "text": "ALARM GlamDB notification setup test. This is a manual delivery test."}
    try:
        request = Request("https://api.telegram.org/bot" + token + "/" + method,
                          data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with build_opener(NoRedirect).open(request, timeout=10) as response:
            body = json.loads(response.read(65536))
        if not body.get("ok"):
            parser.exit(1, "Telegram rejected the operation; verify bot permissions and configuration\n")
    except HTTPError as error:
        code = error.code
        error.close()
        parser.exit(1, f"Telegram returned HTTP {code}; verify token, membership, permissions and webhook settings\n")
    except (OSError, ValueError):
        parser.exit(1, "Telegram request failed; details containing the token were suppressed\n")
    if args.command == "getme":
        print("Bot username:", body["result"].get("username", "unknown"))
    elif args.command == "chats":
        found = {}
        for update in body["result"]:
            for kind in ("message", "channel_post", "my_chat_member"):
                chat = update.get(kind, {}).get("chat", {})
                if "id" in chat:
                    found[chat["id"]] = chat.get("type", "unknown")
        for chat, kind in sorted(found.items()):
            print(f"Chat ID: {chat}; type: {kind}")
        if not found:
            print("No chat update found. Send /start or a bot-addressed group command, then retry.")
    else:
        print("Manual test accepted by Telegram.")


if __name__ == "__main__":
    main()
