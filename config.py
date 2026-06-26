import os

# 🔔 Discord Webhook URL
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ⏱ 掃描間隔
CHECK_INTERVAL = 60

# 🗃 本地紀錄
SEEN_DB = "data/seen.json"