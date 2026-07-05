import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from playwright.sync_api import sync_playwright

from notifier import send_restock_alert


SHOPEE_URL = (
    "https://shopee.tw/mall/search"
    "?keyword=%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA"
    "&shop=285705541"
)

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

HEADLESS = os.getenv("SHOPEE_HEADLESS", "true").lower() in ["1", "true", "yes", "on"]

PAGE_TIMEOUT_MS = int(os.getenv("SHOPEE_PAGE_TIMEOUT_MS", "45000"))
SHOPEE_RETRY_ATTEMPTS = int(os.getenv("SHOPEE_RETRY_ATTEMPTS", "2"))
SHOPEE_RETRY_SLEEP_SECONDS = int(os.getenv("SHOPEE_RETRY_SLEEP_SECONDS", "5"))

LABEL_MAP = {
    "in_stock": "✅ 有貨 / 可加入購物車",
    "out_of_stock": "❌ 目前無庫存",
    "preorder": "📌 預購中",
    "unknown": "❓ 狀態未知",
}


def normalize_text(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace(" ", "")
        .replace("　", "")
        .replace("\n", "")
        .replace("\t", "")
        .strip()
    )


def normalize_product_url(url: str) -> str:
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def clean_shopee_name(name: str) -> str:
    remove_words = [
        "加入購物車",
        "直接購買",
        "立即購買",
        "已售完",
        "售完",
        "缺貨",
        "補貨中",
        "貨到通知",
        "sold out",
        "out of stock",
    ]

    for word in remove_words:
        name = name.replace(word, "").strip()

    name = re.sub(r"NT\$\s*[\d,]+", "", name).strip()
    name = re.sub(r"\$\s*[\d,]+", "", name).strip()
    name = re.sub(r"\s+", " ", name).strip()

    return name


def is_beyblade_product(product: dict) -> bool:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    text_lower = text.lower()

    has_beyblade = (
        "beyblade" in text_lower
        or "戰鬥陀螺" in text
        or "战斗陀螺" in text
        or "ベイブレード" in text
    )

    has_product_code = bool(
        re.search(r"\b(?:BX|UX|CX|BXG|BXH|CXG|UXG)-\d+", text, re.IGNORECASE)
    )

    target_words = [
        "陀螺",
        "發射器",
        "啟動器",
        "握把",
        "對戰組",
        "改造組",
        "強化組",
        "隨機",
        "限定",
        "蒼龍",
        "長矛",
    ]

    has_target_word = any(word in text for word in target_words)

    return has_beyblade and (has_product_code or has_target_word)


def is_excluded_product(product: dict) -> bool:
    text = f"{product.get('name', '')} {product.get('raw_text', '')}"
    compact_text = normalize_text(text)

    exclude_keywords = [
        "韓版",
        "韩版",
        "貼紙",
        "卡牌",
        "紙製",
        "漫畫",
        "雜誌",
        "電子書",
        "ebook",
        "拼圖",
        "puzzle",
    ]

    return any(normalize_text(keyword) in compact_text for keyword in exclude_keywords)


def detect_status(product: dict) -> str:
    text = f"{product.get('name', '')} {product.get('raw_text', '')}"
    compact_text = normalize_text(text)

    out_of_stock_keywords = [
        "已售完",
        "售完",
        "缺貨",
        "補貨中",
        "貨到通知",
        "無庫存",
        "soldout",
        "sold out",
        "outofstock",
        "out of stock",
    ]

    preorder_keywords = [
        "預購",
        "預定",
        "preorder",
        "pre-order",
    ]

    in_stock_keywords = [
        "加入購物車",
        "直接購買",
        "立即購買",
        "購買",
    ]

    if any(normalize_text(keyword) in compact_text for keyword in out_of_stock_keywords):
        return "out_of_stock"

    if any(normalize_text(keyword) in compact_text for keyword in preorder_keywords):
        return "preorder"

    if any(normalize_text(keyword) in compact_text for keyword in in_stock_keywords):
        return "in_stock"

    # 蝦皮搜尋頁很多時候不會明確顯示「加入購物車」，
    # 所以第一版保守處理：unknown 先不通知。
    return "unknown"


def open_shopee_page(page) -> bool:
    try:
        print(f"嘗試網址：{SHOPEE_URL}", flush=True)

        page.goto(
            SHOPEE_URL,
            wait_until="commit",
            timeout=PAGE_TIMEOUT_MS,
        )

        page.wait_for_timeout(12000)

        return True

    except Exception as e:
        print(f"[!] Shopee 開頁失敗：{e}", flush=True)
        return False


def scroll_page(page):
    for i in range(1, 9):
        try:
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(2000)

            link_count = page.locator("a[href]").count()

            print(f"Shopee 滾動 {i}/8，目前連結：{link_count}", flush=True)

        except Exception as e:
            print(f"[!] Shopee 滾動失敗：{e}", flush=True)
            break


def extract_shopee_products(page) -> list[dict]:
    products = page.evaluate(
        """
        () => {
            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }

            function normalizeUrl(href) {
                try {
                    const url = new URL(href);
                    url.hash = '';
                    return url.toString();
                } catch {
                    return href;
                }
            }

            function looksLikeProductUrl(url) {
                return (
                    url.includes('shopee.tw') &&
                    (
                        url.includes('-i.') ||
                        url.includes('/product/')
                    )
                );
            }

            function pickContainer(anchor) {
                const selectors = [
                    '[data-sqe="item"]',
                    '[class*="shop-search-result-view"]',
                    '[class*="col-xs-2"]',
                    'li',
                    'article',
                    'div'
                ];

                for (const selector of selectors) {
                    const node = anchor.closest(selector);

                    if (!node) {
                        continue;
                    }

                    const text = clean(node.innerText || node.textContent || '');

                    if (
                        text.length > 0 &&
                        text.length < 900 &&
                        (
                            text.toLowerCase().includes('beyblade') ||
                            text.includes('戰鬥陀螺') ||
                            text.includes('陀螺') ||
                            text.includes('蒼龍') ||
                            text.includes('長矛') ||
                            text.includes('$') ||
                            text.includes('NT$') ||
                            text.includes('已售完') ||
                            text.includes('售完')
                        )
                    ) {
                        return node;
                    }
                }

                return anchor;
            }

            const anchors = Array.from(document.querySelectorAll('a[href]'));
            const results = [];

            for (const a of anchors) {
                const href = normalizeUrl(a.href || '');

                if (!href || !looksLikeProductUrl(href)) {
                    continue;
                }

                const container = pickContainer(a);
                const rawText = clean(container.innerText || container.textContent || '');
                const anchorText = clean(a.innerText || a.textContent || '');
                const imgAlt = clean(a.querySelector('img')?.alt || '');

                let name = imgAlt || anchorText || rawText || '未知商品';

                if (rawText.length > name.length && rawText.length < 600) {
                    name = rawText;
                }

                results.push({
                    name,
                    url: href,
                    raw_text: rawText || anchorText || imgAlt || name
                });
            }

            return results;
        }
        """
    )

    seen_urls = set()
    unique_products = []

    for product in products:
        url = product.get("url", "").strip()

        if not url:
            continue

        url = normalize_product_url(url)

        if url in seen_urls:
            continue

        seen_urls.add(url)

        product["url"] = url
        product["name"] = clean_shopee_name(product.get("name", "未知商品"))

        unique_products.append(product)

    return unique_products


def empty_result():
    return {
        "all": [],
        "normal": [],
        "excluded": [],
        "non_target": [],
        "in_stock": [],
        "preorder": [],
        "out_of_stock": [],
        "unknown": [],
    }


def scan_shopee_once():
    all_products = []
    normal_products = []
    excluded_products = []
    non_target_products = []

    in_stock_products = []
    preorder_products = []
    out_of_stock_products = []
    unknown_products = []

    print("=" * 50, flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始掃描 Shopee...", flush=True)

    browser = None
    context = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-http2",
                ],
            )

            context = browser.new_context(
                viewport={"width": 1365, "height": 1400},
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()
            page.set_default_timeout(30000)

            print("正在打開 Shopee Mall 搜尋頁面...", flush=True)

            loaded = open_shopee_page(page)

            if not loaded:
                print("Shopee 頁面無法連線，跳過這次掃描", flush=True)
                return empty_result()

            # 如果遇到驗證頁，先印出來，不硬繞
            body_text = page.inner_text("body", timeout=10000)

            if (
                "驗證" in body_text
                or "captcha" in body_text.lower()
                or "robot" in body_text.lower()
                or "請確認" in body_text
            ):
                print("[!] Shopee 可能出現驗證 / 反爬頁，這輪跳過", flush=True)
                return empty_result()

            scroll_page(page)

            all_products = extract_shopee_products(page)

            for product in all_products:
                if is_excluded_product(product):
                    excluded_products.append(product)
                    continue

                if not is_beyblade_product(product):
                    non_target_products.append(product)
                    continue

                status = detect_status(product)

                # 蝦皮第一版保守：unknown 不當成有貨
                if status == "unknown":
                    status = "out_of_stock"

                product["status"] = status
                product["store"] = "Shopee Mall"
                product["status_label"] = LABEL_MAP.get(status, LABEL_MAP["unknown"])
                product["name"] = f"[Shopee Mall] {product['name']}"

                normal_products.append(product)

                if status == "in_stock":
                    in_stock_products.append(product)
                elif status == "preorder":
                    preorder_products.append(product)
                elif status == "out_of_stock":
                    out_of_stock_products.append(product)
                else:
                    unknown_products.append(product)

    except Exception as e:
        print(f"[!] Shopee 掃描錯誤：{e}", flush=True)

    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass

        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass

    return {
        "all": all_products,
        "normal": normal_products,
        "excluded": excluded_products,
        "non_target": non_target_products,
        "in_stock": in_stock_products,
        "preorder": preorder_products,
        "out_of_stock": out_of_stock_products,
        "unknown": unknown_products,
    }


def print_products(title: str, products: list[dict]):
    if not products:
        return

    print(f"\n{title}", flush=True)

    for product in products:
        print(f"- {product.get('name', '未知商品')}", flush=True)
        print(f"  {product.get('url', '')}", flush=True)


def print_summary(result: dict):
    print("=" * 50, flush=True)
    print(f"Shopee 抓到商品：{len(result['all'])} 個", flush=True)
    print(f"Shopee 正常陀螺商品：{len(result['normal'])} 個", flush=True)
    print(f"Shopee 排除商品：{len(result['excluded'])} 個", flush=True)
    print(f"Shopee 非目標商品：{len(result['non_target'])} 個", flush=True)
    print(f"有貨：{len(result['in_stock'])} 個", flush=True)
    print(f"預購：{len(result['preorder'])} 個", flush=True)
    print(f"無貨：{len(result['out_of_stock'])} 個", flush=True)
    print(f"未知：{len(result['unknown'])} 個", flush=True)
    print("=" * 50, flush=True)

    print_products("✅ 有貨商品", result["in_stock"])
    print_products("📌 預購商品", result["preorder"])
    print_products("❌ 無貨商品", result["out_of_stock"])
    print_products("🚫 已排除商品", result["excluded"])
    print_products("⚪ 非目標商品", result["non_target"])


def send_shopee_notifications(products: list[dict]):
    if not products:
        print("\n目前 Shopee 無現貨，不發 Discord", flush=True)
        return

    print("\n🎯 Shopee 發現現貨，準備發 Discord", flush=True)

    for product in products:
        try:
            send_restock_alert(product)
            print(f"已發送 Discord：{product.get('name', '未知商品')}", flush=True)
        except Exception as e:
            print(f"[!] Discord 發送失敗：{e}", flush=True)


def run_once():
    final_result = empty_result()

    for attempt in range(1, SHOPEE_RETRY_ATTEMPTS + 1):
        print(
            f"\nShopee 第 {attempt}/{SHOPEE_RETRY_ATTEMPTS} 次嘗試",
            flush=True,
        )

        result = scan_shopee_once()
        final_result = result

        if len(result["all"]) > 0:
            print(f"Shopee 第 {attempt} 次成功抓到商品，停止重試", flush=True)
            break

        if attempt < SHOPEE_RETRY_ATTEMPTS:
            print(
                f"Shopee 第 {attempt} 次沒有抓到商品，等待 {SHOPEE_RETRY_SLEEP_SECONDS} 秒後重試",
                flush=True,
            )
            time.sleep(SHOPEE_RETRY_SLEEP_SECONDS)
        else:
            print("Shopee 重試次數已用完，這輪跳過", flush=True)

    print_summary(final_result)
    send_shopee_notifications(final_result["in_stock"])


def main():
    print("🛒 Shopee Mall 陀螺獵人啟動", flush=True)
    print(f"   掃描網址：{SHOPEE_URL}", flush=True)
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒", flush=True)
    print(f"   背景模式：{HEADLESS}", flush=True)
    print(f"   單次開頁 timeout：{PAGE_TIMEOUT_MS} ms", flush=True)
    print(f"   失敗重試次數：{SHOPEE_RETRY_ATTEMPTS}", flush=True)
    print(f"   重試間隔：{SHOPEE_RETRY_SLEEP_SECONDS} 秒", flush=True)

    if "--once" in sys.argv:
        run_once()
        print("\n--once 模式結束", flush=True)
        return

    while True:
        run_once()
        print(f"\n等待 {CHECK_INTERVAL} 秒後再次掃描 Shopee...", flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()