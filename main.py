import os
import json
import asyncio
import logging
import re
import html
from datetime import datetime, timedelta
from io import BytesIO

import httpx
from bs4 import BeautifulSoup
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
from supabase import create_client
from openai import AsyncOpenAI
from pypdf import PdfReader

load_dotenv()
logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

if not all([TELEGRAM_TOKEN, SUPABASE_URL, SUPABASE_KEY, DEEPSEEK_API_KEY]):
    raise RuntimeError("Не заданы переменные окружения (TELEGRAM_TOKEN / SUPABASE_URL / SUPABASE_KEY / DEEPSEEK_API_KEY)")

bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ai_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
    timeout=60,
    max_retries=2,
)
MODEL_NAME = "deepseek-chat"

WEBHOOK_PATH = "/tg-webhook"
DEFAULT_INFLATION = 9.3


# ---------- Вспомогательные функции ----------

def md_to_html(text: str) -> str:
    """Экранирует текст и конвертирует **жирный** в <b>."""
    text = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)


async def send_long_message(message: types.Message, text: str, reply_markup=None):
    """Безопасно режет длинный текст на части (экранирование + **bold**)."""
    max_length = 3800
    parts = []
    while len(text) > max_length:
        idx = text.rfind("\n", 0, max_length)
        if idx == -1:
            idx = max_length
        parts.append(text[:idx])
        text = text[idx:].lstrip()
    parts.append(text)

    for i, part in enumerate(parts):
        part_html = md_to_html(part)
        if not part_html.strip():
            continue
        if i == len(parts) - 1:
            await message.answer(part_html, reply_markup=reply_markup)
        else:
            await message.answer(part_html)


def parse_dt(dt_str):
    if not dt_str:
        return None
    try:
        clean = dt_str.replace("Z", "").split("+")[0]
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def get_summary_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📅 Месяц", callback_data="sum_month")
    builder.button(text="📈 6 месяцев", callback_data="sum_halfyear")
    builder.button(text="📊 Год", callback_data="sum_year")
    builder.button(text="♾️ Всё время", callback_data="sum_all")
    builder.adjust(2, 2)
    builder.row(types.InlineKeyboardButton(text="🔍 Глубокий ИИ-анализ", callback_data="ai_analytics"))
    return builder.as_markup()


async def db_get_transactions(user_id: int):
    result = await asyncio.to_thread(
        lambda: supabase.table("transactions").select("*").eq("user_id", user_id).execute()
    )
    return result.data


async def db_insert(rows):
    await asyncio.to_thread(
        lambda: supabase.table("transactions").insert(rows).execute()
    )


async def db_delete_user(user_id: int):
    await asyncio.to_thread(
        lambda: supabase.table("transactions").delete().eq("user_id", user_id).execute()
    )


async def fetch_kazakhstan_inflation() -> float:
    url = "https://stat.gov.kz/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                page_text = soup.get_text(separator=" ", strip=True)[:10000]

                inf_prompt = f"""
                Проанализируй текст с сайта Бюро национальной статистики Казахстана (stat.gov.kz):
                {page_text}
                Найди текущий показатель инфляции или индекса потребительских цен в процентах.
                Верни ТОЛЬКО число с плавающей точкой (например: 108.3 или 8.5). Если цифры нет, верни {DEFAULT_INFLATION}.
                """
                res = await ai_client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[{"role": "user", "content": inf_prompt}],
                    temperature=0.0,
                )
                match = re.search(r"\d+[.,]\d+|\d+", res.choices[0].message.content)
                if match:
                    val = float(match.group(0).replace(",", "."))
                    if val > 100:
                        val = round(val - 100, 1)
                    if 0 < val < 25:
                        return val
    except Exception as e:
        logging.warning(f"Ошибка получения инфляции: {e}")

    return DEFAULT_INFLATION


async def set_commands(bot_instance: Bot):
    commands = [
        types.BotCommand(command="start", description="Запустить бота"),
        types.BotCommand(command="summary", description="Финансовая сводка и периоды"),
        types.BotCommand(command="audit", description="Глубокий макро-аудит и инфляция"),
        types.BotCommand(command="reset", description="Сбросить историю"),
    ]
    await bot_instance.set_my_commands(commands)


# ---------- Хэндлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Финансовый ИИ-ассистент (₸)</b>\n\n"
        "• Пиши траты/доходы текстом (<i>'Кофе 1500 тг'</i>)\n"
        "• Скидывай PDF-выписки для авторазбора\n"
        "• /summary — сводка и периоды\n"
        "• /audit — детальный разбор и инфляция\n"
        "• /reset — очистить историю"
    )


@dp.message(Command("summary"))
async def cmd_summary(message: types.Message):
    user_id = message.from_user.id
    try:
        transactions = await db_get_transactions(user_id)

        if not transactions:
            await message.answer("📭 У тебя пока нет записей в базе.")
            return

        total_income = sum(float(t["amount"]) for t in transactions if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in transactions if t.get("type") == "expense")
        balance = total_income - total_expense

        await message.answer(
            f"📊 <b>Сводка (за всё время):</b>\n\n"
            f"📥 Доход: <code>{total_income:,.2f} ₸</code>\n"
            f"📤 Расход: <code>{total_expense:,.2f} ₸</code>\n"
            f"💰 Баланс: <code>{balance:,.2f} ₸</code>\n"
            f"📝 Операций: {len(transactions)}",
            reply_markup=get_summary_keyboard(),
        )
    except Exception as e:
        logging.error(f"Ошибка summary: {e}")
        await message.answer("⚠️ Не удалось получить данные из базы.")


@dp.callback_query(F.data.startswith("sum_") | (F.data == "ai_analytics"))
async def process_summary_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data

    try:
        transactions = await db_get_transactions(user_id)

        if not transactions:
            await callback.answer("📭 Записей пока нет.", show_alert=True)
            return

        if action == "ai_analytics":
            await callback.message.edit_text("⏳ Анализирую данные...")
            current_inflation = await fetch_kazakhstan_inflation()

            tx_summary = "\n".join(
                f"- {t.get('category', 'Прочее')}: {t.get('amount')} ₸ ({t.get('description', 'Без описания')})"
                for t in transactions
            )

            prompt = f"""
            Ты прагматичный финсоветник. Дай СТРОГО КОМПАКТНЫЙ и лаконичный макро-анализ (без воды, короткие маркированные списки, максимум по 2-3 предложения на пункт). Инфляция в РК: {current_inflation}%.
            1. 🛑 **Утечки бюджета:**
            2. 📈 **Тренды:**
            3. 💡 **Рекомендации:**

            Данные:
            {tx_summary}
            """

            response_ai = await ai_client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            )
            analysis_text = (
                f"🧠 **ИИ-анализ (Инфляция {current_inflation}%):**\n\n"
                f"{response_ai.choices[0].message.content}"
            )
            try:
                await callback.message.delete()
            except TelegramBadRequest:
                pass
            await send_long_message(callback.message, analysis_text, reply_markup=get_summary_keyboard())
            await callback.answer()
            return

        now = datetime.now()
        filtered = []
        period_name = ""

        # Один проход по данным с парсингом даты
        parsed = [(parse_dt(t.get("created_at")), t) for t in transactions]

        if action == "sum_month":
            period_name = f"Текущий месяц ({now.strftime('%B %Y')})"
            filtered = [t for dt, t in parsed if dt and dt.month == now.month and dt.year == now.year]
        elif action == "sum_halfyear":
            period_name = "Последние 6 месяцев"
            six_months_ago = now - timedelta(days=182)
            filtered = [t for dt, t in parsed if dt and dt >= six_months_ago]
        elif action == "sum_year":
            period_name = f"Текущий год ({now.year})"
            filtered = [t for dt, t in parsed if dt and dt.year == now.year]
        else:
            period_name = "Всё время"
            filtered = transactions

        total_income = sum(float(t["amount"]) for t in filtered if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in filtered if t.get("type") == "expense")
        balance = total_income - total_expense

        text = (
            f"📊 <b>Сводка — {period_name}:</b>\n\n"
            f"📥 Доход: <code>{total_income:,.2f} ₸</code>\n"
            f"📤 Расход: <code>{total_expense:,.2f} ₸</code>\n"
            f"💰 Баланс: <code>{balance:,.2f} ₸</code>\n"
            f"📝 Операций: {len(filtered)}"
        )
        try:
            await callback.message.edit_text(text, reply_markup=get_summary_keyboard())
        except TelegramBadRequest as e:
            # Пользователь повторно нажал ту же кнопку — это не ошибка
            if "message is not modified" not in e.message:
                raise

        await callback.answer()

    except Exception as e:
        logging.error(f"Ошибка callback summary: {e}")
        await callback.answer("⚠️ Ошибка обработки.", show_alert=True)


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    try:
        await db_delete_user(user_id)
        await message.answer("🧹 История полностью очищена.")
    except Exception as e:
        logging.error(f"Ошибка reset: {e}")
        await message.answer("⚠️ Не удалось очистить данные.")


@dp.message(Command("audit"))
async def cmd_audit(message: types.Message):
    user_id = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        transactions = await db_get_transactions(user_id)

        if not transactions:
            await message.answer("📭 В базе нет записей для аудита.")
            return

        current_inflation = await fetch_kazakhstan_inflation()

        total_income = sum(float(t["amount"]) for t in transactions if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in transactions if t.get("type") == "expense")
        net_balance = total_income - total_expense

        # Обесценивается только накопленный остаток, а не уже потраченные деньги
        lost_value = max(net_balance, 0) * (current_inflation / 100)

        tx_summary = "\n".join(
            f"- {t.get('category', 'Прочее')}: {t.get('amount')} ₸ ({t.get('description', 'Без описания')})"
            for t in transactions
        )

        prompt = f"""
        Ты — жесткий финансовый советник. Инфляция в РК: {current_inflation}%.
        Сделай лаконичный, плотный и структурированный аудит без лишних слов (короткие списки):
        1. 🛑 **Утечки бюджета:** (главные статьи)
        2. 📉 **Тренды:** (динамика)
        3. 🛡️ **План действий:** (3 шага)

        Данные:
        {tx_summary}
        """

        response_ai = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "Ты экспертный фин-аналитик по экономике РК."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )

        verdict = (
            "⚠️ Расходы превышают доходы, инфляция ускоряет просадку капитала."
            if net_balance < 0
            else "✅ Доходы покрывают расходы, но инфляция съедает часть накоплений."
        )

        audit_text = (
            f"🔍 **Финансовый аудит**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 **Баланс:** Доход {total_income:,.2f} ₸ | Расход {total_expense:,.2f} ₸\n"
            f"📉 **Влияние инфляции ({current_inflation}%):** обесценивание накопленного остатка за год ~{lost_value:,.2f} ₸\n"
            f"💡 **Вердикт:** {verdict}\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"{response_ai.choices[0].message.content}"
        )

        await send_long_message(message, audit_text)

    except Exception as e:
        logging.error(f"Ошибка аудита: {e}")
        await message.answer("⚠️ Не удалось провести аудит.")


@dp.message(F.document)
async def handle_document(message: types.Message):
    document = message.document
    user_id = message.from_user.id

    if not document.file_name.lower().endswith(".pdf"):
        await message.answer("⚠️ Прикрепите PDF-файл выписки.")
        return

    await bot.send_chat_action(message.chat.id, "typing")

    try:
        file_info = await bot.get_file(document.file_id)
        file_bytes = await bot.download_file(file_info.file_path)

        pdf_file = BytesIO(file_bytes.read())
        reader = PdfReader(pdf_file)
        extracted_text = ""
        for page in reader.pages:
            t = page.extract_text()
            if t:
                extracted_text += t + "\n"

        extracted_text = extracted_text[:15000]
        current_inflation = await fetch_kazakhstan_inflation()

        prompt = f"""
        Проанализируй текст банковской выписки (суммы в тенге). Выдели все операции:
        {extracted_text}

        Верни результат СТРОГО в формате JSON (json object) без markdown и без лишнего текста:
        {{
          "transactions": [
            {{
              "type": "expense" или "income",
              "amount": число,
              "category": "категория",
              "description": "описание"
            }}
          ],
          "financial_advice": "краткий емкий совет с учетом инфляции"
        }}
        """

        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
        )

        data = json.loads(response.choices[0].message.content)
        transactions = data.get("transactions", [])
        advice = data.get("financial_advice", "Анализ завершен.")

        if transactions:
            rows = []
            for t in transactions:
                rows.append({
                    "user_id": user_id,
                    "type": t.get("type", "expense"),
                    "amount": float(t.get("amount", 0)),
                    "category": t.get("category", "Прочее"),
                    "description": t.get("description", "Из выписки"),
                })
            await db_insert(rows)

            pdf_income = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") == "income")
            pdf_expense = sum(float(t.get("amount", 0)) for t in transactions if t.get("type") == "expense")
            pdf_balance = pdf_income - pdf_expense

            lost_value = pdf_expense * (current_inflation / 100)
            remaining_real_value = pdf_expense - lost_value

            stats_text = (
                f"📄 <b>Выписка успешно загружена</b>\n\n"
                f"📌 <b>Статистика:</b>\n"
                f"• Доход: <code>{pdf_income:,.2f} ₸</code>\n"
                f"• Расход: <code>{pdf_expense:,.2f} ₸</code>\n"
                f"• Баланс: <code>{pdf_balance:,.2f} ₸</code>\n"
                f"• Операций: {len(transactions)}\n\n"
                f"📉 <b>Эффект инфляции ({current_inflation}%):</b>\n"
                f"• Обесценивание за год: <code>~{lost_value:,.2f} ₸</code>\n"
                f"• Реальная покупательная ценность: <code>~{remaining_real_value:,.2f} ₸</code>\n\n"
                f"💡 <b>Рекомендация:</b>\n{md_to_html(advice)}"
            )

            await message.answer(stats_text)
        else:
            await message.answer("⚠️ Не удалось найти транзакции в файле.")

    except Exception as e:
        logging.error(f"Ошибка PDF: {e}")
        await message.answer("⚠️ Ошибка при чтении выписки.")


@dp.message(F.text)
async def handle_message(message: types.Message):
    text = message.text
    user_id = message.from_user.id

    await bot.send_chat_action(message.chat.id, "typing")

    prompt = f"""
    Проанализируй сообщение пользователя: "{text}".
    Определи, является ли это финансовой транзакцией в тенге (расход или доход).
    Верни результат СТРОГО в формате JSON (json object) без markdown-разметки (без ```json), содержащий поля:
    - "is_transaction": true или false
    - "type": "expense" или "income" (или null если не транзакция)
    - "amount": число (в тенге) или null
    - "category": строка (например: "Еда", "Транспорт", "Развлечения", "Зарплата" и т.д.)
    - "description": короткое описание транзакции
    - "reply": живой, лаконичный ответ пользователю на русском языке с суммой в тенге (₸)
    """

    try:
        response = await ai_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )

        data = json.loads(response.choices[0].message.content)

        if data.get("is_transaction") and data.get("amount"):
            await db_insert({
                "user_id": user_id,
                "type": data.get("type"),
                "amount": float(data.get("amount")),
                "category": data.get("category", "Прочее"),
                "description": data.get("description", "Транзакция"),
            })

        await message.answer(md_to_html(data.get("reply", "✅ Записано!")))

    except Exception as e:
        logging.error(f"Ошибка текста: {e}")
        await message.answer("⚠️ Не смог распознать сообщение. Напиши четче, например: 'Такси 1500 тг'")


# ---------- Запуск ----------

async def on_startup(bot_instance: Bot):
    await set_commands(bot_instance)
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not base_url:
        logging.error("RENDER_EXTERNAL_URL не найден — webhook не будет установлен!")
        return
    webhook_url = f"{base_url}{WEBHOOK_PATH}"
    await bot_instance.set_webhook(webhook_url)
    logging.info(f"✅ Webhook установлен: {webhook_url}")


async def on_shutdown(bot_instance: Bot):
    try:
        await bot_instance.delete_webhook(drop_pending_updates=False)
    except TelegramBadRequest:
        pass
    await bot_instance.session.close()
    logging.info("Bot stopped.")


async def healthcheck(request):
    return web.Response(text="Bot is alive!")


async def run_polling():
    """Локальный режим разработки (POLLING=1 в .env)."""
    await set_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


def main():
    if os.getenv("POLLING", "0") == "1":
        logging.info("Запуск в режиме polling (локальная разработка)...")
        asyncio.run(run_polling())
        return

    app = web.Application()
    app.router.add_get("/", healthcheck)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    port = int(os.environ.get("PORT", 10000))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
