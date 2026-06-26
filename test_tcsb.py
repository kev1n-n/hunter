import time  # 匯入 time，用來讓程式每 60 秒休息一次
import re  # 匯入 re，用來清理商品名稱裡的價格文字

from datetime import datetime  # 匯入 datetime，用來顯示目前掃描時間

from playwright.sync_api import sync_playwright  # 匯入 Playwright 同步版，用來開瀏覽器抓網頁

from notifier import send_restock_alert  # 匯入 Discord 有貨通知函式


TCSB_URL = 'https://www.tcsb.com.tw/v2/Search?q=%22BEYBLADEX%E6%88%B0%E9%AC%A5%E9%99%80%E8%9E%BA%22&shopId=32014'  # 墊腳石搜尋頁

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
        "蒼龍",  # 蒼龍系列
        "魔導",  # 魔導系列
        "鳳凰",  # 鳳凰系列
    ]  # 關鍵字清單結束

    return any(keyword in name_lower for keyword in keywords)  # 命中任一關鍵字就算目標商品


def is_excluded_tcsb_product(product: dict) -> bool:  # 判斷墊腳石商品是不是要排除
    name = product.get("name", "")  # 取得商品名稱
    name_lower = name.lower()  # 商品名稱轉小寫
    compact_name = name_lower.replace(" ", "")  # 移除空白方便比對

    exclude_keywords = [  # 排除關鍵字
        "電子書",  # 排除電子書
        "ebook",  # 排除 ebook
        "e-book",  # 排除 e-book
    ]  # 排除清單結束

    return any(keyword in compact_name for keyword in exclude_keywords)  # 命中排除關鍵字就排除


def clean_tcsb_name(name: str) -> str:  # 清理墊腳石商品名稱
    name = name.replace("貨到通知", "").strip()  # 移除貨到通知文字
    name = re.sub(r"NT\$\s*[\d,]+", "", name).strip()  # 移除 NT$ 價格
    name = re.sub(r"\$\s*[\d,]+", "", name).strip()  # 移除 $ 價格
    name = re.sub(r"\s+", " ", name).strip()  # 移除多餘空白
    return name  # 回傳清理後名稱


def fetch_tcsb_products() -> list:  # 抓墊腳石商品列表
    with sync_playwright() as p:  # 啟動 Playwright
        browser = p.chromium.launch(headless=HEADLESS)  # 開啟 Chromium 瀏覽器
        context = browser.new_context(  # 建立瀏覽器環境
            locale="zh-TW",  # 設定語系為繁中
            viewport={"width": 1280, "height": 1200},  # 設定瀏覽器大小
            user_agent=(  # 模擬一般 Chrome
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()  # 開啟新分頁

        print("正在打開墊腳石...")  # 印出目前進度
        page.goto(TCSB_URL, wait_until="domcontentloaded", timeout=60000)  # 打開墊腳石搜尋頁
        page.wait_for_timeout(6000)  # 等 6 秒讓商品渲染

        for _ in range(6):  # 往下捲幾次，處理懶載入
            page.mouse.wheel(0, 1600)  # 往下捲動
            page.wait_for_timeout(1000)  # 每次等 1 秒

        products = page.evaluate(  # 在瀏覽器內執行 JavaScript 抓商品
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
                        url.includes('tcsb.com.tw') &&
                        (
                            url.includes('SalePage') ||
                            url.includes('salepage') ||
                            url.includes('/SalePage/') ||
                            url.includes('/Product/') ||
                            url.includes('/product/')
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
                            text.includes('NT$') ||
                            text.includes('$') ||
                            text.includes('加入購物車') ||
                            text.includes('加入购物车') ||
                            text.includes('立即購買') ||
                            text.includes('立即购买') ||
                            text.includes('放入購物車') ||
                            text.includes('貨到通知') ||
                            text.includes('售完') ||
                            text.includes('已售完') ||
                            text.includes('缺貨') ||
                            text.includes('補貨中')
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
                        !anchorText.includes('加入購物車') &&
                        !anchorText.includes('立即購買')
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
                            line.includes('發射器') ||
                            line.includes('隨機強化組') ||
                            line.includes('對戰組') ||
                            line.includes('改造組')
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
                        text.includes('放入購物車')
                    );

                    const disabled = (
                        el.disabled === true ||
                        el.hasAttribute('disabled') ||
                        ariaDisabled === 'true' ||
                        className.includes('disabled') ||
                        className.includes('disable') ||
                        text.includes('貨到通知') ||
                        text.includes('售完') ||
                        text.includes('已售完') ||
                        text.includes('缺貨') ||
                        text.includes('補貨中')
                    );

                    const visible = Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

                    return hasBuyText && !disabled && visible;
                }

                function getStatus(card) {
                    const text = clean(card.innerText || '');

                    if (
                        text.includes('貨到通知') ||
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

                const anchors = [...document.querySelectorAll('a[href]')]
                    .filter(a => isProductUrl(a.href));

                for (const anchor of anchors) {
                    const url = normalizeUrl(anchor.href);

                    const card = findCard(anchor);
                    const name = getName(card, anchor);
                    const price = getPrice(card);
                    const status = getStatus(card);

                    products.set(url, {
                        store: '墊腳石',
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
            page.screenshot(path="tcsb_debug.png", full_page=True)  # 存截圖方便 debug
            print("抓到 0 個商品，已產生 tcsb_debug.png")  # 印出 debug 提醒

        browser.close()  # 關閉瀏覽器

    return products  # 回傳商品列表


def run_once():  # 執行一次墊腳石掃描
    products = fetch_tcsb_products()  # 抓墊腳石商品

    beyblade_products = [  # 過濾出戰鬥陀螺商品
        product for product in products  # 逐一檢查商品
        if is_beyblade_name(product["name"]) and not is_excluded_tcsb_product(product)  # 是陀螺且不是排除品
    ]

    excluded_products = [  # 抓出被排除商品方便 debug
        product for product in products  # 逐一檢查商品
        if is_beyblade_name(product["name"]) and is_excluded_tcsb_product(product)  # 是陀螺但被排除
    ]

    for product in beyblade_products:  # 補 Discord 需要的欄位
        product["name"] = clean_tcsb_name(product["name"])  # 清理商品名稱
        product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])  # 補上狀態文字
        product["name"] = f"[墊腳石] {product['name']}"  # 在商品名稱前加店家，Discord 比較好辨識

    in_stock = [p for p in beyblade_products if p["status"] == "in_stock"]  # 有貨商品
    out_of_stock = [p for p in beyblade_products if p["status"] == "out_of_stock"]  # 無貨商品
    unknown = [p for p in beyblade_products if p["status"] == "unknown"]  # 未知商品

    print("=" * 50)  # 印分隔線
    print(f"墊腳石抓到商品：{len(products)} 個")  # 印出總商品數
    print(f"墊腳石戰鬥陀螺商品：{len(beyblade_products)} 個")  # 印出目標商品數
    print(f"墊腳石排除商品：{len(excluded_products)} 個")  # 印出排除商品數
    print(f"有貨：{len(in_stock)} 個")  # 印出有貨數
    print(f"無貨：{len(out_of_stock)} 個")  # 印出無貨數
    print(f"未知：{len(unknown)} 個")  # 印出未知數
    print("=" * 50)  # 印分隔線

    if excluded_products:  # 如果有排除商品
        print("\n🚫 已排除商品")  # 印標題

        for product in excluded_products:  # 逐一印出排除商品
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格
            print(f"  {product['url']}")  # 印商品連結

    if in_stock:  # 如果有現貨
        print("\n✅ 墊腳石有貨商品，準備發送 Discord")  # 印標題

        for product in in_stock:  # 逐一通知
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格
            print(f"  {product['url']}")  # 印商品連結
            send_restock_alert(product)  # 發送 Discord 通知

    else:  # 如果沒有現貨
        print("\n目前墊腳石無現貨，不發 Discord")  # 只在 Terminal 顯示

    if out_of_stock:  # 如果有無貨商品
        print("\n❌ 無貨商品")  # 印標題

        for product in out_of_stock[:20]:  # 最多印 20 個
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格

    if unknown:  # 如果有未知商品
        print("\n❓ 狀態未知商品")  # 印標題

        for product in unknown[:20]:  # 最多印 20 個
            print(f"- {product['name']} {product.get('price', '')}")  # 印商品名稱與價格
            print(f"  {product['url']}")  # 印商品連結


def main():  # 主程式，讓墊腳石持續掃描
    print("📚 墊腳石陀螺獵人啟動")  # 印出啟動訊息
    print(f"   掃描網址：{TCSB_URL}")  # 印出掃描網址
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")  # 印出掃描間隔
    print(f"   背景模式：{HEADLESS}")  # 印出背景模式

    while True:  # 一直重複掃描
        now = datetime.now().strftime("%H:%M:%S")  # 取得目前時間

        print(f"\n{'=' * 50}")  # 印出分隔線
        print(f"[{now}] 開始掃描墊腳石...")  # 印出開始掃描時間

        try:  # 避免單次錯誤中斷整個程式
            run_once()  # 執行一次掃描
        except KeyboardInterrupt:  # 如果按 Ctrl + C
            print("\n已停止墊腳石監控")  # 印出停止
            break  # 結束迴圈
        except Exception as e:  # 如果發生其他錯誤
            print(f"[!] 墊腳石掃描錯誤：{e}")  # 印出錯誤但不中斷

        print(f"\n下次掃描：{CHECK_INTERVAL} 秒後")  # 印出下次掃描時間
        time.sleep(CHECK_INTERVAL)  # 等 60 秒再掃


if __name__ == "__main__":  # 如果直接執行這個檔案
    main()  # 執行主程式