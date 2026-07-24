import re
import sys
import time
from datetime import datetime

from playwright.sync_api import Page, sync_playwright

from config import CHECK_INTERVAL
from notifier import send_restock_alert


FUNBOX_URL = "https://shop.funbox.com.tw/categories/XI/KB"
HEADLESS = True
ONCE_MODE = "--once" in sys.argv

INITIAL_WAIT_MS = 1000
SCROLL_WAIT_MS = 350
PRODUCT_WAIT_TIMEOUT_MS = 4000
ZERO_RETRY_WAIT_MS = 1200
SCREENSHOT_AFTER_ZERO_COUNT = 3

LABEL_MAP = {
    "in_stock": "✅ 有貨 / 可加入購物車",
    "out_of_stock": "❌ 目前無庫存",
    "preorder": "📌 預購中",
    "unknown": "❓ 狀態未知",
}


def normalize_text(text: str) -> str:
    return (
        text.lower()
        .replace(" ", "")
        .replace("　", "")
        .replace("\n", "")
        .replace("\t", "")
        .strip()
    )


def is_app_exchange_product(product: dict) -> bool:
    text = (
        f"{product.get('name', '')} "
        f"{product.get('raw_text', '')} "
        f"{product.get('price', '')}"
    )
    compact_text = normalize_text(text)

    app_keywords = [
        "app兌換",
        "app兑换",
        "預購app兌換",
        "先行預購app兌換",
        "app限定",
    ]

    return (
        any(normalize_text(k) in compact_text for k in app_keywords)
        or "999999" in compact_text
    )


def is_funbox_beyblade_product(product: dict) -> bool:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    text_lower = text.lower()
    compact_text = normalize_text(text)

    if is_app_exchange_product(product):
        product["excluded_reason"] = "APP 兌換商品"
        return False

    has_beyblade_word = (
        "beyblade" in text_lower
        or "戰鬥陀螺" in text
        or "战斗陀螺" in text
        or "ベイブレード" in text
    )

    has_product_code = bool(
        re.search(r"\b(?:BX|UX|CX|BXG)-\d+", text, re.IGNORECASE)
    )

    target_words = [
        "發射器",
        "啟動器",
        "握把",
        "改造組",
        "對戰組",
        "隨機強化組",
        "強化組",
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
    ]

    if any(normalize_text(k) in compact_text for k in exclude_keywords):
        product["excluded_reason"] = "排除關鍵字"
        return False

    return has_beyblade_word and (has_product_code or has_target_word)


def clean_funbox_name(name: str) -> str:
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
    return re.sub(r"\s+", " ", name).strip()


def extract_products(page: Page) -> list:
    return page.evaluate(
        r"""
        () => {
            const products = new Map();

            function clean(text) {
                return (text || '').replace(/\s+/g, ' ').trim();
            }

            function isProductUrl(url) {
                return (
                    url.includes('shop.funbox.com.tw') &&
                    url.includes('/products/')
                );
            }

            function normalizeUrl(href) {
                const url = new URL(href);
                url.search = '';
                return url.toString();
            }

            function countProductLinks(node) {
                if (!node || !node.querySelectorAll) return 0;

                const urls = [...node.querySelectorAll('a[href]')]
                    .map(a => a.href)
                    .filter(isProductUrl)
                    .map(normalizeUrl);

                return new Set(urls).size;
            }

            function findCard(anchor) {
                let node = anchor;
                let best = anchor.parentElement || anchor;

                for (let i = 0; i < 12 && node; i++) {
                    const text = clean(node.innerText || '');
                    const count = countProductLinks(node);

                    const looksLikeCard = (
                        text.includes('$') ||
                        text.includes('NT$') ||
                        text.includes('戰鬥陀螺') ||
                        text.toLowerCase().includes('beyblade') ||
                        text.includes('加入購物車') ||
                        text.includes('售完') ||
                        text.includes('補貨') ||
                        text.includes('APP兌換')
                    );

                    if (looksLikeCard && count <= 1) best = node;
                    if (count > 1) break;

                    node = node.parentElement;
                }

                return best;
            }

            function getName(card, anchor) {
                const imgAlt = clean(anchor.querySelector('img')?.alt || '');
                if (imgAlt.length >= 4) return imgAlt;

                const anchorText = clean(
                    anchor.innerText || anchor.textContent || ''
                );
                if (anchorText.length >= 4) return anchorText;

                const lines = (card.innerText || '')
                    .split('\n')
                    .map(clean)
                    .filter(Boolean);

                const nameLine = lines.find(line => {
                    const lower = line.toLowerCase();
                    return (
                        lower.includes('beyblade') ||
                        line.includes('戰鬥陀螺') ||
                        lower.includes('bx-') ||
                        lower.includes('ux-') ||
                        lower.includes('cx-') ||
                        lower.includes('bxg-')
                    );
                });

                return nameLine || anchorText || imgAlt || '未知商品';
            }

            function getPrice(card) {
                const text = clean(card.innerText || '');
                const patterns = [
                    /NT\$\s*[\d,]+/,
                    /\$\s*[\d,]+/,
                    /[\d,]+\s*元/,
                ];

                for (const pattern of patterns) {
                    const match = text.match(pattern);
                    if (match) return match[0];
                }

                return '';
            }

            function getStatus(card) {
                const compact = clean(card.innerText || '')
                    .replace(/\s+/g, '');

                if (
                    compact.includes('售完') ||
                    compact.includes('補貨') ||
                    compact.includes('缺貨') ||
                    compact.includes('暫無供貨') ||
                    compact.includes('貨到通知')
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

            const anchors = [...document.querySelectorAll('a[href]')]
                .filter(a => isProductUrl(a.href));

            for (const anchor of anchors) {
                const url = normalizeUrl(anchor.href);
                const card = findCard(anchor);

                products.set(url, {
                    store: 'Funbox',
                    name: getName(card, anchor),
                    url,
                    price: getPrice(card),
                    status: getStatus(card),
                    status_label: '',
                    raw_text: clean(card.innerText || ''),
                });
            }

            return [...products.values()];
        }
        """
    )


def load_and_extract(page: Page, first_load: bool) -> list:
    print("正在打開 Funbox...")

    if first_load or page.url == "about:blank":
        page.goto(
            FUNBOX_URL,
            wait_until="domcontentloaded",
            timeout=30000,
        )
    else:
        page.reload(
            wait_until="domcontentloaded",
            timeout=30000,
        )

    try:
        page.wait_for_selector(
            "a[href*='/products/']",
            timeout=PRODUCT_WAIT_TIMEOUT_MS,
        )
    except Exception:
        print("[!] 商品連結尚未出現，繼續快速重試")

    page.wait_for_timeout(INITIAL_WAIT_MS)

    for i in range(2):
        count = page.locator("a[href*='/products/']").count()
        print(f"Funbox 滾動 {i + 1}/2，目前商品連結：{count}")
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(SCROLL_WAIT_MS)

    products = extract_products(page)

    if not products:
        print("[!] 第一次抓到 0 個商品，快速重試")
        page.wait_for_timeout(ZERO_RETRY_WAIT_MS)
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(SCROLL_WAIT_MS)
        products = extract_products(page)

    return products


def process_products(products: list) -> None:
    target_products = []
    excluded_products = []

    for product in products:
        if is_funbox_beyblade_product(product):
            target_products.append(product)
        else:
            excluded_products.append(product)

    for product in target_products:
        if product["status"] == "unknown":
            product["status"] = "out_of_stock"

        product["name"] = clean_funbox_name(product["name"])
        product["status_label"] = LABEL_MAP.get(
            product["status"],
            LABEL_MAP["unknown"],
        )
        product["name"] = f"[Funbox] {product['name']}"

    app_exchange_products = [
        p for p in excluded_products if is_app_exchange_product(p)
    ]

    in_stock = [p for p in target_products if p["status"] == "in_stock"]
    out_of_stock = [
        p for p in target_products if p["status"] == "out_of_stock"
    ]
    unknown = [p for p in target_products if p["status"] == "unknown"]

    print("=" * 50)
    print(f"Funbox 抓到商品：{len(products)} 個")
    print(f"Funbox 戰鬥陀螺商品：{len(target_products)} 個")
    print(f"Funbox 排除 APP 兌換商品：{len(app_exchange_products)} 個")
    print(f"有貨：{len(in_stock)} 個")
    print(f"無貨：{len(out_of_stock)} 個")
    print(f"未知：{len(unknown)} 個")
    print("=" * 50)

    if in_stock:
        print("\n✅ Funbox 有貨商品，準備發送 Discord")

        for product in in_stock:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")
            send_restock_alert(product)
    else:
        print("\n目前 Funbox 無現貨，不發 Discord")


def main():
    print("🧸 Funbox 陀螺獵人啟動")
    print(f"   掃描網址：{FUNBOX_URL}")
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")
    print(f"   背景模式：{HEADLESS}")

    if ONCE_MODE:
        print("   執行模式：單次掃描模式")
    else:
        print("   執行模式：Chromium 常駐高速模式")

    zero_result_count = 0

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
        page.set_default_timeout(5000)
        first_load = True

        try:
            while True:
                now = datetime.now().strftime("%H:%M:%S")
                started_at = time.time()

                print(f"\n{'=' * 50}")
                print(f"[{now}] 開始掃描 Funbox...")

                try:
                    products = load_and_extract(page, first_load)
                    first_load = False
                    process_products(products)

                    if products:
                        zero_result_count = 0
                    else:
                        zero_result_count += 1
                        print(
                            f"抓到 0 個商品，連續失敗次數："
                            f"{zero_result_count}"
                        )

                        if (
                            zero_result_count
                            >= SCREENSHOT_AFTER_ZERO_COUNT
                        ):
                            page.screenshot(
                                path="funbox_debug.png",
                                full_page=True,
                            )
                            print(
                                "已產生 funbox_debug.png，"
                                "並重新建立分頁"
                            )
                            zero_result_count = 0
                            page.close()
                            page = context.new_page()
                            page.set_default_timeout(5000)
                            first_load = True

                except KeyboardInterrupt:
                    print("\n已停止 Funbox 監控")
                    break
                except Exception as e:
                    print(f"[!] Funbox 掃描錯誤：{e}")

                    try:
                        page.close()
                    except Exception:
                        pass

                    page = context.new_page()
                    page.set_default_timeout(5000)
                    first_load = True

                elapsed = time.time() - started_at
                print(f"本輪耗時：{elapsed:.1f} 秒")

                if ONCE_MODE:
                    print("--once 模式結束")
                    break

                print(f"下次掃描：{CHECK_INTERVAL} 秒後")
                time.sleep(CHECK_INTERVAL)

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()