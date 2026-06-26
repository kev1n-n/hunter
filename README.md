# 🌀 陀螺獵人 — 誠品專版

自動監控誠品線上指定商品庫存，一有貨就推播到 Discord。

---

## 📁 檔案結構

```
eslite-hunter/
├── main.py        ← 執行這個
├── eslite.py      ← 誠品爬蟲（偵測有貨/無貨/預購）
├── notifier.py    ← Discord 推播
├── config.py      ← ⚙️ 設定（Webhook + 商品清單）
├── requirements.txt
└── data/
    └── seen.json  ← 自動產生，記錄每個商品的上次狀態
```

---

## 🚀 安裝與啟動

```bash
# 1. 安裝套件
pip install -r requirements.txt

# 2. 設定 Webhook（打開 config.py）
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/你的..."

# 3. 加入要監控的商品網址（config.py 裡 WATCH_URLS）

# 4. 啟動
python main.py

# 或只跑一次測試
python main.py --once
```

---

## 📌 怎麼抓商品網址？

1. 在誠品線上搜尋「戰鬥陀螺」或「beyblade」
2. 點進你想監控的商品頁
3. 複製網址，貼到 config.py 的 `WATCH_URLS` 清單

---

## 🔔 Discord 會收到什麼？

**有貨時：**
```
@everyone 🌀 誠品有貨！快去搶！

🔴 補貨！BEYBLADE X戰鬥陀螺 CX-01 蒼龍勇氣
💰 售價：NT$495   📦 狀態：✅ 有貨！   🏪 來源：誠品線上
```

**預購開始時：**
```
@everyone 📌 誠品開放預購！

📌 預購開始！BEYBLADE X戰鬥陀螺 BX-07 ...
```

---

## ⚙️ 推播邏輯

| 狀態變化 | 動作 |
|---------|------|
| 無貨 → **有貨** | 🔴 緊急推播 |
| 無貨 → **預購** | 📌 預購通知 |
| 有貨 → 無貨 | 不推播（賣完了） |
| 狀態不變 | 不推播（安靜） |

---

## ☁️ 24 小時不停運作

**免費方案推薦：Railway**
1. 去 https://railway.app 建立帳號
2. New Project → Deploy from GitHub（或直接上傳）
3. 環境變數加入 `DISCORD_WEBHOOK_URL`
4. 啟動指令：`python main.py`

---

## ⚠️ 注意

- 掃描間隔建議 **30~120 秒**，太頻繁可能被誠品封 IP
- `data/seen.json` 刪掉可重置狀態（下次會重新通知）
- 誠品若改版，`eslite.py` 裡的關鍵字可能需要更新
