import asyncio
import io
import os
import re
from datetime import datetime

from aiohttp import web
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardButton, InlineKeyboardMarkup,
)

from database import (
    init_db, get_or_create_user,
    log_food_to_db, get_today_foods,
    delete_food_from_db, delete_food_by_id, update_food_in_db,
    soft_delete_today, restore_today, hard_delete_soft_deleted,
    clear_today_foods, log_activity_to_db, get_today_burned,
    clear_today_activity,
)
from keyboards import get_reply_menu
import food_db
import gemini_service
import groq_service

BOT_TOKEN = os.environ["BOT_TOKEN"]
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

_STATS_PHRASES   = ["📊 статистика за день", "статистика за день"]
_WORKOUT_PHRASES = ["💪 добавить тренировку", "добавить тренировку"]
_CLEAR_PHRASES   = [
    "🧹 сбросить день", "сбросить день", "очистить калории",
    "сбросить калории", "обнулить калории", "очисти калории",
    "сбрось калории", "очистить рацион", "сбросить рацион",
]

_NORM_P, _NORM_F, _NORM_C = (
    int(os.environ.get("DAILY_PROTEIN", 140)),
    int(os.environ.get("DAILY_FAT", 80)),
    int(os.environ.get("DAILY_CARBS", 220)),
)

# ── Emoji-маппинг ─────────────────────────────────────────────────────────────

_EMOJI_MAP: list[tuple[list[str], str]] = [
    (["курин", "цыплён", "окорочок", "грудк", "бедр", "крыл"], "🍗"),
    (["говядин", "телятин", "стейк", "бифштекс", "свинин", "карбонад",
      "фарш", "шашлык", "котлет"], "🥩"),
    (["сосис", "колбас", "сардел", "хот-дог", "хотдог"], "🌭"),
    (["печень", "почк", "сердц", "пупочк", "желудк"], "🫀"),
    (["рыб", "сёмг", "лосос", "форел", "тунец", "треск", "судак",
      "минтай", "горбуш", "скумбри", "сельд"], "🐟"),
    (["креветк", "кальмар", "краб", "морепродукт", "мидий"], "🦐"),
    (["яйц", "омлет", "глазунья"], "🥚"),
    (["молок", "кефир", "ряженк"], "🥛"),
    (["творог", "йогурт", "сметан"], "🧴"),
    (["сыр"], "🧀"),
    (["масл", "маргарин"], "🧈"),
    (["гречк", "гречнев"], "🌾"),
    (["рис", "рисов"], "🍚"),
    (["овсянк", "овёс", "овсян", "геркулес"], "🥣"),
    (["макарон", "паста", "спагетти", "лапш", "феттучин"], "🍝"),
    (["хлеб", "батон", "багет", "буханк", "лаваш", "тост"], "🍞"),
    (["каш"], "🥣"),
    (["картофел", "картошк", "пюре"], "🥔"),
    (["помидор", "томат"], "🍅"),
    (["огурец", "огурц"], "🥒"),
    (["морков"], "🥕"),
    (["капуст", "броккол", "цветн"], "🥦"),
    (["перец болгар", "паприк"], "🫑"),
    (["лук", "чеснок"], "🧅"),
    (["кукуруз"], "🌽"),
    (["шпинат", "салат", "зелен", "листов"], "🥗"),
    (["баклажан", "кабачок", "цукини"], "🍆"),
    (["свёкл", "свекл"], "🫚"),
    (["яблок"], "🍎"),
    (["банан"], "🍌"),
    (["апельсин", "мандарин", "грейпфрут"], "🍊"),
    (["груш"], "🍐"),
    (["виноград"], "🍇"),
    (["клубник", "малин", "смородин", "черник", "ягод"], "🍓"),
    (["арбуз"], "🍉"),
    (["ананас"], "🍍"),
    (["манго", "папайя"], "🥭"),
    (["авокадо"], "🥑"),
    (["чай"], "🍵"),
    (["кофе", "капучино", "латте", "эспрессо", "американо"], "☕"),
    (["сок", "нектар", "смузи"], "🧃"),
    (["вода"], "💧"),
    (["шоколад", "конфет", "батончик"], "🍫"),
    (["торт", "пирог", "пирожн", "кекс"], "🎂"),
    (["бисквит", "крекер", "вафл", "пряник", "печень"], "🍪"),
    (["мороженое"], "🍦"),
    (["мёд", "мед"], "🍯"),
    (["варень", "джем"], "🍓"),
    (["суп", "борщ", "щи", "солянк", "рассольник", "окрошк"], "🍲"),
    (["пельмен", "вареник", "манты", "хинкал"], "🥟"),
    (["пицц"], "🍕"),
    (["бургер", "сэндвич", "бутерброд"], "🍔"),
    (["блин", "оладьи", "панкейк"], "🥞"),
    (["роллы", "суши"], "🍱"),
    (["орех", "миндаль", "грецк", "кешью", "фундук", "фисташк"], "🥜"),
    (["горох", "фасоль", "чечевиц", "нут", "боб"], "🫘"),
    (["оливк", "подсолнечн", "растительн"], "🫒"),
]
_DEFAULT_EMOJI = "🍽"


def _pick_emoji(name: str) -> str:
    nl = name.lower()
    for keywords, emoji in _EMOJI_MAP:
        if any(kw in nl for kw in keywords):
            return emoji
    return _DEFAULT_EMOJI


# ── MarkdownV2 helpers ────────────────────────────────────────────────────────

def _esc(text) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def _bar(current: float, total: float, length: int = 10) -> str:
    filled = int(length * min(current, total) / total) if total > 0 else 0
    return "[" + "|" * filled + "." * (length - filled) + "]"


# ── Определение времени приёма пищи ──────────────────────────────────────────

def _meal_type_by_time() -> str:
    h = datetime.now().hour
    if h < 11:  return "Завтрак"
    if h < 15:  return "Обед"
    if h < 19:  return "Полдник"
    return "Ужин"


# ── Контекст для Gemini ───────────────────────────────────────────────────────

def _build_context(target: int, eaten: float, burned: float, foods: list[dict]) -> str:
    now = datetime.now()
    net = eaten - burned
    ctx = (
        f"Дата: {now.strftime('%Y-%m-%d')}. Время: {now.strftime('%H:%M')}. "
        f"Цель: {target} ккал. Съедено: {eaten:.0f} ккал. "
        f"Сожжено: {burned:.0f} ккал. Баланс: {net:.0f} ккал."
    )
    if foods:
        food_lines = "\n".join(
            f"- {f['name']} ({f['calories']:.0f} ккал, Б:{f['p']:.1f} Ж:{f['f']:.1f} У:{f['c']:.1f})"
            for f in foods
        )
        ctx += f"\n\nРацион:\n{food_lines}"
    return ctx


# ── Разрешение продуктов: Obsidian DB → Gemini estimate ──────────────────────

async def _resolve_items(raw_items: list[dict]) -> list[dict]:
    """
    Для каждого {name, weight} ищет КБЖУ в базе, при необходимости — оценивает Gemini.
    Авто-добавляет неизвестные продукты в базу.
    """
    resolved = []
    for item in raw_items:
        name   = item.get("name", "неизвестный продукт")
        weight = float(item.get("weight", 100))

        found = food_db.search(name, weight)
        if found:
            found["emoji"] = _pick_emoji(found["name"])
            resolved.append(found)
        else:
            # Gemini оценивает КБЖУ/100г
            nutrition = await gemini_service.estimate_nutrition(name)
            factor = weight / 100.0
            entry = {
                "name":     name,
                "emoji":    _pick_emoji(name),
                "weight":   weight,
                "calories": round(nutrition["calories"] * factor, 1),
                "p":        round(nutrition["protein"]  * factor, 1),
                "f":        round(nutrition["fat"]      * factor, 1),
                "c":        round(nutrition["carbs"]    * factor, 1),
                "from_db":  False,
            }
            resolved.append(entry)
            # Авто-обучение: дописать в базу
            food_db.append_product(
                name,
                nutrition["calories"], nutrition["protein"],
                nutrition["fat"],      nutrition["carbs"],
            )
            logger.info("New product added to DB: {}", name)

    return resolved


# ── Форматирование сообщений ──────────────────────────────────────────────────

def _fmt_food_items(items: list[dict], target: int, eaten_before: float) -> str:
    total_cal = sum(i.get("calories", 0) for i in items)
    total_p   = sum(i.get("p", 0) for i in items)
    total_f   = sum(i.get("f", 0) for i in items)
    total_c   = sum(i.get("c", 0) for i in items)

    lines = []
    for item in items:
        emoji  = item.get("emoji", _DEFAULT_EMOJI)
        weight = item.get("weight", 0)
        name   = item.get("name", "")
        src    = "" if item.get("from_db", True) else " _\\(оценка ИИ\\)_"
        label  = _esc(f"{name} ({int(weight)}г)") if weight else _esc(name)
        cal = _esc(f"{item.get('calories', 0):.0f}")
        p_  = _esc(f"{item.get('p', 0):.1f}")
        f_  = _esc(f"{item.get('f', 0):.1f}")
        c_  = _esc(f"{item.get('c', 0):.1f}")
        lines.append(
            f"{emoji} *{label}*{src}: {cal} ккал "
            f"\\| Б: {p_} \\| Ж: {f_} \\| У: {c_}"
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
    sign = "\\+" if remaining < 0 else ""
    remain_str = f"{sign}{_esc(f'{abs(remaining):.0f}')}"
    lines.append(
        f"\nОсталось сегодня: *{remain_str}* ккал из {_esc(str(target))}"
        if remaining >= 0 else
        f"\nПревышение: *{_esc(f'{abs(remaining):.0f}')}* ккал \\(лимит {_esc(str(target))}\\)"
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
        emoji      = _pick_emoji(item.get("name", ""))
        weight     = item.get("weight") or 0
        w_part     = f" \\({_esc(str(int(weight)))}г\\)" if weight > 0 else ""
        cal = _esc(f"{item.get('calories', 0):.0f}")
        p_  = _esc(f"{item.get('p', 0):.1f}")
        f_v = _esc(f"{item.get('f', 0):.1f}")
        c_  = _esc(f"{item.get('c', 0):.1f}")
        lines.append(
            f"{emoji} *{_esc(item['name'])}*{w_part}: "
            f"{cal} ккал \\| Б: {p_} \\| Ж: {f_v} \\| У: {c_}"
        )

    lines.append("ーーー")
    lines.append(
        f"📊 *Итого:* {_esc(f'{total_cal:.0f}')} / {_esc(str(target))} ккал "
        f"\\| Б: {_esc(f'{total_p:.1f}')} "
        f"\\| Ж: {_esc(f'{total_f:.1f}')} "
        f"\\| У: {_esc(f'{total_c:.1f}')}"
    )
    return "\n".join(lines)


# ── UI: клавиатуры ────────────────────────────────────────────────────────────

def _build_delete_keyboard(foods: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"❌ {f['name']}",
            callback_data=f"del_food:{f['id']}",
        )]
        for f in foods
    ]
    buttons.append([InlineKeyboardButton(text="🧹 Очистить день", callback_data="clear_day")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_DIARY_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="📋 Рацион / удалить блюда", callback_data="diary_today"),
]])


# ── Статистика ────────────────────────────────────────────────────────────────

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
        f"🔥 Сгорело: *{_esc(f'{burned:.0f}')}* ккал\n"
        f"⚖️ Баланс: *{_esc(f'{net:.0f}')}* ккал\n\n"
        f"🥩 Белки: *{_esc(f'{total_p:.1f}')}* / {_esc(str(_NORM_P))} г "
        f"{_esc(_bar(total_p, _NORM_P))}\n"
        f"🥑 Жиры: *{_esc(f'{total_f:.1f}')}* / {_esc(str(_NORM_F))} г "
        f"{_esc(_bar(total_f, _NORM_F))}\n"
        f"🍞 Углеводы: *{_esc(f'{total_c:.1f}')}* / {_esc(str(_NORM_C))} г "
        f"{_esc(_bar(total_c, _NORM_C))}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Рацион / удалить блюда", callback_data="diary_today"),
    ]])
    await message.answer(text, parse_mode="MarkdownV2", reply_markup=kb)


# ── Обработка ответа Gemini ───────────────────────────────────────────────────

async def _process_intent(
    message: Message, user_id: int,
    user_text: str, context: str,
    target: int, date_str: str,
) -> None:
    result = await gemini_service.extract_intent(user_text, context)
    logger.info("user={} intent type={} action={}", user_id, result.get("type"), result.get("action"))

    rtype  = result.get("type", "")
    action = result.get("action", "")

    # ── FOOD: log_food ────────────────────────────────────────────────────────
    if rtype == "food" and action == "log_food":
        raw_items  = result.get("items", [])
        meal_type  = result.get("meal_type") or _meal_type_by_time()
        if not raw_items:
            await message.answer("Не распознал продукты — попробуй ещё раз.")
            return

        foods_before = get_today_foods(user_id, date_str)
        eaten_before = sum(f.get("calories", 0) for f in foods_before)

        items = await _resolve_items(raw_items)
        log_food_to_db(user_id, date_str, meal_type, items)
        logger.info("user={} logged {} items (meal={})", user_id, len(items), meal_type)

        await message.answer(
            _fmt_food_items(items, target, eaten_before),
            parse_mode="MarkdownV2",
            reply_markup=_DIARY_KB,
        )
        return

    # ── FOOD: delete_food ─────────────────────────────────────────────────────
    if rtype == "food" and action == "delete_food":
        product_name = result.get("product_name", "")
        if product_name:
            delete_food_from_db(user_id, date_str, product_name)
        foods = get_today_foods(user_id, date_str)
        await message.answer(
            _fmt_daily_log(foods, target),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        return

    # ── FOOD: change_weight ───────────────────────────────────────────────────
    if rtype == "food" and action == "change_weight":
        product_name = result.get("product_name", "")
        new_weight   = float(result.get("new_weight", 0))
        if product_name and new_weight > 0:
            # Пересчитать КБЖУ из базы по новому весу
            found = food_db.search(product_name, new_weight)
            if found:
                update_food_in_db(
                    user_id, date_str, product_name, product_name,
                    found["calories"], found["p"], found["f"], found["c"],
                    weight=new_weight,
                )
        foods = get_today_foods(user_id, date_str)
        await message.answer(
            _fmt_daily_log(foods, target),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        return

    # ── FOOD: edit_food ───────────────────────────────────────────────────────
    if rtype == "food" and action == "edit_food":
        old_name   = result.get("old_name", "")
        new_name   = result.get("new_name", "")
        new_weight = float(result.get("new_weight", 100))
        if old_name and new_name:
            found = food_db.search(new_name, new_weight)
            if found:
                update_food_in_db(
                    user_id, date_str, old_name, found["name"],
                    found["calories"], found["p"], found["f"], found["c"],
                    weight=new_weight,
                )
        foods = get_today_foods(user_id, date_str)
        await message.answer(
            _fmt_daily_log(foods, target),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        return

    # ── ACTIVITY ──────────────────────────────────────────────────────────────
    if rtype == "activity":
        burned      = float(result.get("burned_calories", 0))
        description = result.get("description", "")
        if burned > 0:
            log_activity_to_db(user_id, date_str, description, burned)
        desc_part = f" — {_esc(description)}" if description else ""
        await message.answer(
            f"💪 *Сгорело {_esc(f'{burned:.0f}')} ккал*{desc_part}\\.\nДобавлено в зачёт дня\\.",
            parse_mode="MarkdownV2",
        )
        return

    # ── QUESTION ──────────────────────────────────────────────────────────────
    if rtype == "question":
        await message.answer(result.get("reply", "Нет ответа."))
        return

    # Fallback
    await message.answer(result.get("reply", result.get("user_message", "Не удалось понять запрос.")))


# ── Telegram handlers ─────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_handler(message: Message):
    user_id = message.from_user.id
    name    = message.from_user.full_name or "друг"
    target  = get_or_create_user(user_id, name)
    logger.info("user={} /start", user_id)
    await message.answer(
        f"Привет, {name}\\! 👋 Я твой ИИ\\-дневник питания\\.\n\n"
        f"📊 Дневная норма: *{_esc(str(target))} ккал*\n\n"
        f"🎙️ Скажи голосом или напиши что съел — запишу КБЖУ\\.\n"
        f"💪 Расскажи об активности — посчитаю сожжённые калории\\.\n"
        f"💡 Задай вопрос — отвечу с учётом твоего дня\\.\n\n"
        f"База продуктов: *10 000\\+* позиций",
        parse_mode="MarkdownV2",
        reply_markup=get_reply_menu(),
    )


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    user_id  = message.from_user.id
    target   = get_or_create_user(user_id, message.from_user.full_name or "друг")
    date_str = datetime.now().strftime("%Y-%m-%d")
    await _send_stats(message, user_id, target, date_str)


@dp.message(Command("workout"))
async def workout_handler(message: Message):
    await message.answer(
        "💪 Опиши активность:\n«прошёл 8000 шагов», «30 мин бег», «час в зале»"
    )


@dp.message(Command("clear_today"))
async def clear_today_handler(message: Message):
    user_id  = message.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    clear_today_foods(user_id, date_str)
    clear_today_activity(user_id, date_str)
    await message.answer("🧹 Дневной рацион сброшен.")


@dp.message(F.voice)
async def voice_handler(message: Message):
    user_id  = message.from_user.id
    name     = message.from_user.full_name or "друг"
    target   = get_or_create_user(user_id, name)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # Скачать OGG
    voice_file = await bot.get_file(message.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(voice_file.file_path, destination=buf)
    ogg_bytes = buf.getvalue()

    # Groq Whisper
    try:
        recognized = await groq_service.transcribe(ogg_bytes)
    except Exception as e:
        logger.error("user={} Whisper error: {}", user_id, e)
        await message.answer("Не удалось распознать речь — попробуй ещё раз или напиши текстом.")
        return

    logger.info("user={} voice: {!r}", user_id, recognized)
    await message.answer(f"🎙️ _{_esc(recognized)}_", parse_mode="MarkdownV2")

    foods   = get_today_foods(user_id, date_str)
    burned  = get_today_burned(user_id, date_str)
    eaten   = sum(f.get("calories", 0) for f in foods)
    context = _build_context(target, eaten, burned, foods)

    try:
        await _process_intent(message, user_id, recognized, context, target, date_str)
    except Exception as e:
        logger.error("user={} AI error: {}", user_id, e)
        await message.answer(f"Ошибка ИИ: {e}")


@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message):
    user_id    = message.from_user.id
    name       = message.from_user.full_name or "друг"
    target     = get_or_create_user(user_id, name)
    date_str   = datetime.now().strftime("%Y-%m-%d")
    text_lower = message.text.lower()

    if any(p in text_lower for p in _STATS_PHRASES):
        await _send_stats(message, user_id, target, date_str)
        return
    if any(p in text_lower for p in _WORKOUT_PHRASES):
        await message.answer("💪 Опиши активность:\n«прошёл 8000 шагов», «30 мин бег», «час в зале»")
        return
    if any(p in text_lower for p in _CLEAR_PHRASES):
        clear_today_foods(user_id, date_str)
        clear_today_activity(user_id, date_str)
        await message.answer("🧹 Дневной рацион сброшен.")
        return

    logger.info("user={} text: {!r}", user_id, message.text[:80])
    foods   = get_today_foods(user_id, date_str)
    burned  = get_today_burned(user_id, date_str)
    eaten   = sum(f.get("calories", 0) for f in foods)
    context = _build_context(target, eaten, burned, foods)

    try:
        await _process_intent(message, user_id, message.text, context, target, date_str)
    except Exception as e:
        logger.error("user={} AI error: {}", user_id, e)
        await message.answer(f"Ошибка ИИ: {e}")


# ── Inline callbacks ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "diary_today")
async def cb_diary_today(callback: CallbackQuery):
    await callback.answer()
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    target   = get_or_create_user(user_id, callback.from_user.full_name or "друг")
    foods    = get_today_foods(user_id, date_str)
    if foods:
        await callback.message.answer(
            _fmt_daily_log(foods, target),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods),
        )
    else:
        await callback.message.answer("Сегодня записей нет.")


@dp.callback_query(F.data.startswith("del_food:"))
async def cb_delete_food(callback: CallbackQuery):
    food_id  = int(callback.data.split(":")[1])
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    target   = get_or_create_user(user_id, callback.from_user.full_name or "друг")
    delete_food_by_id(food_id)
    foods = get_today_foods(user_id, date_str)
    if foods:
        await callback.message.edit_text(
            _fmt_daily_log(foods, target),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods),
        )
    else:
        await callback.message.edit_text("Рацион за сегодня пуст\\.", parse_mode="MarkdownV2")
    await callback.answer("Удалено ✓")


@dp.callback_query(F.data == "clear_day")
async def cb_clear_day(callback: CallbackQuery):
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    soft_delete_today(user_id, date_str)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Восстановить", callback_data="restore_day"),
        InlineKeyboardButton(text="🗑️ Удалить",      callback_data="confirm_clear"),
    ]])
    await callback.message.edit_text(
        "🧹 Лог за сегодня очищен\\!\n\nВосстановить записи?",
        parse_mode="MarkdownV2",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data == "restore_day")
async def cb_restore_day(callback: CallbackQuery):
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    restore_today(user_id, date_str)
    target = get_or_create_user(user_id, callback.from_user.full_name or "друг")
    foods  = get_today_foods(user_id, date_str)
    await callback.message.edit_text(
        _fmt_daily_log(foods, target),
        parse_mode="MarkdownV2",
        reply_markup=_build_delete_keyboard(foods) if foods else None,
    )
    await callback.answer("↩️ Восстановлено")


@dp.callback_query(F.data == "confirm_clear")
async def cb_confirm_clear(callback: CallbackQuery):
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    hard_delete_soft_deleted(user_id, date_str)
    await callback.message.edit_text("🗑️ Данные удалены\\.", parse_mode="MarkdownV2")
    await callback.answer()


@dp.callback_query(F.data == "diary_week")
async def cb_diary_week(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📅 Функция в разработке — скоро появится статистика за неделю.")


@dp.callback_query(F.data == "ask_food")
async def cb_ask_food(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("💡 Напиши название продукта — скажу, стоит ли его есть.")


# ── Веб-сервер для health-check (Render / Railway) ────────────────────────────

PORT = int(os.environ.get("PORT", 10000))


async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    init_db()

    # Загрузить базу продуктов из GitHub (блокирующий вызов в пуле потоков)
    await asyncio.to_thread(food_db.preload)
    # Фоновое обновление кэша каждые 5 минут
    asyncio.create_task(food_db.refresh_loop())

    app = web.Application()
    app.add_routes([web.get("/", _ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("Web server on port {}", PORT)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
