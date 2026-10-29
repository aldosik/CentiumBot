import os
import json
import asyncio
import logging
import re
from datetime import datetime, timedelta
from io import BytesIO
import httpx
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from supabase import create_client
from openai import AsyncOpenAI
from pypdf import PdfReader

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

ai_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)
MODEL_NAME = "deepseek-chat"

async def fetch_kazakhstan_inflation() -> float:
    url = "https://stat.gov.kz/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'html.parser')
                page_text = soup.get_text(separator=' ', strip=True)[:10000]
                
                inf_prompt = f"""
                Проанализируй текст с сайта Бюро национальной статистики Казахстана (stat.gov.kz):
                {page_text}
                Найди текущий показатель инфляции или индекса потребительских цен в процентах. 
                Верни ТОЛЬКО число с плавающей точкой (например: 108.3 или 8.5). Если цифры нет, верни 9.3.
                """
                res = await ai_client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=[{"role": "user", "content": inf_prompt}],
                    temperature=0.0
                )
                match = re.search(r'\d+[.,]\d+|\d+', res.choices[0].message.content)
                if match:
                    val = float(match.group(0).replace(',', '.'))
                    if val > 20:
                        val = round(val - 100, 1)
                    return val
    except Exception as e:
        logging.warning(f"Ошибка получения инфляции: {e}")
    
    return 9.3

def parse_dt(dt_str):
    if not dt_str:
        return None
    try:
        clean = dt_str.replace("Z", "").split("+")[0]
        return datetime.fromisoformat(clean)
    except:
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

async def send_long_message(message: types.Message, text: str, reply_markup=None):
    max_length = 4000
    if len(text) <= max_length:
        await message.answer(text, parse_mode="Markdown", reply_markup=reply_markup)
        return
    
    parts = []
    while len(text) > 0:
        if len(text) > max_length:
            break_index = text.rfind('\n', 0, max_length)
            if break_index == -1:
                break_index = max_length
            parts.append(text[:break_index])
            text = text[break_index:].lstrip()
        else:
            parts.append(text)
            break
            
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            await message.answer(part, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await message.answer(part, parse_mode="Markdown")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Финансовый ИИ-ассистент (₸)**\n\n"
        "• Пиши траты/доходы текстом (*'Кофе 1500 тг'*)\n"
        "• Скидывай PDF-выписки для авторазбора\n"
        "• `/summary` — сводка и периоды\n"
        "• `/audit` — детальный разбор и инфляция\n"
        "• `/reset` — очистить историю",
        parse_mode="Markdown"
    )

@dp.message(Command("summary"))
async def cmd_summary(message: types.Message):
    user_id = message.from_user.id
    try:
        response = supabase.table("transactions").select("*").eq("user_id", user_id).execute()
        transactions = response.data
        
        if not transactions:
            await message.answer("📭 У тебя пока нет записей в базе.")
            return

        total_income = sum(float(t["amount"]) for t in transactions if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in transactions if t.get("type") == "expense")
        balance = total_income - total_expense

        await message.answer(
            f"📊 **Сводка (За всё время):**\n\n"
            f"📥 Доход: `{total_income:,.2f} ₸`\n"
            f"📤 Расход: `{total_expense:,.2f} ₸`\n"
            f"💰 Баланс: `{balance:,.2f} ₸`\n"
            f"📝 Операций: {len(transactions)}",
            parse_mode="Markdown",
            reply_markup=get_summary_keyboard()
        )
    except Exception as e:
        logging.error(f"Ошибка summary: {e}")
        await message.answer("⚠️ Не удалось получить данные из базы.")

@dp.callback_query(F.data.startswith("sum_") | (F.data == "ai_analytics"))
async def process_summary_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data
    
    try:
        response = supabase.table("transactions").select("*").eq("user_id", user_id).execute()
        transactions = response.data
        
        if not transactions:
            await callback.answer("📭 Записей пока нет.", show_alert=True)
            return

        now = datetime.now()
        filtered = []
        period_name = ""

        if action == "sum_month":
            period_name = f"Текущий месяц ({now.strftime('%B %Y')})"
            filtered = [
                t for t in transactions 
                if parse_dt(t.get("created_at")) and parse_dt(t.get("created_at")).month == now.month 
                and parse_dt(t.get("created_at")).year == now.year
            ]
        elif action == "sum_halfyear":
            period_name = "Последние 6 месяцев"
            six_months_ago = now - timedelta(days=182)
            filtered = [
                t for t in transactions 
                if parse_dt(t.get("created_at")) and parse_dt(t.get("created_at")) >= six_months_ago
            ]
        elif action == "sum_year":
            period_name = f"Текущий год ({now.year})"
            filtered = [
                t for t in transactions 
                if parse_dt(t.get("created_at")) and parse_dt(t.get("created_at")).year == now.year
            ]
        else:
            period_name = "Всё время"
            filtered = transactions

        if action == "ai_analytics":
            await callback.message.edit_text("⏳ Анализирую данные...")
            current_inflation = await fetch_kazakhstan_inflation()
            
            tx_summary = "\n".join([f"- {t.get('category', 'Прочее')}: {t.get('amount')} ₸ ({t.get('description', 'Без описания')})" for t in transactions])
            
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
                temperature=0.3
            )
            analysis_text = f"🧠 **ИИ-анализ (Инфляция {current_inflation}%):**\n\n{response_ai.choices[0].message.content}"
            await callback.message.delete()
            await send_long_message(callback.message, analysis_text, reply_markup=get_summary_keyboard())
            await callback.answer()
            return

        total_income = sum(float(t["amount"]) for t in filtered if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in filtered if t.get("type") == "expense")
        balance = total_income - total_expense

        text = (
            f"📊 **Сводка — {period_name}:**\n\n"
            f"📥 Доход: `{total_income:,.2f} ₸`\n"
            f"📤 Расход: `{total_expense:,.2f} ₸`\n"
            f"💰 Баланс: `{balance:,.2f} ₸`\n"
            f"📝 Операций: {len(filtered)}"
        )
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=get_summary_keyboard())
        await callback.answer()

    except Exception as e:
        logging.error(f"Ошибка callback summary: {e}")
        await callback.answer("⚠️ Ошибка обработки.", show_alert=True)

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    user_id = message.from_user.id
    try:
        supabase.table("transactions").delete().eq("user_id", user_id).execute()
        await message.answer("🧹 История полностью очищена.")
    except Exception as e:
        logging.error(f"Ошибка reset: {e}")
        await message.answer("⚠️ Не удалось очистить данные.")

@dp.message(Command("audit"))
async def cmd_audit(message: types.Message):
    user_id = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")
    
    try:
        response = supabase.table("transactions").select("*").eq("user_id", user_id).execute()
        transactions = response.data
        
        if not transactions:
            await message.answer("📭 В базе нет записей для аудита.")
            return

        current_inflation = await fetch_kazakhstan_inflation()
        
        total_income = sum(float(t["amount"]) for t in transactions if t.get("type") == "income")
        total_expense = sum(float(t["amount"]) for t in transactions if t.get("type") == "expense")
        net_balance = total_income - total_expense
        
        lost_value = total_expense * (current_inflation / 100)
        
        tx_summary = "\n".join([f"- {t.get('category', 'Прочее')}: {t.get('amount')} ₸ ({t.get('description', 'Без описания')})" for t in transactions])
        
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
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        
        verdict = "⚠️ Расходы превышают доходы, инфляция ускоряет просадку капитала." if net_balance < 0 else "✅ Доходы покрывают расходы, но инфляция съедает часть накоплений."
        
        audit_text = (
            f"🔍 **Финансовый аудит**\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📊 **Баланс:** Доход `{total_income:,.2f} ₸` | Расход `{total_expense:,.2f} ₸`\n"
            f"📉 **Влияние инфляции ({current_inflation}%):** Обесценивание капитала за год составит `~{lost_value:,.2f} ₸`.\n"
            f"💡 **Вердикт:** {verdict}\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
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

    if not document.file_name.lower().endswith(('.pdf', '.txt', '.csv')):
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
        
        Верни результат СТРОГО в формате JSON без markdown и без лишнего текста:
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
            temperature=0.1
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
                    "description": t.get("description", "Из выписки")
                })
            supabase.table("transactions").insert(rows).execute()
            
            pdf_income = sum(t["amount"] for t in transactions if t.get("type") == "income")
            pdf_expense = sum(t["amount"] for t in transactions if t.get("type") == "expense")
            pdf_balance = pdf_income - pdf_expense
            
            lost_value = pdf_expense * (current_inflation / 100)
            remaining_real_value = pdf_expense - lost_value

            stats_text = (
                f"📄 **Выписка успешно загружена**\n\n"
                f"📌 **Статистика:**\n"
                f"• Доход: `{pdf_income:,.2f} ₸`\n"
                f"• Расход: `{pdf_expense:,.2f} ₸`\n"
                f"• Баланс: `{pdf_balance:,.2f} ₸`\n"
                f"• Операций: {len(transactions)}\n\n"
                f"📉 **Эффект инфляции ({current_inflation}%):**\n"
                f"• Обесценивание за год: `~{lost_value:,.2f} ₸`\n"
                f"• Реальная покупательная ценность: `~{remaining_real_value:,.2f} ₸`\n\n"
                f"💡 **Рекомендация:**\n{advice}"
            )
            
            await message.answer(stats_text, parse_mode="Markdown")
        else:
            await message.answer("⚠️ Не удалось найти транзакции в файле.")

    except Exception as e:
        logging.error(f"Ошибка PDF: {e}")
        await message.answer("⚠️ Ошибка при чтении выписки.")

@dp.message()
async def handle_message(message: types.Message):
    text = message.text
    user_id = message.from_user.id

    await bot.send_chat_action(message.chat.id, "typing")

    prompt = f"""
    Проанализируй сообщение пользователя: "{text}".
    Определи, является ли это финансовой транзакцией в тенге (расход или доход).
    Верни результат СТРОГО в формате JSON без markdown-разметки (без ```json), содержащий поля:
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
            temperature=0.2
        )

        data = json.loads(response.choices[0].message.content)

        if data.get("is_transaction") and data.get("amount"):
            supabase.table("transactions").insert({
                "user_id": user_id,
                "type": data.get("type"),
                "amount": float(data.get("amount")),
                "category": data.get("category", "Прочее"),
                "description": data.get("description", "Транзакция")
            }).execute()

        await message.answer(data.get("reply", "✅ Записано!"))
        
    except Exception as e:
        logging.error(f"Ошибка текста: {e}")
        await message.answer("⚠️ Не смог распознать сообщение. Напиши четче, например: 'Такси 1500 тг'")

async def main():
    commands = [
        types.BotCommand(command="start", description="Запустить бота"),
        types.BotCommand(command="summary", description="Финансовая сводка и периоды"),
        types.BotCommand(command="audit", description="Глубокий макро-аудит и инфляция"),
        types.BotCommand(command="reset", description="Сбросить историю"),
    ]
    await bot.set_my_commands(commands)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())