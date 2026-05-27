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

# ── Приёмы пищи ───────────────────────────────────────────────────────────────

_MEAL_ORDER = ["Завтрак", "Обед", "Полдник", "Ужин", "Перекус"]
_MEAL_ICONS = {
    "Завтрак": "🌅",
    "Обед":    "☀️",
    "Полдник": "🌤",
    "Ужин":    "🌙",
    "Перекус": "🍎",
}

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
    (["бисквит", "крекер", "вафл", "пряник"], "🍪"),
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
    """Прогресс-бар: █░"""
    filled = int(length * min(current, total) / total) if total > 0 else 0
    return "█" * filled + "░" * (length - filled)


def _pct(current: float, total: float) -> int:
    return min(int(current / total * 100), 100) if total > 0 else 0


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
    resolved = []
    for item in raw_items:
        name   = item.get("name", "неизвестный продукт")
        weight = float(item.get("weight", 100))

        found = food_db.search(name, weight)
        if found:
            found["emoji"]   = _pick_emoji(found["name"])
            found["from_db"] = True
            resolved.append(found)
        else:
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
            food_db.append_product(
                name,
                nutrition["calories"], nutrition["protein"],
                nutrition["fat"],      nutrition["carbs"],
            )
            logger.info("New product added to DB: {}", name)

    return resolved


# ── FatSecret-style форматирование ────────────────────────────────────────────

def _fmt_day_header(target: int, total_cal: float, total_p: float,
                    total_f: float, total_c: float, burned: float) -> list[str]:
    """Шапка дня: дата + калории + макросы с прогресс-барами."""
    now = datetime.now()
    day_names   = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    month_names = ["янв","фев","мар","апр","май","июн",
                   "июл","авг","сен","окт","ноя","дек"]
    date_label = f"{day_names[now.weekday()]}, {now.day} {month_names[now.month - 1]}"

    net       = total_cal - burned
    remaining = target - net
    cal_pct   = _pct(total_cal, target)

    lines = [
        f"📅 *{_esc(date_label)}*",
        "",
        "📊 *Калории*",
        f"🔥 *{_esc(f'{total_cal:.0f}')}* / {_esc(str(target))} ккал",
        f"`{_bar(total_cal, target, 14)}` {_esc(str(cal_pct))}%",
    ]

    if remaining >= 0:
        lines.append(f"Осталось: *{_esc(f'{remaining:.0f}')}* ккал")
    else:
        lines.append(f"⚠️ Превышение: *{_esc(f'{abs(remaining):.0f}')}* ккал")

    if burned > 0:
        lines.append(f"💪 Сожжено: {_esc(f'{burned:.0f}')} ккал")

    lines += [
        "",
        "🧬 *Макросы*",
        f"🥩 Белки:    *{_esc(f'{total_p:.1f}')}* / {_esc(str(_NORM_P))} г  "
        f"`{_bar(total_p, _NORM_P, 10)}` {_esc(str(_pct(total_p, _NORM_P)))}%",
        f"🥑 Жиры:     *{_esc(f'{total_f:.1f}')}* / {_esc(str(_NORM_F))} г  "
        f"`{_bar(total_f, _NORM_F, 10)}` {_esc(str(_pct(total_f, _NORM_F)))}%",
        f"🍞 Углеводы: *{_esc(f'{total_c:.1f}')}* / {_esc(str(_NORM_C))} г  "
        f"`{_bar(total_c, _NORM_C, 10)}` {_esc(str(_pct(total_c, _NORM_C)))}%",
    ]
    return lines


def _fmt_meals_section(foods: list[dict]) -> list[str]:
    """Блок с приёмами пищи, сгруппированными по типу."""
    if not foods:
        return ["", "_Рацион пуст — расскажи что съел\\!_"]

    # Группировка
    by_meal: dict[str, list[dict]] = {}
    for f in foods:
        mt = f.get("meal_type") or "Перекус"
        by_meal.setdefault(mt, []).append(f)

    ordered = [m for m in _MEAL_ORDER if m in by_meal]
    ordered += [m for m in by_meal if m not in _MEAL_ORDER]

    lines = []
    for meal in ordered:
        items    = by_meal[meal]
        meal_cal = sum(i.get("calories", 0) for i in items)
        meal_p   = sum(i.get("p", 0) for i in items)
        meal_f   = sum(i.get("f", 0) for i in items)
        meal_c   = sum(i.get("c", 0) for i in items)
        icon     = _MEAL_ICONS.get(meal, "🍽")

        lines.append("")
        lines.append(
            f"{icon} *{_esc(meal)}*  —  "
            f"{_esc(f'{meal_cal:.0f}')} ккал "
            f"\\| Б:{_esc(f'{meal_p:.1f}')} "
            f"Ж:{_esc(f'{meal_f:.1f}')} "
            f"У:{_esc(f'{meal_c:.1f}')}"
        )
        lines.append("┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
        for item in items:
            emoji  = _pick_emoji(item.get("name", ""))
            name   = _esc(item.get("name", ""))
            weight = item.get("weight") or 0
            cal    = _esc(f"{item.get('calories', 0):.0f}")
            p_     = _esc(f"{item.get('p', 0):.1f}")
            f_     = _esc(f"{item.get('f', 0):.1f}")
            c_     = _esc(f"{item.get('c', 0):.1f}")
            w_part = f" {_esc(str(int(weight)))}г" if weight > 0 else ""
            ai_tag = " _\\(ИИ\\)_" if not item.get("from_db", True) else ""
            lines.append(
                f"  {emoji} *{name}*{w_part}{ai_tag}\n"
                f"       {cal} ккал  Б:{p_}  Ж:{f_}  У:{c_}"
            )

    return lines


def _fmt_full_day(foods: list[dict], target: int, burned: float = 0.0) -> str:
    """Полный FatSecret-дашборд: шапка + приёмы пищи."""
    total_cal = sum(f.get("calories", 0) for f in foods)
    total_p   = sum(f.get("p", 0) for f in foods)
    total_f   = sum(f.get("f", 0) for f in foods)
    total_c   = sum(f.get("c", 0) for f in foods)

    lines  = _fmt_day_header(target, total_cal, total_p, total_f, total_c, burned)
    lines += _fmt_meals_section(foods)
    return "\n".join(lines)


def _fmt_log_confirm(items: list[dict], meal_type: str,
                     target: int, eaten_before: float) -> str:
    """Подтверждение после записи еды: что добавлено + мини-итог дня."""
    added_cal = sum(i.get("calories", 0) for i in items)
    added_p   = sum(i.get("p", 0) for i in items)
    added_f   = sum(i.get("f", 0) for i in items)
    added_c   = sum(i.get("c", 0) for i in items)

    icon = _MEAL_ICONS.get(meal_type, "🍽")
    lines = [f"✅ *Записано в {_esc(meal_type)}* {icon}", ""]

    for item in items:
        emoji  = _pick_emoji(item.get("name", ""))
        name   = _esc(item.get("name", ""))
        weight = item.get("weight") or 0
        cal    = _esc(f"{item.get('calories', 0):.0f}")
        p_     = _esc(f"{item.get('p', 0):.1f}")
        f_     = _esc(f"{item.get('f', 0):.1f}")
        c_     = _esc(f"{item.get('c', 0):.1f}")
        w_part = f" {_esc(str(int(weight)))}г" if weight > 0 else ""
        ai_tag = " _\\(оценка ИИ\\)_" if not item.get("from_db", True) else ""
        lines.append(
            f"{emoji} *{name}*{w_part}{ai_tag}\n"
            f"   {cal} ккал  \\|  Б:{p_}  Ж:{f_}  У:{c_}"
        )

    # Мини-итог дня
    new_total = eaten_before + added_cal
    remaining = target - new_total
    lines += [
        "",
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"*Итого за день:* {_esc(f'{new_total:.0f}')} / {_esc(str(target))} ккал",
        f"`{_bar(new_total, target, 14)}`",
    ]
    if remaining >= 0:
        lines.append(f"Осталось: *{_esc(f'{remaining:.0f}')}* ккал")
    else:
        lines.append(f"⚠️ Превышение: *{_esc(f'{abs(remaining):.0f}')}* ккал")

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


def _build_logged_kb(logged_foods: list[dict]) -> InlineKeyboardMarkup:
    """Клавиатура под подтверждением записи: ❌ каждый продукт + ✏️ + 📋"""
    buttons = []
    # Кнопки удаления — по одной на строку (или по 2 если названия короткие)
    row: list[InlineKeyboardButton] = []
    for item in logged_foods:
        name_short = item.get("name", "")[:18]
        btn = InlineKeyboardButton(
            text=f"❌ {name_short}",
            callback_data=f"del_food:{item['id']}",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    # Строка: Изменить + Дневник
    buttons.append([
        InlineKeyboardButton(text="✏️ Изменить", callback_data="hint_edit"),
        InlineKeyboardButton(text="📋 Дневник", callback_data="diary_today"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Статистика ────────────────────────────────────────────────────────────────

async def _send_stats(message: Message, user_id: int, target: int, date_str: str) -> None:
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)

    text = _fmt_full_day(foods, target, burned)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Дневник / удалить блюда", callback_data="diary_today"),
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
        raw_items = result.get("items", [])
        meal_type = result.get("meal_type") or _meal_type_by_time()
        if not raw_items:
            await message.answer("Не распознал продукты — попробуй ещё раз.")
            return

        foods_before = get_today_foods(user_id, date_str)
        eaten_before = sum(f.get("calories", 0) for f in foods_before)

        items = await _resolve_items(raw_items)
        log_food_to_db(user_id, date_str, meal_type, items)
        logger.info("user={} logged {} items (meal={})", user_id, len(items), meal_type)

        # Получаем ID только что добавленных записей (последние N в БД)
        all_foods_now = get_today_foods(user_id, date_str)
        just_logged   = all_foods_now[-len(items):]
        # Переносим emoji и from_db из resolved items
        for i, src in enumerate(items):
            if i < len(just_logged):
                just_logged[i]["emoji"]   = src.get("emoji", _DEFAULT_EMOJI)
                just_logged[i]["from_db"] = src.get("from_db", True)

        await message.answer(
            _fmt_log_confirm(items, meal_type, target, eaten_before),
            parse_mode="MarkdownV2",
            reply_markup=_build_logged_kb(just_logged),
        )
        return

    # ── FOOD: delete_food ─────────────────────────────────────────────────────
    if rtype == "food" and action == "delete_food":
        product_name = result.get("product_name", "")
        if product_name:
            delete_food_from_db(user_id, date_str, product_name)
        foods  = get_today_foods(user_id, date_str)
        burned = get_today_burned(user_id, date_str)
        await message.answer(
            _fmt_full_day(foods, target, burned),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        return

    # ── FOOD: change_weight ───────────────────────────────────────────────────
    if rtype == "food" and action == "change_weight":
        product_name = result.get("product_name", "")
        new_weight   = float(result.get("new_weight", 0))
        if product_name and new_weight > 0:
            found = food_db.search(product_name, new_weight)
            if found:
                update_food_in_db(
                    user_id, date_str, product_name, product_name,
                    found["calories"], found["p"], found["f"], found["c"],
                    weight=new_weight,
                )
        foods  = get_today_foods(user_id, date_str)
        burned = get_today_burned(user_id, date_str)
        await message.answer(
            _fmt_full_day(foods, target, burned),
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
        foods  = get_today_foods(user_id, date_str)
        burned = get_today_burned(user_id, date_str)
        await message.answer(
            _fmt_full_day(foods, target, burned),
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
        total_burned = get_today_burned(user_id, date_str)
        await message.answer(
            f"💪 *Сгорело {_esc(f'{burned:.0f}')} ккал*{desc_part}\n"
            f"Всего сожжено сегодня: *{_esc(f'{total_burned:.0f}')}* ккал",
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
        f"Привет, {_esc(name)}\\! 👋\n\n"
        f"Я твой AI\\-дневник питания в стиле FatSecret\\.\n\n"
        f"📊 Дневная цель: *{_esc(str(target))} ккал*\n"
        f"🥩 Белки: *{_esc(str(_NORM_P))} г*  "
        f"🥑 Жиры: *{_esc(str(_NORM_F))} г*  "
        f"🍞 Углеводы: *{_esc(str(_NORM_C))} г*\n\n"
        f"🎙️ Скажи голосом или напиши что съел\n"
        f"💪 Расскажи об активности\n"
        f"📋 Нажми *Статистика за день* чтобы открыть дневник\n\n"
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

    voice_file = await bot.get_file(message.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(voice_file.file_path, destination=buf)
    ogg_bytes = buf.getvalue()

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
    burned   = get_today_burned(user_id, date_str)
    await callback.message.answer(
        _fmt_full_day(foods, target, burned),
        parse_mode="MarkdownV2",
        reply_markup=_build_delete_keyboard(foods) if foods else None,
    )


@dp.callback_query(F.data.startswith("del_food:"))
async def cb_delete_food(callback: CallbackQuery):
    food_id  = int(callback.data.split(":")[1])
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    target   = get_or_create_user(user_id, callback.from_user.full_name or "друг")
    delete_food_by_id(food_id)
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)
    if foods:
        await callback.message.edit_text(
            _fmt_full_day(foods, target, burned),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods),
        )
    else:
        await callback.message.edit_text(
            _fmt_full_day([], target, 0.0),
            parse_mode="MarkdownV2",
        )
    await callback.answer("Удалено ✓")


@dp.callback_query(F.data == "clear_day")
async def cb_clear_day(callback: CallbackQuery):
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    soft_delete_today(user_id, date_str)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Восстановить", callback_data="restore_day"),
        InlineKeyboardButton(text="🗑️ Удалить навсегда", callback_data="confirm_clear"),
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
    burned = get_today_burned(user_id, date_str)
    await callback.message.edit_text(
        _fmt_full_day(foods, target, burned),
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


@dp.callback_query(F.data == "hint_edit")
async def cb_hint_edit(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "✏️ *Что изменить?* Просто напиши:\n\n"
        "• «молоко было 200г» — изменить вес\n"
        "• «вместо гречки рис 150г» — заменить продукт\n"
        "• «удали молоко» — удалить запись",
        parse_mode="MarkdownV2",
    )


# ── Веб-сервер для health-check (Render) ─────────────────────────────────────

PORT = int(os.environ.get("PORT", 10000))


async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    init_db()

    # Загрузить базу продуктов из GitHub
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
