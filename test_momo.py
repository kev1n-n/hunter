import time
import re
import sys

from datetime import datetime

from playwright.sync_api import sync_playwright

from config import CHECK_INTERVAL
from notifier import send_restock_alert


MOMO_URL = (
    "https://www.momoshop.com.tw/search/%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA"
    "?entpCode=TP0002451&_isFuzzy=0"
)

HEADLESS = True

# 搜尋網址已經帶 entpCode=TP0002451
# 所以不再用商品頁文字判斷是不是墊腳石
CHECK_PRODUCT_DETAIL_SELLER = False

# 但要進商品頁確認狀態，避免「即將開賣通知我」被誤判成有貨
CHECK_PRODUCT_DETAIL_STATUS = True

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


def is_momo_beyblade_product(product: dict) -> bool:
    name = product.get("name", "")
    raw_text = product.get("raw_text", "")

    text = f"{name} {raw_text}"
    text_lower = text.lower()
    name_compact = normalize_text(name)

    has_beyblade_word = (
        "beyblade" in text_lower
        or "戰鬥陀螺" in text
        or "战斗陀螺" in text
        or "ベイブレード" in text
    )

    has_product_code = bool(
        re.search(r"\b(?:BX|UX|CX|BXG)-\d+", text, re.IGNORECASE)
    )

    momo_target_words = [
        "發射器",
        "啟動器",
        "握把",
        "改造組",
        "對戰組",
        "隨機強化組",
        "強化組",
        "陀螺",
    ]

    has_target_word = any(word in text for word in momo_target_words)

    # 一般排除只檢查商品名稱，不檢查 raw_text
    # 因為商品頁下方推薦商品可能會污染 raw_text
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

    compact_exclude_keywords = [
        normalize_text(keyword)
        for keyword in exclude_keywords
    ]

    if any(keyword in name_compact for keyword in compact_exclude_keywords):
        product["excluded_reason"] = "排除關鍵字"
        return False

    # 韓版只檢查商品名稱，不檢查 raw_text
    korean_version_keywords = [
        "韓版",
        "韩版",
    ]

    if any(normalize_text(keyword) in name_compact for keyword in korean_version_keywords):
        product["excluded_reason"] = "韓版商品"
        return False

    return has_beyblade_word and (has_product_code or has_target_word)


def clean_momo_name(name: str) -> str:
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
        "即將開賣通知我",
        "即將開賣",
        "開賣通知我",
    ]

    for word in remove_words:
        name = name.replace(word, "").strip()

    name = re.sub(r"\$\s*[\d,]+", "", name).strip()
    name = re.sub(r"NT\$\s*[\d,]+", "", name).strip()
    name = re.sub(r"\s+", " ", name).strip()

    return name


def detect_momo_status_from_text(text: str) -> str:
    compact_text = normalize_text(text)

    out_of_stock_keywords = [
        "即將開賣通知我",
        "即將開賣",
        "尚未開賣",
        "開賣通知我",
        "貨到通知",
        "售完補貨中",
        "已售完",
        "搶購一空",
        "補貨中",
        "暫無供貨",
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


def refresh_momo_status_from_detail(page, product: dict):
    """
    進 momo 商品頁確認狀態。
    只更新狀態，不用商品頁文字判斷是不是墊腳石。
    """
    url = product.get("url", "")

    if not url:
        return

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)

        try:
            page.wait_for_function(
                """
                () => {
                    const body = document.body?.innerText || '';
                    return body.length > 500;
                }
                """,
                timeout=15000,
            )
        except Exception:
            pass

        page.wait_for_timeout(3000)

        body_text = page.inner_text("body")

        # 只看前段，避免下面推薦商品干擾狀態
        top_text = body_text[:8000]

        detail_status = detect_momo_status_from_text(top_text)

        if detail_status != "unknown":
            product["status"] = detail_status

        product["raw_text"] = f"{product.get('raw_text', '')} {top_text}"

    except Exception as e:
        print(f"  [!] momo 商品頁狀態確認失敗：{url}，原因：{e}")


def fetch_momo_products() -> list:
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
            extra_http_headers={
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        page = context.new_page()

        print("正在打開 momo 墊腳石平台...")

        try:
            page.goto(MOMO_URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print(f"[!] momo 開啟失敗：{e}")
            browser.close()
            return []

        try:
            page.wait_for_function(
                """
                () => {
                    const body = document.body?.innerText || '';
                    const links = document.querySelectorAll("a[href*='GoodsDetail']").length;

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
            print("[!] 等待 momo 商品列表逾時，改用目前頁面內容繼續抓")

        page.wait_for_timeout(5000)

        for i in range(8):
            count = page.evaluate(
                """
                () => document.querySelectorAll("a[href*='GoodsDetail']").length
                """
            )

            print(f"momo 滾動 {i + 1}/8，目前商品連結：{count}")

            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1500)

        products = page.evaluate(
            """
            () => {
                const products = new Map();

                function clean(text) {
                    return (text || '').replace(/\\s+/g, ' ').trim();
                }

                function normalizeUrl(href) {
                    const url = new URL(href);

                    const iCode =
                        url.searchParams.get('i_code') ||
                        url.searchParams.get('iCode') ||
                        url.searchParams.get('goodsCode');

                    if (iCode) {
                        return `${url.origin}${url.pathname}?i_code=${iCode}`;
                    }

                    url.search = '';
                    return url.toString();
                }

                function isProductUrl(url) {
                    return (
                        url.includes('momoshop.com.tw') &&
                        (
                            url.includes('/goods/GoodsDetail.jsp') ||
                            url.includes('/goods/GoodsDetail') ||
                            url.includes('GoodsDetail.jsp') ||
                            url.includes('GoodsDetail')
                        )
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
                            text.includes('直接購買') ||
                            text.includes('即將開賣') ||
                            text.includes('開賣通知我') ||
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
                        !anchorText.includes('直接購買')
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
                        /售價\\s*[\\d,]+/,
                    ];

                    for (const pattern of patterns) {
                        const match = text.match(pattern);

                        if (match) {
                            return match[0];
                        }
                    }

                    return '';
                }

                function detectStatus(text) {
                    const compact = clean(text).replace(/\\s+/g, '');

                    if (
                        compact.includes('即將開賣通知我') ||
                        compact.includes('即將開賣') ||
                        compact.includes('尚未開賣') ||
                        compact.includes('開賣通知我') ||
                        compact.includes('貨到通知') ||
                        compact.includes('暫無供貨') ||
                        compact.includes('售完補貨中') ||
                        compact.includes('已售完') ||
                        compact.includes('搶購一空') ||
                        compact.includes('補貨中') ||
                        compact.includes('已下架')
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
                        compact.includes('直接購買') ||
                        compact.includes('立即購買')
                    ) {
                        return 'in_stock';
                    }

                    return 'unknown';
                }

                function isEnabledBuyElement(el) {
                    const text = clean(el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    const className = (el.className || '').toString().toLowerCase();
                    const ariaDisabled = el.getAttribute('aria-disabled');

                    const hasBuyText = (
                        text.includes('加入購物車') ||
                        text.includes('放入購物車') ||
                        text.includes('直接購買') ||
                        text.includes('立即購買')
                    );

                    const disabled = (
                        el.disabled === true ||
                        el.hasAttribute('disabled') ||
                        ariaDisabled === 'true' ||
                        className.includes('disabled') ||
                        className.includes('disable') ||
                        text.includes('即將開賣') ||
                        text.includes('開賣通知我') ||
                        text.includes('售完') ||
                        text.includes('補貨') ||
                        text.includes('暫無供貨')
                    );

                    const visible = Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

                    return hasBuyText && !disabled && visible;
                }

                function getStatus(card) {
                    const text = clean(card.innerText || '');

                    const textStatus = detectStatus(text);

                    if (textStatus !== 'unknown') {
                        return textStatus;
                    }

                    const buttons = [...card.querySelectorAll("button, a, [role='button'], div, span")];

                    if (buttons.some(isEnabledBuyElement)) {
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
                        store: 'momo 墊腳石',
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
            page.screenshot(path="momo_debug.png", full_page=True)
            print("抓到 0 個商品，已產生 momo_debug.png")

        if CHECK_PRODUCT_DETAIL_STATUS:
            print("\n開始進商品頁確認 momo 商品狀態...")

            for index, product in enumerate(products, start=1):
                print(
                    f"  確認狀態 {index}/{len(products)}：{product.get('name', '')[:45]}",
                    flush=True,
                )

                refresh_momo_status_from_detail(page, product)

        browser.close()

    return products


def run_once():
    products = fetch_momo_products()

    target_products = []
    excluded_products = []

    for product in products:
        if is_momo_beyblade_product(product):
            target_products.append(product)
        else:
            excluded_products.append(product)

    for product in target_products:
        # momo 如果狀態抓不到，保守當成無貨，避免誤發 Discord
        if product["status"] == "unknown":
            product["status"] = "out_of_stock"

        product["name"] = clean_momo_name(product["name"])
        product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])
        product["name"] = f"[momo 墊腳石] {product['name']}"

    in_stock = [p for p in target_products if p["status"] == "in_stock"]
    preorder = [p for p in target_products if p["status"] == "preorder"]
    out_of_stock = [p for p in target_products if p["status"] == "out_of_stock"]
    unknown = [p for p in target_products if p["status"] == "unknown"]

    print("=" * 50)
    print(f"momo 抓到商品：{len(products)} 個")
    print(f"momo 戰鬥陀螺商品：{len(target_products)} 個")
    print(f"momo 排除商品：{len(excluded_products)} 個")
    print(f"有貨：{len(in_stock)} 個")
    print(f"預購：{len(preorder)} 個")
    print(f"無貨：{len(out_of_stock)} 個")
    print(f"未知：{len(unknown)} 個")
    print("=" * 50)

    if excluded_products:
        print("\n🚫 momo 已排除商品")

        for product in excluded_products[:30]:
            reason = product.get("excluded_reason", "非目標商品")
            print(f"- {product.get('name', '')}｜{reason}")
            print(f"  {product.get('url', '')}")

    if in_stock:
        print("\n✅ momo 有貨商品，準備發送 Discord")

        for product in in_stock:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")
            send_restock_alert(product)

    else:
        print("\n目前 momo 無現貨，不發 Discord")

    if preorder:
        print("\n📌 momo 預購商品")

        for product in preorder[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")

    if out_of_stock:
        print("\n❌ momo 無貨商品")

        for product in out_of_stock[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")

    if unknown:
        print("\n❓ momo 狀態未知商品")

        for product in unknown[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")


def main():
    print("🛒 momo 墊腳石陀螺獵人啟動")
    print(f"   掃描網址：{MOMO_URL}")
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")
    print(f"   背景模式：{HEADLESS}")

    if "--once" in sys.argv:
        now = datetime.now().strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[momo][{now}] 開始掃描 momo 墊腳石...")

        try:
            run_once()
        except Exception as e:
            print(f"[!] momo 掃描錯誤：{e}")

        print("\n--once 模式結束")
        return

    while True:
        now = datetime.now().strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[momo][{now}] 開始掃描 momo 墊腳石...")

        try:
            run_once()
        except KeyboardInterrupt:
            print("\n已停止 momo 監控")
            break
        except Exception as e:
            print(f"[!] momo 掃描錯誤：{e}")

        print(f"\n下次掃描：{CHECK_INTERVAL} 秒後")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()