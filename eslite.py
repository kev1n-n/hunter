"""
eslite.py — 誠品庫存偵測（Playwright 版）

使用真正的 Chromium 瀏覽器，可繞過誠品的反爬蟲保護。

偵測邏輯：
  有貨  → 頁面出現「加入購物車」或「立即購買」
  無貨  → 頁面出現「貨到通知」或「補貨通知」
  預購  → 頁面出現「預購」「預計」
"""

from playwright.sync_api import sync_playwright


def check_product(url: str) -> dict | None:
    """
    用 Playwright 開啟誠品商品頁，回傳庫存資訊。
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="zh-TW",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            # 等到網路幾乎閒置（JS 渲染完成）
            page.goto(url, wait_until="networkidle", timeout=30000)

            # 額外等 1 秒讓動態內容載入
            page.wait_for_timeout(1000)

            html    = page.content()
            title   = page.title()
            og_img  = page.evaluate(
                "document.querySelector('meta[property=\"og:image\"]')?.content || ''"
            )

            browser.close()

    except Exception as e:
        print(f"  [✗] Playwright 錯誤：{url}\n      {e}")
        return None

    # ── 商品名稱 ─────────────────────────────────
    name = title.replace(" | 誠品線上", "").strip() or url

    # ── 價格（找頁面上純數字 100~9999 範圍） ────
    import re
    prices = re.findall(r'(?<!\d)(\d{3,4})(?!\d)', html)
    price = f"NT${prices[0]}" if prices else "未知"

    # ── 庫存狀態（關鍵字比對） ───────────────────
    # 注意：順序很重要，無貨優先
    out_kw    = ["貨到通知", "補貨通知", "暫時缺貨"]
    pre_kw    = ["預購", "開始預購", "尚未開賣", "預計出貨"]
    stock_kw  = ["加入購物車", "立即購買", "直接結帳"]

    status = "unknown"

    for kw in out_kw:
        if kw in html:
            status = "out_of_stock"
            break

    if status == "unknown":
        for kw in pre_kw:
            if kw in html:
                status = "preorder"
                break

    if status == "unknown":
        for kw in stock_kw:
            if kw in html:
                status = "in_stock"
                break

    label_map = {
        "in_stock":     "✅ 有貨！",
        "out_of_stock": "❌ 無貨",
        "preorder":     "📌 預購中",
        "unknown":      "❓ 無法判斷",
    }

    return {
        "url":          url,
        "name":         name,
        "price":        price,
        "image":        og_img,
        "status":       status,
        "status_label": label_map[status],
    }
