import os
import re
import sys
import time
import subprocess

from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.sync_api import sync_playwright

from notifier import send_restock_alert


TAKARA_IN_STOCK_URL = (
    "https://takaratomymall.jp/shop/goods/search.aspx"
    "?stock_on_sales=0&keyword=BEYBLADE+X&min_price=&max_price=&search=x&wovn=ja"
)

BASE_TAKARA_URLS = [
    TAKARA_IN_STOCK_URL,
]

FALLBACK_IN_STOCK_URLS = [
    TAKARA_IN_STOCK_URL,
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))

# TAKARA 這站 headless=True 容易 timeout
# Zeabur 請用 Dockerfile 的 Xvfb 跑 headed Chromium
HEADLESS = os.getenv("TAKARA_HEADLESS", "false").lower() in ["1", "true", "yes", "on"]

BROWSER_WIDTH = int(os.getenv("TAKARA_BROWSER_WIDTH", "500"))
BROWSER_HEIGHT = int(os.getenv("TAKARA_BROWSER_HEIGHT", "400"))
BROWSER_X = int(os.getenv("TAKARA_BROWSER_X", "-2000"))
BROWSER_Y = int(os.getenv("TAKARA_BROWSER_Y", "100"))

PAGE_TIMEOUT_MS = int(os.getenv("TAKARA_PAGE_TIMEOUT_MS", "45000"))

LABEL_MAP = {
    "in_stock": "✅ 有貨 / 可加入購物車",
    "out_of_stock": "❌ 目前無庫存",
    "preorder": "📌 預購中",
    "unknown": "❓ 狀態未知",
}


def hide_chromium_window():
    """
    只在 macOS 本機使用 AppleScript 隱藏 Chromium 視窗。
    Zeabur 是 Linux，不能跑 osascript，所以直接略過。
    """
    if sys.platform != "darwin":
        return

    try:
        apple_script = """
        tell application "System Events"
            repeat with p in (every process whose name contains "Chromium")
                set visible of p to false
            end repeat
            repeat with p in (every process whose name contains "Google Chrome for Testing")
                set visible of p to false
            end repeat
        end tell
        """

        subprocess.run(
            ["osascript", "-e", apple_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )

        print("已嘗試隱藏 Chromium 視窗", flush=True)

    except Exception as e:
        print(f"[!] 隱藏 Chromium 視窗失敗：{e}", flush=True)


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
    """
    移除 #revico-comment 這種 anchor，避免同一個商品重複出現。
    """
    parsed = urlparse(url)
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def clean_takara_name(name: str) -> str:
    remove_words = [
        "カートに入れる",
        "買い物かごに入れる",
        "予約する",
        "購入する",
        "在庫あり",
        "在庫なし",
        "品切れ",
        "販売終了",
        "販売期間終了",
        "入荷案内申込",
        "入荷案内",
        "入荷お知らせ",
        "再入荷通知",
        "SOLD OUT",
        "SOLDOUT",
    ]

    for word in remove_words:
        name = name.replace(word, "").strip()

    name = re.sub(r"￥\s*[\d,]+", "", name).strip()
    name = re.sub(r"¥\s*[\d,]+", "", name).strip()
    name = re.sub(r"\s+", " ", name).strip()

    return name


def is_normal_takara_beyblade_product(product: dict) -> bool:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    text_lower = text.lower()
    compact_text = normalize_text(text)

    has_beyblade = (
        "beyblade x" in text_lower
        or "ベイブレードx" in compact_text
        or "ベイブレード x" in text_lower
    )

    has_product_code = bool(
        re.search(r"\b(?:BX|UX|CX|BXG|BXH|CXG|UXG)-\d+", text, re.IGNORECASE)
    )

    return has_beyblade and has_product_code


def is_excluded_takara_product(product: dict) -> bool:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    compact_text = normalize_text(text)

    exclude_keywords = [
        # 電子書 / 書籍 / 攻略書
        "ebook",
        "電子書",
        "book",
        "ガイド",
        "ブック",
        "雑誌",
        "書籍",

        # APP / 活動限定
        "アプリ・イベント限定",
        "アプリ限定",
        "イベント限定",
        "アプリイベント限定",

        # 稀有陀螺交換券相關
        "レアベイ交換チケット対象",
        "レアベイ交換チケット",
        "交換チケット",
        "チケット対象",

        # 貼紙
        "ベイエンブレムステッカー",
        "エンブレムステッカー",
        "ベイブレードステッカー",
        "ステッカー",
        "シール",

        # Premium X 會員限定 / 抽選販售 / 入會特典
        "プレミアムx会員限定",
        "プレミアムx会員",
        "プレミアム会員",
        "会員限定",
        "抽選販売",
        "抽選",
        "新規入会特典",
        "入会特典",

        # 非一般陀螺商品 / 特典配件
        "ベイバトルパスシート",
        "バトルパスシート",
        "ロックチップ",

        # 拼圖 / 玩具周邊
        "ジグソーパズル",
        "パズル",

        # 明顯不是實體商品
        "ダウンロード",
        "壁紙",
    ]

    compact_exclude_keywords = [
        normalize_text(keyword)
        for keyword in exclude_keywords
    ]

    return any(keyword in compact_text for keyword in compact_exclude_keywords)


def get_takara_status(product: dict, page_url: str) -> str:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    compact_text = normalize_text(text)

    preorder_keywords = [
        "予約する",
        "予約受付中",
        "予約商品",
        "予約",
    ]

    out_of_stock_keywords = [
        "在庫なし",
        "品切れ",
        "販売終了",
        "販売期間終了",

        # 到貨通知 / 補貨通知，不是現貨
        "入荷案内申込",
        "入荷案内",
        "入荷お知らせ",
        "再入荷通知",

        "soldout",
        "sold out",
    ]

    in_stock_keywords = [
        "カートに入れる",
        "買い物かごに入れる",
        "購入する",
        "在庫あり",
    ]

    if any(normalize_text(keyword) in compact_text for keyword in preorder_keywords):
        return "preorder"

    if any(normalize_text(keyword) in compact_text for keyword in out_of_stock_keywords):
        return "out_of_stock"

    if any(normalize_text(keyword) in compact_text for keyword in in_stock_keywords):
        return "in_stock"

    # 這個網址本身已經是 TAKARA 的「在庫あり」篩選結果。
    # 但只有在沒有出現缺貨 / 補貨通知 / 預購文字時，才視為有貨。
    if "stock_on_sales=0" in page_url:
        return "in_stock"

    return "unknown"


def open_takara_base_page(page) -> bool:
    """
    TAKARA 直接打開已套用「在庫あり」的網址。
    如果這個網址本輪開不起來，就直接放棄，不再嘗試其他網址。
    """
    try:
        print(f"嘗試網址：{TAKARA_IN_STOCK_URL}", flush=True)

        page.goto(
            TAKARA_IN_STOCK_URL,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        hide_chromium_window()

        page.wait_for_timeout(10000)

        return True

    except Exception as e:
        print(f"[!] TAKARA 在庫あり頁開啟失敗：{e}", flush=True)
        return False


def apply_in_stock_filter(page) -> bool:
    """
    目前已經直接打開 stock_on_sales=0 的網址，
    所以不用再點畫面上的篩選條件。
    """
    if "stock_on_sales=0" in page.url:
        print("已使用 TAKARA 在庫あり篩選網址，略過畫面點選篩選", flush=True)
        return True

    print("[!] 目前頁面不是 TAKARA 在庫あり篩選頁，略過本輪", flush=True)
    return False


def extract_takara_products(page) -> list[dict]:
    products = page.evaluate(
        """
        () => {
            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }

            function pickContainer(anchor) {
                const selectors = [
                    'li',
                    '.item',
                    '.product',
                    '.goods',
                    '.goodsList',
                    '.block-goods-list--item',
                    '.block-thumbnail-t',
                    'article',
                    'div'
                ];

                for (const selector of selectors) {
                    const node = anchor.closest(selector);

                    if (node) {
                        const text = clean(node.innerText || node.textContent || '');

                        if (text.length > 0 && text.length < 700) {
                            return node;
                        }
                    }
                }

                return anchor;
            }

            const anchors = Array.from(document.querySelectorAll('a[href]'));
            const results = [];

            for (const a of anchors) {
                const href = a.href || '';

                if (!href) {
                    continue;
                }

                if (!href.includes('/shop/g/g')) {
                    continue;
                }

                const container = pickContainer(a);
                const rawText = clean(container.innerText || container.textContent || '');
                const anchorText = clean(a.innerText || a.textContent || '');

                let name = anchorText || rawText || '未知商品';

                if (rawText.length > name.length && rawText.length < 500) {
                    name = rawText;
                }

                results.push({
                    name,
                    url: href,
                    raw_text: rawText || anchorText || name
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

        url = urljoin("https://takaratomymall.jp", url)
        url = normalize_product_url(url)
        product["url"] = url

        if url in seen_urls:
            continue

        seen_urls.add(url)

        product["name"] = clean_takara_name(product.get("name", "未知商品"))

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


def print_products(title: str, products: list[dict]):
    if not products:
        return

    print(f"\n{title}", flush=True)

    for product in products:
        name = product.get("name", "未知商品")
        url = product.get("url", "")

        print(f"- {name}", flush=True)
        print(f"  {url}", flush=True)


def scan_takara_once():
    all_products = []
    normal_products = []
    excluded_products = []
    non_target_products = []

    in_stock_products = []
    preorder_products = []
    out_of_stock_products = []
    unknown_products = []

    print("=" * 50, flush=True)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 開始掃描 TAKARA...", flush=True)

    browser = None
    context = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-http2",
                    "--disable-blink-features=AutomationControlled",
                    f"--window-size={BROWSER_WIDTH},{BROWSER_HEIGHT}",
                    f"--window-position={BROWSER_X},{BROWSER_Y}",
                ],
            )

            hide_chromium_window()

            context = browser.new_context(
                viewport={
                    "width": BROWSER_WIDTH,
                    "height": BROWSER_HEIGHT,
                },
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            page = context.new_page()
            page.set_default_timeout(30000)

            print("正在打開 TAKARA TOMY MALL...", flush=True)

            loaded = open_takara_base_page(page)

            if not loaded:
                print("TAKARA 在庫あり頁無法連線，跳過這次掃描", flush=True)
                return empty_result()

            filtered = apply_in_stock_filter(page)

            if not filtered:
                print("TAKARA 篩選頁確認失敗，跳過這次掃描", flush=True)
                return empty_result()

            hide_chromium_window()

            all_products = extract_takara_products(page)

            for product in all_products:
                if is_excluded_takara_product(product):
                    excluded_products.append(product)
                    continue

                if not is_normal_takara_beyblade_product(product):
                    non_target_products.append(product)
                    continue

                status = get_takara_status(product, page.url)
                product["status"] = status
                product["store"] = "TAKARA TOMY MALL"

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
        print(f"[!] TAKARA 掃描錯誤：{e}", flush=True)

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


def send_takara_notifications(products: list[dict]):
    if not products:
        print("\n目前 TAKARA 無現貨，不發 Discord", flush=True)
        return

    print("\n🎯 TAKARA 發現現貨，準備發 Discord", flush=True)

    for product in products:
        try:
            send_restock_alert(product)
            print(f"已發送 Discord：{product.get('name', '未知商品')}", flush=True)
        except Exception as e:
            print(f"[!] Discord 發送失敗：{e}", flush=True)


def print_summary(result: dict):
    all_products = result["all"]
    normal_products = result["normal"]
    excluded_products = result["excluded"]
    non_target_products = result["non_target"]
    in_stock_products = result["in_stock"]
    preorder_products = result["preorder"]
    out_of_stock_products = result["out_of_stock"]
    unknown_products = result["unknown"]

    print("=" * 50, flush=True)
    print(f"TAKARA 抓到商品：{len(all_products)} 個", flush=True)
    print(f"TAKARA 正常陀螺商品：{len(normal_products)} 個", flush=True)
    print(f"TAKARA 排除商品：{len(excluded_products)} 個", flush=True)
    print(f"TAKARA 非目標商品：{len(non_target_products)} 個", flush=True)
    print(f"有貨：{len(in_stock_products)} 個", flush=True)
    print(f"預購：{len(preorder_products)} 個", flush=True)
    print(f"無貨：{len(out_of_stock_products)} 個", flush=True)
    print(f"未知：{len(unknown_products)} 個", flush=True)
    print("=" * 50, flush=True)

    print_products("✅ 有貨商品", in_stock_products)
    print_products("📌 預購商品", preorder_products)
    print_products("❌ 無貨商品", out_of_stock_products)
    print_products("❓ 未知商品", unknown_products)
    print_products("🚫 已排除商品", excluded_products)
    print_products("⚪ 非目標商品", non_target_products)


def run_once():
    result = scan_takara_once()
    print_summary(result)
    send_takara_notifications(result["in_stock"])


def main():
    print("🇯🇵 TAKARA TOMY MALL 陀螺獵人啟動", flush=True)
    print(f"   掃描網址：{TAKARA_IN_STOCK_URL}", flush=True)
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒", flush=True)
    print(f"   背景模式：{HEADLESS}", flush=True)
    print(f"   視窗大小：{BROWSER_WIDTH} x {BROWSER_HEIGHT}", flush=True)
    print(f"   視窗位置：{BROWSER_X}, {BROWSER_Y}", flush=True)
    print("", flush=True)

    if "--once" in sys.argv:
        run_once()
        print("\n--once 模式結束", flush=True)
        return

    while True:
        run_once()
        print(f"\n等待 {CHECK_INTERVAL} 秒後再次掃描 TAKARA...", flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()