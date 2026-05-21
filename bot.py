import io
import os
import tempfile
from datetime import datetime

import speech_recognition as sr
from pydub import AudioSegment

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from database import init_db, get_or_create_user, get_today_calories, log_food_to_db
from openrouter_client import ask_gemini

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))
WEBHOOK_PATH = "/webhook"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _build_context(target: int, eaten: float) -> str:
    now = datetime.now()
    return (
        f"Текущая дата: {now.strftime('%Y-%m-%d')}. "
        f"Время: {now.strftime('%H:%M')}. "
        f"Цель на день: {target} ккал. "
        f"Уже съедено за сегодня: {eaten:.0f} ккал."
    )


async def _process_ai_response(message: Message, user_id: int, text: str, context: str):
    result = await ask_gemini(text, context)

    action = result.get("action")
    data = result.get("data")
    user_message = result.get("user_message", "Не удалось получить ответ от тренера.")

    if action == "log_food" and data:
        date_str = datetime.now().strftime("%Y-%m-%d")
        meal_type = data.get("meal_type", "Перекус")
        items = data.get("items", [])
        if items:
            log_food_to_db(user_id, date_str, meal_type, items)

    await message.answer(user_message)


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def start_handler(message: Message):
    user_id = message.from_user.id
    name = message.from_user.full_name or "друг"
    target = get_or_create_user(user_id, name)
    await message.answer(
        f"Привет, {name}! Я твой персональный ИИ-тренер и умный дневник питания.\n\n"
        f"Твоя дневная норма - {target} ккал.\n\n"
        f"Просто скажи или напиши, что ты съел - я найду точные КБЖУ и всё запишу. "
        f"Можешь также спрашивать советы, просить план питания или уточнять - "
        f"можно ли съесть тот или иной продукт."
    )


@dp.message(F.voice)
async def voice_handler(message: Message):
    user_id = message.from_user.id
    name = message.from_user.full_name or "друг"
    target = get_or_create_user(user_id, name)

    date_str = datetime.now().strftime("%Y-%m-%d")
    eaten_today = get_today_calories(user_id, date_str)
    context = _build_context(target, eaten_today)

    voice_file = await bot.get_file(message.voice.file_id)
    ogg_buffer = io.BytesIO()
    await bot.download_file(voice_file.file_path, destination=ogg_buffer)
    ogg_buffer.seek(0)

    ogg_tmp = None
    wav_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            ogg_tmp = f.name
            f.write(ogg_buffer.read())

        wav_tmp = ogg_tmp.replace(".ogg", ".wav")
        AudioSegment.from_ogg(ogg_tmp).export(wav_tmp, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_tmp) as source:
            audio_data = recognizer.record(source)

        try:
            recognized_text = recognizer.recognize_google(audio_data, language="ru-RU")
        except sr.UnknownValueError:
            await message.answer(
                "Не удалось распознать речь - попробуй ещё раз или напиши текстом."
            )
            return
        except sr.RequestError as e:
            await message.answer(f"Ошибка сервиса распознавания речи: {e}")
            return
    finally:
        if ogg_tmp and os.path.exists(ogg_tmp):
            os.unlink(ogg_tmp)
        if wav_tmp and os.path.exists(wav_tmp):
            os.unlink(wav_tmp)

    await message.answer(f"Распознал: \"{recognized_text}\"\nОбрабатываю...")

    try:
        await _process_ai_response(message, user_id, recognized_text, context)
    except RuntimeError as e:
        await message.answer(f"Ошибка при обращении к ИИ: {e}")


@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message):
    user_id = message.from_user.id
    name = message.from_user.full_name or "друг"
    target = get_or_create_user(user_id, name)

    date_str = datetime.now().strftime("%Y-%m-%d")
    eaten_today = get_today_calories(user_id, date_str)
    context = _build_context(target, eaten_today)

    await message.answer("Обрабатываю...")

    try:
        await _process_ai_response(message, user_id, message.text, context)
    except RuntimeError as e:
        await message.answer(f"Ошибка при обращении к ИИ: {e}")


# ---------------------------------------------------------------------------
# Жизненный цикл веб-сервера
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application):
    init_db()
    if not WEBHOOK_URL:
        print("CRITICAL: env var WEBHOOK_URL is not set - bot will not receive updates")
        return
    full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(full_url)
    print(f"Webhook registered: {full_url}")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    print("Webhook deleted")


async def health_check(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def main():
    app = web.Application()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # GET / - health check для UptimeRobot
    app.router.add_get("/", health_check)

    # POST /webhook - входящие апдейты от Telegram
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
