import sys
import os
import re
import threading
import pytz
import requests
from datetime import datetime, timedelta

from flask import Flask
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Ensure terminal prints emojis correctly
sys.stdout.reconfigure(encoding='utf-8')

# ==============================
# CONFIG (from environment variables — set these on Render)
# ==============================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Each store needs a name + a "token URL" endpoint that returns {"access_token": "..."}
# Add/remove stores here if the count ever changes.
STORES = [
    {"key": "store1", "name": os.environ.get("STORE1_NAME", "مايكرو"), "token_url": os.environ["STORE1_TOKEN_URL"]},
    {"key": "store2", "name": os.environ.get("STORE2_NAME", "بيرق"), "token_url": os.environ["STORE2_TOKEN_URL"]},
    {"key": "store3", "name": os.environ.get("STORE3_NAME", "الشاهين"), "token_url": os.environ["STORE3_TOKEN_URL"]},
    {"key": "store4", "name": os.environ.get("STORE4_NAME", "زمرد"), "token_url": os.environ["STORE4_TOKEN_URL"]},
]
STORES_BY_KEY = {s["key"]: s for s in STORES}

SALLA_ORDERS_URL = "https://api.salla.dev/admin/v2/orders?"
SALLA_SHIPPING_URL = "https://api.salla.dev/admin/v2"

# In-memory cache of access tokens per store. Refreshed on startup and on 401.
ACCESS_TOKENS = {}


# ==============================
# TOKEN HANDLING
# ==============================

def fetch_access_token(store):
    """Hit the store's token URL to get a fresh Salla access token."""
    try:
        response = requests.get(store["token_url"], timeout=15)
        response.raise_for_status()
        token = response.json().get("access_token")
        if not token:
            print(f"⚠️ [{store['name']}] Token endpoint returned no access_token.")
        return token
    except Exception as e:
        print(f"⚠️ [{store['name']}] Failed to fetch access token: {e}")
        return None


def get_token(store_key):
    """Return cached token for a store, fetching it if missing."""
    if store_key not in ACCESS_TOKENS or not ACCESS_TOKENS[store_key]:
        ACCESS_TOKENS[store_key] = fetch_access_token(STORES_BY_KEY[store_key])
    return ACCESS_TOKENS[store_key]


def refresh_token(store_key):
    """Force-refresh a store's token (called after a 401)."""
    ACCESS_TOKENS[store_key] = fetch_access_token(STORES_BY_KEY[store_key])
    return ACCESS_TOKENS[store_key]


def salla_get(store_key, url, params=None):
    """
    GET request to Salla with automatic token refresh on 401.
    Returns the requests.Response object, or None if both attempts failed
    to even produce a response (e.g. network error).
    """
    token = get_token(store_key)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=20)
    except Exception as e:
        print(f"❌ [{store_key}] Request error: {e}")
        return None

    if response.status_code == 401:
        # Token likely expired — refresh once and retry.
        print(f"🔄 [{store_key}] Got 401, refreshing token and retrying...")
        token = refresh_token(store_key)
        if not token:
            return response  # still return the 401 so callers can handle it
        headers["Authorization"] = f"Bearer {token}"
        try:
            response = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            print(f"❌ [{store_key}] Retry request error: {e}")
            return None

    return response


# ==============================
# HELPERS
# ==============================

def normalize_phone(phone):
    return re.sub(r"\D", "", phone)


def get_order_details(store_key, order_id):
    url = f"https://api.salla.dev/admin/v2/orders/{order_id}"
    response = salla_get(store_key, url)
    if response is not None and response.status_code == 200:
        return response.json().get("data", {})
    return {}


def get_shipping_details(store_key, shipping_id):
    url = f"{SALLA_SHIPPING_URL}/{shipping_id}/shipments"
    response = salla_get(store_key, url)
    if response is not None and response.status_code == 200:
        return response.json().get("data", {})
    return {}


def sending_message_to_customer(store_key, orders):
    message = ""
    for order in orders:
        order_number = order.get("reference_id", "—")
        amount = order.get("total", {}).get("amount", 0)
        phone = order.get("customer", {}).get("mobile", "غير متوفر")

        items = order.get("items", [])
        product_names = ", ".join([item.get("name", "") for item in items])

        status = order.get("status", {}).get("name", "غير معروف")

        details = get_order_details(store_key, order["id"])
        tags = [tag.get("name", "لا يوجد وسوم") for tag in details.get("tags", [])]

        message += (
            f"🧾 رقم الطلب: #<code>{order_number}</code>\n"
            f"💰 السعر: {amount} SAR\n"
            f"📞 الهاتف: <code>{phone}</code>\n"
            f"📦 المنتجات: {product_names}\n"
            f"📍 حالة الشحنة: {status}\n"
            f"🏷️ الوسوم: {tags}\n"
            f"-----------------------------\n"
        )
    return message


# ==============================
# SALLA QUERIES
# ==============================

def get_today_orders(store_key):
    riyadh = pytz.timezone("Asia/Riyadh")
    now = datetime.now(riyadh)

    today_start = now.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
    today_end = now.replace(hour=23, minute=59, second=59).strftime("%Y-%m-%d %H:%M:%S")

    params = {"from_date": today_start, "to_date": today_end}
    response = salla_get(store_key, SALLA_ORDERS_URL, params=params)
    if response is None or response.status_code != 200:
        return []

    data = response.json()
    cleaned_data = []
    for order in data.get("data", []):
        details = get_order_details(store_key, order["id"])
        tags = details.get("tags", [])
        if not tags:
            cleaned_data.append(order)
    return cleaned_data


def get_orders_by_status(store_key, status_id):
    now = datetime.now(pytz.timezone("Asia/Riyadh"))
    one_year_ago = (now - timedelta(days=365)).strftime("%Y-%m-%d")

    if status_id == 1:
        salla_status_id = 1283428545  # under_review
        from_date = one_year_ago
        to_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    elif status_id == 2:
        salla_status_id = 1458516934  # completed
        from_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        to_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    elif status_id == 3:
        salla_status_id = 401284301  # delivering
        from_date = one_year_ago
        to_date = (now - timedelta(days=10)).strftime("%Y-%m-%d")
    else:
        salla_status_id = None
        from_date = None
        to_date = None

    all_orders = []
    page = 1
    per_page = 100

    while True:
        params = {"page": page, "per_page": per_page}
        if salla_status_id:
            params["status"] = salla_status_id
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date

        response = salla_get(store_key, SALLA_ORDERS_URL, params=params)
        if response is None or response.status_code != 200:
            print(f"Error fetching orders by status: {response.status_code if response else 'no response'}")
            break

        res_json = response.json()
        data = res_json.get("data", [])
        meta = res_json.get("meta", {}).get("pagination", {})

        all_orders.extend(data)

        if page >= meta.get("total_pages", 0):
            break
        page += 1

    return all_orders


def get_today_orders_message(store_key):
    riyadh = pytz.timezone("Asia/Riyadh")
    now = datetime.now(riyadh)
    yesterday = now - timedelta(days=1)

    yesterday_start = yesterday.replace(hour=0, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
    yesterday_end = yesterday.replace(hour=23, minute=59, second=59).strftime("%Y-%m-%d %H:%M:%S")

    params = {"from_date": yesterday_start, "to_date": yesterday_end}
    response = salla_get(store_key, SALLA_ORDERS_URL, params=params)
    data = response.json() if response is not None and response.status_code == 200 else {}

    orders_without_tags = []
    for order in data.get("data", []):
        details = get_order_details(store_key, order["id"])
        tags = details.get("tags", [])
        if not tags:
            orders_without_tags.append(order)

    # MESSAGE 1 (phone + product)
    message1 = ""
    for order in orders_without_tags:
        phone = order.get("customer", {}).get("mobile", "")
        code = order.get("customer", {}).get("mobile_code", "")
        full_phone = f"{code}{phone}"

        items = order.get("items", [])
        product_names = " + ".join([item.get("name", "") for item in items])

        message1 += f"{full_phone}\n{product_names}\n\n"

    # MESSAGE 2 (statistics)
    whatsapp_total = 0
    website_total = 0
    snap = 0
    recommendation = 0

    for order in orders_without_tags:
        source = order.get("source", "")
        if source == "dashboard":
            whatsapp_total += 1
        else:
            website_total += 1
            customer_city = order.get("customer", {}).get("city", "")
            if "سناب" in customer_city:
                snap += 1
            if "توصية" in customer_city:
                recommendation += 1

    total_sales = len(orders_without_tags)
    replied = 0
    not_replied = 0

    store_name = STORES_BY_KEY[store_key]["name"]
    message2 = f"""
مبيعات متجر {store_name}■

●اجمالي الواتس : {whatsapp_total}
- تيك توك :
- سناب :
- جوجل :
- توصية :
- انستا :
- يوتيوب :

●اجمالي الموقع : {website_total}
- تيك توك :
- سناب : {snap}
- جوجل :
- توصية : {recommendation}
- انستا :
- يوتيوب :

○اجمالي الرد : {replied}
○اجمالي لم يرد : {not_replied}
●اجمالي المبيعات الكلي:{total_sales}
"""

    return message1, message2


def get_order_by_number(store_key, order_number):
    params = {"reference_id": order_number}
    response = salla_get(store_key, SALLA_ORDERS_URL, params=params)
    if response is not None and response.status_code == 200:
        return response.json().get("data", {})
    return None


def get_orders_by_phone(store_key, phone_number):
    clean_phone = normalize_phone(phone_number)
    params = {"keyword": clean_phone}
    response = salla_get(store_key, SALLA_ORDERS_URL, params=params)
    if response is not None and response.status_code == 200:
        return response.json().get("data", [])
    return []


# ==============================
# TELEGRAM UI
# ==============================

STORE_MENU = [
    ["📦 طلبات اليوم", "📊 رسالة المبيعات"],
    ["🔎 البحث عن طلب", "📱 البحث برقم الهاتف"],
    [" 🕐طلبات قيد المراجعة", "🕐 طلبات قيد التنفيذ", "🕐 طلبات جاري التوصيل"],
    ["🔙 تغيير المتجر"],
]


def store_picker_keyboard():
    # 2 per row
    names = [s["name"] for s in STORES]
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def store_menu_keyboard():
    return ReplyKeyboardMarkup(STORE_MENU, resize_keyboard=True)


def get_active_store(context):
    """Returns the store dict the user currently has selected, or None."""
    store_key = context.user_data.get("store_key")
    return STORES_BY_KEY.get(store_key)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("store_key", None)
    context.user_data.pop("search_mode", None)
    await update.message.reply_text(
        "👋 الرجاء اختيار المتجر:",
        reply_markup=store_picker_keyboard(),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # --- Store selection step ---
    store_names = {s["name"]: s["key"] for s in STORES}
    if text in store_names:
        context.user_data["store_key"] = store_names[text]
        context.user_data.pop("search_mode", None)
        await update.message.reply_text(
            f"✅ تم اختيار متجر: {text}\nالرجاء اختيار خيار من القائمة:",
            reply_markup=store_menu_keyboard(),
        )
        return

    if "تغيير المتجر" in text:
        context.user_data.pop("store_key", None)
        context.user_data.pop("search_mode", None)
        await update.message.reply_text(
            "👋 الرجاء اختيار المتجر:",
            reply_markup=store_picker_keyboard(),
        )
        return

    store = get_active_store(context)
    if not store:
        await update.message.reply_text(
            "⚠️ الرجاء اختيار متجر أولاً عن طريق /start",
            reply_markup=store_picker_keyboard(),
        )
        return

    store_key = store["key"]

    # 1️⃣ TODAY ORDERS
    if "طلبات اليوم" in text:
        context.user_data.pop("search_mode", None)
        await update.message.reply_text("⏳ جاري جلب الطلبات...")

        orders = get_today_orders(store_key)
        if not orders:
            await update.message.reply_text("📦 لا توجد طلبات اليوم.")
            return

        message = f"📦 طلبات اليوم - {store['name']}:\n\n"
        acc = 0
        for order in orders:
            if order["total"]["amount"] > 0:
                amount = order["total"]["amount"]
                source = " واتساب" if order["source"] == "dashboard" else " موقع"
                details = get_order_details(store_key, order["id"])
                phone = order.get("customer", {}).get("mobile", "غير متوفر")
                message += f"Order: #<code>{order['reference_id']}</code> -   {source}\n{phone}\n"
                acc += float(amount)

        message += f"\n💰 Total: {round(acc, 0)} SAR"
        await update.message.reply_text(message, parse_mode="HTML")

    # 2️⃣ SEARCH BY ORDER NUMBER
    elif "البحث عن طلب" in text:
        context.user_data["search_mode"] = "order_number"
        await update.message.reply_text("ادخل رقم الطلب فضلا:")

    # 3️⃣ SEARCH BY PHONE NUMBER
    elif "البحث برقم الهاتف" in text:
        context.user_data["search_mode"] = "phone_number"
        await update.message.reply_text("ادخل رقم الهاتف فضلا:")

    # 4️⃣ TODAY SALES MESSAGE
    elif "رسالة المبيعات" in text:
        context.user_data.pop("search_mode", None)
        await update.message.reply_text("⏳ جاري جلب الطلبات...")

        message1, message2 = get_today_orders_message(store_key)
        if message1.strip() == "":
            message1 = "📦 لا توجد طلبات اليوم."

        await update.message.reply_text(message1, parse_mode="HTML")
        await update.message.reply_text(message2, parse_mode="HTML")

    elif "طلبات قيد المراجعة" in text:
        context.user_data.pop("search_mode", None)
        await update.message.reply_text("⏳ جاري جلب الطلبات...")

        orders = get_orders_by_status(store_key, 1)
        if not orders:
            await update.message.reply_text("📦 لا توجد طلبات قيد المراجعة.")
            return

        message = f"📦 طلبات قيد المراجعة - {store['name']}:\n\n"
        for order in orders:
            order_date_str = datetime.strptime(order["date"]["date"], "%Y-%m-%d %H:%M:%S.%f").strftime("%Y-%m-%d")
            message += f"Order: #<code>{order['reference_id']}</code> - {order_date_str} \n"
        await update.message.reply_text(message, parse_mode="HTML")

    elif "طلبات قيد التنفيذ" in text:
        context.user_data.pop("search_mode", None)
        await update.message.reply_text("⏳ جاري جلب الطلبات...")

        orders = get_orders_by_status(store_key, 2)
        if not orders:
            await update.message.reply_text("📦 لا توجد طلبات قيد التنفيذ.")
            return

        message = f"📦 طلبات قيد التنفيذ - {store['name']}:\n\n"
        for order in orders:
            order_date_str = datetime.strptime(order["date"]["date"], "%Y-%m-%d %H:%M:%S.%f").strftime("%Y-%m-%d")
            message += f"Order: #<code>{order['reference_id']}</code> - {order_date_str} \n"
        await update.message.reply_text(message, parse_mode="HTML")

    elif "طلبات جاري التوصيل" in text:
        context.user_data.pop("search_mode", None)
        await update.message.reply_text("⏳ جاري جلب الطلبات...")

        orders = get_orders_by_status(store_key, 3)
        if not orders:
            await update.message.reply_text("📦 لا توجد طلبات جاري التوصيل.")
            return

        message = f"📦 طلبات جاري التوصيل - {store['name']}:\n\n"
        for order in orders:
            order_date_str = datetime.strptime(order["date"]["date"], "%Y-%m-%d %H:%M:%S.%f").strftime("%Y-%m-%d")
            message += f"Order: #<code>{order['reference_id']}</code> - {order_date_str} \n"
        await update.message.reply_text(message, parse_mode="HTML")

    # HANDLE SEARCH INPUT
    else:
        search_mode = context.user_data.get("search_mode")

        if search_mode == "order_number" and text.isdigit():
            orders = get_order_by_number(store_key, int(text))
            if orders:
                message = sending_message_to_customer(store_key, orders)
                await update.message.reply_text(message, parse_mode="HTML")
            else:
                await update.message.reply_text("❌ لا يُوجد طلب بهذا الرقم.")

        elif search_mode == "phone_number":
            orders = get_orders_by_phone(store_key, text)
            if not orders:
                await update.message.reply_text("❌ لا يُوجد طلبات متعلقة بهذا الرقم.")
            else:
                message = sending_message_to_customer(store_key, orders)
                await update.message.reply_text(message, parse_mode="HTML")

        else:
            await update.message.reply_text("الرجاء إدخال رقم صالح أو اختيار خيار من القائمة.")


# ==============================
# KEEP-ALIVE WEB SERVER
# ==============================
# Render's free tier only allows "Web Service" (not Background Worker),
# and free Web Services spin down after 15 min with no HTTP traffic.
# This tiny server gives Render something to host, and an external
# pinger (e.g. UptimeRobot / cron-job.org) hitting "/" every ~10 min
# keeps the process alive so the Telegram polling loop doesn't die.

keep_alive_app = Flask(__name__)


@keep_alive_app.route("/")
def health_check():
    return "Bot is running ✅", 200


def run_keep_alive_server():
    port = int(os.environ.get("PORT", 10000))
    keep_alive_app.run(host="0.0.0.0", port=port)


# ==============================
# MAIN
# ==============================

def run_telegram_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Warm the token cache for all stores on startup (best-effort; failures are logged, not fatal)
    for store in STORES:
        token = fetch_access_token(store)
        ACCESS_TOKENS[store["key"]] = token
        status = "✅" if token else "⚠️ failed"
        print(f"{status} [{store['name']}] token fetch on startup")

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    app.run_polling()


if __name__ == "__main__":
    # Start the keep-alive web server in a background thread so Render
    # sees an open port, then run the Telegram bot's polling loop in
    # the main thread.
    threading.Thread(target=run_keep_alive_server, daemon=True).start()
    run_telegram_bot()
