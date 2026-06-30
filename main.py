import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
import schedule
from playwright.sync_api import sync_playwright

from config import CHECK_INTERVAL, DISCORD_WEBHOOK_URL
from notifier import send_restock_alert


TAIWAN_TZ = timezone(timedelta(hours=8))

KEYWORD = "BEYBLADE X戰鬥陀螺"

HEADLESS = True

PAGE_SIZE = 20

LABEL_MAP = {
    "in_stock": "✅ 有貨 / 可加入購物車",
    "out_of_stock": "❌ 目前無庫存",
    "preorder": "📌 預購中",
    "unknown": "❓ 狀態未知",
}


def now_taiwan_text() -> str:
    return datetime.now(TAIWAN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def build_search_url(start: int = 0) -> str:
    keyword = quote(KEYWORD)

    return (
        f"https://www.eslite.com/Search?"
        f"keyword={keyword}"
        f"&final_price=0,"
        f"&publishDate=0"
        f"&sort=_weight_+desc"
        f"&size={PAGE_SIZE}"
        f"&display=list"
        f"&start={start}"
        f"&exp=b"
    )


def normalize_text(text: str) -> str:
    return (
        text.lower()
        .replace(" ", "")
        .replace("　", "")
        .replace("\n", "")
        .replace("\t", "")
        .strip()
    )


def clean_eslite_name(name: str) -> str:
    remove_words = [
        "加入購物車",
        "直接購買",
        "立即購買",
        "放入購物車",
        "售完補貨中",
        "暫無供貨",
        "已售完",
        "搶購一空",
        "貨到通知",
    ]

    for word in remove_words:
        name = name.replace(word, "").strip()

    name = re.sub(r"\$\s*[\d,]+", "", name).strip()
    name = re.sub(r"NT\$\s*[\d,]+", "", name).strip()
    name = re.sub(r"\s+", " ", name).strip()

    return name


def is_beyblade(product: dict) -> bool:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    text_lower = text.lower()
    compact_text = normalize_text(text)
    name_compact = normalize_text(name)

    has_beyblade_word = (
        "beyblade" in text_lower
        or "戰鬥陀螺" in text
        or "战斗陀螺" in text
        or "ベイブレード" in text
    )

    has_product_code = bool(
        re.search(r"\b(?:BX|UX|CX|BXG|BXH)-\d+", text, re.IGNORECASE)
    )

    target_words = [
        "發射器",
        "啟動器",
        "握把",
        "改造組",
        "對戰組",
        "隨機強化組",
        "強化組",
        "限定版",
        "附陀螺",
        "附附錄組",
        "陀螺",
    ]

    has_target_word = any(word in text for word in target_words)

    exclude_keywords = [
        "電子書",
        "ebook",
        "e-book",
        "漫畫",
        "雜誌",
        "貼紙",
        "卡牌",
        "紙製",
        "戰鬥陀螺x 1/2",
        "戰鬥陀螺x(1)",
        "戰鬥陀螺x(2)",
        "戰鬥陀螺x 1",
        "戰鬥陀螺x 2",
        "vol. 3",
    ]

    compact_exclude_keywords = [
        normalize_text(keyword)
        for keyword in exclude_keywords
    ]

    if any(keyword in name_compact for keyword in compact_exclude_keywords):
        product["excluded_reason"] = "排除關鍵字"
        return False

    if any(keyword in compact_text for keyword in compact_exclude_keywords):
        product["excluded_reason"] = "排除關鍵字"
        return False

    return has_beyblade_word and (has_product_code or has_target_word)


def detect_status_from_text(text: str) -> str:
    compact_text = normalize_text(text)

    out_of_stock_keywords = [
        "已售完",
        "售完補貨中",
        "搶購一空",
        "補貨中",
        "缺貨",
        "暫無供貨",
        "貨到通知",
        "無庫存",
        "已下架",
    ]

    preorder_keywords = [
        "預購",
        "預定",
    ]

    buy_keywords = [
        "加入購物車",
        "放入購物車",
        "直接購買",
        "立即購買",
    ]

    compact_out_keywords = [
        normalize_text(keyword)
        for keyword in out_of_stock_keywords
    ]

    compact_preorder_keywords = [
        normalize_text(keyword)
        for keyword in preorder_keywords
    ]

    compact_buy_keywords = [
        normalize_text(keyword)
        for keyword in buy_keywords
    ]

    if any(keyword in compact_text for keyword in compact_out_keywords):
        return "out_of_stock"

    if any(keyword in compact_text for keyword in compact_preorder_keywords):
        return "preorder"

    if any(keyword in compact_text for keyword in compact_buy_keywords):
        return "in_stock"

    return "unknown"


def fetch_page_products(page, start: int) -> tuple[list, int | None]:
    url = build_search_url(start)

    print(f"正在打開誠品搜尋頁 start={start}...")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    try:
        page.wait_for_function(
            """
            () => {
                const body = document.body?.innerText || '';
                const productLinks = document.querySelectorAll("a[href*='/product/']").length;

                return (
                    body.includes('共有') ||
                    body.includes('符合商品') ||
                    productLinks > 0
                );
            }
            """,
            timeout=15000,
        )
    except Exception:
        print("  [!] 等待誠品商品列表逾時，改用目前頁面內容繼續抓")

    page.wait_for_timeout(3000)

    body = page.inner_text("body")

    total_count = None

    match = re.search(r"共有?\s*(\d+)\s*筆", body)

    if match:
        total_count = int(match.group(1))
        print(f"共 {total_count} 筆商品")

    products = page.evaluate(
        """
        () => {
            const products = new Map();

            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }

            function normalizeUrl(href) {
                const url = new URL(href);
                url.hash = '';
                return url.toString();
            }

            function isProductUrl(url) {
                return (
                    url.includes('eslite.com') &&
                    url.includes('/product/')
                );
            }

            function countProductLinks(node) {
                if (!node || !node.querySelectorAll) {
                    return 0;
                }

                const urls = [...node.querySelectorAll('a[href]')]
                    .map(a => a.href)
                    .filter(href => isProductUrl(href))
                    .map(href => normalizeUrl(href));

                return new Set(urls).size;
            }

            function findCard(anchor) {
                let node = anchor;
                let best = anchor.parentElement || anchor;

                for (let i = 0; i < 14 && node; i++) {
                    const text = clean(node.innerText || '');
                    const productCount = countProductLinks(node);

                    const looksLikeCard = (
                        text.includes('$') ||
                        text.includes('NT$') ||
                        text.includes('戰鬥陀螺') ||
                        text.toLowerCase().includes('beyblade') ||
                        text.includes('加入購物車') ||
                        text.includes('立即購買') ||
                        text.includes('貨到通知') ||
                        text.includes('已售完') ||
                        text.includes('售完') ||
                        text.includes('補貨') ||
                        text.includes('暫無供貨')
                    );

                    if (looksLikeCard && productCount <= 1) {
                        best = node;
                    }

                    if (productCount > 1) {
                        break;
                    }

                    node = node.parentElement;
                }

                return best;
            }

            function getName(card, anchor) {
                const imgAlt = clean(anchor.querySelector('img')?.alt || '');

                if (imgAlt.length >= 4) {
                    return imgAlt;
                }

                const anchorText = clean(anchor.innerText || anchor.textContent || '');

                if (
                    anchorText.length >= 4 &&
                    !anchorText.includes('加入購物車') &&
                    !anchorText.includes('立即購買') &&
                    !anchorText.includes('貨到通知')
                ) {
                    return anchorText;
                }

                const lines = (card.innerText || '')
                    .split('\\n')
                    .map(line => clean(line))
                    .filter(Boolean);

                const nameLine = lines.find(line => {
                    const lower = line.toLowerCase();

                    return (
                        lower.includes('beyblade') ||
                        line.includes('戰鬥陀螺') ||
                        lower.includes('bx-') ||
                        lower.includes('ux-') ||
                        lower.includes('cx-') ||
                        lower.includes('bxg-') ||
                        lower.includes('bxh-')
                    );
                });

                return nameLine || anchorText || imgAlt || '未知商品';
            }

            function getPrice(card) {
                const text = clean(card.innerText || '');

                const patterns = [
                    /NT\\$\\s*[\\d,]+/,
                    /\\$\\s*[\\d,]+/,
                    /[\\d,]+\\s*元/,
                ];

                for (const pattern of patterns) {
                    const match = text.match(pattern);

                    if (match) {
                        return match[0];
                    }
                }

                return '';
            }

            function getStatus(card) {
                const text = clean(card.innerText || '');
                const compact = text.replace(/\\s+/g, '');

                if (
                    compact.includes('已售完') ||
                    compact.includes('售完') ||
                    compact.includes('補貨') ||
                    compact.includes('缺貨') ||
                    compact.includes('暫無供貨') ||
                    compact.includes('貨到通知') ||
                    compact.includes('無庫存')
                ) {
                    return 'out_of_stock';
                }

                if (
                    compact.includes('預購') ||
                    compact.includes('預定')
                ) {
                    return 'preorder';
                }

                if (
                    compact.includes('加入購物車') ||
                    compact.includes('放入購物車') ||
                    compact.includes('立即購買') ||
                    compact.includes('直接購買')
                ) {
                    return 'in_stock';
                }

                return 'unknown';
            }

            const anchors = [...document.querySelectorAll("a[href*='/product/']")]
                .filter(a => isProductUrl(a.href));

            for (const anchor of anchors) {
                const url = normalizeUrl(anchor.href);
                const card = findCard(anchor);
                const name = getName(card, anchor);
                const price = getPrice(card);
                const status = getStatus(card);
                const rawText = clean(card.innerText || '');

                products.set(url, {
                    store: '誠品',
                    name,
                    url,
                    price,
                    status,
                    status_label: '',
                    raw_text: rawText,
                });
            }

            return [...products.values()];
        }
        """
    )

    return products, total_count


def fetch_all_products() -> list:
    all_products = {}
    total_count = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
            timezone_id="Asia/Taipei",
            viewport={"width": 1365, "height": 1400},
            ignore_https_errors=True,
        )

        page = context.new_page()

        start = 0
        page_index = 1

        while True:
            products, page_total_count = fetch_page_products(page, start)

            if page_total_count is not None:
                total_count = page_total_count

            new_count = 0

            for product in products:
                url = product.get("url")

                if url and url not in all_products:
                    all_products[url] = product
                    new_count += 1

            print(
                f"第 {page_index} 頁：新增 {new_count} 個（累計 {len(all_products)} 個）"
            )

            if total_count is None:
                break

            if len(all_products) >= total_count:
                break

            start += PAGE_SIZE
            page_index += 1

            if page_index > math.ceil(total_count / PAGE_SIZE) + 1:
                break

        browser.close()

    products = list(all_products.values())

    print(f"全部抓完，共 {len(products)} 個商品")

    return products


def send_startup_message(total_count: int, in_stock_count: int):
    if not DISCORD_WEBHOOK_URL:
        print("[!] DISCORD_WEBHOOK_URL 尚未設定，略過啟動通知")
        return

    payload = {
        "content": (
            "🌀 **陀螺獵人已啟動**\n"
            "開始監控誠品。\n"
            f"目前實體陀螺商品：{total_count} 個\n"
            f"目前現貨：{in_stock_count} 個\n"
            f"啟動時間：{now_taiwan_text()}"
        )
    }

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=15,
        )

        if response.status_code >= 400:
            print(f"[!] 啟動通知發送失敗：{response.status_code} {response.text}")
        else:
            print("[Discord] 已發送啟動通知")

    except Exception as e:
        print(f"[!] 啟動通知錯誤：{e}")


def run_scan(send_startup: bool = False):
    products = fetch_all_products()

    target_products = []
    excluded_products = []

    for product in products:
        if is_beyblade(product):
            target_products.append(product)
        else:
            excluded_products.append(product)

    for product in target_products:
        if product["status"] == "unknown":
            product["status"] = "out_of_stock"

        product["name"] = clean_eslite_name(product["name"])
        product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])
        product["name"] = f"[誠品] {product['name']}"

    in_stock = [p for p in target_products if p["status"] == "in_stock"]
    preorder = [p for p in target_products if p["status"] == "preorder"]
    out_of_stock = [p for p in target_products if p["status"] == "out_of_stock"]

    print("=" * 50)
    print(f"掃描結果（共 {len(target_products)} 個實體陀螺商品）")
    print(f"有貨：{len(in_stock)} 個")
    print(f"預購：{len(preorder)} 個")
    print(f"無貨：{len(out_of_stock)} 個")
    print(f"排除商品：{len(excluded_products)} 個")
    print("=" * 50)

    if send_startup:
        send_startup_message(
            total_count=len(target_products),
            in_stock_count=len(in_stock),
        )

    if in_stock:
        print("\n✅ 誠品有貨商品，準備發送 Discord")

        for product in in_stock:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")
            send_restock_alert(product)

    else:
        print("\n目前誠品無現貨，不發 Discord")

    if preorder:
        print("\n📌 誠品預購商品")

        for product in preorder[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")

    if out_of_stock:
        print("\n❌ 誠品無貨商品")

        for product in out_of_stock[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")


def safe_run_scan(send_startup: bool = False):
    try:
        run_scan(send_startup=send_startup)
    except Exception as e:
        now = datetime.now(TAIWAN_TZ).strftime("%H:%M:%S")

        print(f"[誠品][{now}] 掃描錯誤，但程式不中斷：{e}", flush=True)
        print(f"下次掃描：{CHECK_INTERVAL} 秒後", flush=True)


def main():
    print("🌀 誠品陀螺獵人啟動")
    print(f"   掃描關鍵字：{KEYWORD}")
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")
    print(f"   背景模式：{HEADLESS}")

    if "--once" in sys.argv:
        now = datetime.now(TAIWAN_TZ).strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[誠品][{now}] 開始掃描...")

        # run_all 輪巡模式會用 --once。
        # 這裡不能發「陀螺獵人已啟動」通知，
        # 不然每一輪都會在無貨時也通知。
        safe_run_scan(send_startup=False)

        print("\n--once 模式結束")
        return

    # 單獨執行 main.py 的時候，才發一次啟動通知。
    safe_run_scan(send_startup=True)

    schedule.every(CHECK_INTERVAL).seconds.do(
        lambda: safe_run_scan(send_startup=False)
    )

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()