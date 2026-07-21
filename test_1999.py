import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from notifier import send_restock_alert


HOBBYSEARCH_URL = (
    "https://www.1999.co.jp/search"
    "?typ1_c=100&cat=&state=&sold=0&sortid=7&searchkey=Beyblade+X"
)

HEADLESS = True
NOTIFY_INTERVAL_SECONDS = int(
    os.getenv("HOBBYSEARCH_NOTIFY_INTERVAL_SECONDS", "86400")
)

DEFAULT_STATE_FILE = (
    "/data/hobbysearch_notification_state.json"
    if Path("/data").is_dir()
    else "hobbysearch_notification_state.json"
)
STATE_FILE = Path(os.getenv("HOBBYSEARCH_STATE_FILE", DEFAULT_STATE_FILE))

LABEL_MAP = {
    "in_stock": "✅ 現貨可購買",
    "preorder": "📌 預購可下單",
    "out_of_stock": "❌ 目前無庫存",
    "unknown": "❓ 狀態未知",
}


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower().strip()


def normalize_product_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def load_notification_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"[!] 讀取通知紀錄失敗：{exc}", flush=True)
        return {}


def save_notification_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(STATE_FILE.parent),
            prefix=f".{STATE_FILE.name}.",
            suffix=".tmp",
        ) as temp_file:
            json.dump(state, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
            temp_name = temp_file.name
        os.replace(temp_name, STATE_FILE)
    except Exception as exc:
        print(f"[!] 儲存通知紀錄失敗：{exc}", flush=True)


def should_notify(product: dict, state: dict, now_timestamp: float) -> bool:
    key = normalize_product_url(product["url"])
    current_status = product["status"]
    previous = state.get(key)

    if not previous:
        return True

    if previous.get("status") != current_status:
        return True

    last_notified_at = float(previous.get("last_notified_at", 0))
    return now_timestamp - last_notified_at >= NOTIFY_INTERVAL_SECONDS


def update_state_after_notification(product: dict, state: dict, now_timestamp: float) -> None:
    key = normalize_product_url(product["url"])
    state[key] = {
        "name": product.get("name", ""),
        "status": product.get("status", ""),
        "last_notified_at": now_timestamp,
        "last_notified_iso": datetime.fromtimestamp(
            now_timestamp, tz=timezone.utc
        ).isoformat(),
    }


def update_observed_status(product: dict, state: dict, now_timestamp: float) -> None:
    key = normalize_product_url(product["url"])
    previous = state.get(key, {})
    if product["status"] == "out_of_stock":
        previous["status"] = "out_of_stock"
        previous["name"] = product.get("name", "")
        previous["last_seen_at"] = now_timestamp
        state[key] = previous


def clean_product_name(name: str) -> str:
    name = re.sub(
        r"^\d{4}年\d{1,2}月(?:上旬|中旬|下旬|\d{1,2}日)?\s*発売\s*",
        "",
        name,
    )
    name = re.sub(r"\s*\(スポーツ玩具\)\s*$", "", name)
    return re.sub(r"\s+", " ", name).strip()


def is_beyblade_x_product(product: dict) -> bool:
    text = f"{product.get('name', '')} {product.get('raw_text', '')}"
    compact = normalize_text(text)
    has_beyblade = (
        "beybladex" in compact
        or "ベイブレードx" in compact
        or "戦闘ベイブレード" in compact
    )
    has_code = bool(re.search(r"\b(?:BX|UX|CX|BXG)-\d+", text, re.IGNORECASE))

    exclude_keywords = ["電子書籍", "書籍", "コミック", "雑誌", "カード", "ステッカー", "シール"]
    if any(normalize_text(word) in compact for word in exclude_keywords):
        product["excluded_reason"] = "排除非陀螺商品"
        return False

    return has_beyblade or has_code


def fetch_hobbysearch_products() -> list:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
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
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1365, "height": 1400},
            ignore_https_errors=True,
            extra_http_headers={"Accept-Language": "ja,en-US;q=0.8,en;q=0.7"},
        )
        page = context.new_page()

        print("正在打開 Hobby Search（1999.co.jp）...", flush=True)
        try:
            page.goto(HOBBYSEARCH_URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            print(f"[!] Hobby Search 開啟失敗：{exc}", flush=True)
            browser.close()
            return []

        try:
            page.wait_for_function(
                r"""
                () => {
                    const body = document.body?.innerText || '';
                    const links = [...document.querySelectorAll('a[href]')]
                        .filter(a => /^\/\d{8}(?:[/?#]|$)/.test(new URL(a.href).pathname));
                    return body.includes('BEYBLADE X') || links.length > 0;
                }
                """,
                timeout=20000,
            )
        except Exception:
            print("[!] 等待商品列表逾時，使用目前內容繼續抓", flush=True)

        page.wait_for_timeout(3000)

        for index in range(2):
            count = page.evaluate(
                r"""
                () => [...document.querySelectorAll('a[href]')]
                    .filter(a => /^\/\d{8}(?:[/?#]|$)/.test(new URL(a.href).pathname)).length
                """
            )
            print(
                f"Hobby Search 滾動 {index + 1}/2，目前候選連結：{count}",
                flush=True,
            )
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1200)

        products = page.evaluate(
            r"""
            () => {
                const result = new Map();
                const clean = value => (value || '').replace(/\s+/g, ' ').trim();

                function isProductUrl(href) {
                    try {
                        const url = new URL(href);
                        return url.hostname.endsWith('1999.co.jp') && /^\/\d{8}$/.test(url.pathname);
                    } catch {
                        return false;
                    }
                }

                function normalizedUrl(href) {
                    const url = new URL(href);
                    return `${url.origin}${url.pathname}`;
                }

                function productLinkCount(node) {
                    if (!node?.querySelectorAll) return 0;
                    return new Set(
                        [...node.querySelectorAll('a[href]')]
                            .map(a => a.href)
                            .filter(isProductUrl)
                            .map(normalizedUrl)
                    ).size;
                }

                function findCard(anchor) {
                    let node = anchor;
                    let best = anchor.parentElement || anchor;
                    for (let depth = 0; depth < 14 && node; depth++) {
                        const text = clean(node.innerText || '');
                        const count = productLinkCount(node);
                        const looksLikeProduct = (
                            text.includes('BEYBLADE X') ||
                            text.includes('ベイブレードX') ||
                            /\b(?:BX|UX|CX|BXG)-\d+/i.test(text)
                        );
                        const hasUsefulStatus = (
                            text.includes('在庫なし') ||
                            text.includes('注文再開メール') ||
                            text.includes('予約') ||
                            text.includes('カートに入れる') ||
                            text.includes('注文する') ||
                            text.includes('在庫あり') ||
                            text.includes('¥')
                        );
                        if (looksLikeProduct && hasUsefulStatus && count <= 1) best = node;
                        if (count > 1) break;
                        node = node.parentElement;
                    }
                    return best;
                }

                function productName(card, anchor) {
                    const anchorText = clean(
                        anchor.innerText || anchor.textContent || anchor.querySelector('img')?.alt || ''
                    );
                    if (
                        anchorText.includes('BEYBLADE X') ||
                        anchorText.includes('ベイブレードX') ||
                        /\b(?:BX|UX|CX|BXG)-\d+/i.test(anchorText)
                    ) return anchorText;

                    const lines = (card?.innerText || '').split('\n').map(clean).filter(Boolean);
                    return lines.find(line =>
                        line.includes('BEYBLADE X') ||
                        line.includes('ベイブレードX') ||
                        /\b(?:BX|UX|CX|BXG)-\d+/i.test(line)
                    ) || anchorText || '未知商品';
                }

                function priceFromCard(card) {
                    const text = clean(card?.innerText || '');
                    const matches = [...text.matchAll(/¥\s*[\d,]+/g)];
                    return matches.length ? matches[0][0].replace(/\s+/g, '') : '';
                }

                function statusFromText(text) {
                    const compact = clean(text).replace(/\s+/g, '');
                    if (
                        compact.includes('在庫なし') ||
                        compact.includes('品切れ') ||
                        compact.includes('売り切れ') ||
                        compact.includes('売切') ||
                        compact.includes('注文再開メール') ||
                        compact.includes('販売終了') ||
                        compact.includes('受付終了') ||
                        compact.includes('予約終了')
                    ) return 'out_of_stock';

                    if (
                        compact.includes('予約受付中') ||
                        compact.includes('予約受付') ||
                        compact.includes('予約する') ||
                        compact.includes('予約注文') ||
                        compact.includes('予約商品') ||
                        compact.includes('ご予約')
                    ) return 'preorder';

                    if (
                        compact.includes('カートに入れる') ||
                        compact.includes('注文する') ||
                        compact.includes('購入する') ||
                        compact.includes('在庫あり') ||
                        compact.includes('残りわずか')
                    ) return 'in_stock';

                    return 'unknown';
                }

                const anchors = [...document.querySelectorAll('a[href]')].filter(a => isProductUrl(a.href));
                for (const anchor of anchors) {
                    const url = normalizedUrl(anchor.href);
                    const card = findCard(anchor);
                    const rawText = clean(card?.innerText || '');
                    const name = productName(card, anchor);

                    if (
                        !name.toLowerCase().includes('beyblade x') &&
                        !name.includes('ベイブレードX') &&
                        !/\b(?:BX|UX|CX|BXG)-\d+/i.test(name)
                    ) continue;

                    const candidate = {
                        store: 'Hobby Search 1999',
                        name,
                        url,
                        price: priceFromCard(card),
                        status: statusFromText(rawText),
                        status_label: '',
                        raw_text: rawText,
                    };

                    const existing = result.get(url);
                    if (
                        !existing ||
                        (existing.status === 'unknown' && candidate.status !== 'unknown') ||
                        candidate.raw_text.length > existing.raw_text.length
                    ) result.set(url, candidate);
                }
                return [...result.values()];
            }
            """
        )

        if not products:
            page.screenshot(path="hobbysearch_debug.png", full_page=True)
            print("抓到 0 個商品，已產生 hobbysearch_debug.png", flush=True)

        browser.close()
    return products


def run_once() -> None:
    products = fetch_hobbysearch_products()
    target_products = []
    excluded_products = []

    for product in products:
        product["name"] = clean_product_name(product.get("name", ""))
        if is_beyblade_x_product(product):
            product["status_label"] = LABEL_MAP.get(product["status"], LABEL_MAP["unknown"])
            product["name"] = f"[Hobby Search 1999] {product['name']}"
            target_products.append(product)
        else:
            excluded_products.append(product)

    in_stock = [p for p in target_products if p["status"] == "in_stock"]
    preorder = [p for p in target_products if p["status"] == "preorder"]
    out_of_stock = [p for p in target_products if p["status"] == "out_of_stock"]
    unknown = [p for p in target_products if p["status"] == "unknown"]

    print("=" * 60, flush=True)
    print(f"Hobby Search 抓到商品：{len(products)} 個", flush=True)
    print(f"Beyblade X 目標商品：{len(target_products)} 個", flush=True)
    print(f"現貨：{len(in_stock)} 個", flush=True)
    print(f"可預購：{len(preorder)} 個", flush=True)
    print(f"無貨：{len(out_of_stock)} 個", flush=True)
    print(f"未知：{len(unknown)} 個", flush=True)
    print(f"通知紀錄：{STATE_FILE}", flush=True)
    print("=" * 60, flush=True)

    state = load_notification_state()
    now_timestamp = datetime.now(tz=timezone.utc).timestamp()
    notify_products = in_stock + preorder

    if notify_products:
        print("\n🔔 檢查現貨／預購通知條件", flush=True)
        for product in notify_products:
            if should_notify(product, state, now_timestamp):
                print(
                    f"- 發送通知：{product['name']}｜{product['status_label']}｜{product.get('price', '')}",
                    flush=True,
                )
                print(f"  {product['url']}", flush=True)
                try:
                    send_restock_alert(product)
                    update_state_after_notification(product, state, now_timestamp)
                    save_notification_state(state)
                except Exception as exc:
                    print(f"  [!] Discord 通知失敗，不更新通知時間：{exc}", flush=True)
            else:
                print(f"- 24 小時內已通知，略過：{product['name']}", flush=True)
    else:
        print("\n目前沒有可下單的現貨或預購商品", flush=True)

    for product in out_of_stock:
        update_observed_status(product, state, now_timestamp)
    save_notification_state(state)

    if out_of_stock:
        print("\n❌ 無貨商品（最多顯示 30 個）", flush=True)
        for product in out_of_stock[:30]:
            print(f"- {product['name']} {product.get('price', '')}", flush=True)
            print(f"  {product['url']}", flush=True)

    if unknown:
        print("\n❓ 狀態未知商品（不通知）", flush=True)
        for product in unknown[:30]:
            print(f"- {product['name']} {product.get('price', '')}", flush=True)
            print(f"  原始內容：{product.get('raw_text', '')[:300]}", flush=True)
            print(f"  {product['url']}", flush=True)


def main() -> None:
    print("🛒 Hobby Search 1999 Beyblade X 獵人啟動", flush=True)
    print(f"   掃描網址：{HOBBYSEARCH_URL}", flush=True)
    print(f"   重複通知間隔：{NOTIFY_INTERVAL_SECONDS} 秒", flush=True)
    print(f"   通知紀錄檔：{STATE_FILE}", flush=True)
    print(f"   背景模式：{HEADLESS}", flush=True)

    if "--once" in sys.argv:
        now = datetime.now().strftime("%H:%M:%S")
        print(f"\n{'=' * 60}", flush=True)
        print(f"[1999][{now}] 開始掃描 Hobby Search...", flush=True)
        try:
            run_once()
        except Exception as exc:
            print(f"[!] Hobby Search 掃描錯誤：{exc}", flush=True)
            raise
        print("\n--once 模式結束", flush=True)
        return

    run_once()


if __name__ == "__main__":
    main()