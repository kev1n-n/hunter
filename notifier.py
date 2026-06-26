import requests  # 匯入 requests，用來發送 Discord Webhook 請求

from datetime import datetime, timezone, timedelta  # 匯入 datetime 與時區工具

from config import DISCORD_WEBHOOK_URL  # 從 config.py 匯入 Discord Webhook URL


TAIWAN_TZ = timezone(timedelta(hours=8))  # 台灣時區 UTC+8


def is_valid_webhook() -> bool:  # 定義檢查 Discord Webhook 是否有效的函式
    if not DISCORD_WEBHOOK_URL:  # 如果 Webhook URL 是空的
        print("  [!] DISCORD_WEBHOOK_URL 尚未設定")  # 印出提醒
        return False  # 回傳 False，代表不能發送

    if "YOUR_WEBHOOK_ID" in DISCORD_WEBHOOK_URL or "YOUR_TOKEN" in DISCORD_WEBHOOK_URL:  # 如果還是範例值
        print("  [!] Discord Webhook 還是範例值，請去 config.py 換成真的 URL")  # 印出提醒
        return False  # 回傳 False，代表不能發送

    return True  # Webhook 看起來正常，可以發送


def send_discord_payload(payload: dict):  # 定義送出 Discord payload 的共用函式
    if not is_valid_webhook():  # 如果 Webhook 不可用
        return  # 直接結束，不送 Discord

    try:  # 嘗試發送 Discord
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)  # 用 POST 把資料送到 Discord

        if response.status_code >= 300:  # 如果 Discord 回傳錯誤狀態碼
            print(f"  [✗] Discord 失敗：{response.status_code} — {response.text}")  # 印出錯誤內容
        else:  # 如果 Discord 回傳成功
            print("  [✓] Discord 已發送")  # 印出成功訊息

    except Exception as e:  # 如果發送過程出錯
        print(f"  [✗] Discord 發送錯誤：{e}")  # 印出錯誤原因


def send_restock_alert(product: dict):  # 定義有貨通知函式
    name = product.get("name", "未知商品")  # 取得商品名稱，如果沒有就用未知商品
    url = product.get("url", "")  # 取得商品連結，如果沒有就用空字串
    price = product.get("price", "未取得價格")  # 取得商品價格，如果沒有就用未取得價格
    status_label = product.get("status_label", "✅ 有貨 / 可加入購物車")  # 取得狀態文字，沒有就用有貨
    now_text = datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d %H:%M:%S")  # 取得目前台灣時間文字

    fields = [  # 建立 Discord embed 欄位
        {  # 第一個欄位開始
            "name": "📦 狀態",  # 欄位名稱
            "value": status_label,  # 欄位內容
            "inline": True,  # 讓欄位可以並排顯示
        },  # 第一個欄位結束
        {  # 第二個欄位開始
            "name": "💰 價格",  # 欄位名稱
            "value": price if price else "未取得價格",  # 欄位內容
            "inline": True,  # 讓欄位可以並排顯示
        },  # 第二個欄位結束
        {  # 第三個欄位開始
            "name": "🔗 商品連結",  # 欄位名稱
            "value": f"[點我打開商品頁]({url})" if url else "未取得連結",  # Discord 支援 markdown 連結
            "inline": False,  # 商品連結單獨一行
        },  # 第三個欄位結束
        {  # 第四個欄位開始
            "name": "🕒 偵測時間",  # 欄位名稱
            "value": now_text,  # 欄位內容
            "inline": False,  # 時間單獨一行
        },  # 第四個欄位結束
    ]  # Discord embed 欄位結束

    embed = {  # 建立 Discord embed
        "title": f"🔥 偵測到現貨：{name}",  # Discord 通知標題，會顯示商品名稱
        "description": "誠品列表偵測到這個商品可以加入購物車。",  # Discord 通知說明
        "url": url if url else None,  # 讓標題本身也可以點進商品頁
        "color": 5763719,  # Discord embed 顏色，綠色系
        "fields": fields,  # 放入欄位資料
        "footer": {"text": "eslite-hunter"},  # Discord footer
        "timestamp": datetime.now(TAIWAN_TZ).isoformat(),  # Discord 時間戳，使用台灣時間
    }  # embed 結束

    if not url:  # 如果沒有商品連結
        embed.pop("url", None)  # 移除空的 url 欄位，避免 Discord 不接受

    payload = {  # 建立 Discord Webhook payload
        "content": "🔥 有現貨！",  # Discord 訊息正文
        "embeds": [embed],  # 放入 embed
    }  # payload 結束

    send_discord_payload(payload)  # 發送 Discord 通知


def send_preorder_alert(product: dict):  # 定義預購通知函式，保留給舊版 main.py 使用
    name = product.get("name", "未知商品")  # 取得商品名稱
    url = product.get("url", "")  # 取得商品連結
    price = product.get("price", "未取得價格")  # 取得商品價格
    now_text = datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d %H:%M:%S")  # 取得目前台灣時間文字

    embed = {  # 建立 Discord embed
        "title": f"📌 偵測到預購：{name}",  # Discord 通知標題
        "description": "誠品列表偵測到這個商品目前是預購狀態。",  # Discord 通知說明
        "url": url if url else None,  # 讓標題可以點商品頁
        "color": 16753920,  # Discord embed 顏色，橘色系
        "fields": [  # 欄位開始
            {  # 價格欄位
                "name": "💰 價格",  # 欄位名稱
                "value": price if price else "未取得價格",  # 欄位內容
                "inline": True,  # 並排顯示
            },  # 價格欄位結束
            {  # 連結欄位
                "name": "🔗 商品連結",  # 欄位名稱
                "value": f"[點我打開商品頁]({url})" if url else "未取得連結",  # 商品連結
                "inline": False,  # 單獨一行
            },  # 連結欄位結束
            {  # 時間欄位
                "name": "🕒 偵測時間",  # 欄位名稱
                "value": now_text,  # 欄位內容
                "inline": False,  # 單獨一行
            },  # 時間欄位結束
        ],  # 欄位結束
        "footer": {"text": "eslite-hunter"},  # Discord footer
        "timestamp": datetime.now(TAIWAN_TZ).isoformat(),  # Discord 時間戳，使用台灣時間
    }  # embed 結束

    if not url:  # 如果沒有商品連結
        embed.pop("url", None)  # 移除空 url

    payload = {  # 建立 Discord payload
        "content": "📌 有預購商品",  # Discord 訊息正文
        "embeds": [embed],  # 放入 embed
    }  # payload 結束

    send_discord_payload(payload)  # 發送 Discord 通知