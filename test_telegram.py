#!/usr/bin/env python3
"""Test QR Code Telegram"""
import requests

BOT_TOKEN = "8427446550:AAEuaHC4Bqz2n5ZW3wIocpLcETVpklMcZbg"
CHAT_ID = "-4993705211"

# Test exportChatInviteLink
print("=== Test exportChatInviteLink ===")
url = f"https://api.telegram.org/bot{BOT_TOKEN}/exportChatInviteLink"
r = requests.post(url, json={"chat_id": CHAT_ID})
print(r.text)

# Test createChatInviteLink
print("\n=== Test createChatInviteLink ===")
url = f"https://api.telegram.org/bot{BOT_TOKEN}/createChatInviteLink"
r = requests.post(url, json={"chat_id": CHAT_ID})
print(r.text)

# Test getChat
print("\n=== Test getChat ===")
url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChat"
r = requests.post(url, json={"chat_id": CHAT_ID})
print(r.text)
