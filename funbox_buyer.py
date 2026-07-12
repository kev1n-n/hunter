import argparse
import json
import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


STATE_FILE = os.getenv("FUNBOX_STATE_FILE", "funbox_state.json")
ORDER_SUMMARY_FILE = os.getenv("FUNBOX_ORDER_SUMMARY_FILE", "order_summary.json")


def load_dotenv_if_exists():
    env_path = Path(".env")

    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


load_dotenv_if_exists()


FUNBOX_EMAIL = os.getenv("FUNBOX_EMAIL", "").strip()
FUNBOX_PASSWORD = os.getenv("FUNBOX_PASSWORD", "").strip()

FUNBOX_CART_QUANTITY = int(os.getenv("FUNBOX_CART_QUANTITY", "1"))
FUNBOX_MAX_PRICE = int(os.getenv("FUNBOX_MAX_PRICE", "2500"))
FUNBOX_MAX_TOTAL = int(os.getenv("FUNBOX_MAX_TOTAL", "2500"))

FUNBOX_AUTO_SUBMIT_ORDER = os.getenv("FUNBOX_AUTO_SUBMIT_ORDER", "false").lower() in [
    "1",
    "true",
    "yes",
    "on",
]

FUNBOX_HEADLESS = os.getenv("FUNBOX_BUYER_HEADLESS", "false").lower() in [
    "1",
    "true",
    "yes",
    "on",
]

FUNBOX_STORE_KEYWORD = os.getenv("FUNBOX_STORE_KEYWORD", "廣惠")
FUNBOX_FAMI_STORE_NAME = os.getenv("FUNBOX_FAMI_STORE_NAME", "全家佳冬廣惠店")

PAGE_TIMEOUT_MS = int(os.getenv("FUNBOX_BUYER_PAGE_TIMEOUT_MS", "60000"))


def log(message: str):
    print(f"[funbox-buyer] {message}", flush=True)


def require_credentials():
    if not FUNBOX_EMAIL or not FUNBOX_PASSWORD:
        raise RuntimeError(
            "找不到 FUNBOX_EMAIL / FUNBOX_PASSWORD，請先在 .env 設定 Funbox 帳密"
        )


def save_json(path: str, data: dict):
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def click_text(page, texts: list[str], timeout: int = 10000) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=False)

            if locator.count() > 0:
                locator.first.click(timeout=timeout)
                log(f"已點擊文字：{text}")
                return True

        except Exception:
            continue

    return False


def click_text_anywhere(page, texts: list[str], label: str = "頁面", timeout: int = 10000) -> bool:
    def click_in_target(target, target_label: str) -> bool:
        for text in texts:
            candidates = [
                target.get_by_text(text, exact=False),
                target.locator("button").filter(has_text=text),
                target.locator("a").filter(has_text=text),
                target.locator("div").filter(has_text=text),
                target.locator("span").filter(has_text=text),
                target.locator("td").filter(has_text=text),
                target.locator("li").filter(has_text=text),
                target.locator(f"xpath=//*[contains(text(), '{text}') or contains(@value, '{text}')]"),
            ]

            for locator in candidates:
                try:
                    count = locator.count()

                    for i in range(count):
                        item = locator.nth(i)

                        try:
                            item.scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass

                        if item.is_visible():
                            item.click(timeout=timeout, force=True)
                            log(f"已在 {target_label} 點擊：{text}")
                            return True

                except Exception:
                    continue

        return False

    if click_in_target(page, f"{label} 主頁面"):
        return True

    for index, frame in enumerate(page.frames):
        try:
            if click_in_target(frame, f"{label} frame {index}"):
                return True
        except Exception:
            continue

    return False


def fill_first_visible_input(page, value: str, keywords: list[str] | None = None) -> bool:
    inputs = page.locator("input")
    count = inputs.count()

    for i in range(count):
        try:
            box = inputs.nth(i)

            if not box.is_visible():
                continue

            placeholder = box.get_attribute("placeholder") or ""
            input_type = box.get_attribute("type") or ""
            name = box.get_attribute("name") or ""

            meta = f"{placeholder} {input_type} {name}"

            if keywords:
                if not any(keyword.lower() in meta.lower() for keyword in keywords):
                    continue

            box.fill(value, timeout=5000)
            return True

        except Exception:
            continue

    return False


def is_login_page(page) -> bool:
    url = page.url

    if "/account/login" in url:
        return True

    try:
        body = page.inner_text("body", timeout=5000)
    except Exception:
        return False

    login_words = [
        "會員登入",
        "請輸入您的E-MAIL或手機",
        "請輸入您的密碼",
        "快速登入",
    ]

    return any(word in body for word in login_words)


def login_funbox(page):
    require_credentials()

    log("準備自動登入 Funbox")

    page.goto(
        "https://shop.funbox.com.tw/account/login",
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )
    page.wait_for_timeout(2000)

    filled_email = fill_first_visible_input(
        page,
        FUNBOX_EMAIL,
        keywords=["email", "e-mail", "手機", "login", "account"],
    )

    if not filled_email:
        raise RuntimeError("找不到帳號輸入欄位")

    filled_password = fill_first_visible_input(
        page,
        FUNBOX_PASSWORD,
        keywords=["password", "密碼"],
    )

    if not filled_password:
        raise RuntimeError("找不到密碼輸入欄位")

    page.keyboard.press("Tab")
    page.wait_for_timeout(1000)

    log("帳密已輸入，準備點擊登入按鈕")

    clicked_login = False

    button_candidates = [
        page.locator("button").filter(has_text="會員登入"),
        page.locator('input[type="submit"]'),
        page.locator('button[type="submit"]'),
        page.locator("form button"),
    ]

    for locator in button_candidates:
        try:
            count = locator.count()

            for i in range(count):
                button = locator.nth(i)

                if button.is_visible():
                    button.click(timeout=15000)
                    clicked_login = True
                    log("已點擊登入按鈕")
                    break

            if clicked_login:
                break

        except Exception:
            continue

    if not clicked_login:
        log("找不到可點擊登入按鈕，改用 Enter 送出")
        page.keyboard.press("Enter")

    page.wait_for_timeout(6000)

    if is_login_page(page):
        raise RuntimeError("登入可能失敗，請確認帳號密碼是否正確，或登入按鈕仍未成功送出")

    log("Funbox 登入成功")

    page.context.storage_state(path=STATE_FILE)
    log(f"已更新登入狀態：{STATE_FILE}")


def ensure_logged_in(page):
    if is_login_page(page):
        log("目前未登入，準備登入")
        login_funbox(page)
    else:
        log("目前看起來已登入")


def extract_price_from_page(page) -> int | None:
    try:
        body = page.inner_text("body", timeout=10000)
    except Exception:
        return None

    patterns = [
        r"NT\$\s*([\d,]+)",
        r"\$\s*([\d,]+)",
    ]

    prices = []

    for pattern in patterns:
        matches = re.findall(pattern, body)

        for match in matches:
            try:
                prices.append(int(match.replace(",", "")))
            except ValueError:
                pass

    if not prices:
        return None

    return min(prices)


def confirm_product_available_and_price(page) -> bool:
    body_text = page.inner_text("body", timeout=15000)

    price = extract_price_from_page(page)

    if price is not None:
        log(f"偵測到商品價格：NT${price}")

        if price > FUNBOX_MAX_PRICE:
            raise RuntimeError(
                f"商品價格 NT${price} 超過上限 NT${FUNBOX_MAX_PRICE}，停止購買"
            )
    else:
        log("沒有偵測到價格，繼續但請注意")

    add_to_cart_candidates = [
        page.get_by_text("加入購物車", exact=False),
        page.locator("button").filter(has_text="加入購物車"),
        page.locator("a").filter(has_text="加入購物車"),
        page.locator("div").filter(has_text="加入購物車"),
    ]

    for locator in add_to_cart_candidates:
        try:
            count = locator.count()

            for i in range(count):
                item = locator.nth(i)

                if item.is_visible():
                    log("找到可見的加入購物車按鈕，商品判定為有貨")
                    return True

        except Exception:
            continue

    sold_out_words = [
        "已售完",
        "暫無庫存",
        "無庫存",
        "補貨中",
    ]

    if any(word in body_text for word in sold_out_words):
        log("找不到購買按鈕，且頁面有缺貨字樣，判定可能無貨")
        return False

    log("找不到明確購買按鈕，為安全起見不購買")
    return False


def set_quantity(page, quantity: int):
    if quantity <= 1:
        log("購買數量為 1，不調整數量")
        return

    log(f"準備調整數量到 {quantity}")

    for _ in range(quantity - 1):
        clicked = click_text(page, ["＋", "+"], timeout=5000)

        if not clicked:
            log("找不到增加數量按鈕，停止調整")
            break

        page.wait_for_timeout(500)


def add_to_cart(page):
    log("準備加入購物車")

    candidates = [
        page.get_by_text("加入購物車", exact=False),
        page.locator("button").filter(has_text="加入購物車"),
        page.locator("a").filter(has_text="加入購物車"),
        page.locator("div").filter(has_text="加入購物車"),
    ]

    for locator in candidates:
        try:
            count = locator.count()

            for i in range(count):
                item = locator.nth(i)

                if item.is_visible():
                    item.click(timeout=15000)
                    page.wait_for_timeout(3000)
                    log("已加入購物車")
                    return

        except Exception:
            continue

    raise RuntimeError("找不到加入購物車按鈕")


def go_to_checkout(page):
    log("準備進入結帳")

    page.wait_for_timeout(2000)

    checkout_texts = [
        "立即結帳",
        "前往結帳",
        "結帳",
    ]

    for text in checkout_texts:
        try:
            locator = page.get_by_text(text, exact=False)
            count = locator.count()

            for i in range(count):
                item = locator.nth(i)

                if item.is_visible():
                    item.scroll_into_view_if_needed(timeout=5000)
                    page.wait_for_timeout(500)
                    item.click(timeout=15000)
                    log(f"已點擊結帳按鈕：{text}")
                    page.wait_for_timeout(3000)
                    return

        except Exception:
            continue

    log("一開始找不到結帳按鈕，開始往下滾動尋找")

    for scroll_round in range(1, 8):
        page.mouse.wheel(0, 1000)
        page.wait_for_timeout(1000)

        log(f"往下滾動尋找結帳按鈕 {scroll_round}/7")

        for text in checkout_texts:
            try:
                locator = page.get_by_text(text, exact=False)
                count = locator.count()

                for i in range(count):
                    item = locator.nth(i)

                    if item.is_visible():
                        item.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(500)
                        item.click(timeout=15000)
                        log(f"已點擊結帳按鈕：{text}")
                        page.wait_for_timeout(3000)
                        return

            except Exception:
                continue

    try:
        page.locator('a[href*="/carts"]').first.click(timeout=10000)
        page.wait_for_timeout(3000)
    except Exception as e:
        raise RuntimeError(f"無法進入購物車 / 結帳頁：{e}")


def choose_delivery_and_payment(page):
    log("準備選擇配送方式與付款方式")

    page.wait_for_timeout(2000)

    click_text(page, ["超商"], timeout=15000)
    page.wait_for_timeout(1000)

    clicked = click_text(
        page,
        [
            "全家 貨到付款",
            "全家貨到付款",
            "全家 取貨付款",
            "全家取貨付款",
            "全家",
        ],
        timeout=15000,
    )

    if not clicked:
        raise RuntimeError("找不到全家貨到付款選項")

    log("已選擇全家貨到付款")
    page.wait_for_timeout(1000)


def open_store_selector(page):
    log("準備開啟全家門市選擇頁")

    candidates = [
        "請選擇取貨門市",
        "選擇取貨門市",
        "選擇門市",
        "門市",
    ]

    for text in candidates:
        try:
            locator = page.get_by_text(text, exact=False)

            if locator.count() > 0:
                with page.context.expect_page(timeout=20000) as new_page_info:
                    locator.first.click(timeout=10000)

                store_page = new_page_info.value
                store_page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                store_page.wait_for_timeout(3000)

                log("已開啟全家門市選擇頁")
                return store_page

        except Exception:
            continue

    raise RuntimeError("無法開啟全家門市選擇頁")


def familymart_click_store_name_search(store_page):
    log("準備點擊全家『店名查詢』")

    clicked = click_text_anywhere(
        store_page,
        [
            "店名查詢",
        ],
        label="全家店名查詢",
        timeout=15000,
    )

    if not clicked:
        raise RuntimeError("找不到全家店名查詢")

    log("已點擊全家店名查詢")
    store_page.wait_for_timeout(1500)


def familymart_fill_store_name_modal(store_page):
    log(f"準備輸入全家店名關鍵字：{FUNBOX_STORE_KEYWORD}")

    filled = False

    try:
        inputs = store_page.locator("input")
        count = inputs.count()

        for i in range(count):
            box = inputs.nth(i)

            try:
                if not box.is_visible():
                    continue

                input_type = box.get_attribute("type") or ""

                if input_type.lower() in ["hidden", "button", "submit", "radio", "checkbox"]:
                    continue

                box.click(timeout=5000)
                box.fill(FUNBOX_STORE_KEYWORD, timeout=5000)
                filled = True
                log("已在主頁面輸入全家店名")
                break

            except Exception:
                continue

    except Exception:
        pass

    if not filled:
        for index, frame in enumerate(store_page.frames):
            try:
                inputs = frame.locator("input")
                count = inputs.count()

                for i in range(count):
                    box = inputs.nth(i)

                    try:
                        if not box.is_visible():
                            continue

                        input_type = box.get_attribute("type") or ""

                        if input_type.lower() in ["hidden", "button", "submit", "radio", "checkbox"]:
                            continue

                        box.click(timeout=5000)
                        box.fill(FUNBOX_STORE_KEYWORD, timeout=5000)
                        filled = True
                        log(f"已在 frame {index} 輸入全家店名")
                        break

                    except Exception:
                        continue

                if filled:
                    break

            except Exception:
                continue

    if not filled:
        raise RuntimeError("找不到全家店名輸入框")

    clicked_confirm = click_text_anywhere(
        store_page,
        [
            "確定",
        ],
        label="全家店名查詢確定",
        timeout=15000,
    )

    if not clicked_confirm:
        raise RuntimeError("找不到全家店名查詢的確定按鈕")

    log("已送出全家店名查詢")
    store_page.wait_for_timeout(3000)


def familymart_click_store_result(store_page):
    log(f"準備點選全家門市結果：{FUNBOX_FAMI_STORE_NAME}")

    clicked = click_text_anywhere(
        store_page,
        [
            FUNBOX_FAMI_STORE_NAME,
            "佳冬廣惠",
            "廣惠",
        ],
        label="全家門市結果",
        timeout=15000,
    )

    if clicked:
        log("已點選全家門市結果")
        store_page.wait_for_timeout(3000)
        return

    raise RuntimeError("找不到全家門市搜尋結果")


def familymart_confirm_store(store_page):
    log("準備點擊全家『確定店舖』")

    clicked = click_text_anywhere(
        store_page,
        [
            "確定店舖",
            "確定店鋪",
            "確認店舖",
            "確認店鋪",
        ],
        label="全家確定店舖",
        timeout=15000,
    )

    if clicked:
        log("已點擊全家確定店舖")

        try:
            store_page.wait_for_timeout(5000)
        except Exception:
            log("全家門市視窗已自動關閉，這代表門市選擇成功")
            return

        return

    raise RuntimeError("找不到全家確定店舖按鈕")


def choose_familymart_store(store_page):
    log("準備選擇全家門市")

    familymart_click_store_name_search(store_page)
    familymart_fill_store_name_modal(store_page)
    familymart_click_store_result(store_page)
    familymart_confirm_store(store_page)

    log("全家門市選擇流程已完成")


def extract_amount_from_text(body_text: str, label: str) -> int | None:
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        if label not in line:
            continue

        nearby = " ".join(lines[index:index + 4])

        match = re.search(r"([+-]?)\s*NT\$?\s*([\d,]+)", nearby)

        if match:
            sign = match.group(1)
            number = int(match.group(2).replace(",", ""))

            if sign == "-":
                return -number

            return number

    return None


def extract_cart_items_from_text(body_text: str) -> list[dict]:
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    items = []

    start_index = None
    end_index = None

    for index, line in enumerate(lines):
        if "購物車內容" in line:
            start_index = index
            break

    if start_index is not None:
        for index in range(start_index + 1, min(start_index + 12, len(lines))):
            if "合計有" in lines[index] or "預估可獲得紅利" in lines[index]:
                end_index = index
                break

        chunk = lines[start_index + 1:end_index] if end_index else lines[start_index + 1:start_index + 8]

        name_lines = []
        quantity = 1

        for line in chunk:
            if "數量" in line:
                match = re.search(r"(\d+)", line)
                if match:
                    quantity = int(match.group(1))
                continue

            if "合計" in line:
                continue

            if line:
                name_lines.append(line)

        product_name = " ".join(name_lines).strip()

        if product_name:
            items.append(
                {
                    "name": product_name,
                    "quantity": quantity,
                }
            )

    return items


def extract_order_summary(page, product_url: str | None = None) -> dict:
    log("準備擷取訂單摘要")

    page.wait_for_timeout(2000)

    try:
        body_text = page.inner_text("body", timeout=15000)
    except Exception:
        body_text = ""

    subtotal = extract_amount_from_text(body_text, "商品合計")
    shipping = extract_amount_from_text(body_text, "運費")
    discount = extract_amount_from_text(body_text, "紅利點數")
    total = extract_amount_from_text(body_text, "應付總額")

    if total is None:
        amount_matches = re.findall(r"NT\$?\s*([\d,]+)", body_text)

        if amount_matches:
            total = int(amount_matches[-1].replace(",", ""))

    items = extract_cart_items_from_text(body_text)

    summary = {
        "status": "prepared",
        "product_url": product_url,
        "checkout_url": page.url,
        "items": items,
        "subtotal": subtotal,
        "shipping": shipping,
        "discount": discount,
        "total": total,
        "store": FUNBOX_FAMI_STORE_NAME,
        "payment": "全家貨到付款",
        "raw_preview": body_text[:2500],
    }

    save_json(ORDER_SUMMARY_FILE, summary)

    log(f"已輸出訂單摘要：{ORDER_SUMMARY_FILE}")
    log(json.dumps(summary, ensure_ascii=False, indent=2))

    if total is not None and total > FUNBOX_MAX_TOTAL:
        raise RuntimeError(
            f"應付總額 NT${total} 超過上限 NT${FUNBOX_MAX_TOTAL}，停止"
        )

    return summary


def agree_terms(page):
    log("準備勾選同意條款")

    try:
        checkboxes = page.locator('input[type="checkbox"]')
        count = checkboxes.count()

        for i in range(count):
            try:
                checkbox = checkboxes.nth(i)

                if checkbox.is_visible() and not checkbox.is_checked():
                    checkbox.check(timeout=5000)

            except Exception:
                continue

        log("已嘗試勾選所有可見同意條款")

    except Exception as e:
        log(f"勾選條款時發生問題：{e}")

    page.wait_for_timeout(1000)


def click_final_submit_button(page):
    log("準備點擊最後送出訂單按鈕")

    final_texts = [
        "立即結帳",
        "送出訂單",
        "確認結帳",
        "確認送出",
    ]

    for round_index in range(1, 10):
        for text in final_texts:
            try:
                locator = page.get_by_text(text, exact=False)
                count = locator.count()

                for i in range(count):
                    item = locator.nth(i)

                    if item.is_visible():
                        item.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(500)
                        item.click(timeout=15000, force=True)
                        log(f"已點擊最後送出訂單按鈕：{text}")
                        page.wait_for_timeout(5000)
                        return

            except Exception:
                continue

        page.mouse.wheel(0, 900)
        page.wait_for_timeout(700)
        log(f"往下尋找最後送出按鈕 {round_index}/9")

    raise RuntimeError("找不到最後送出訂單按鈕")


def submit_or_stop(page):
    if FUNBOX_AUTO_SUBMIT_ORDER:
        log("FUNBOX_AUTO_SUBMIT_ORDER=true，準備送出訂單")
        agree_terms(page)
        click_final_submit_button(page)
        log("已嘗試送出訂單，請確認頁面結果")
    else:
        log("安全模式：停在最後結帳前，不會自動送出訂單")
        log("確認畫面沒問題後，你可以手動按最後的立即結帳")


def create_browser_context(playwright):
    browser = playwright.chromium.launch(
        headless=FUNBOX_HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )

    context_kwargs = {
        "viewport": {"width": 1365, "height": 1400},
        "locale": "zh-TW",
        "timezone_id": "Asia/Taipei",
    }

    if Path(STATE_FILE).exists():
        context_kwargs["storage_state"] = STATE_FILE
        log(f"使用既有登入狀態：{STATE_FILE}")
    else:
        log("沒有找到登入狀態，會使用帳密自動登入")

    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)

    return browser, context, page


def buy_funbox_product(product_url: str, json_mode: bool = False):
    log(f"準備購買商品：{product_url}")
    log(f"購買數量：{FUNBOX_CART_QUANTITY}")
    log(f"價格上限：NT${FUNBOX_MAX_PRICE}")
    log(f"總額上限：NT${FUNBOX_MAX_TOTAL}")
    log(f"自動送出訂單：{FUNBOX_AUTO_SUBMIT_ORDER}")
    log(f"指定全家門市關鍵字：{FUNBOX_STORE_KEYWORD}")
    log(f"指定全家門市：{FUNBOX_FAMI_STORE_NAME}")
    log(f"JSON 模式：{json_mode}")

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p)

        try:
            page.goto(product_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(3000)

            if is_login_page(page):
                login_funbox(page)
                page.goto(product_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.wait_for_timeout(3000)

            ensure_logged_in(page)

            if not confirm_product_available_and_price(page):
                raise RuntimeError("購買前重新確認：商品目前可能已無貨")

            set_quantity(page, FUNBOX_CART_QUANTITY)
            add_to_cart(page)
            go_to_checkout(page)

            if is_login_page(page):
                login_funbox(page)
                go_to_checkout(page)

            choose_delivery_and_payment(page)

            store_page = open_store_selector(page)
            choose_familymart_store(store_page)

            page.bring_to_front()
            page.wait_for_timeout(5000)

            agree_terms(page)
            summary = extract_order_summary(page, product_url=product_url)

            context.storage_state(path=STATE_FILE)

            submit_or_stop(page)

            if not FUNBOX_AUTO_SUBMIT_ORDER and not json_mode and not FUNBOX_HEADLESS:
                input("\n安全模式：請確認畫面。確認完按 Enter 關閉瀏覽器...")

            return summary

        finally:
            try:
                context.storage_state(path=STATE_FILE)
            except Exception:
                pass

            browser.close()


def submit_prepared_order():
    if not Path(ORDER_SUMMARY_FILE).exists():
        raise RuntimeError(f"找不到 {ORDER_SUMMARY_FILE}，請先執行 --json 準備訂單")

    summary = load_json(ORDER_SUMMARY_FILE)

    checkout_url = summary.get("checkout_url")
    old_total = summary.get("total")

    if not checkout_url:
        raise RuntimeError("order_summary.json 裡面沒有 checkout_url，不能送出")

    if old_total is not None and int(old_total) > FUNBOX_MAX_TOTAL:
        raise RuntimeError(
            f"order_summary.json 的總額 NT${old_total} 超過上限 NT${FUNBOX_MAX_TOTAL}，停止送出"
        )

    log("準備送出已準備好的訂單")
    log(f"checkout_url: {checkout_url}")

    with sync_playwright() as p:
        browser, context, page = create_browser_context(p)

        try:
            page.goto(checkout_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(5000)

            if is_login_page(page):
                login_funbox(page)
                page.goto(checkout_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.wait_for_timeout(5000)

            ensure_logged_in(page)

            current_summary = extract_order_summary(page, product_url=summary.get("product_url"))
            current_total = current_summary.get("total")

            if current_total is not None and int(current_total) > FUNBOX_MAX_TOTAL:
                raise RuntimeError(
                    f"目前應付總額 NT${current_total} 超過上限 NT${FUNBOX_MAX_TOTAL}，停止送出"
                )

            agree_terms(page)
            click_final_submit_button(page)

            result = {
                "status": "submitted",
                "old_summary": summary,
                "current_summary": current_summary,
                "final_url": page.url,
            }

            save_json("order_submit_result.json", result)

            log("已送出訂單，結果已存到 order_submit_result.json")
            log(json.dumps(result, ensure_ascii=False, indent=2))

            context.storage_state(path=STATE_FILE)
            return result

        finally:
            try:
                context.storage_state(path=STATE_FILE)
            except Exception:
                pass

            browser.close()


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("product_url", nargs="?", help="Funbox 商品網址")
    parser.add_argument("--json", action="store_true", help="不等待 Enter，輸出 order_summary.json")
    parser.add_argument("--submit-prepared", action="store_true", help="讀取 order_summary.json 並送出訂單")

    args = parser.parse_args()

    if args.submit_prepared:
        submit_prepared_order()
        return

    if not args.product_url:
        print("用法：")
        print("python funbox_buyer.py 商品網址")
        print("python funbox_buyer.py 商品網址 --json")
        print("python funbox_buyer.py --submit-prepared")
        sys.exit(1)

    buy_funbox_product(args.product_url, json_mode=args.json)


if __name__ == "__main__":
    main()