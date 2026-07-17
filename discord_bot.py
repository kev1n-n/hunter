import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import discord
from discord.ext import commands
from playwright.sync_api import sync_playwright


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


DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))
DISCORD_OWNER_ID = int(os.getenv("DISCORD_OWNER_ID", "0"))

FUNBOX_MONITOR_ENABLED = os.getenv("FUNBOX_MONITOR_ENABLED", "false").lower() in [
    "1",
    "true",
    "yes",
    "on",
]

FUNBOX_MONITOR_INTERVAL_SECONDS = int(
    os.getenv("FUNBOX_MONITOR_INTERVAL_SECONDS", "180")
)

FUNBOX_MONITOR_MODE = os.getenv("FUNBOX_MONITOR_MODE", "product").strip().lower()

FUNBOX_MONITOR_URLS = [
    url.strip()
    for url in os.getenv(
        "FUNBOX_MONITOR_URLS",
        "https://shop.funbox.com.tw/products/tm07984",
    ).split(",")
    if url.strip()
]

FUNBOX_MONITOR_HEADLESS = os.getenv("FUNBOX_MONITOR_HEADLESS", "true").lower() in [
    "1",
    "true",
    "yes",
    "on",
]

FUNBOX_MONITOR_PAGE_TIMEOUT_MS = int(
    os.getenv("FUNBOX_MONITOR_PAGE_TIMEOUT_MS", "60000")
)

FUNBOX_MAX_PRICE = int(os.getenv("FUNBOX_MAX_PRICE", "2500"))

FUNBOX_CATEGORY_SCAN_ALL = os.getenv("FUNBOX_CATEGORY_SCAN_ALL", "false").lower() in [
    "1",
    "true",
    "yes",
    "on",
]

FUNBOX_CATEGORY_MAX_PRODUCTS = int(os.getenv("FUNBOX_CATEGORY_MAX_PRODUCTS", "30"))

FUNBOX_PRODUCT_KEYWORDS = [
    keyword.strip()
    for keyword in os.getenv(
        "FUNBOX_PRODUCT_KEYWORDS",
        "BEYBLADE,戰鬥陀螺,陀螺,BX-,CX-,UX-,XONE,爆旋陀螺",
    ).split(",")
    if keyword.strip()
]

FUNBOX_EXCLUDE_KEYWORDS = [
    keyword.strip()
    for keyword in os.getenv(
        "FUNBOX_EXCLUDE_KEYWORDS",
        "預購,預定,商品預購,電子書,電子序號,序號兌換,序號,兌換碼,下載版,虛擬商品,DLC,點數卡,交換券,體驗券,抽選,預約",
    ).split(",")
    if keyword.strip()
]


TEST_PRODUCT = {
    "name": "Funbox 測試商品",
    "price": "NT$295",
    "url": "https://shop.funbox.com.tw/products/tm07984",
}


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

monitor_task: asyncio.Task | None = None
scan_lock = asyncio.Lock()
alerted_products: dict[str, float] = {}


def log(message: str):
    print(f"[discord-bot] {message}", flush=True)


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def run_command(command: list[str], timeout: int = 360) -> tuple[bool, str]:
    env = os.environ.copy()

    try:
        result = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = ""
        output += result.stdout or ""
        output += "\n"
        output += result.stderr or ""

        if result.returncode == 0:
            return True, output[-2500:]

        return False, output[-2500:]

    except subprocess.TimeoutExpired:
        return False, f"指令執行超過 {timeout} 秒，已逾時停止"

    except Exception as e:
        return False, f"執行指令發生錯誤：{e}"


def prepare_funbox_order(product_url: str) -> tuple[bool, str, dict | None]:
    command = [
        sys.executable,
        "funbox_buyer.py",
        product_url,
        "--json",
    ]

    success, output = run_command(command, timeout=420)

    if not success:
        return False, output, None

    if not Path(ORDER_SUMMARY_FILE).exists():
        return False, "funbox_buyer.py 執行成功，但找不到 order_summary.json", None

    try:
        summary = load_json(ORDER_SUMMARY_FILE)
        return True, output, summary

    except Exception as e:
        return False, f"讀取 order_summary.json 失敗：{e}\n\n{output}", None


def submit_prepared_order() -> tuple[bool, str]:
    command = [
        sys.executable,
        "funbox_buyer.py",
        "--submit-prepared",
    ]

    return run_command(command, timeout=420)


def extract_price_from_text(text: str) -> int | None:
    patterns = [
        r"NT\$\s*([\d,]+)",
        r"\$\s*([\d,]+)",
    ]

    prices = []

    for pattern in patterns:
        for match in re.findall(pattern, text):
            try:
                value = int(match.replace(",", ""))

                if 10 <= value <= 100000:
                    prices.append(value)

            except Exception:
                continue

    if not prices:
        return None

    return min(prices)


def extract_product_name_from_page(page) -> str:
    selectors = [
        "h1",
        ".product-title",
        ".product_name",
        ".product-name",
        "[class*='product'][class*='title']",
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector)

            if locator.count() > 0:
                text = locator.first.inner_text(timeout=3000).strip()

                if text:
                    return re.sub(r"\s+", " ", text)

        except Exception:
            continue

    try:
        title = page.title()
        title = title.replace("Funbox Toys官方購物網站", "").strip(" |-")

        if title:
            return title

    except Exception:
        pass

    return "Funbox 商品"


def is_add_to_cart_visible(page) -> bool:
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
                    disabled = item.get_attribute("disabled")
                    aria_disabled = item.get_attribute("aria-disabled")

                    if disabled is not None:
                        continue

                    if aria_disabled == "true":
                        continue

                    return True

        except Exception:
            continue

    return False


def keyword_match(text: str) -> bool:
    if not text:
        return False

    lower_text = text.lower()

    for keyword in FUNBOX_PRODUCT_KEYWORDS:
        if keyword.lower() in lower_text:
            return True

    return False


def exclude_match(text: str) -> bool:
    if not text:
        return False

    lower_text = text.lower()

    for keyword in FUNBOX_EXCLUDE_KEYWORDS:
        if keyword.lower() in lower_text:
            return True

    return False


def find_matched_keywords(text: str, keywords: list[str]) -> list[str]:
    if not text:
        return []

    lower_text = text.lower()
    matched = []

    for keyword in keywords:
        if keyword.lower() in lower_text:
            matched.append(keyword)

    return matched


def short_preview(text: str, limit: int = 120) -> str:
    if not text:
        return ""

    compact = re.sub(r"\s+", " ", text).strip()

    if len(compact) <= limit:
        return compact

    return compact[:limit] + "..."


def normalize_funbox_url(url: str) -> str:
    if not url:
        return ""

    url = url.strip()

    if url.startswith("/"):
        return urljoin("https://shop.funbox.com.tw", url)

    return url


def extract_name_from_category_text(text: str) -> str:
    if not text:
        return "Funbox 商品"

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    bad_words = [
        "加入購物車",
        "已售完",
        "補貨中",
        "NT$",
        "$",
        "購買",
        "商品列表",
        "加入收藏",
    ]

    candidates = []

    for line in lines:
        if len(line) <= 2:
            continue

        if any(word in line for word in bad_words):
            continue

        candidates.append(line)

    if candidates:
        return candidates[0][:120]

    compact = re.sub(r"\s+", " ", text).strip()

    if compact:
        return compact[:120]

    return "Funbox 商品"


def collect_products_from_category(category_url: str) -> list[dict]:
    log(f"準備掃描 Funbox 分類頁：{category_url}")

    products: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=FUNBOX_MONITOR_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1365, "height": 1600},
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )

        page = context.new_page()
        page.set_default_timeout(FUNBOX_MONITOR_PAGE_TIMEOUT_MS)

        try:
            page.goto(
                category_url,
                wait_until="domcontentloaded",
                timeout=FUNBOX_MONITOR_PAGE_TIMEOUT_MS,
            )

            page.wait_for_timeout(3000)

            for index in range(6):
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(1000)
                log(f"分類頁滾動載入商品 {index + 1}/6")

            raw_products = page.evaluate(
                """
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));

                    const items = anchors
                        .map(a => {
                            const href = a.href || '';
                            const text = (a.innerText || a.textContent || '').trim();

                            let parentText = '';

                            let node = a;
                            for (let i = 0; i < 5; i++) {
                                if (!node || !node.parentElement) break;
                                node = node.parentElement;
                                const t = (node.innerText || node.textContent || '').trim();
                                if (t.length > parentText.length) {
                                    parentText = t;
                                }
                            }

                            return {
                                href,
                                text,
                                parentText,
                            };
                        })
                        .filter(item => item.href.includes('/products/'));

                    const seen = new Set();
                    const results = [];

                    for (const item of items) {
                        const cleanUrl = item.href.split('?')[0];

                        if (seen.has(cleanUrl)) {
                            continue;
                        }

                        seen.add(cleanUrl);

                        const fullText = `${item.text}\\n${item.parentText}`.trim();

                        results.push({
                            url: cleanUrl,
                            text: fullText,
                        });
                    }

                    return results;
                }
                """
            )

            log(f"分類頁抓到商品連結數：{len(raw_products)}")

            for item in raw_products:
                product_url = normalize_funbox_url(item.get("url", ""))
                text = item.get("text", "")

                if not product_url:
                    continue

                name = extract_name_from_category_text(text)

                combined_text = f"{name}\n{text}\n{product_url}"

                exclude_hits = find_matched_keywords(
                    combined_text,
                    FUNBOX_EXCLUDE_KEYWORDS,
                )

                include_hits = find_matched_keywords(
                    combined_text,
                    FUNBOX_PRODUCT_KEYWORDS,
                )

                # 黑名單優先：預購、電子書、序號兌換、下載版先排除。
                # 注意：這裡只看商品卡片文字、商品名稱與網址，不用整個頁面 body，避免被導覽列「商品預購」誤殺。
                if exclude_hits:
                    log(
                        "排除商品：命中黑名單 | "
                        f"商品={name} | "
                        f"命中={', '.join(exclude_hits)} | "
                        f"網址={product_url} | "
                        f"卡片文字={short_preview(text)}"
                    )
                    continue

                # 白名單：只抓戰鬥陀螺相關商品。
                if not FUNBOX_CATEGORY_SCAN_ALL:
                    if not include_hits:
                        log(
                            "排除商品：未命中戰鬥陀螺關鍵字 | "
                            f"商品={name} | "
                            f"網址={product_url} | "
                            f"卡片文字={short_preview(text)}"
                        )
                        continue

                log(
                    "加入候選商品："
                    f"商品={name} | "
                    f"命中白名單={', '.join(include_hits) if include_hits else '無'} | "
                    f"網址={product_url}"
                )

                products.append(
                    {
                        "url": product_url,
                        "name": name,
                        "raw_text": text,
                    }
                )

                if len(products) >= FUNBOX_CATEGORY_MAX_PRODUCTS:
                    break

            log(f"分類頁最後準備逐一確認商品數：{len(products)}")
            return products

        except Exception as e:
            log(f"分類頁掃描失敗：{e}")
            return products

        finally:
            browser.close()


def scan_funbox_product(product_url: str, fallback_name: str | None = None) -> dict:
    result = {
        "url": product_url,
        "name": fallback_name or "Funbox 商品",
        "price": None,
        "price_text": "未抓到",
        "available": False,
        "reason": "",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=FUNBOX_MONITOR_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1365, "height": 1200},
            locale="zh-TW",
            timezone_id="Asia/Taipei",
        )

        page = context.new_page()
        page.set_default_timeout(FUNBOX_MONITOR_PAGE_TIMEOUT_MS)

        try:
            page.goto(
                product_url,
                wait_until="domcontentloaded",
                timeout=FUNBOX_MONITOR_PAGE_TIMEOUT_MS,
            )

            page.wait_for_timeout(3000)

            name = extract_product_name_from_page(page)

            if name and name != "Funbox 商品":
                result["name"] = name

            # 商品名稱命中黑名單就直接排除。
            # 不用整頁 body 判斷，避免被導覽列「商品預購」誤殺。
            name_exclude_hits = find_matched_keywords(
                result["name"],
                FUNBOX_EXCLUDE_KEYWORDS,
            )

            if name_exclude_hits:
                result["available"] = False
                result["reason"] = (
                    f"商品名稱命中排除關鍵字：{', '.join(name_exclude_hits)} | "
                    f"{result['name']}"
                )
                return result

            # 如果是分類頁模式，也再次確認商品名稱真的有戰鬥陀螺關鍵字。
            if FUNBOX_MONITOR_MODE == "category" and not FUNBOX_CATEGORY_SCAN_ALL:
                include_hits = find_matched_keywords(
                    f"{result['name']}\n{product_url}",
                    FUNBOX_PRODUCT_KEYWORDS,
                )

                if not include_hits:
                    result["available"] = False
                    result["reason"] = f"商品名稱未命中戰鬥陀螺關鍵字：{result['name']}"
                    return result

            body_text = page.inner_text("body", timeout=15000)
            price = extract_price_from_text(body_text)

            result["price"] = price
            result["price_text"] = f"NT${price}" if price is not None else "未抓到"

            sold_out_words = [
                "已售完",
                "暫無庫存",
                "無庫存",
                "補貨中",
            ]

            has_sold_out_word = any(word in body_text for word in sold_out_words)
            has_add_to_cart = is_add_to_cart_visible(page)

            if price is not None and price > FUNBOX_MAX_PRICE:
                result["available"] = False
                result["reason"] = f"價格 NT${price} 超過上限 NT${FUNBOX_MAX_PRICE}"
                return result

            if has_add_to_cart:
                result["available"] = True
                result["reason"] = "找到可見的加入購物車按鈕，符合現貨通知條件"
                return result

            if has_sold_out_word:
                result["available"] = False
                result["reason"] = "頁面顯示售完或無庫存"
                return result

            result["available"] = False
            result["reason"] = "沒有找到加入購物車按鈕"

            return result

        except Exception as e:
            result["available"] = False
            result["reason"] = f"掃描失敗：{e}"
            return result

        finally:
            browser.close()


def expand_monitor_targets() -> list[dict]:
    targets: list[dict] = []

    for url in FUNBOX_MONITOR_URLS:
        is_category = (
            FUNBOX_MONITOR_MODE == "category"
            or "/categories/" in url
        )

        if is_category:
            products = collect_products_from_category(url)

            for product in products:
                targets.append(
                    {
                        "url": product["url"],
                        "name": product.get("name") or "Funbox 商品",
                        "source": "category",
                    }
                )
        else:
            targets.append(
                {
                    "url": url,
                    "name": "Funbox 商品",
                    "source": "product",
                }
            )

    deduped = []
    seen = set()

    for target in targets:
        clean_url = target["url"].split("?")[0]

        if clean_url in seen:
            continue

        seen.add(clean_url)
        target["url"] = clean_url
        deduped.append(target)

    return deduped


def extract_product_name_from_raw_preview(raw_preview: str) -> str | None:
    lines = [line.strip() for line in raw_preview.splitlines() if line.strip()]

    for index, line in enumerate(lines):
        if line == "購物車內容":
            nearby = lines[index + 1:index + 8]

            for candidate in nearby:
                if not candidate:
                    continue

                bad_words = [
                    "會員專區",
                    "付款運送方式",
                    "結帳金額",
                    "紅利點數",
                    "運費",
                    "應付總額",
                    "購物車內容",
                    "數量",
                    "合計有",
                    "預估可獲得紅利",
                ]

                if any(word in candidate for word in bad_words):
                    continue

                if "NT$" in candidate:
                    continue

                return candidate.strip()

    for index, line in enumerate(lines):
        if line == "商品明細":
            nearby = lines[index + 1:index + 20]

            for candidate in nearby:
                candidate = candidate.strip()

                if not candidate:
                    continue

                bad_words = [
                    "單價",
                    "數量",
                    "小計",
                    "NT$",
                    "-",
                    "+",
                ]

                if candidate in bad_words:
                    continue

                if any(word in candidate for word in bad_words):
                    continue

                return candidate

    match = re.search(r"(Online Mall限定[^\n]+)", raw_preview)

    if match:
        return match.group(1).strip()

    return None


def clean_product_name_from_summary(summary: dict) -> str:
    raw_preview = summary.get("raw_preview", "")
    raw_name = extract_product_name_from_raw_preview(raw_preview)

    if raw_name:
        return raw_name

    items = summary.get("items") or []

    if items:
        name = items[0].get("name", "").strip()

        bad_words = [
            "會員專區",
            "付款運送方式",
            "結帳金額",
            "紅利點數",
            "運費",
            "應付總額",
        ]

        if name and not any(word in name for word in bad_words):
            return name

    return "未知商品"


def get_quantity_from_summary(summary: dict) -> int:
    items = summary.get("items") or []

    if items:
        quantity = items[0].get("quantity")

        if isinstance(quantity, int):
            return quantity

    raw_preview = summary.get("raw_preview", "")
    match = re.search(r"數量：\s*(\d+)", raw_preview)

    if match:
        return int(match.group(1))

    return 1


def money(value) -> str:
    if value is None:
        return "未抓到"

    value = int(value)

    if value < 0:
        return f"-NT${abs(value)}"

    return f"NT${value}"


def build_product_embed(product: dict) -> discord.Embed:
    embed = discord.Embed(
        title="🎯 Funbox 發現現貨",
        description=product["name"],
        color=0x00AA55,
    )

    embed.add_field(name="價格", value=product["price"], inline=True)
    embed.add_field(name="狀態", value=product.get("reason", "有貨"), inline=False)
    embed.add_field(name="模式", value="先準備訂單，不會直接送出", inline=False)
    embed.add_field(name="商品連結", value=product["url"], inline=False)

    return embed


def build_order_summary_embed(summary: dict) -> discord.Embed:
    product_name = clean_product_name_from_summary(summary)
    quantity = get_quantity_from_summary(summary)

    subtotal = summary.get("subtotal")
    shipping = summary.get("shipping")
    discount = summary.get("discount")
    total = summary.get("total")
    store = summary.get("store", "未抓到")
    payment = summary.get("payment", "未抓到")
    checkout_url = summary.get("checkout_url", "")

    embed = discord.Embed(
        title="🛒 訂單確認",
        description="請確認內容與金額，確認無誤後再按「確認送出訂單」。",
        color=0xFFAA00,
    )

    embed.add_field(
        name="商品",
        value=f"{product_name}\n數量：{quantity}",
        inline=False,
    )

    embed.add_field(name="付款方式", value=payment, inline=True)
    embed.add_field(name="取貨門市", value=store, inline=True)

    embed.add_field(name="商品合計", value=money(subtotal), inline=True)
    embed.add_field(name="運費", value=money(shipping), inline=True)
    embed.add_field(name="紅利折抵", value=money(discount), inline=True)
    embed.add_field(name="應付總額", value=money(total), inline=False)

    if checkout_url:
        embed.add_field(name="結帳網址", value=checkout_url, inline=False)

    embed.set_footer(text="注意：按下確認送出訂單後，會真的嘗試送出訂單。")

    return embed


class FinalSubmitView(discord.ui.View):
    def __init__(self, summary: dict):
        super().__init__(timeout=300)
        self.summary = summary
        self.has_clicked = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if DISCORD_OWNER_ID == 0:
            await interaction.response.send_message(
                "DISCORD_OWNER_ID 尚未設定，為了安全不能送出訂單。",
                ephemeral=True,
            )
            return False

        if interaction.user.id != DISCORD_OWNER_ID:
            await interaction.response.send_message(
                "這個送出訂單按鈕只允許指定使用者操作。",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="確認送出訂單", style=discord.ButtonStyle.danger)
    async def confirm_submit_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.has_clicked:
            await interaction.response.send_message(
                "這筆訂單已經處理過了。",
                ephemeral=True,
            )
            return

        self.has_clicked = True

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.followup.send(
            "收到確認，準備送出 Funbox 訂單。",
            ephemeral=False,
        )

        loop = asyncio.get_running_loop()
        success, output = await loop.run_in_executor(
            None,
            submit_prepared_order,
        )

        if success:
            await interaction.followup.send(
                "✅ 已嘗試送出訂單。\n"
                "請到 Funbox 會員訂單頁或信箱確認是否成立。\n\n"
                f"```text\n{output[-1800:]}\n```"
            )
        else:
            await interaction.followup.send(
                "❌ 送出訂單失敗。\n\n"
                f"```text\n{output[-1800:]}\n```"
            )

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.has_clicked = True

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.followup.send(
            "已取消送出訂單。",
            ephemeral=False,
        )


class BuyConfirmView(discord.ui.View):
    def __init__(self, product: dict):
        super().__init__(timeout=180)
        self.product = product
        self.has_clicked = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if DISCORD_OWNER_ID == 0:
            await interaction.response.send_message(
                "DISCORD_OWNER_ID 尚未設定，為了安全不能購買。",
                ephemeral=True,
            )
            return False

        if interaction.user.id != DISCORD_OWNER_ID:
            await interaction.response.send_message(
                "這個購買按鈕只允許指定使用者操作。",
                ephemeral=True,
            )
            return False

        return True

    @discord.ui.button(label="購買 / 準備訂單", style=discord.ButtonStyle.green)
    async def buy_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if self.has_clicked:
            await interaction.response.send_message(
                "這筆購買流程已經執行過了。",
                ephemeral=True,
            )
            return

        self.has_clicked = True

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)

        product_name = self.product["name"]
        product_url = self.product["url"]

        await interaction.followup.send(
            f"收到確認，準備建立 Funbox 訂單：\n{product_name}",
            ephemeral=False,
        )

        loop = asyncio.get_running_loop()
        success, output, summary = await loop.run_in_executor(
            None,
            prepare_funbox_order,
            product_url,
        )

        if not success or summary is None:
            await interaction.followup.send(
                "❌ Funbox 準備訂單失敗。\n\n"
                f"```text\n{output[-1800:]}\n```"
            )
            return

        order_embed = build_order_summary_embed(summary)
        final_view = FinalSubmitView(summary)

        await interaction.followup.send(
            "✅ 訂單已準備好，請確認以下內容。",
            embed=order_embed,
            view=final_view,
        )

    @discord.ui.button(label="略過", style=discord.ButtonStyle.red)
    async def skip_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        self.has_clicked = True

        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(view=self)

        await interaction.followup.send(
            "已略過這筆商品。",
            ephemeral=False,
        )


async def get_alert_channel():
    if DISCORD_CHANNEL_ID == 0:
        return None

    channel = bot.get_channel(DISCORD_CHANNEL_ID)

    if channel is not None:
        return channel

    try:
        channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
        return channel
    except Exception as e:
        log(f"抓取 DISCORD_CHANNEL_ID 失敗：{e}")
        return None


async def send_product_alert(product: dict):
    channel = await get_alert_channel()

    if channel is None:
        log("找不到 Discord 頻道，無法發送商品通知")
        return False

    embed = build_product_embed(product)
    view = BuyConfirmView(product)

    await channel.send(embed=embed, view=view)
    return True


async def scan_all_targets_once(send_alerts: bool = True) -> list[dict]:
    if scan_lock.locked():
        log("上一輪掃描尚未完成，本輪跳過")
        return []

    async with scan_lock:
        results = []
        targets = await asyncio.to_thread(expand_monitor_targets)

        log(f"本輪實際掃描商品數：{len(targets)}")

        for target in targets:
            product_url = target["url"]
            fallback_name = target.get("name") or "Funbox 商品"

            log(f"開始確認商品頁：{product_url}")

            result = await asyncio.to_thread(
                scan_funbox_product,
                product_url,
                fallback_name,
            )

            results.append(result)

            log(
                f"掃描結果：available={result['available']} "
                f"name={result['name']} "
                f"price={result['price_text']} "
                f"reason={result['reason']}"
            )

            key = result["url"].split("?")[0]

            if result["available"]:
                if key not in alerted_products:
                    product = {
                        "name": result["name"],
                        "price": result["price_text"],
                        "url": result["url"],
                        "reason": result["reason"],
                    }

                    if send_alerts:
                        sent = await send_product_alert(product)

                        if sent:
                            alerted_products[key] = time.time()
                            log(f"已發送有貨通知：{result['name']}")
                        else:
                            log("通知發送失敗，不記錄已通知狀態")
                    else:
                        log("send_alerts=False，不發送 Discord 通知")
                else:
                    log("此商品已通知過，略過避免洗版")
            else:
                if key in alerted_products:
                    log("商品目前無貨，解除已通知狀態，等待下次重新補貨")
                    alerted_products.pop(key, None)

        return results


async def monitor_funbox_loop():
    await bot.wait_until_ready()

    log("Funbox 監控背景任務啟動")
    log(f"監控啟用：{FUNBOX_MONITOR_ENABLED}")
    log(f"監控模式：{FUNBOX_MONITOR_MODE}")
    log(f"分類頁掃全部：{FUNBOX_CATEGORY_SCAN_ALL}")
    log(f"分類頁最多商品數：{FUNBOX_CATEGORY_MAX_PRODUCTS}")
    log(f"監控間隔：{FUNBOX_MONITOR_INTERVAL_SECONDS} 秒")
    log(f"監控網址數：{len(FUNBOX_MONITOR_URLS)}")
    log(f"白名單關鍵字：{', '.join(FUNBOX_PRODUCT_KEYWORDS)}")
    log(f"黑名單關鍵字：{', '.join(FUNBOX_EXCLUDE_KEYWORDS)}")

    try:
        while not bot.is_closed():
            try:
                if not FUNBOX_MONITOR_ENABLED:
                    log("Funbox 監控目前停用，等待下一輪")
                    await asyncio.sleep(FUNBOX_MONITOR_INTERVAL_SECONDS)
                    continue

                log("開始新一輪 Funbox 掃描")
                await scan_all_targets_once(send_alerts=True)
                log(
                    f"本輪 Funbox 掃描完成，"
                    f"{FUNBOX_MONITOR_INTERVAL_SECONDS} 秒後再掃描"
                )

                await asyncio.sleep(FUNBOX_MONITOR_INTERVAL_SECONDS)

            except asyncio.CancelledError:
                log("Funbox 監控背景任務被取消")
                raise

            except Exception as e:
                log(f"Funbox 監控背景任務錯誤：{type(e).__name__}: {e}")
                await asyncio.sleep(30)

    finally:
        log("Funbox 監控背景任務已結束")


def monitor_task_done(task: asyncio.Task):
    if task.cancelled():
        log("Funbox 監控 task 狀態：已取消")
        return

    error = task.exception()

    if error is not None:
        log(f"Funbox 監控 task 意外結束：{type(error).__name__}: {error}")
    else:
        log("Funbox 監控 task 已正常結束")


@bot.event
async def on_ready():
    global monitor_task

    log(f"已登入：{bot.user}")
    log("在 Discord 頻道輸入 !ping 測試")
    log("在 Discord 頻道輸入 !testbuy 測試購買按鈕")
    log("在 Discord 頻道輸入 !summary 測試訂單摘要")
    log("在 Discord 頻道輸入 !monitorstatus 查看監控狀態")
    log("在 Discord 頻道輸入 !scanonce 手動掃描一次")
    log("在 Discord 頻道輸入 !resetalerts 清空已通知紀錄")

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(
            monitor_funbox_loop(),
            name="funbox-monitor",
        )
        monitor_task.add_done_callback(monitor_task_done)
        log("已建立 Funbox 監控背景任務")
    else:
        log("Funbox 監控背景任務仍在執行，不重複建立")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.reply("pong")


@bot.command(name="testbuy")
async def testbuy(ctx: commands.Context):
    if DISCORD_OWNER_ID != 0 and ctx.author.id != DISCORD_OWNER_ID:
        await ctx.reply("你不是指定的操作使用者。")
        return

    embed = build_product_embed(TEST_PRODUCT)
    view = BuyConfirmView(TEST_PRODUCT)

    await ctx.send(embed=embed, view=view)


@bot.command(name="summary")
async def summary(ctx: commands.Context):
    if DISCORD_OWNER_ID != 0 and ctx.author.id != DISCORD_OWNER_ID:
        await ctx.reply("你不是指定的操作使用者。")
        return

    if not Path(ORDER_SUMMARY_FILE).exists():
        await ctx.reply("目前找不到 order_summary.json。")
        return

    data = load_json(ORDER_SUMMARY_FILE)
    embed = build_order_summary_embed(data)
    view = FinalSubmitView(data)

    await ctx.send(embed=embed, view=view)


@bot.command(name="monitorstatus")
async def monitorstatus(ctx: commands.Context):
    if DISCORD_OWNER_ID != 0 and ctx.author.id != DISCORD_OWNER_ID:
        await ctx.reply("你不是指定的操作使用者。")
        return

    urls_text = "\n".join(FUNBOX_MONITOR_URLS)
    keywords_text = ", ".join(FUNBOX_PRODUCT_KEYWORDS)
    excludes_text = ", ".join(FUNBOX_EXCLUDE_KEYWORDS)

    message = (
        "Funbox 監控狀態\n"
        f"啟用：{FUNBOX_MONITOR_ENABLED}\n"
        f"模式：{FUNBOX_MONITOR_MODE}\n"
        f"間隔：{FUNBOX_MONITOR_INTERVAL_SECONDS} 秒\n"
        f"Headless：{FUNBOX_MONITOR_HEADLESS}\n"
        f"價格上限：NT${FUNBOX_MAX_PRICE}\n"
        f"分類頁掃全部：{FUNBOX_CATEGORY_SCAN_ALL}\n"
        f"分類頁最多商品數：{FUNBOX_CATEGORY_MAX_PRODUCTS}\n"
        f"背景任務：{'未建立' if monitor_task is None else ('執行中' if not monitor_task.done() else '已停止')}\n"
        f"掃描鎖定中：{scan_lock.locked()}\n"
        f"已通知商品數：{len(alerted_products)}\n"
        f"白名單關鍵字：{keywords_text}\n"
        f"黑名單關鍵字：{excludes_text}\n"
        f"監控網址：\n{urls_text}"
    )

    await ctx.reply(f"```text\n{message}\n```")


@bot.command(name="resetalerts")
async def resetalerts(ctx: commands.Context):
    if DISCORD_OWNER_ID != 0 and ctx.author.id != DISCORD_OWNER_ID:
        await ctx.reply("你不是指定的操作使用者。")
        return

    alerted_products.clear()
    await ctx.reply("已清空已通知紀錄。")


@bot.command(name="scanonce")
async def scanonce(ctx: commands.Context):
    if DISCORD_OWNER_ID != 0 and ctx.author.id != DISCORD_OWNER_ID:
        await ctx.reply("你不是指定的操作使用者。")
        return

    await ctx.reply("開始手動掃描 Funbox 商品，請稍等...")

    results = await scan_all_targets_once(send_alerts=True)

    if not results:
        await ctx.send("沒有掃到任何商品。")
        return

    available_count = sum(1 for item in results if item.get("available"))

    lines = [
        f"本次掃描商品數：{len(results)}",
        f"有貨商品數：{available_count}",
        "",
    ]

    for item in results[:15]:
        lines.append(f"商品：{item['name']}")
        lines.append(f"價格：{item['price_text']}")
        lines.append(f"有貨：{item['available']}")
        lines.append(f"原因：{item['reason']}")
        lines.append(f"網址：{item['url']}")
        lines.append("-" * 30)

    await ctx.send(f"```text\n{chr(10).join(lines)}\n```")


def main():
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("找不到 DISCORD_BOT_TOKEN，請先在 .env 設定")

    log("啟動中...")

    if DISCORD_CHANNEL_ID == 0:
        log("警告：DISCORD_CHANNEL_ID 尚未設定")

    if DISCORD_OWNER_ID == 0:
        log("警告：DISCORD_OWNER_ID 尚未設定，購買按鈕會被禁止")

    bot.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()