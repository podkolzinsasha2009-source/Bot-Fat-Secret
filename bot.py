import asyncio
import io
import os
import tempfile
from datetime import datetime

import imageio_ffmpeg
import speech_recognition as sr
from aiohttp import web
from pydub import AudioSegment

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from database import init_db, get_or_create_user, get_today_calories, log_food_to_db
from openrouter_client import ask_gemini

# Указываем pydub использовать ffmpeg из пакета imageio-ffmpeg,
# а не искать системный ffmpeg - это решает проблему на Windows и Render.
AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

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
# Фоновый веб-сервер - нужен только для прохождения Port Binding на Render.
# Render требует, чтобы процесс слушал порт в течение первых 5 минут.
# Сам бот работает через Long Polling и этот сервер не использует.
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", 10000))


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    init_db()

    # ШАГ 1: сначала поднимаем веб-сервер и ГАРАНТИРОВАННО открываем порт.
    # site.start() регистрирует сервер в event loop и сразу возвращает управление -
    # он не блокирует. После этой строки порт уже слушается.
    app = web.Application()
    app.add_routes([web.get("/", handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"Порт {PORT} открыт - Render может пройти проверку")

    # ШАГ 2: только после открытия порта запускаем Long Polling.
    # aiohttp-сервер продолжает работать в фоне того же event loop.
    print("Бот запущен в режиме Long Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
