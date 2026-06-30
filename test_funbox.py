import re
import sys
import time

from datetime import datetime

from playwright.sync_api import sync_playwright

from config import CHECK_INTERVAL
from notifier import send_restock_alert


FUNBOX_URL = "https://shop.funbox.com.tw/categories/XI/KB"

HEADLESS = True

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
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")
    price = product.get("price", "")

    text = f"{name} {raw_text} {price}"
    compact_text = normalize_text(text)

    app_keywords = [
        "app兌換",
        "app兑换",
        "預購app兌換",
        "先行預購app兌換",
        "app限定",
    ]

    if any(normalize_text(keyword) in compact_text for keyword in app_keywords):
        return True

    if "999999" in compact_text:
        return True

    return False


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

    if any(normalize_text(keyword) in compact_text for keyword in exclude_keywords):
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
    name = re.sub(r"\s+", " ", name).strip()

    return name


def fetch_funbox_products() -> list:
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

        print("正在打開 Funbox...")

        try:
            page.goto(FUNBOX_URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"[!] Funbox 開啟失敗：{e}")
            browser.close()
            return []

        try:
            page.wait_for_function(
                """
                () => {
                    const body = document.body?.innerText || '';
                    const links = document.querySelectorAll("a[href*='/products/']").length;

                    return (
                        body.includes('戰鬥陀螺') ||
                        body.includes('BEYBLADE') ||
                        body.includes('商品') ||
                        links > 0
                    );
                }
                """,
                timeout=20000,
            )
        except Exception:
            print("[!] 等待 Funbox 商品列表逾時，改用目前頁面內容繼續抓")

        page.wait_for_timeout(5000)

        for i in range(5):
            count = page.evaluate(
                """
                () => document.querySelectorAll("a[href*='/products/']").length
                """
            )

            print(f"Funbox 滾動 {i + 1}/5，目前商品連結：{count}")

            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1200)

        products = page.evaluate(
            """
            () => {
                const products = new Map();

                function clean(text) {
                    return (text || '').replace(/\\s+/g, ' ').trim();
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

                    for (let i = 0; i < 12 && node; i++) {
                        const text = clean(node.innerText || '');
                        const productCount = countProductLinks(node);

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

                    if (anchorText.length >= 4) {
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
                            lower.includes('bxg-')
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
                    const name = getName(card, anchor);
                    const price = getPrice(card);
                    const status = getStatus(card);
                    const rawText = clean(card.innerText || '');

                    products.set(url, {
                        store: 'Funbox',
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

        if not products:
            page.screenshot(path="funbox_debug.png", full_page=True)
            print("抓到 0 個商品，已產生 funbox_debug.png")

        browser.close()

    return products


def run_once():
    products = fetch_funbox_products()

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
        product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])
        product["name"] = f"[Funbox] {product['name']}"

    app_exchange_products = [
        product for product in excluded_products
        if is_app_exchange_product(product)
    ]

    in_stock = [p for p in target_products if p["status"] == "in_stock"]
    out_of_stock = [p for p in target_products if p["status"] == "out_of_stock"]
    unknown = [p for p in target_products if p["status"] == "unknown"]

    print("=" * 50)
    print(f"Funbox 抓到商品：{len(products)} 個")
    print(f"Funbox 戰鬥陀螺商品：{len(target_products)} 個")
    print(f"Funbox 排除 APP 兌換商品：{len(app_exchange_products)} 個")
    print(f"有貨：{len(in_stock)} 個")
    print(f"無貨：{len(out_of_stock)} 個")
    print(f"未知：{len(unknown)} 個")
    print("=" * 50)

    if app_exchange_products:
        print("\n🚫 已排除 APP 兌換商品")

        for product in app_exchange_products[:30]:
            print(f"- {product.get('name', '')} {product.get('price', '')}")
            print(f"  {product.get('url', '')}")

    if in_stock:
        print("\n✅ Funbox 有貨商品，準備發送 Discord")

        for product in in_stock:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")
            send_restock_alert(product)

    else:
        print("\n目前 Funbox 無現貨，不發 Discord")

    if out_of_stock:
        print("\n❌ Funbox 無貨商品")

        for product in out_of_stock[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")


def main():
    print("🧸 Funbox 陀螺獵人啟動")
    print(f"   掃描網址：{FUNBOX_URL}")
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")
    print(f"   背景模式：{HEADLESS}")

    if "--once" in sys.argv:
        now = datetime.now().strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[{now}] 開始掃描 Funbox...")

        try:
            run_once()
        except Exception as e:
            print(f"[!] Funbox 掃描錯誤：{e}")

        print("\n--once 模式結束")
        return

    while True:
        now = datetime.now().strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[{now}] 開始掃描 Funbox...")

        try:
            run_once()
        except KeyboardInterrupt:
            print("\n已停止 Funbox 監控")
            break
        except Exception as e:
            print(f"[!] Funbox 掃描錯誤：{e}")

        print(f"\n下次掃描：{CHECK_INTERVAL} 秒後")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()