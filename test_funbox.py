import time  # 匯入 time，用來讓程式每 60 秒休息一次

from datetime import datetime  # 匯入 datetime，用來顯示目前掃描時間

from playwright.sync_api import sync_playwright  # 匯入 Playwright 同步版，用來開瀏覽器抓網頁

from notifier import send_restock_alert  # 匯入 Discord 有貨通知函式


FUNBOX_URL = "https://shop.funbox.com.tw/categories/XI/KB"  # Funbox 戰鬥陀螺分類頁網址

CHECK_INTERVAL = 60  # 每 60 秒掃描一次

HEADLESS = True  # True 代表背景執行，不跳瀏覽器視窗；想看畫面可以改 False

LABEL_MAP = {  # 商品狀態顯示文字對照表
    "in_stock": "✅ 有貨 / 可加入購物車",  # 有貨狀態
    "out_of_stock": "❌ 目前無庫存",  # 無貨狀態
    "unknown": "❓ 狀態未知",  # 未知狀態
}


def is_beyblade_name(name: str) -> bool:  # 判斷商品名稱是不是戰鬥陀螺
    name_lower = name.lower()  # 把名稱轉小寫，方便比對英文

    keywords = [  # 戰鬥陀螺常見關鍵字
        "beyblade",  # 英文 BEYBLADE
        "戰鬥陀螺",  # 中文戰鬥陀螺
        "bx-",  # BX 系列
        "ux-",  # UX 系列
        "cx-",  # CX 系列
        "bxg-",  # BXG 系列
        "發射器",  # 發射器
        "隨機強化組",  # 隨機強化組
        "對戰組",  # 對戰組
        "改造組",  # 改造組
    ]  # 關鍵字清單結束

    return any(keyword in name_lower for keyword in keywords)  # 命中任一關鍵字就算目標商品


def is_excluded_funbox_product(product: dict) -> bool:  # 判斷 Funbox 商品是不是要排除
    name = product.get("name", "")  # 取得商品名稱
    name_lower = name.lower()  # 把商品名稱轉成小寫
    compact_name = name_lower.replace(" ", "")  # 移除空白，避免 APP 兌換 / APP兌換 差異

    price = product.get("price", "")  # 取得商品價格
    compact_price = price.replace(",", "").replace(" ", "")  # 移除價格逗號與空白

    exclude_name_keywords = [  # 要排除的商品名稱關鍵字
        "app兌換",  # 排除 APP兌換
        "app兌換限定",  # 排除 APP兌換限定
        "預購app兌換",  # 排除 預購APP兌換
    ]  # 排除關鍵字清單結束

    if any(keyword in compact_name for keyword in exclude_name_keywords):  # 如果名稱命中 APP 兌換
        return True  # 排除這個商品

    if "999999" in compact_price:  # 如果價格是 NT$999999，通常是 APP 兌換用假價格
        return True  # 排除這個商品

    return False  # 沒有命中排除條件，就保留


def fetch_funbox_products() -> list:  # 抓 Funbox 商品列表
    with sync_playwright() as p:  # 啟動 Playwright
        browser = p.chromium.launch(headless=HEADLESS)  # 開啟 Chromium 瀏覽器
        context = browser.new_context(  # 建立瀏覽器環境
            locale="zh-TW",  # 設定語系為繁體中文
            viewport={"width": 1280, "height": 1200},  # 設定瀏覽器視窗大小
            user_agent=(  # 設定 User-Agent，模擬一般 Chrome
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()  # 開新分頁

        print("正在打開 Funbox...")  # 印出目前進度
        page.goto(FUNBOX_URL, wait_until="domcontentloaded", timeout=60000)  # 打開 Funbox 分類頁
        page.wait_for_timeout(5000)  # 等 5 秒，讓商品列表渲染完成

        for _ in range(5):  # 往下捲 5 次，避免商品懶載入還沒出現
            page.mouse.wheel(0, 1600)  # 往下捲動
            page.wait_for_timeout(1000)  # 每次捲動後等 1 秒

        products = page.evaluate(  # 在瀏覽器裡執行 JavaScript 抓商品
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

                function countProductLinks(node) {
                    if (!node || !node.querySelectorAll) {
                        return 0;
                    }

                    const urls = [...node.querySelectorAll("a[href*='/products/'], a[href*='/product/']")]
                        .map(a => normalizeUrl(a.href));

                    return new Set(urls).size;
                }

                function findCard(anchor) {
                    let node = anchor;
                    let best = anchor.parentElement || anchor;

                    for (let i = 0; i < 12 && node; i++) {
                        const text = clean(node.innerText || '');
                        const productCount = countProductLinks(node);

                        const looksLikeCard = (
                            text.includes('NT$') ||
                            text.includes('$') ||
                            text.includes('加入購物車') ||
                            text.includes('加入购物车') ||
                            text.includes('立即購買') ||
                            text.includes('立即购买') ||
                            text.includes('售完') ||
                            text.includes('已售完') ||
                            text.includes('缺貨')
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
                            lower.includes('bxg-') ||
                            line.includes('發射器') ||
                            line.includes('隨機強化組')
                        );
                    });

                    return nameLine || anchorText || '未知商品';
                }

                function getPrice(card) {
                    const text = clean(card.innerText || '');
                    const match = text.match(/(?:NT\\$|\\$)\\s*[\\d,]+/);
                    return match ? match[0] : '';
                }

                function isBuyButton(el) {
                    const text = clean(el.innerText || el.textContent || '').replace(/\\s+/g, '');
                    const className = (el.className || '').toString().toLowerCase();
                    const ariaDisabled = el.getAttribute('aria-disabled');

                    const hasBuyText = (
                        text.includes('加入購物車') ||
                        text.includes('加入购物车') ||
                        text.includes('立即購買') ||
                        text.includes('立即购买') ||
                        text.includes('加入購物袋')
                    );

                    const disabled = (
                        el.disabled === true ||
                        el.hasAttribute('disabled') ||
                        ariaDisabled === 'true' ||
                        className.includes('disabled') ||
                        className.includes('disable') ||
                        text.includes('售完') ||
                        text.includes('已售完') ||
                        text.includes('缺貨')
                    );

                    const visible = Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

                    return hasBuyText && !disabled && visible;
                }

                function getStatus(card) {
                    const text = clean(card.innerText || '');

                    if (
                        text.includes('售完') ||
                        text.includes('已售完') ||
                        text.includes('缺貨') ||
                        text.includes('補貨中')
                    ) {
                        return 'out_of_stock';
                    }

                    const buttons = [...card.querySelectorAll("button, a, [role='button'], div, span")];

                    if (buttons.some(isBuyButton)) {
                        return 'in_stock';
                    }

                    return 'unknown';
                }

                const anchors = [...document.querySelectorAll("a[href*='/products/'], a[href*='/product/']")];

                for (const anchor of anchors) {
                    const url = normalizeUrl(anchor.href);

                    if (!url.includes('shop.funbox.com.tw')) {
                        continue;
                    }

                    const card = findCard(anchor);
                    const name = getName(card, anchor);
                    const price = getPrice(card);
                    const status = getStatus(card);

                    products.set(url, {
                        store: 'Funbox',
                        name,
                        url,
                        price,
                        status,
                        status_label: '',
                    });
                }

                return [...products.values()];
            }
            """
        )

        if not products:  # 如果完全抓不到商品
            page.screenshot(path="funbox_debug.png", full_page=True)  # 存一張截圖方便 debug
            print("抓到 0 個商品，已產生 funbox_debug.png")  # 告訴使用者有截圖

        browser.close()  # 關閉瀏覽器

    return products  # 回傳商品列表


def run_once():  # 執行一次 Funbox 掃描
    products = fetch_funbox_products()  # 抓 Funbox 商品

    beyblade_products = [  # 過濾出戰鬥陀螺商品
        product for product in products  # 逐一檢查商品
        if is_beyblade_name(product["name"]) and not is_excluded_funbox_product(product)  # 是陀螺，而且不是 APP 兌換商品才保留
    ]

    excluded_products = [  # 抓出被排除的商品，方便 debug
        product for product in products  # 逐一檢查商品
        if is_beyblade_name(product["name"]) and is_excluded_funbox_product(product)  # 是陀螺，但被判斷為 APP 兌換
    ]

    for product in beyblade_products:  # 逐一補上 Discord 要用的狀態文字
        product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])  # 補 status_label

    in_stock = [p for p in beyblade_products if p["status"] == "in_stock"]  # 有貨商品
    out_of_stock = [p for p in beyblade_products if p["status"] == "out_of_stock"]  # 無貨商品
    unknown = [p for p in beyblade_products if p["status"] == "unknown"]  # 狀態未知商品

    print("=" * 50)  # 印分隔線
    print(f"Funbox 抓到商品：{len(products)} 個")  # 印出總商品數
    print(f"Funbox 戰鬥陀螺商品：{len(beyblade_products)} 個")  # 印出戰鬥陀螺商品數
    print(f"Funbox 排除 APP 兌換商品：{len(excluded_products)} 個")  # 印出被排除數量
    print(f"有貨：{len(in_stock)} 個")  # 印出有貨數
    print(f"無貨：{len(out_of_stock)} 個")  # 印出無貨數
    print(f"未知：{len(unknown)} 個")  # 印出未知數
    print("=" * 50)  # 印分隔線

    if excluded_products:  # 如果有被排除的 APP 兌換商品
        print("\n🚫 已排除 APP 兌換商品")  # 印標題

        for product in excluded_products:  # 逐一印出被排除商品
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格
            print(f"  {product['url']}")  # 印商品連結

    if in_stock:  # 如果有真正現貨
        print("\n✅ 有貨商品，準備發送 Discord")  # 印標題

        for product in in_stock:  # 逐一印出並通知有貨商品
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格
            print(f"  {product['url']}")  # 印商品連結
            send_restock_alert(product)  # 發送 Discord 通知

    else:  # 如果沒有真正現貨
        print("\n目前 Funbox 無現貨，不發 Discord")  # 只在 Terminal 顯示

    if out_of_stock:  # 如果有無貨商品
        print("\n❌ 無貨商品")  # 印標題

        for product in out_of_stock[:20]:  # 最多印前 20 個，避免太長
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格

    if unknown:  # 如果有未知狀態商品
        print("\n❓ 狀態未知商品")  # 印標題

        for product in unknown[:20]:  # 最多印前 20 個
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格
            print(f"  {product['url']}")  # 印商品連結


def main():  # 主程式，讓 Funbox 持續掃描
    print("🧸 Funbox 陀螺獵人啟動")  # 印出啟動訊息
    print(f"   掃描網址：{FUNBOX_URL}")  # 印出掃描網址
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")  # 印出掃描間隔
    print(f"   背景模式：{HEADLESS}")  # 印出是否背景模式

    while True:  # 一直重複掃描
        now = datetime.now().strftime("%H:%M:%S")  # 取得目前時間

        print(f"\n{'=' * 50}")  # 印出分隔線
        print(f"[{now}] 開始掃描 Funbox...")  # 印出開始掃描時間

        try:  # 避免單次錯誤讓程式整個停止
            run_once()  # 執行一次掃描
        except KeyboardInterrupt:  # 如果使用者按 Ctrl + C
            print("\n已停止 Funbox 監控")  # 印出停止訊息
            break  # 跳出 while 迴圈
        except Exception as e:  # 如果發生其他錯誤
            print(f"[!] Funbox 掃描錯誤：{e}")  # 印出錯誤但不中斷

        print(f"\n下次掃描：{CHECK_INTERVAL} 秒後")  # 印出下次掃描時間
        time.sleep(CHECK_INTERVAL)  # 等待指定秒數再掃描


if __name__ == "__main__":  # 如果直接執行這個檔案
    main()  # 執行主程式