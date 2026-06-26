import time
import re
import sys
import subprocess

from datetime import datetime

from playwright.sync_api import sync_playwright

from notifier import send_restock_alert


BASE_TAKARA_URLS = [
    "https://takaratomymall.jp/shop/goods/search.aspx?search=x&keyword=BEYBLADE+X&wovn=ja",
    "https://takaratomymall.jp/shop/goods/search.aspx?search=x&keyword=BEYBLADE+X",
]

FALLBACK_IN_STOCK_URLS = [
    "https://takaratomymall.jp/shop/goods/search.aspx?stock_on_sales=0&keyword=BEYBLADE+X&min_price=&max_price=&search=x&wovn=ja",
    "https://takaratomymall.jp/shop/goods/search.aspx?stock_on_sales=0&keyword=BEYBLADE+X&min_price=&max_price=&search=x",
]

CHECK_INTERVAL = 60

# TAKARA 這站 headless=True 容易 timeout
# 所以保持 False，但啟動後會嘗試把 Chromium 隱藏起來
HEADLESS = False

# 視窗大小
BROWSER_WIDTH = 500
BROWSER_HEIGHT = 400

# 把視窗丟到螢幕外面，不干擾你操作電腦
BROWSER_X = -2000
BROWSER_Y = 100

LABEL_MAP = {
    "in_stock": "✅ 有貨 / 可加入購物車",
    "out_of_stock": "❌ 目前無庫存",
    "preorder": "📌 預購中",
    "unknown": "❓ 狀態未知",
}


def hide_chromium_window():
    """
    macOS 專用：
    TAKARA 不能用真正 headless，
    所以讓 Chromium 開起來後立刻隱藏視窗。

    如果 macOS 跳權限，允許 Terminal / iTerm 控制 System Events。
    """
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

        print("已嘗試隱藏 Chromium 視窗")

    except Exception as e:
        print(f"[!] 隱藏 Chromium 視窗失敗：{e}")


def normalize_text(text: str) -> str:
    return (
        text.lower()
        .replace(" ", "")
        .replace("　", "")
        .replace("\n", "")
        .replace("\t", "")
        .strip()
    )


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
        re.search(r"\b(?:BX|UX|CX|BXG)-\d+", text, re.IGNORECASE)
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

        # 明顯不是實體商品
        "ダウンロード",
        "壁紙",
    ]

    compact_exclude_keywords = [
        normalize_text(keyword)
        for keyword in exclude_keywords
    ]

    return any(keyword in compact_text for keyword in compact_exclude_keywords)


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
        "SOLD OUT",
        "SOLDOUT",
    ]

    for word in remove_words:
        name = name.replace(word, "").strip()

    name = re.sub(r"￥\s*[\d,]+", "", name).strip()
    name = re.sub(r"¥\s*[\d,]+", "", name).strip()
    name = re.sub(r"\s+", " ", name).strip()

    return name


def open_takara_base_page(page) -> bool:
    loaded = False

    for url in BASE_TAKARA_URLS:
        try:
            print(f"嘗試網址：{url}")

            # TAKARA 不要等完整 load，會很容易卡住
            page.goto(url, wait_until="domcontentloaded", timeout=90000)

            # 再隱藏一次，避免頁面開啟後視窗又跳回來
            hide_chromium_window()

            # 給頁面時間慢慢渲染
            page.wait_for_timeout(10000)

            loaded = True
            break

        except Exception as e:
            print(f"  [!] TAKARA 開啟失敗：{e}")
            page.wait_for_timeout(3000)

    return loaded


def apply_in_stock_filter(page) -> bool:
    """
    流程：
    1. 找「販売中商品」那個框框
    2. 確認它有勾選
    3. 點「販売中商品」區塊裡的「在庫あり」
    4. 點「絞り込む」
    """
    print("準備設定 TAKARA 篩選條件...")

    try:
        page.get_by_text("詳細検索").first.scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(1000)
    except Exception:
        pass

    print("確認「販売中商品」框框...")

    sales_checked = False

    try:
        sales_checked = page.evaluate(
            """
            () => {
                function clean(text) {
                    return (text || '').replace(/\\s+/g, '').trim();
                }

                function getInputFromLabel(label) {
                    const inputInside = label.querySelector('input');

                    if (inputInside) {
                        return inputInside;
                    }

                    const forId = label.getAttribute('for');

                    if (forId) {
                        return document.getElementById(forId);
                    }

                    return null;
                }

                const labels = [...document.querySelectorAll('label')];

                const salesLabel = labels.find(label => {
                    const text = clean(label.innerText || label.textContent || '');
                    return text.includes('販売中商品');
                });

                if (salesLabel) {
                    const input = getInputFromLabel(salesLabel);

                    if (input && input.type === 'checkbox') {
                        if (!input.checked) {
                            salesLabel.click();
                        }

                        return true;
                    }

                    salesLabel.click();
                    return true;
                }

                const nodes = [...document.querySelectorAll('div, span, p, td, th')];

                const salesNode = nodes.find(node => {
                    const text = clean(node.innerText || node.textContent || '');
                    return text.includes('販売中商品');
                });

                if (!salesNode) {
                    return false;
                }

                let container = salesNode;

                for (let i = 0; i < 5 && container; i++) {
                    const checkbox = container.querySelector("input[type='checkbox']");

                    if (checkbox) {
                        if (!checkbox.checked) {
                            checkbox.click();
                        }

                        return true;
                    }

                    container = container.parentElement;
                }

                return false;
            }
            """
        )

        if sales_checked:
            print("已確認「販売中商品」框框有勾選")
        else:
            print("  [!] 找不到「販売中商品」框框")

    except Exception as e:
        print(f"  [!] 確認販売中商品失敗：{e}")

    if not sales_checked:
        return False

    page.wait_for_timeout(1000)

    print("準備點選「販売中商品」區塊裡的「在庫あり」...")

    clicked_in_stock = False

    try:
        clicked_in_stock = page.evaluate(
            """
            () => {
                function clean(text) {
                    return (text || '').replace(/\\s+/g, '').trim();
                }

                function getInputFromLabel(label) {
                    const inputInside = label.querySelector('input');

                    if (inputInside) {
                        return inputInside;
                    }

                    const forId = label.getAttribute('for');

                    if (forId) {
                        return document.getElementById(forId);
                    }

                    return null;
                }

                const labels = [...document.querySelectorAll('label')];

                const salesIndex = labels.findIndex(label => {
                    const text = clean(label.innerText || label.textContent || '');
                    return text.includes('販売中商品');
                });

                if (salesIndex === -1) {
                    return false;
                }

                let reserveIndex = labels.findIndex((label, index) => {
                    if (index <= salesIndex) {
                        return false;
                    }

                    const text = clean(label.innerText || label.textContent || '');
                    return text.includes('予約商品');
                });

                if (reserveIndex === -1) {
                    reserveIndex = labels.length;
                }

                const salesAreaLabels = labels.slice(salesIndex, reserveIndex);

                const inStockLabel = salesAreaLabels.find(label => {
                    const text = clean(label.innerText || label.textContent || '');
                    return text === '在庫あり' || text.includes('在庫あり');
                });

                if (!inStockLabel) {
                    return false;
                }

                const input = getInputFromLabel(inStockLabel);

                if (input && (input.type === 'radio' || input.type === 'checkbox')) {
                    if (!input.checked) {
                        inStockLabel.click();
                    }

                    return true;
                }

                inStockLabel.click();
                return true;
            }
            """
        )

        if clicked_in_stock:
            print("已點選「販売中商品」區塊裡的「在庫あり」")
        else:
            print("  [!] 找不到販売中商品區塊裡的在庫あり")

    except Exception as e:
        print(f"  [!] 點選在庫あり失敗：{e}")

    if not clicked_in_stock:
        return False

    page.wait_for_timeout(1000)

    print("準備點選「絞り込む」...")

    clicked_filter = False

    try:
        clicked_filter = page.evaluate(
            """
            () => {
                function clean(text) {
                    return (text || '').replace(/\\s+/g, '').trim();
                }

                const candidates = [
                    ...document.querySelectorAll("button"),
                    ...document.querySelectorAll("input[type='submit']"),
                    ...document.querySelectorAll("input[type='button']"),
                    ...document.querySelectorAll("a"),
                    ...document.querySelectorAll("[role='button']")
                ];

                const target = candidates.find(el => {
                    const text = clean(el.innerText || el.textContent || el.value || '');
                    return text.includes('絞り込む');
                });

                if (!target) {
                    return false;
                }

                target.click();
                return true;
            }
            """
        )

        if clicked_filter:
            print("已點選「絞り込む」")
        else:
            print("  [!] 找不到絞り込む按鈕")

    except Exception as e:
        print(f"  [!] 點選絞り込む失敗：{e}")

    if not clicked_filter:
        return False

    hide_chromium_window()

    try:
        page.wait_for_load_state("domcontentloaded", timeout=60000)
    except Exception:
        pass

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    page.wait_for_timeout(6000)

    print(f"篩選後網址：{page.url}")

    return True


def open_fallback_in_stock_page(page) -> bool:
    print("自動點擊篩選失敗，改用備用在庫あり網址...")

    for url in FALLBACK_IN_STOCK_URLS:
        try:
            print(f"嘗試備用網址：{url}")

            page.goto(url, wait_until="domcontentloaded", timeout=90000)

            hide_chromium_window()

            page.wait_for_timeout(10000)

            return True

        except Exception as e:
            print(f"  [!] 備用在庫あり網址失敗：{e}")
            page.wait_for_timeout(3000)

    return False


def fetch_takara_products() -> list:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-http2",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",

                # 視窗仍然存在，但會開在螢幕外面
                f"--window-size={BROWSER_WIDTH},{BROWSER_HEIGHT}",
                f"--window-position={BROWSER_X},{BROWSER_Y}",
            ],
        )

        hide_chromium_window()

        context = browser.new_context(
            locale="ja-JP",
            viewport={"width": BROWSER_WIDTH, "height": BROWSER_HEIGHT},
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
                "Upgrade-Insecure-Requests": "1",
            },
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        hide_chromium_window()

        print("正在打開 TAKARA TOMY MALL...")

        loaded = open_takara_base_page(page)

        if not loaded:
            print("TAKARA TOMY MALL 一般搜尋頁無法連線，跳過這次掃描")
            browser.close()
            return []

        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""

        if (
            "Queue-it" in body_text
            or "waiting room" in body_text.lower()
            or "アクセスが集中" in body_text
            or "しばらくお待ちください" in body_text
        ):
            page.screenshot(path="takara_queue_or_blocked.png", full_page=True)
            print("疑似被 TAKARA TOMY MALL 排隊頁或防護頁擋住，已產生 takara_queue_or_blocked.png")
            browser.close()
            return []

        filter_ok = apply_in_stock_filter(page)

        if not filter_ok:
            fallback_ok = open_fallback_in_stock_page(page)

            if not fallback_ok:
                print("TAKARA TOMY MALL 無法切到在庫あり結果，跳過這次掃描")
                browser.close()
                return []

        hide_chromium_window()

        for _ in range(6):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(1000)

        products = page.evaluate(
            """
            () => {
                const products = new Map();

                function clean(text) {
                    return (text || '').replace(/\\s+/g, ' ').trim();
                }

                function normalizeUrl(href) {
                    const url = new URL(href);
                    url.search = '';
                    return url.toString();
                }

                function isProductUrl(url) {
                    return (
                        url.includes('takaratomymall.jp') &&
                        (
                            url.includes('/shop/g/g') ||
                            url.includes('/shop/goods/') ||
                            url.includes('goods.aspx') ||
                            url.includes('goods_detail')
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

                    for (let i = 0; i < 16 && node; i++) {
                        const text = clean(node.innerText || '');
                        const productCount = countProductLinks(node);

                        const looksLikeCard = (
                            text.includes('￥') ||
                            text.includes('¥') ||
                            text.includes('カートに入れる') ||
                            text.includes('買い物かごに入れる') ||
                            text.includes('予約') ||
                            text.includes('在庫あり') ||
                            text.includes('在庫なし') ||
                            text.includes('品切れ') ||
                            text.includes('販売終了') ||
                            text.includes('SOLD OUT') ||
                            text.includes('SOLDOUT') ||
                            text.includes('BEYBLADE X') ||
                            text.includes('ベイブレード')
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
                    const anchorText = clean(anchor.innerText || anchor.textContent || '');

                    if (
                        anchorText.length >= 4 &&
                        !anchorText.includes('カートに入れる') &&
                        !anchorText.includes('買い物かごに入れる') &&
                        !anchorText.includes('在庫なし') &&
                        !anchorText.includes('品切れ')
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
                            lower.includes('beyblade x') ||
                            line.includes('ベイブレードX') ||
                            line.includes('ベイブレード X') ||
                            lower.includes('bx-') ||
                            lower.includes('ux-') ||
                            lower.includes('cx-') ||
                            lower.includes('bxg-')
                        );
                    });

                    return nameLine || anchorText || '未知商品';
                }

                function getPrice(card) {
                    const text = clean(card.innerText || '');
                    const match = text.match(/[￥¥]\\s*[\\d,]+/);
                    return match ? match[0] : '';
                }

                function isBuyButton(el) {
                    const text = clean(el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    const className = (el.className || '').toString().toLowerCase();
                    const ariaDisabled = el.getAttribute('aria-disabled');

                    const hasBuyText = (
                        text.includes('カートに入れる') ||
                        text.includes('買い物かごに入れる') ||
                        text.includes('購入する')
                    );

                    const disabled = (
                        el.disabled === true ||
                        el.hasAttribute('disabled') ||
                        ariaDisabled === 'true' ||
                        className.includes('disabled') ||
                        className.includes('disable') ||
                        text.includes('在庫なし') ||
                        text.includes('品切れ') ||
                        text.includes('販売終了') ||
                        text.includes('SOLDOUT') ||
                        text.includes('SOLD OUT')
                    );

                    const visible = Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

                    return hasBuyText && !disabled && visible;
                }

                function getStatus(card) {
                    const text = clean(card.innerText || '');

                    if (
                        text.includes('在庫なし') ||
                        text.includes('品切れ') ||
                        text.includes('販売終了') ||
                        text.includes('SOLD OUT') ||
                        text.includes('SOLDOUT')
                    ) {
                        return 'out_of_stock';
                    }

                    if (text.includes('予約')) {
                        return 'preorder';
                    }

                    const buttons = [...card.querySelectorAll("button, a, [role='button'], div, span, input")];

                    if (buttons.some(isBuyButton)) {
                        return 'in_stock';
                    }

                    return 'in_stock';
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
                        store: 'TAKARA TOMY MALL',
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
            page.screenshot(path="takara_debug.png", full_page=True)
            print("抓到 0 個商品，已產生 takara_debug.png")

        browser.close()

    return products


def run_once():
    products = fetch_takara_products()

    normal_beyblade_products = [
        product for product in products
        if is_normal_takara_beyblade_product(product)
        and not is_excluded_takara_product(product)
    ]

    excluded_products = [
        product for product in products
        if is_excluded_takara_product(product)
    ]

    not_target_products = [
        product for product in products
        if not is_normal_takara_beyblade_product(product)
        and not is_excluded_takara_product(product)
    ]

    for product in normal_beyblade_products:
        product["name"] = clean_takara_name(product["name"])
        product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])
        product["name"] = f"[TAKARA TOMY MALL] {product['name']}"

    in_stock = [p for p in normal_beyblade_products if p["status"] == "in_stock"]
    preorder = [p for p in normal_beyblade_products if p["status"] == "preorder"]
    out_of_stock = [p for p in normal_beyblade_products if p["status"] == "out_of_stock"]
    unknown = [p for p in normal_beyblade_products if p["status"] == "unknown"]

    print("=" * 50)
    print(f"TAKARA 抓到商品：{len(products)} 個")
    print(f"TAKARA 正常陀螺商品：{len(normal_beyblade_products)} 個")
    print(f"TAKARA 排除商品：{len(excluded_products)} 個")
    print(f"TAKARA 非目標商品：{len(not_target_products)} 個")
    print(f"有貨：{len(in_stock)} 個")
    print(f"預購：{len(preorder)} 個")
    print(f"無貨：{len(out_of_stock)} 個")
    print(f"未知：{len(unknown)} 個")
    print("=" * 50)

    if excluded_products:
        print("\n🚫 已排除商品")

        for product in excluded_products[:30]:
            print(f"- {product.get('name', '')} {product.get('price', '')}")
            print(f"  {product.get('url', '')}")

    if not_target_products:
        print("\n⚪ 非目標商品")

        for product in not_target_products[:20]:
            print(f"- {product.get('name', '')} {product.get('price', '')}")
            print(f"  {product.get('url', '')}")

    if in_stock:
        print("\n✅ TAKARA 有貨商品，準備發送 Discord")

        for product in in_stock:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")
            send_restock_alert(product)

    else:
        print("\n目前 TAKARA 無現貨，不發 Discord")

    if preorder:
        print("\n📌 預購商品")

        for product in preorder[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")

    if out_of_stock:
        print("\n❌ 無貨商品")

        for product in out_of_stock[:30]:
            print(f"- {product['name']} {product.get('price', '')}")

    if unknown:
        print("\n❓ 狀態未知商品")

        for product in unknown[:30]:
            print(f"- {product['name']} {product.get('price', '')}")
            print(f"  {product['url']}")


def main():
    print("🇯🇵 TAKARA TOMY MALL 陀螺獵人啟動")
    print(f"   掃描網址：{BASE_TAKARA_URLS[0]}")
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")
    print(f"   背景模式：{HEADLESS}")
    print(f"   視窗大小：{BROWSER_WIDTH} x {BROWSER_HEIGHT}")
    print(f"   視窗位置：{BROWSER_X}, {BROWSER_Y}")

    if "--once" in sys.argv:
        now = datetime.now().strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[{now}] 開始掃描 TAKARA...")

        try:
            run_once()
        except Exception as e:
            print(f"[!] TAKARA 掃描錯誤：{e}")

        print("\n--once 模式結束")
        return

    while True:
        now = datetime.now().strftime("%H:%M:%S")

        print(f"\n{'=' * 50}")
        print(f"[{now}] 開始掃描 TAKARA...")

        try:
            run_once()
        except KeyboardInterrupt:
            print("\n已停止 TAKARA 監控")
            break
        except Exception as e:
            print(f"[!] TAKARA 掃描錯誤：{e}")

        print(f"\n下次掃描：{CHECK_INTERVAL} 秒後")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()