"""
強制發送 Telegram 測試訊息
==========================

用途:不用等實際符合五個條件,直接測試 Telegram Bot/頻道有沒有設定正確、
訊息送不送得到。跟 fetch_and_alert.py 用同一組環境變數:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID(可用逗號分隔多個 Chat ID / @頻道username,同時發給多人)

本機測試時,你可以用:
    export TELEGRAM_BOT_TOKEN="123456:xxxx"
    export TELEGRAM_CHAT_ID="你的ChatID,朋友的ChatID"
    python send_test_message.py

也可以在 GitHub Actions 的 workflow_dispatch 手動觸發執行。
"""

import os
from datetime import datetime, timezone, timedelta

import requests

TAIPEI_TZ = timezone(timedelta(hours=8))


def send_telegram_message(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id_raw:
        print("[錯誤] 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID,無法測試。")
        return

    chat_ids = [c.strip() for c in chat_id_raw.split(",") if c.strip()]
    if not chat_ids:
        print("[錯誤] TELEGRAM_CHAT_ID 格式不正確(沒有任何有效的 Chat ID)。")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        body = {"chat_id": chat_id, "text": text}
        r = requests.post(url, json=body, timeout=15)
        if r.status_code != 200:
            print(f"[失敗] chat_id={chat_id} → {r.status_code} {r.text}")
        else:
            print(f"[成功] chat_id={chat_id} 已送達")


def main():
    now_taipei = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    message = (
        "🔔 這是一則測試訊息\n"
        f"發送時間:{now_taipei} UTC+8\n"
        "如果你收到這則訊息,代表 Bot Token / Chat ID(或頻道)設定正確,"
        "之後正式的條件符合快訊都會用同一個管道送達。"
    )
    print("準備發送測試訊息...")
    print(message)
    print("---")
    send_telegram_message(message)


if __name__ == "__main__":
    main()
