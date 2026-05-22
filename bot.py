import asyncio
import io
import os
import re
import tempfile
from datetime import datetime

import imageio_ffmpeg
import speech_recognition as sr
from aiohttp import web
from pydub import AudioSegment

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from database import (
    init_db, get_or_create_user, log_food_to_db,
    get_today_foods, delete_food_from_db, update_food_in_db,
    clear_today_foods, log_activity_to_db, get_today_burned,
    clear_today_activity,
)
from keyboards import get_main_menu, get_reply_menu
from openrouter_client import ask_gemini

AudioSegment.converter = imageio_ffmpeg.get_ffmpeg_exe()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

_CLEAR_PHRASES = [
    "🧹 сбросить день", "сбросить день",
    "очистить калории", "сбросить калории", "обнулить калории",
    "очисти калории", "сбрось калории", "очистить рацион", "сбросить рацион",
]
_STATS_PHRASES = ["📊 статистика за день", "статистика за день"]
_WORKOUT_PHRASES = ["💪 добавить тренировку", "добавить тренировку"]

# Norms
_NORM_P, _NORM_F, _NORM_C = 140, 70, 210


# ---------------------------------------------------------------------------
# MarkdownV2 helpers
# ---------------------------------------------------------------------------

def _esc(text) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def _bar(current: float, total: float, length: int = 10) -> str:
    filled = int(length * min(current, total) / total) if total > 0 else 0
    return "[" + "|" * filled + "." * (length - filled) + "]"


# Keyword → emoji mapping для продуктов из БД (без сохранённого emoji)
_EMOJI_MAP: list[tuple[list[str], str]] = [
    # Птица
    (["курин", "цыплён", "окорочок", "грудк", "бедр", "крыл"], "🍗"),
    # Мясо
    (["говядин", "телятин", "стейк", "бифштекс", "свинин", "карбонад", "фарш",
      "шашлык", "котлет"], "🥩"),
    (["сосис", "колбас", "сардел", "хот-дог", "хотдог"], "🌭"),
    # Субпродукты
    (["печень", "почк", "сердц", "пупочк", "желудк"], "🫀"),
    # Рыба / морепродукты
    (["рыб", "сёмг", "лосос", "форел", "тунец", "треск", "судак", "минтай",
      "горбуш", "скумбри", "сельд"], "🐟"),
    (["креветк", "кальмар", "краб", "морепродукт", "мидий"], "🦐"),
    # Яйца / молочка
    (["яйц", "омлет", "глазунья"], "🥚"),
    (["молок", "кефир", "ряженк"], "🥛"),
    (["творог", "йогурт", "сметан", "ряженк"], "🧴"),
    (["сыр"], "🧀"),
    (["масл", "маргарин"], "🧈"),
    # Крупы / злаки
    (["гречк", "гречнев"], "🌾"),
    (["рис", "рисов"], "🍚"),
    (["овсянк", "овёс", "овсян", "геркулес"], "🥣"),
    (["макарон", "паста", "спагетти", "лапш", "феттучин"], "🍝"),
    (["хлеб", "батон", "багет", "буханк", "лаваш", "тост"], "🍞"),
    (["каш"], "🥣"),
    # Картофель / овощи
    (["картофел", "картошк", "пюре"], "🥔"),
    (["помидор", "томат"], "🍅"),
    (["огурец", "огурц"], "🥒"),
    (["морков"], "🥕"),
    (["капуст", "броккол", "цветн"], "🥦"),
    (["перец болгар", "паприк"], "🫑"),
    (["лук", "чеснок"], "🧅"),
    (["кукуруз"], "🌽"),
    (["салат", "шпинат", "зелен", "листов"], "🥗"),
    (["баклажан", "кабачок", "цукини"], "🍆"),
    (["свёкл", "свекл"], "🫚"),
    # Фрукты
    (["яблок", "яблочн"], "🍎"),
    (["банан"], "🍌"),
    (["апельсин", "мандарин", "грейпфрут"], "🍊"),
    (["груш"], "🍐"),
    (["виноград"], "🍇"),
    (["клубник", "малин", "смородин", "черник", "ягод"], "🍓"),
    (["арбуз"], "🍉"),
    (["дын"], "🍈"),
    (["ананас"], "🍍"),
    (["манго", "папайя"], "🥭"),
    (["авокадо"], "🥑"),
    # Напитки
    (["чай"], "🍵"),
    (["кофе", "капучино", "латте", "эспрессо", "американо"], "☕"),
    (["сок", "нектар", "смузи"], "🧃"),
    (["вода"], "💧"),
    (["молочн коктейл"], "🥤"),
    # Сладкое / выпечка
    (["шоколад", "конфет", "батончик"], "🍫"),
    (["торт", "пирог", "пирожн", "кекс"], "🎂"),
    (["печень", "бисквит", "крекер", "вафл", "пряник"], "🍪"),
    (["мороженое", "мороженн"], "🍦"),
    (["мёд", "мед"], "🍯"),
    (["варень", "джем"], "🍓"),
    # Супы / готовые блюда
    (["суп", "борщ", "щи", "солянк", "рассольник", "окрошк"], "🍲"),
    (["пельмен", "вареник", "манты", "хинкал"], "🥟"),
    (["пицц"], "🍕"),
    (["бургер", "сэндвич", "бутерброд"], "🍔"),
    (["блин", "оладьи", "панкейк"], "🥞"),
    (["роллы", "суши"], "🍱"),
    # Орехи / бобовые
    (["орех", "миндаль", "грецк", "кешью", "фундук", "фисташк"], "🥜"),
    (["горох", "фасоль", "чечевиц", "нут", "боб"], "🫘"),
    # Масла (уточнение после "масл" выше)
    (["оливк", "подсолнечн", "растительн"], "🫒"),
]
_DEFAULT_FOOD_EMOJI = "🍽"


def _pick_emoji(name: str) -> str:
    """Подбирает эмодзи по ключевым словам в названии продукта."""
    nl = name.lower()
    for keywords, emoji in _EMOJI_MAP:
        if any(kw in nl for kw in keywords):
            return emoji
    return _DEFAULT_FOOD_EMOJI


def _fmt_food_items(items: list[dict], target: int, eaten_before: float) -> str:
    lines = []
    total_cal = sum(i.get("calories", 0) for i in items)
    total_p   = sum(i.get("p", 0) for i in items)
    total_f   = sum(i.get("f", 0) for i in items)
    total_c   = sum(i.get("c", 0) for i in items)

    for item in items:
        emoji  = item.get("emoji", "🍽")
        weight = item.get("weight", 0)
        name   = item.get("name", "") + (f" ({weight}г)" if weight else "")
        cal    = f"{item.get('calories', 0):.0f}"
        p      = f"{item.get('p', 0):.1f}"
        f_     = f"{item.get('f', 0):.1f}"
        c      = f"{item.get('c', 0):.1f}"
        lines.append(
            f"{emoji} *{_esc(name)}:* {_esc(cal)} ккал "
            f"\\| Б: {_esc(p)} \\| Ж: {_esc(f_)} \\| У: {_esc(c)}"
        )

    if len(items) > 1:
        lines.append("ーーー")
        lines.append(
            f"📊 *Итого за приём:* {_esc(f'{total_cal:.0f}')} ккал "
            f"\\| Б: {_esc(f'{total_p:.1f}')} "
            f"\\| Ж: {_esc(f'{total_f:.1f}')} "
            f"\\| У: {_esc(f'{total_c:.1f}')}"
        )

    remaining = target - (eaten_before + total_cal)
    lines.append(
        f"\nОсталось сегодня: *{_esc(f'{remaining:.0f}')}* ккал из {_esc(str(target))}"
    )
    return "\n".join(lines)


def _fmt_daily_log(foods: list[dict], target: int) -> str:
    if not foods:
        return "Рацион за сегодня пуст\\."

    lines = ["*Рацион за сегодня:*\n"]
    total_cal = total_p = total_f = total_c = 0.0

    for item in foods:
        total_cal += item.get("calories", 0)
        total_p   += item.get("p", 0)
        total_f   += item.get("f", 0)
        total_c   += item.get("c", 0)
        emoji = _pick_emoji(item.get("name", ""))
        cal = f"{item.get('calories', 0):.0f}"
        p   = f"{item.get('p', 0):.1f}"
        f_  = f"{item.get('f', 0):.1f}"
        c   = f"{item.get('c', 0):.1f}"
        lines.append(
            f"{emoji} *{_esc(item['name'])}:* {_esc(cal)} ккал "
            f"\\| Б: {_esc(p)} \\| Ж: {_esc(f_)} \\| У: {_esc(c)}"
        )

    lines.append("ーーー")
    lines.append(
        f"📊 *Итого за день:* {_esc(f'{total_cal:.0f}')} / {_esc(str(target))} ккал "
        f"\\| Б: {_esc(f'{total_p:.1f}')} "
        f"\\| Ж: {_esc(f'{total_f:.1f}')} "
        f"\\| У: {_esc(f'{total_c:.1f}')}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _build_context(target: int, eaten: float, burned: float, foods: list[dict]) -> str:
    now = datetime.now()
    net = eaten - burned
    ctx = (
        f"Дата: {now.strftime('%Y-%m-%d')}. "
        f"Время: {now.strftime('%H:%M')}. "
        f"Цель: {target} ккал. "
        f"Съедено: {eaten:.0f} ккал. "
        f"Сожжено (активность): {burned:.0f} ккал. "
        f"Чистый баланс: {net:.0f} ккал."
    )
    if foods:
        food_lines = "\n".join(
            f"- {item['name']} ({item['calories']:.0f} ккал, "
            f"Б:{item['p']:.1f} Ж:{item['f']:.1f} У:{item['c']:.1f})"
            for item in foods
        )
        ctx += f"\n\nРацион сегодня:\n{food_lines}"
    return ctx


def _convert_audio(ogg_path: str, wav_path: str) -> None:
    AudioSegment.from_ogg(ogg_path).export(wav_path, format="wav")


def _do_recognize(wav_path: str) -> str:
    recognizer = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio_data = recognizer.record(source)
    return recognizer.recognize_google(audio_data, language="ru-RU")


async def _send_stats(message: Message, user_id: int, target: int, date_str: str) -> None:
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)

    total_cal = sum(f.get("calories", 0) for f in foods)
    total_p   = sum(f.get("p", 0) for f in foods)
    total_f   = sum(f.get("f", 0) for f in foods)
    total_c   = sum(f.get("c", 0) for f in foods)
    net       = total_cal - burned

    text = (
        f"📅 *Итог за сегодня:*\n\n"
        f"📊 Калории: *{_esc(f'{total_cal:.0f}')}* / {_esc(str(target))} ккал "
        f"{_esc(_bar(total_cal, target))}\n"
        f"🔥 Сгорело за день: *{_esc(f'{burned:.0f}')}* ккал\n"
        f"⚖️ Чистый баланс: *{_esc(f'{net:.0f}')}* ккал\n\n"
        f"🥩 Белки: *{_esc(f'{total_p:.1f}')}* / {_esc(str(_NORM_P))} г "
        f"{_esc(_bar(total_p, _NORM_P))}\n"
        f"🥑 Жиры: *{_esc(f'{total_f:.1f}')}* / {_esc(str(_NORM_F))} г "
        f"{_esc(_bar(total_f, _NORM_F))}\n"
        f"🍞 Углеводы: *{_esc(f'{total_c:.1f}')}* / {_esc(str(_NORM_C))} г "
        f"{_esc(_bar(total_c, _NORM_C))}"
    )
    await message.answer(text, parse_mode="MarkdownV2")


async def _process_ai_response(
    message: Message, user_id: int, text: str,
    context: str, target: int, date_str: str,
) -> None:
    result = await ask_gemini(text, context)

    # Support both new type-field format and legacy action-only format
    type_  = result.get("type", "")
    action = result.get("action", "")
    data   = result.get("data")

    # Backward compat: no type but has action → treat as food
    if not type_ and action:
        type_ = "food"

    # ── FOOD ──────────────────────────────────────────────────────────────
    if type_ == "food":
        if action == "log_food" and data:
            foods_before = get_today_foods(user_id, date_str)
            eaten_before = sum(f.get("calories", 0) for f in foods_before)
            meal_type    = data.get("meal_type", "Перекус")
            items        = data.get("items", [])
            if items:
                log_food_to_db(user_id, date_str, meal_type, items)
                await message.answer(
                    _fmt_food_items(items, target, eaten_before),
                    parse_mode="MarkdownV2",
                )
            return

        if action == "delete_food" and data:
            product_name = data.get("product_name", "")
            if product_name:
                delete_food_from_db(user_id, date_str, product_name)
            foods = get_today_foods(user_id, date_str)
            await message.answer(_fmt_daily_log(foods, target), parse_mode="MarkdownV2")
            return

        if action == "edit_food" and data:
            old_name = data.get("old_name", "")
            new_name = data.get("new_name", "")
            if old_name and new_name:
                update_food_in_db(
                    user_id, date_str, old_name, new_name,
                    data.get("calories", 0), data.get("p", 0),
                    data.get("f", 0), data.get("c", 0),
                )
            foods = get_today_foods(user_id, date_str)
            await message.answer(_fmt_daily_log(foods, target), parse_mode="MarkdownV2")
            return

        if action == "change_weight" and data:
            product_name = data.get("product_name", "")
            if product_name:
                update_food_in_db(
                    user_id, date_str, product_name, product_name,
                    data.get("calories", 0), data.get("p", 0),
                    data.get("f", 0), data.get("c", 0),
                )
            foods = get_today_foods(user_id, date_str)
            await message.answer(_fmt_daily_log(foods, target), parse_mode="MarkdownV2")
            return

    # ── ACTIVITY ──────────────────────────────────────────────────────────
    if type_ == "activity":
        burned      = float(result.get("burned_calories", 0))
        description = result.get("description", "")
        if burned > 0:
            log_activity_to_db(user_id, date_str, description, burned)
        desc_part = f" — {_esc(description)}" if description else ""
        await message.answer(
            f"💪 *Сгорело {_esc(f'{burned:.0f}')} ккал*{desc_part}\\.\n"
            f"Добавлено в зачёт дня\\.",
            parse_mode="MarkdownV2",
        )
        return

    # ── QUESTION ──────────────────────────────────────────────────────────
    if type_ == "question":
        reply = result.get("reply", "Нет ответа.")
        await message.answer(reply)
        return

    # Fallback
    fallback = result.get("reply", result.get("user_message", "Не удалось получить ответ."))
    await message.answer(fallback)


# ---------------------------------------------------------------------------
# Обработчики Telegram
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def start_handler(message: Message):
    user_id = message.from_user.id
    name    = message.from_user.full_name or "друг"
    target  = get_or_create_user(user_id, name)
    await message.answer(
        f"Привет, {name}! 👋 Я твой персональный ИИ-тренер и умный дневник питания.\n\n"
        f"📊 Твоя дневная норма — {target} ккал.\n\n"
        f"🍎 Напиши или скажи голосом, что съел — запишу КБЖУ.\n"
        f"💪 Скажи об активности — посчитаю сожжённые калории.\n"
        f"💡 Задай вопрос — отвечу с учётом твоего дня.",
        reply_markup=get_reply_menu(),
    )


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    user_id = message.from_user.id
    target  = get_or_create_user(user_id, message.from_user.full_name or "друг")
    date_str = datetime.now().strftime("%Y-%m-%d")
    await _send_stats(message, user_id, target, date_str)


@dp.message(Command("workout"))
async def workout_handler(message: Message):
    await message.answer(
        "💪 Опиши свою активность:\n"
        "«прошёл 8000 шагов», «30 мин бег», «час в зале»"
    )


@dp.message(Command("clear_today"))
async def clear_today_handler(message: Message):
    user_id  = message.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    clear_today_foods(user_id, date_str)
    clear_today_activity(user_id, date_str)
    await message.answer("🧹 Дневной рацион и калории за сегодня успешно сброшены.")


@dp.message(F.voice)
async def voice_handler(message: Message):
    user_id  = message.from_user.id
    name     = message.from_user.full_name or "друг"
    target   = get_or_create_user(user_id, name)
    date_str = datetime.now().strftime("%Y-%m-%d")

    foods       = get_today_foods(user_id, date_str)
    burned      = get_today_burned(user_id, date_str)
    eaten_today = sum(f.get("calories", 0) for f in foods)
    context     = _build_context(target, eaten_today, burned, foods)

    voice_file = await bot.get_file(message.voice.file_id)
    ogg_buffer = io.BytesIO()
    await bot.download_file(voice_file.file_path, destination=ogg_buffer)
    ogg_buffer.seek(0)

    ogg_tmp = wav_tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            ogg_tmp = f.name
            f.write(ogg_buffer.read())

        wav_tmp = ogg_tmp.replace(".ogg", ".wav")
        await asyncio.to_thread(_convert_audio, ogg_tmp, wav_tmp)

        try:
            recognized_text = await asyncio.to_thread(_do_recognize, wav_tmp)
        except sr.UnknownValueError:
            await message.answer("Не удалось распознать речь — попробуй ещё раз или напиши текстом.")
            return
        except sr.RequestError as e:
            await message.answer(f"Ошибка сервиса распознавания: {e}")
            return
    finally:
        if ogg_tmp and os.path.exists(ogg_tmp):
            os.unlink(ogg_tmp)
        if wav_tmp and os.path.exists(wav_tmp):
            os.unlink(wav_tmp)

    try:
        await _process_ai_response(message, user_id, recognized_text, context, target, date_str)
    except RuntimeError as e:
        await message.answer(f"Ошибка при обращении к ИИ: {e}")


@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message):
    user_id    = message.from_user.id
    name       = message.from_user.full_name or "друг"
    target     = get_or_create_user(user_id, name)
    date_str   = datetime.now().strftime("%Y-%m-%d")
    text_lower = message.text.lower()

    # ── Reply-кнопка: Статистика ──
    if any(phrase in text_lower for phrase in _STATS_PHRASES):
        await _send_stats(message, user_id, target, date_str)
        return

    # ── Reply-кнопка: Добавить тренировку ──
    if any(phrase in text_lower for phrase in _WORKOUT_PHRASES):
        await message.answer(
            "💪 Опиши свою активность:\n"
            "«прошёл 8000 шагов», «30 мин бег», «час в зале»"
        )
        return

    # ── Reply-кнопка / фраза: Сбросить день ──
    if any(phrase in text_lower for phrase in _CLEAR_PHRASES):
        clear_today_foods(user_id, date_str)
        clear_today_activity(user_id, date_str)
        await message.answer("🧹 Дневной рацион и калории за сегодня успешно сброшены.")
        return

    foods       = get_today_foods(user_id, date_str)
    burned      = get_today_burned(user_id, date_str)
    eaten_today = sum(f.get("calories", 0) for f in foods)
    context     = _build_context(target, eaten_today, burned, foods)

    try:
        await _process_ai_response(message, user_id, message.text, context, target, date_str)
    except RuntimeError as e:
        await message.answer(f"Ошибка при обращении к ИИ: {e}")


# ---------------------------------------------------------------------------
# Обработчики inline-кнопок меню
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "diary_today")
async def cb_diary_today(callback: CallbackQuery):
    await callback.answer()
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    target   = get_or_create_user(user_id, callback.from_user.full_name or "друг")
    foods    = get_today_foods(user_id, date_str)
    if foods:
        await callback.message.answer(_fmt_daily_log(foods, target), parse_mode="MarkdownV2")
    else:
        await callback.message.answer("Сегодня записей о питании нет.")


@dp.callback_query(F.data == "diary_week")
async def cb_diary_week(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📅 Функция в разработке — скоро здесь появится статистика за неделю.")


@dp.callback_query(F.data == "ask_food")
async def cb_ask_food(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("💡 Напиши или скажи голосом название продукта — я скажу, стоит ли его есть.")


# ---------------------------------------------------------------------------
# Фоновый веб-сервер (Port Binding для Render)
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", 10000))


async def handle_ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main():
    init_db()

    app = web.Application()
    app.add_routes([web.get("/", handle_ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"Порт {PORT} открыт — Render может пройти проверку")

    await bot.delete_webhook(drop_pending_updates=True)
    print("Старые сессии сброшены")

    print("Бот запущен в режиме Long Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
