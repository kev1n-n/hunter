#!/usr/bin/env python3  # 使用 python3 執行這個檔案
"""main.py - 陀螺獵人（誠品列表偵測版）"""  # 檔案說明

import json  # 匯入 json，用來讀寫狀態檔
import os  # 匯入 os，用來處理檔案與資料夾
import sys  # 匯入 sys，用來讀取命令列參數
import time  # 匯入 time，用來 sleep
import re  # 匯入 re，用來抓「共 73 筆」這種文字
import requests  # 匯入 requests，用來直接發 Discord Webhook
import schedule  # 匯入 schedule，用來定時掃描
from datetime import datetime, timezone, timedelta  # 匯入 datetime 與時區工具
from urllib.parse import quote  # 匯入 quote，用來把中文關鍵字轉成網址格式

from playwright.sync_api import sync_playwright  # 匯入 Playwright 同步 API

from config import CHECK_INTERVAL, SEEN_DB, DISCORD_WEBHOOK_URL  # 從 config.py 匯入設定
from notifier import send_restock_alert  # 從 notifier.py 匯入 Discord 到貨通知函式


KEYWORD = "BEYBLADE X戰鬥陀螺"  # 誠品搜尋關鍵字

TEST_INCLUDE_EBOOK = False  # 正式監控關閉電子書測試，避免電子書一直通知

TAIWAN_TZ = timezone(timedelta(hours=8))  # 台灣時區 UTC+8

PROGRAM_STARTED_AT = datetime.now(TAIWAN_TZ)  # 記錄這次程式啟動時間，使用台灣時間

IS_FIRST_SCAN = True  # 標記這是不是程式啟動後第一次掃描

LABEL_MAP = {  # 商品狀態顯示文字對照表
    "in_stock": "✅ 有貨 / 可加入購物車",  # in_stock 代表可以加入購物車
    "out_of_stock": "❌ 目前無庫存",  # out_of_stock 代表目前不能買
    "preorder": "📌 預購中",  # preorder 代表預購中
    "unknown": "❓ 狀態未知",  # unknown 代表無法判斷
}


def format_taiwan_time(dt: datetime) -> str:  # 把時間格式改成中文顯示
    hour = dt.hour  # 取得小時

    if 0 <= hour < 6:  # 0 點到 5 點
        period = "凌晨"  # 顯示凌晨
    elif 6 <= hour < 12:  # 6 點到 11 點
        period = "早上"  # 顯示早上
    elif 12 <= hour < 18:  # 12 點到 17 點
        period = "下午"  # 顯示下午
    else:  # 18 點到 23 點
        period = "晚上"  # 顯示晚上

    return dt.strftime(f"%Y-%m-%d {period}%H:%M:%S")  # 回傳像 2026-06-16 晚上23:46:46


def load_state() -> dict:  # 定義讀取狀態檔的函式
    if os.path.exists(SEEN_DB):  # 如果狀態檔存在
        with open(SEEN_DB, "r", encoding="utf-8") as f:  # 用 UTF-8 開啟狀態檔
            return json.load(f)  # 把 JSON 內容轉成 dict 後回傳

    return {}  # 如果狀態檔不存在，就回傳空字典


def save_state(state: dict):  # 定義儲存狀態檔的函式
    db_dir = os.path.dirname(SEEN_DB)  # 取得狀態檔所在資料夾

    if db_dir:  # 如果狀態檔有資料夾路徑
        os.makedirs(db_dir, exist_ok=True)  # 建立資料夾，已存在就不報錯

    with open(SEEN_DB, "w", encoding="utf-8") as f:  # 用寫入模式開啟狀態檔
        json.dump(state, f, ensure_ascii=False, indent=2)  # 把狀態資料寫成 JSON


def send_discord_text(title: str, description: str):  # 定義發送一般 Discord 訊息的函式
    if not DISCORD_WEBHOOK_URL:  # 如果沒有設定 Discord Webhook
        print("  [!] DISCORD_WEBHOOK_URL 尚未設定")  # 印出錯誤
        return  # 不發送訊息

    if "YOUR_WEBHOOK_ID" in DISCORD_WEBHOOK_URL or "YOUR_TOKEN" in DISCORD_WEBHOOK_URL:  # 如果還是範例 webhook
        print("  [!] Discord Webhook 還是範例值，請去 config.py 換成真的 URL")  # 印出錯誤
        return  # 不發送訊息

    payload = {  # 建立 Discord Webhook payload
        "embeds": [  # 使用 Discord embed 格式
            {  # embed 內容開始
                "title": title,  # Discord 標題
                "description": description,  # Discord 內文
                "color": 16753920,  # Discord embed 顏色
                "footer": {"text": "eslite-hunter"},  # footer 顯示專案名稱
                "timestamp": datetime.now(TAIWAN_TZ).isoformat(),  # Discord 時間戳，使用台灣時間
            }  # embed 內容結束
        ]  # embeds 結束
    }  # payload 結束

    try:  # 嘗試發送 Discord
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)  # 發送 POST 到 Discord Webhook

        if response.status_code >= 300:  # 如果 Discord 回傳錯誤
            print(f"  [✗] Discord 失敗：{response.status_code} — {response.text}")  # 印出錯誤
        else:  # 如果成功
            print("  [✓] Discord 已發送")  # 印出成功

    except Exception as e:  # 如果 requests 發生錯誤
        print(f"  [✗] Discord 發送錯誤：{e}")  # 印出錯誤


def is_beyblade(name: str) -> bool:  # 定義判斷是不是實體戰鬥陀螺商品的函式
    name_lower = name.lower()  # 把商品名稱轉成小寫

    if TEST_INCLUDE_EBOOK:  # 如果目前開啟電子書測試模式
        ebook_test_keywords = [  # 測試用電子書關鍵字
            "電子書",  # 電子書
            "戰鬥陀螺x (1)",  # 電子書第 1 集
            "戰鬥陀螺x (2)",  # 電子書第 2 集
        ]  # 電子書測試關鍵字結束

        if any(keyword in name_lower for keyword in ebook_test_keywords):  # 如果商品名稱命中電子書測試關鍵字
            return True  # 讓電子書通過篩選，用來測 Discord 通知流程

    exclude_keywords = [  # 正式監控時要排除的商品關鍵字
        "電子書",  # 排除電子書
        "ebook",  # 排除英文 ebook
        "e-book",  # 排除英文 e-book
        "vol. 3",  # 排除單純漫畫英文卷數
        "戰鬥陀螺x 1",  # 排除漫畫第 1 集
        "戰鬥陀螺x 2",  # 排除漫畫第 2 集
        "戰鬥陀螺x (1)",  # 排除電子書第 1 集
        "戰鬥陀螺x (2)",  # 排除電子書第 2 集
    ]  # 排除清單結束

    if any(keyword in name_lower for keyword in exclude_keywords):  # 如果命中排除關鍵字
        return False  # 直接判斷不是目標商品

    physical_keywords = [  # 實體陀螺商品常見關鍵字
        "bx-",  # BX 系列
        "ux-",  # UX 系列
        "cx-",  # CX 系列
        "bxg-",  # BXG 系列
        "發射器",  # 發射器商品
        "隨機強化組",  # 隨機強化組
        "對戰組",  # 對戰組
        "改造組",  # 改造組
        "限定版",  # 限定版
        "附陀螺",  # 書籍但有附實體陀螺
        "附附錄組",  # 書籍但可能有實體附錄
    ]  # 實體商品關鍵字清單結束

    return any(keyword in name_lower for keyword in physical_keywords)  # 命中實體商品關鍵字才算目標商品


def build_search_url() -> str:  # 定義建立誠品搜尋網址的函式
    return (  # 回傳組好的搜尋網址
        f"https://www.eslite.com/Search?keyword={quote(KEYWORD)}"  # 放入搜尋關鍵字
        f"&final_price=0,&publishDate=0&sort=_weight_+desc"  # 保留原本排序參數
        f"&size=20&display=list&start=0&exp=b"  # 使用列表顯示
    )  # 搜尋網址結束


def get_total_count(page) -> int:  # 定義從頁面取得總商品數的函式
    body = page.inner_text("body")  # 取得整個頁面的文字
    match = re.search(r"共有?\s*(\d+)\s*筆", body)  # 尋找「共 73 筆」或「共有 73 筆」
    return int(match.group(1)) if match else 0  # 找得到就回傳數字，找不到就回傳 0


def merge_product(products: dict, product: dict):  # 定義合併商品資料的函式
    url = product["url"]  # 取得商品網址

    if url not in products:  # 如果這個商品還沒有出現過
        products[url] = product  # 直接加入商品資料
        return  # 結束函式

    old = products[url]  # 取得舊的商品資料

    if product["status"] == "in_stock":  # 如果新的資料判斷為有貨
        old["status"] = "in_stock"  # 優先保留有貨狀態

    if product["status"] == "preorder" and old["status"] != "in_stock":  # 如果新資料是預購，而且舊資料不是有貨
        old["status"] = "preorder"  # 更新為預購狀態

    if len(product.get("name", "")) > len(old.get("name", "")):  # 如果新商品名稱比較完整
        old["name"] = product["name"]  # 更新商品名稱

    if product.get("price"):  # 如果新資料有價格
        old["price"] = product["price"]  # 更新商品價格


def collect_products_from_page(page) -> list:  # 定義從目前列表頁抓商品資料的函式
    return page.evaluate(  # 在瀏覽器內執行 JavaScript
        """
        () => {
            const products = new Map();

            function clean(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }

            function normalizeUrl(href) {
                const urlObj = new URL(href);
                urlObj.search = '';
                return urlObj.toString();
            }

            function statusRank(status) {
                if (status === 'in_stock') return 3;
                if (status === 'preorder') return 2;
                if (status === 'out_of_stock') return 1;
                return 0;
            }

            function upsert(product) {
                const old = products.get(product.url);

                if (!old) {
                    products.set(product.url, product);
                    return;
                }

                if (statusRank(product.status) > statusRank(old.status)) {
                    old.status = product.status;
                }

                if ((product.name || '').length > (old.name || '').length) {
                    old.name = product.name;
                }

                if (product.price && !old.price) {
                    old.price = product.price;
                }
            }

            function countUniqueProductUrls(node) {
                if (!node || !node.querySelectorAll) {
                    return 0;
                }

                const urls = [...node.querySelectorAll("a[href*='/product/']")]
                    .map(a => normalizeUrl(a.href));

                return new Set(urls).size;
            }

            function isEnabledBuyElement(el) {
                const text = clean(el.innerText || el.textContent || '').replace(/\\s+/g, '');
                const className = (el.className || '').toString().toLowerCase();
                const ariaDisabled = el.getAttribute('aria-disabled');

                const disabled = (
                    el.disabled === true ||
                    el.hasAttribute('disabled') ||
                    ariaDisabled === 'true' ||
                    className.includes('disabled') ||
                    className.includes('disable')
                );

                const visible = Boolean(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

                const isBuy = (
                    text.includes('加入購物車') ||
                    text.includes('加入购物车') ||
                    text.includes('立即購買') ||
                    text.includes('立即购买')
                );

                return visible && !disabled && isBuy;
            }

            function findCardFromAnchor(anchor) {
                let node = anchor;
                let best = anchor.parentElement || anchor;

                for (let i = 0; i < 12 && node; i++) {
                    const text = clean(node.innerText || '');
                    const productCount = countUniqueProductUrls(node);
                    const hasProductInfo =
                        text.includes('$') ||
                        text.includes('預購') ||
                        text.includes('收藏') ||
                        text.includes('加入購物車') ||
                        text.includes('立即購買');

                    if (hasProductInfo && productCount <= 1) {
                        best = node;
                    }

                    if (productCount > 1) {
                        break;
                    }

                    node = node.parentElement;
                }

                return best;
            }

            function findAnchorFromBuyElement(el) {
                let node = el;

                for (let i = 0; i < 12 && node; i++) {
                    if (node.querySelectorAll) {
                        const anchors = [...node.querySelectorAll("a[href*='/product/']")];
                        const uniqueUrls = [...new Set(anchors.map(a => normalizeUrl(a.href)))];

                        if (uniqueUrls.length === 1 && anchors.length > 0) {
                            return anchors[0];
                        }

                        if (uniqueUrls.length > 1) {
                            break;
                        }
                    }

                    node = node.parentElement;
                }

                return null;
            }

            function getName(card, anchor) {
                const anchorTexts = [...card.querySelectorAll("a[href*='/product/']")]
                    .map(a => clean(a.innerText || a.textContent || ''))
                    .filter(t => t.length >= 4)
                    .sort((a, b) => b.length - a.length);

                if (anchorTexts.length > 0) {
                    return anchorTexts[0];
                }

                const lines = (card.innerText || '')
                    .split('\\n')
                    .map(line => clean(line))
                    .filter(Boolean);

                const nameLine = lines.find(line =>
                    line.toLowerCase().includes('beyblade') ||
                    line.includes('戰鬥陀螺') ||
                    line.toLowerCase().includes('bx-') ||
                    line.toLowerCase().includes('ux-') ||
                    line.toLowerCase().includes('cx-') ||
                    line.toLowerCase().includes('bxg-') ||
                    line.includes('電子書')
                );

                return nameLine || clean(anchor.innerText || anchor.textContent || document.title);
            }

            function getPrice(card) {
                const text = clean(card.innerText || '');
                const match = text.match(/\\$\\s*[\\d,]+/);
                return match ? match[0] : '';
            }

            function buildProduct(anchor, forceInStock = false) {
                const url = normalizeUrl(anchor.href);
                const card = findCardFromAnchor(anchor);
                const rawText = clean(card.innerText || '');
                const name = getName(card, anchor);
                const price = getPrice(card);

                const canBuy =
                    forceInStock ||
                    [...card.querySelectorAll("button, a, [role='button'], div, span")].some(isEnabledBuyElement);

                const isPreorder = rawText.includes('預購') || rawText.includes('预购');

                let status = 'out_of_stock';

                if (canBuy) {
                    status = 'in_stock';
                } else if (isPreorder) {
                    status = 'preorder';
                }

                return {
                    url,
                    name,
                    price,
                    status,
                    status_label: '',
                };
            }

            const anchors = [...document.querySelectorAll("a[href*='/product/']")];

            for (const anchor of anchors) {
                upsert(buildProduct(anchor, false));
            }

            const possibleButtons = [...document.querySelectorAll("button, a, [role='button'], div, span")];

            for (const el of possibleButtons) {
                if (!isEnabledBuyElement(el)) {
                    continue;
                }

                const anchor = findAnchorFromBuyElement(el);

                if (!anchor) {
                    continue;
                }

                upsert(buildProduct(anchor, true));
            }

            return [...products.values()];
        }
        """
    )


def try_click_page_number(page, page_no: int) -> bool:  # 定義嘗試點指定頁碼的函式
    try:  # 用 try 避免找不到元素時中斷
        handle = page.evaluate_handle(  # 用 JavaScript 找頁碼按鈕
            """
            (pageNo) => {
                const target = String(pageNo);
                const elements = [...document.querySelectorAll('a, button')];

                return elements.find(el => {
                    const text = (el.innerText || el.textContent || '').trim();
                    const className = (el.className || '').toString().toLowerCase();
                    const ariaDisabled = el.getAttribute('aria-disabled');
                    const disabled = el.disabled || ariaDisabled === 'true' || className.includes('disabled');

                    const inPager = Boolean(
                        el.closest('nav') ||
                        el.closest('[class*="pagination"]') ||
                        el.closest('[class*="Pagination"]') ||
                        el.closest('[class*="pager"]') ||
                        el.closest('[class*="Pager"]')
                    );

                    return text === target && inPager && !disabled;
                });
            }
            """,
            page_no,
        )

        element = handle.as_element()  # 把 JSHandle 轉成 Playwright ElementHandle

        if not element:  # 如果找不到元素
            return False  # 回傳失敗

        element.scroll_into_view_if_needed(timeout=3000)  # 把頁碼按鈕捲到畫面中
        element.click(timeout=5000)  # 點擊頁碼
        page.wait_for_timeout(3000)  # 等頁面更新
        return True  # 回傳成功

    except Exception:  # 如果發生錯誤
        return False  # 回傳失敗


def fetch_all_products() -> list:  # 定義抓全部商品資料的函式
    products = {}  # 用 dict 儲存商品，key 是商品網址

    with sync_playwright() as p:  # 啟動 Playwright
        browser = p.chromium.launch(headless=True)  # 開啟無頭瀏覽器
        context = browser.new_context(  # 建立瀏覽器環境
            user_agent=(  # 設定 User-Agent
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",  # 設定語系
            viewport={"width": 1280, "height": 1200},  # 設定瀏覽器視窗大小
        )

        page = context.new_page()  # 開啟新分頁
        page.goto(build_search_url(), wait_until="domcontentloaded", timeout=60000)  # 進入搜尋頁
        page.wait_for_timeout(3000)  # 等商品列表渲染

        total = get_total_count(page)  # 取得總商品數
        print(f"  共 {total} 筆商品")  # 印出總商品數

        page_no = 1  # 從第 1 頁開始

        while True:  # 持續翻頁抓商品
            page_products = collect_products_from_page(page)  # 抓目前頁面的商品資料
            before = len(products)  # 記錄加入前的商品數量

            for product in page_products:  # 逐一處理目前頁面的商品
                merge_product(products, product)  # 合併商品資料

            added = len(products) - before  # 計算這頁新增幾個商品
            print(f"  第 {page_no} 頁：新增 {added} 個（累計 {len(products)} 個）")  # 印出翻頁進度

            if total and len(products) >= total:  # 如果已經抓到總商品數以上
                break  # 停止翻頁

            next_page = page_no + 1  # 計算下一頁頁碼

            if not try_click_page_number(page, next_page):  # 如果點不到下一頁
                print("  沒有下一頁了，停止")  # 印出停止原因
                break  # 停止翻頁

            page_no += 1  # 頁碼加一

            if page_no > 20:  # 防呆，避免無限翻頁
                print("  超過安全頁數，停止")  # 印出防呆訊息
                break  # 停止翻頁

        page.close()  # 關閉頁面
        browser.close()  # 關閉瀏覽器

    print(f"  全部抓完，共 {len(products)} 個商品")  # 印出最後抓到的商品數
    return list(products.values())  # 回傳商品列表


def run_scan():  # 定義執行一次完整掃描的函式
    global IS_FIRST_SCAN  # 使用全域變數，判斷是否第一次掃描

    now = datetime.now(TAIWAN_TZ).strftime("%H:%M:%S")  # 取得目前台灣時間

    print(f"\n{'=' * 50}")  # 印出分隔線
    print(f"[誠品][{now}] 開始掃描...")
    
    print("Step 1 / 翻頁抓取所有商品與購物車狀態...")  # 印出 Step 1
    products = fetch_all_products()  # 抓全部商品與狀態

    if not products:  # 如果沒有抓到商品
        print("  [!] 沒找到商品，跳過")  # 印出警告
        return  # 結束這次掃描

    print(f"\nStep 2 / 整理 {len(products)} 個商品狀態...")  # 印出 Step 2

    state = load_state()  # 讀取舊狀態
    beyblade = [product for product in products if is_beyblade(product["name"])]  # 過濾出實體戰鬥陀螺商品

    for product in beyblade:  # 逐一處理實體戰鬥陀螺商品
        url = product["url"]  # 取得商品網址
        curr_status = product["status"]  # 取得這次商品狀態

        product["status_label"] = LABEL_MAP.get(curr_status, LABEL_MAP["unknown"])  # 補上 notifier.py 需要的 status_label

        state[url] = {  # 更新狀態資料
            "status": curr_status,  # 儲存目前狀態
            "status_label": product["status_label"],  # 儲存狀態文字
            "name": product["name"],  # 儲存商品名稱
            "price": product.get("price", ""),  # 儲存商品價格
            "last_checked": datetime.now(TAIWAN_TZ).isoformat(),  # 儲存最後檢查時間，使用台灣時間
        }

    in_stock = [p for p in beyblade if p["status"] == "in_stock"]  # 篩出有貨商品
    preorder = [p for p in beyblade if p["status"] == "preorder"]  # 篩出預購商品
    out_of_stock = [p for p in beyblade if p["status"] == "out_of_stock"]  # 篩出無貨商品

    print(f"\n{'=' * 50}")  # 印出分隔線
    print(f"掃描結果（共 {len(beyblade)} 個實體陀螺商品）")  # 印出實體陀螺商品數量
    print(f"{'=' * 50}")  # 印出分隔線

    if in_stock:  # 如果有有貨商品
        print(f"\n✅ 有貨 / 可加入購物車（{len(in_stock)} 個）")  # 印出有貨商品數量
        for product in in_stock:  # 逐一印出有貨商品
            price = f" {product.get('price', '')}" if product.get("price") else ""  # 有價格就顯示價格
            print(f"   {product['name'][:55]}{price}")  # 印出商品名稱與價格

    if preorder:  # 如果有預購商品
        print(f"\n📌 預購中（{len(preorder)} 個）")  # 印出預購商品數量
        for product in preorder:  # 逐一印出預購商品
            print(f"   {product['name'][:55]}")  # 印出商品名稱

    if out_of_stock:  # 如果有無貨商品
        print(f"\n❌ 目前無庫存（{len(out_of_stock)} 個）")  # 印出無貨商品數量
        for product in out_of_stock:  # 逐一印出無貨商品
            print(f"   {product['name'][:55]}")  # 印出商品名稱

    print("\nStep 3 / Discord 通知放最後...")  # 印出 Step 3

    if IS_FIRST_SCAN:  # 如果這是程式啟動後第一次掃描
        send_discord_text(  # 發送啟動通知到 Discord
            "🌀 陀螺獵人已啟動",  # Discord 標題
            (
                "開始監控誠品。\n"
                f"目前實體陀螺商品：{len(beyblade)} 個\n"
                f"目前現貨：{len(in_stock)} 個\n"
                f"啟動時間：{format_taiwan_time(PROGRAM_STARTED_AT)}"
            ),  # Discord 內文
        )

    if in_stock:  # 如果目前有任何商品可以加入購物車
        print(f"  偵測到 {len(in_stock)} 個有貨商品，發送 Discord 通知")  # 印出有貨通知數量

        for product in in_stock:  # 逐一處理每個有貨商品
            send_restock_alert(product)  # 發送 Discord 有貨通知

    else:  # 如果目前完全沒有現貨
        print("  目前無現貨，不發 Discord")  # 只在 Terminal 顯示，不發 Discord

    IS_FIRST_SCAN = False  # 第一次掃描結束後，改成 False

    save_state(state)  # 儲存最新狀態與 meta 資料

    print(f"\n下次掃描：{CHECK_INTERVAL} 秒後")  # 印出下次掃描時間


def main():  # 定義主程式入口
    once = "--once" in sys.argv  # 判斷是否有 --once 參數

    print("🌀 陀螺獵人啟動（誠品列表偵測版）")  # 印出啟動訊息
    print(f"   關鍵字：{KEYWORD}")  # 印出搜尋關鍵字
    print(f"   掃描間隔：{CHECK_INTERVAL} 秒")  # 印出掃描間隔
    print(f"   電子書測試模式：{TEST_INCLUDE_EBOOK}")  # 印出目前是否開啟電子書測試
    print("=" * 50)  # 印出分隔線

    if once:  # 如果使用 --once
        run_scan()  # 只執行一次掃描
        return  # 掃完後結束程式

    run_scan()  # 沒有 --once 時，啟動後先掃一次

    schedule.every(CHECK_INTERVAL).seconds.do(run_scan)  # 設定每隔 CHECK_INTERVAL 秒掃描一次

    while True:  # 持續執行程式
        schedule.run_pending()  # 執行到時間的排程
        time.sleep(5)  # 每 5 秒檢查一次排程


if __name__ == "__main__":  # 如果這個檔案是直接執行
    main()  # 執行主程式