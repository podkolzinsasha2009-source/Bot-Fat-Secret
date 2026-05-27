import asyncio
import io
import os
import re
from datetime import datetime, timedelta

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
    clear_today_activity, restore_from_obsidian,
)
from keyboards import get_reply_menu
import food_db
import gemini_service
import groq_service
import obsidian_sync

BOT_TOKEN = os.environ["BOT_TOKEN"]
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ── Глобальное состояние ──────────────────────────────────────────────────────

_goals: dict     = obsidian_sync.DEFAULT_GOALS.copy()
_awaiting_edit: set[int] = set()

# ID пользователя для напоминаний (из env или обновляется при /start)
_bot_user_id: int = int(os.environ.get("TELEGRAM_USER_ID", "0"))

# Стрик: кэшируется на день
_current_streak: int = 1
_streak_date: str    = ""

# ── Константы ─────────────────────────────────────────────────────────────────

_STATS_PHRASES   = ["📊 статистика за день", "статистика за день"]
_WEEK_PHRASES    = ["📅 неделя", "неделя", "статистика за неделю"]
_WORKOUT_PHRASES = ["💪 добавить тренировку", "добавить тренировку"]
_CLEAR_PHRASES   = [
    "🧹 сбросить день", "сбросить день", "очистить калории",
    "сбросить калории", "обнулить калории", "очисти калории",
    "сбрось калории", "очистить рацион", "сбросить рацион",
]

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


# ── Targets helper ────────────────────────────────────────────────────────────

def _targets() -> tuple[int, int, int, int]:
    """Возвращает (cal, protein, fat, carbs) из текущих целей."""
    return (
        int(_goals.get("calories", 2000)),
        int(_goals.get("protein",  140)),
        int(_goals.get("fat",      80)),
        int(_goals.get("carbs",    220)),
    )


# ── MarkdownV2 helpers ────────────────────────────────────────────────────────

def _esc(text) -> str:
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


def _bar(current: float, total: float, length: int = 10) -> str:
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


# ── Извлечение веса из текста (для подписи к фото) ───────────────────────────

def _extract_weight_from_text(text: str) -> float | None:
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:г|гр|грамм|g)\b', text.lower())
    if m:
        return float(m.group(1).replace(",", "."))
    return None


# ── Контекст для Gemini ───────────────────────────────────────────────────────

def _build_context(target: int, eaten: float, burned: float, foods: list[dict]) -> str:
    now = datetime.now()
    net = eaten - burned
    ctx = (
        f"Дата: {now.strftime('%Y-%m-%d')}. Время: {now.strftime('%H:%M')}. "
        f"Цель: {target} ккал. Съедено: {eaten:.0f} ккал. "
        f"Сожжено: {burned:.0f} ккал. Баланс: {net:.0f} ккал. "
        f"Стрик: {_current_streak} дней."
    )
    if foods:
        food_lines = "\n".join(
            f"- {f['name']} ({f['calories']:.0f} ккал)"
            for f in foods
        )
        ctx += f"\n\nРацион сегодня:\n{food_lines}"
    return ctx


def _build_whisper_hint(foods: list[dict]) -> str:
    """Подсказка для Whisper — последние продукты из рациона."""
    if not foods:
        return ""
    names = [f["name"] for f in foods[-10:]]
    return ", ".join(names)


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
            # async: не блокирует event loop
            await food_db.append_product(
                name,
                nutrition["calories"], nutrition["protein"],
                nutrition["fat"],      nutrition["carbs"],
            )
            logger.info("New product added to DB: {}", name)

    return resolved


# ── FatSecret-style форматирование ────────────────────────────────────────────

def _fmt_day_header(
    target_cal: int, target_p: int, target_f: int, target_c: int,
    total_cal: float, total_p: float, total_f: float, total_c: float,
    burned: float,
    streak: int = 0,
) -> list[str]:
    now = datetime.now()
    day_names   = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    month_names = ["янв","фев","мар","апр","май","июн",
                   "июл","авг","сен","окт","ноя","дек"]
    date_label = f"{day_names[now.weekday()]}, {now.day} {month_names[now.month - 1]}"

    net       = total_cal - burned
    remaining = target_cal - net
    cal_pct   = _pct(total_cal, target_cal)

    lines = [
        f"📅 *{_esc(date_label)}*",
    ]
    if streak > 0:
        lines.append(f"🔥 Стрик: *{_esc(str(streak))} дней*")

    lines += [
        "",
        "📊 *Калории*",
        f"🔥 *{_esc(f'{total_cal:.0f}')}* / {_esc(str(target_cal))} ккал",
        f"`{_bar(total_cal, target_cal, 14)}` {_esc(str(cal_pct))}%",
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
        f"🥩 Белки:    *{_esc(f'{total_p:.1f}')}* / {_esc(str(target_p))} г  "
        f"`{_bar(total_p, target_p, 10)}` {_esc(str(_pct(total_p, target_p)))}%",
        f"🥑 Жиры:     *{_esc(f'{total_f:.1f}')}* / {_esc(str(target_f))} г  "
        f"`{_bar(total_f, target_f, 10)}` {_esc(str(_pct(total_f, target_f)))}%",
        f"🍞 Углеводы: *{_esc(f'{total_c:.1f}')}* / {_esc(str(target_c))} г  "
        f"`{_bar(total_c, target_c, 10)}` {_esc(str(_pct(total_c, target_c)))}%",
    ]
    return lines


def _fmt_meals_section(foods: list[dict]) -> list[str]:
    if not foods:
        return ["", "_Рацион пуст — расскажи что съел\\!_"]

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


def _fmt_full_day(foods: list[dict], burned: float = 0.0) -> str:
    cal, p, f, c = _targets()
    total_cal = sum(x.get("calories", 0) for x in foods)
    total_p   = sum(x.get("p", 0) for x in foods)
    total_f   = sum(x.get("f", 0) for x in foods)
    total_c   = sum(x.get("c", 0) for x in foods)

    lines  = _fmt_day_header(cal, p, f, c, total_cal, total_p, total_f, total_c,
                              burned, streak=_current_streak)
    lines += _fmt_meals_section(foods)
    return "\n".join(lines)


def _fmt_log_confirm(items: list[dict], meal_type: str, eaten_before: float) -> str:
    cal, _, _, _ = _targets()
    added_cal = sum(i.get("calories", 0) for i in items)
    added_p   = sum(i.get("p", 0) for i in items)
    added_f   = sum(i.get("f", 0) for i in items)
    added_c   = sum(i.get("c", 0) for i in items)

    icon  = _MEAL_ICONS.get(meal_type, "🍽")
    lines = [f"✅ *Записано в {_esc(meal_type)}* {icon}", ""]

    for item in items:
        emoji  = _pick_emoji(item.get("name", ""))
        name   = _esc(item.get("name", ""))
        weight = item.get("weight") or 0
        kcal   = _esc(f"{item.get('calories', 0):.0f}")
        p_     = _esc(f"{item.get('p', 0):.1f}")
        f_     = _esc(f"{item.get('f', 0):.1f}")
        c_     = _esc(f"{item.get('c', 0):.1f}")
        w_part = f" {_esc(str(int(weight)))}г" if weight > 0 else ""
        ai_tag = " _\\(оценка ИИ\\)_" if not item.get("from_db", True) else ""
        lines.append(
            f"{emoji} *{name}*{w_part}{ai_tag}\n"
            f"   {kcal} ккал  \\|  Б:{p_}  Ж:{f_}  У:{c_}"
        )

    new_total = eaten_before + added_cal
    remaining = cal - new_total
    streak_line = f"🔥 Стрик: *{_esc(str(_current_streak))} дней*\n" if _current_streak > 0 else ""
    lines += [
        "",
        "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
        f"*Итого за день:* {_esc(f'{new_total:.0f}')} / {_esc(str(cal))} ккал",
        f"`{_bar(new_total, cal, 14)}`",
    ]
    if remaining >= 0:
        lines.append(f"Осталось: *{_esc(f'{remaining:.0f}')}* ккал")
    else:
        lines.append(f"⚠️ Превышение: *{_esc(f'{abs(remaining):.0f}')}* ккал")
    if streak_line:
        lines.append(streak_line.strip())

    return "\n".join(lines)


# ── Форматирование недели ─────────────────────────────────────────────────────

def _fmt_week(week_data: list[dict]) -> str:
    cal, _, _, _ = _targets()
    day_names   = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    month_names = ["янв","фев","мар","апр","май","июн",
                   "июл","авг","сен","окт","ноя","дек"]

    lines = ["📅 *Статистика за 7 дней*", ""]
    total_cal  = 0.0
    active_days = 0

    for day in week_data:
        date_str = day.get("date", "")
        is_empty = day.get("empty", True)
        try:
            dt        = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = f"{day_names[dt.weekday()]}, {dt.day} {month_names[dt.month-1]}"
        except Exception:
            day_label = date_str

        if is_empty:
            lines.append(f"📭 *{_esc(day_label)}* — нет данных")
        else:
            kcal = float(day.get("calories_total", 0))
            total_cal  += kcal
            active_days += 1
            pct = _pct(kcal, cal)
            bar = _bar(kcal, cal, 10)
            tick = "✅" if kcal >= cal * 0.8 else "🔸"
            lines.append(
                f"{tick} *{_esc(day_label)}*\n"
                f"   🔥 {_esc(f'{kcal:.0f}')} ккал  `{bar}` {_esc(str(pct))}%"
            )

    lines.append("")
    lines.append("┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄")
    if active_days > 0:
        avg = total_cal / active_days
        lines.append(
            f"📊 Среднее за {_esc(str(active_days))} дней: "
            f"*{_esc(f'{avg:.0f}')}* / {_esc(str(cal))} ккал"
        )
    lines.append(f"🔥 Текущий стрик: *{_esc(str(_current_streak))} дней*")

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
    buttons = []
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
    buttons.append([
        InlineKeyboardButton(text="✏️ Изменить", callback_data="hint_edit"),
        InlineKeyboardButton(text="📋 Дневник",  callback_data="diary_today"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Статистика ────────────────────────────────────────────────────────────────

async def _send_stats(message: Message, user_id: int, date_str: str) -> None:
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)
    text   = _fmt_full_day(foods, burned)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Дневник / удалить блюда", callback_data="diary_today"),
        InlineKeyboardButton(text="📅 Неделя",                 callback_data="diary_week"),
    ]])
    await message.answer(text, parse_mode="MarkdownV2", reply_markup=kb)


# ── Фоновая запись в Obsidian ─────────────────────────────────────────────────

def _obsidian_write_bg(user_id: int, date_str: str) -> None:
    """Запустить запись в Obsidian в фоне (не блокирует handler)."""
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)
    asyncio.create_task(
        obsidian_sync.write_daily_log(date_str, foods, burned, _goals, _current_streak)
    )


# ── Обработка ответа Gemini ───────────────────────────────────────────────────

async def _process_intent(
    message: Message, user_id: int,
    user_text: str, context: str,
    date_str: str,
    force_edit: bool = False,
) -> None:
    result = await gemini_service.extract_intent(user_text, context, force_edit=force_edit)
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

        all_foods_now = get_today_foods(user_id, date_str)
        just_logged   = all_foods_now[-len(items):]
        for i, src in enumerate(items):
            if i < len(just_logged):
                just_logged[i]["emoji"]   = src.get("emoji", _DEFAULT_EMOJI)
                just_logged[i]["from_db"] = src.get("from_db", True)

        await message.answer(
            _fmt_log_confirm(items, meal_type, eaten_before),
            parse_mode="MarkdownV2",
            reply_markup=_build_logged_kb(just_logged),
        )
        _obsidian_write_bg(user_id, date_str)
        return

    # ── FOOD: delete_food ─────────────────────────────────────────────────────
    if rtype == "food" and action == "delete_food":
        product_name = result.get("product_name", "")
        if product_name:
            delete_food_from_db(user_id, date_str, product_name)
        foods  = get_today_foods(user_id, date_str)
        burned = get_today_burned(user_id, date_str)
        await message.answer(
            _fmt_full_day(foods, burned),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        _obsidian_write_bg(user_id, date_str)
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
            _fmt_full_day(foods, burned),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        _obsidian_write_bg(user_id, date_str)
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
            _fmt_full_day(foods, burned),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods) if foods else None,
        )
        _obsidian_write_bg(user_id, date_str)
        return

    # ── ACTIVITY ──────────────────────────────────────────────────────────────
    if rtype == "activity":
        burned_added = float(result.get("burned_calories", 0))
        description  = result.get("description", "")
        if burned_added > 0:
            log_activity_to_db(user_id, date_str, description, burned_added)
        desc_part    = f" — {_esc(description)}" if description else ""
        total_burned = get_today_burned(user_id, date_str)
        await message.answer(
            f"💪 *Сгорело {_esc(f'{burned_added:.0f}')} ккал*{desc_part}\n"
            f"Всего сожжено сегодня: *{_esc(f'{total_burned:.0f}')}* ккал",
            parse_mode="MarkdownV2",
        )
        _obsidian_write_bg(user_id, date_str)
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
    global _bot_user_id
    user_id = message.from_user.id
    name    = message.from_user.full_name or "друг"
    get_or_create_user(user_id, name)

    # Запоминаем ID для напоминаний
    if not _bot_user_id:
        _bot_user_id = user_id

    cal, p, f, c = _targets()
    logger.info("user={} /start", user_id)
    await message.answer(
        f"Привет, {_esc(name)}\\! 👋\n\n"
        f"Я твой AI\\-дневник питания в стиле FatSecret\\.\n\n"
        f"📊 Дневная цель: *{_esc(str(cal))} ккал*\n"
        f"🥩 Белки: *{_esc(str(p))} г*  "
        f"🥑 Жиры: *{_esc(str(f))} г*  "
        f"🍞 Углеводы: *{_esc(str(c))} г*\n"
        f"🔥 Стрик: *{_esc(str(_current_streak))} дней*\n\n"
        f"🎙️ Скажи голосом или напиши что съел\n"
        f"📸 Сфотографируй еду или штрихкод\n"
        f"💪 Расскажи об активности\n"
        f"📋 Нажми *Статистика за день* для дневника\n\n"
        f"База продуктов: *10 000\\+* позиций",
        parse_mode="MarkdownV2",
        reply_markup=get_reply_menu(),
    )


@dp.message(Command("stats"))
async def stats_handler(message: Message):
    user_id  = message.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    get_or_create_user(user_id, message.from_user.full_name or "друг")
    await _send_stats(message, user_id, date_str)


@dp.message(Command("week"))
async def week_handler(message: Message):
    await _handle_week(message)


@dp.message(Command("clear_today"))
async def clear_today_handler(message: Message):
    user_id  = message.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    clear_today_foods(user_id, date_str)
    clear_today_activity(user_id, date_str)
    await message.answer("🧹 Дневной рацион сброшен.")


@dp.message(F.voice)
async def voice_handler(message: Message):
    global _bot_user_id
    user_id  = message.from_user.id
    name     = message.from_user.full_name or "друг"
    date_str = datetime.now().strftime("%Y-%m-%d")
    get_or_create_user(user_id, name)
    if not _bot_user_id:
        _bot_user_id = user_id

    voice_file = await bot.get_file(message.voice.file_id)
    buf = io.BytesIO()
    await bot.download_file(voice_file.file_path, destination=buf)
    ogg_bytes = buf.getvalue()

    # Подсказка для Whisper: последние продукты из рациона
    foods       = get_today_foods(user_id, date_str)
    whisper_ctx = _build_whisper_hint(foods)

    try:
        recognized = await groq_service.transcribe(ogg_bytes, context_hint=whisper_ctx)
    except Exception as e:
        logger.error("user={} Whisper error: {}", user_id, e)
        await message.answer("Не удалось распознать речь — попробуй ещё раз или напиши текстом.")
        return

    logger.info("user={} voice: {!r}", user_id, recognized)
    await message.answer(f"🎙️ _{_esc(recognized)}_", parse_mode="MarkdownV2")

    force_edit = user_id in _awaiting_edit
    _awaiting_edit.discard(user_id)

    burned  = get_today_burned(user_id, date_str)
    eaten   = sum(f.get("calories", 0) for f in foods)
    cal, _, _, _ = _targets()
    context = _build_context(cal, eaten, burned, foods)

    try:
        await _process_intent(message, user_id, recognized, context, date_str,
                              force_edit=force_edit)
    except Exception as e:
        logger.error("user={} AI error: {}", user_id, e)
        await message.answer(f"Ошибка ИИ: {e}")


@dp.message(F.photo)
async def photo_handler(message: Message):
    global _bot_user_id
    user_id  = message.from_user.id
    name     = message.from_user.full_name or "друг"
    date_str = datetime.now().strftime("%Y-%m-%d")
    get_or_create_user(user_id, name)
    if not _bot_user_id:
        _bot_user_id = user_id

    caption = message.caption or ""

    # Скачать фото лучшего качества
    photo = message.photo[-1]
    file  = await bot.get_file(photo.file_id)
    buf   = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    image_bytes = buf.getvalue()

    wait_msg = await message.answer("🔍 Анализирую фото\\.\\.\\.", parse_mode="MarkdownV2")

    try:
        result = await gemini_service.analyze_photo(image_bytes, caption)
    except Exception as e:
        logger.error("user={} photo analyze error: {}", user_id, e)
        await wait_msg.delete()
        await message.answer("Не удалось распознать фото. Попробуй написать текстом.")
        return

    await wait_msg.delete()

    # ── Штрихкод ─────────────────────────────────────────────────────────────
    if result.get("type") == "barcode":
        barcode = re.sub(r"\D", "", result.get("barcode", ""))
        if not barcode:
            await message.answer("Штрихкод не распознан. Сфотографируй ближе и чётче.")
            return

        await message.answer(
            f"📦 Штрихкод: `{_esc(barcode)}`  Ищу в базе\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        product = await gemini_service.lookup_barcode(barcode)

        if not product:
            await message.answer(
                f"❌ Продукт `{_esc(barcode)}` не найден в Open Food Facts\\.\n"
                "Напиши название продукта вручную\\.",
                parse_mode="MarkdownV2",
            )
            return

        weight = _extract_weight_from_text(caption) or 100.0
        factor = weight / 100.0
        item = {
            "name":     product["name"],
            "weight":   weight,
            "calories": round(product["calories"] * factor, 1),
            "p":        round(product["protein"]  * factor, 1),
            "f":        round(product["fat"]       * factor, 1),
            "c":        round(product["carbs"]     * factor, 1),
            "from_db":  False,
            "emoji":    _pick_emoji(product["name"]),
        }
        meal_type    = _meal_type_by_time()
        foods_before = get_today_foods(user_id, date_str)
        eaten_before = sum(f.get("calories", 0) for f in foods_before)

        log_food_to_db(user_id, date_str, meal_type, [item])
        all_foods   = get_today_foods(user_id, date_str)
        just_logged = all_foods[-1:]
        if just_logged:
            just_logged[0]["emoji"]   = item["emoji"]
            just_logged[0]["from_db"] = False

        await message.answer(
            _fmt_log_confirm([item], meal_type, eaten_before),
            parse_mode="MarkdownV2",
            reply_markup=_build_logged_kb(just_logged) if just_logged else None,
        )
        _obsidian_write_bg(user_id, date_str)

    # ── Еда на фото ───────────────────────────────────────────────────────────
    elif result.get("type") == "food":
        raw_items = result.get("items", [])
        meal_type = result.get("meal_type") or _meal_type_by_time()

        if not raw_items:
            await message.answer(
                "Не удалось распознать еду на фото\\.\n"
                "Попробуй написать текстом что съел\\.",
                parse_mode="MarkdownV2",
            )
            return

        foods_before = get_today_foods(user_id, date_str)
        eaten_before = sum(f.get("calories", 0) for f in foods_before)

        items = await _resolve_items(raw_items)
        log_food_to_db(user_id, date_str, meal_type, items)

        all_foods   = get_today_foods(user_id, date_str)
        just_logged = all_foods[-len(items):]
        for i, src in enumerate(items):
            if i < len(just_logged):
                just_logged[i]["emoji"]   = src.get("emoji", _DEFAULT_EMOJI)
                just_logged[i]["from_db"] = src.get("from_db", True)

        await message.answer(
            _fmt_log_confirm(items, meal_type, eaten_before),
            parse_mode="MarkdownV2",
            reply_markup=_build_logged_kb(just_logged),
        )
        _obsidian_write_bg(user_id, date_str)

    else:
        await message.answer(
            "Не понял что на фото\\.\nПопробуй чётче или напиши текстом\\.",
            parse_mode="MarkdownV2",
        )


@dp.message(F.text & ~F.text.startswith("/"))
async def text_handler(message: Message):
    global _bot_user_id
    user_id    = message.from_user.id
    name       = message.from_user.full_name or "друг"
    date_str   = datetime.now().strftime("%Y-%m-%d")
    get_or_create_user(user_id, name)
    if not _bot_user_id:
        _bot_user_id = user_id

    text_lower = message.text.lower()

    if any(p in text_lower for p in _STATS_PHRASES):
        await _send_stats(message, user_id, date_str)
        return
    if any(p in text_lower for p in _WEEK_PHRASES):
        await _handle_week(message)
        return
    if any(p in text_lower for p in _WORKOUT_PHRASES):
        await message.answer("💪 Опиши активность:\n«прошёл 8000 шагов», «30 мин бег», «час в зале»")
        return
    if any(p in text_lower for p in _CLEAR_PHRASES):
        clear_today_foods(user_id, date_str)
        clear_today_activity(user_id, date_str)
        await message.answer("🧹 Дневной рацион сброшен.")
        _obsidian_write_bg(user_id, date_str)
        return

    logger.info("user={} text: {!r}", user_id, message.text[:80])

    force_edit = user_id in _awaiting_edit
    _awaiting_edit.discard(user_id)

    foods   = get_today_foods(user_id, date_str)
    burned  = get_today_burned(user_id, date_str)
    eaten   = sum(f.get("calories", 0) for f in foods)
    cal, _, _, _ = _targets()
    context = _build_context(cal, eaten, burned, foods)

    try:
        await _process_intent(message, user_id, message.text, context, date_str,
                              force_edit=force_edit)
    except Exception as e:
        logger.error("user={} AI error: {}", user_id, e)
        await message.answer(f"Ошибка ИИ: {e}")


# ── Неделя — общий helper ─────────────────────────────────────────────────────

async def _handle_week(message: Message) -> None:
    wait = await message.answer("📅 Загружаю данные за неделю\\.\\.\\.", parse_mode="MarkdownV2")
    try:
        week = await obsidian_sync.read_week()
    except Exception as e:
        logger.warning("read_week error: {}", e)
        await wait.delete()
        await message.answer("Не удалось загрузить данные. Попробуй позже.")
        return
    await wait.delete()
    await message.answer(_fmt_week(week), parse_mode="MarkdownV2")


# ── Inline callbacks ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "diary_today")
async def cb_diary_today(callback: CallbackQuery):
    await callback.answer()
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    get_or_create_user(user_id, callback.from_user.full_name or "друг")
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)
    await callback.message.answer(
        _fmt_full_day(foods, burned),
        parse_mode="MarkdownV2",
        reply_markup=_build_delete_keyboard(foods) if foods else None,
    )


@dp.callback_query(F.data.startswith("del_food:"))
async def cb_delete_food(callback: CallbackQuery):
    food_id  = int(callback.data.split(":")[1])
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    get_or_create_user(user_id, callback.from_user.full_name or "друг")
    delete_food_by_id(food_id)
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)
    if foods:
        await callback.message.edit_text(
            _fmt_full_day(foods, burned),
            parse_mode="MarkdownV2",
            reply_markup=_build_delete_keyboard(foods),
        )
    else:
        await callback.message.edit_text(
            _fmt_full_day([], 0.0),
            parse_mode="MarkdownV2",
        )
    _obsidian_write_bg(user_id, date_str)
    await callback.answer("Удалено ✓")


@dp.callback_query(F.data == "clear_day")
async def cb_clear_day(callback: CallbackQuery):
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    soft_delete_today(user_id, date_str)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Восстановить",   callback_data="restore_day"),
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
    get_or_create_user(user_id, callback.from_user.full_name or "друг")
    foods  = get_today_foods(user_id, date_str)
    burned = get_today_burned(user_id, date_str)
    await callback.message.edit_text(
        _fmt_full_day(foods, burned),
        parse_mode="MarkdownV2",
        reply_markup=_build_delete_keyboard(foods) if foods else None,
    )
    await callback.answer("↩️ Восстановлено")


@dp.callback_query(F.data == "confirm_clear")
async def cb_confirm_clear(callback: CallbackQuery):
    user_id  = callback.from_user.id
    date_str = datetime.now().strftime("%Y-%m-%d")
    hard_delete_soft_deleted(user_id, date_str)
    _obsidian_write_bg(user_id, date_str)
    await callback.message.edit_text("🗑️ Данные удалены\\.", parse_mode="MarkdownV2")
    await callback.answer()


@dp.callback_query(F.data == "diary_week")
async def cb_diary_week(callback: CallbackQuery):
    await callback.answer()
    await _handle_week(callback.message)


@dp.callback_query(F.data == "ask_food")
async def cb_ask_food(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("💡 Напиши название продукта — скажу, стоит ли его есть.")


@dp.callback_query(F.data == "hint_edit")
async def cb_hint_edit(callback: CallbackQuery):
    await callback.answer()
    _awaiting_edit.add(callback.from_user.id)
    await callback.message.answer(
        "✏️ *Что изменить?* Просто напиши:\n\n"
        "• «молоко 200г» — изменить вес\n"
        "• «гречка → рис 150г» — заменить продукт\n"
        "• «удали молоко» — удалить запись\n\n"
        "_Одно следующее сообщение будет обработано как редактирование_",
        parse_mode="MarkdownV2",
    )


# ── Фоновые задачи ────────────────────────────────────────────────────────────

async def _goals_refresh_loop() -> None:
    """Обновляет цели из Obsidian каждые 5 минут."""
    while True:
        await asyncio.sleep(300)
        try:
            global _goals
            _goals = await obsidian_sync.load_goals()
            logger.info("goals refreshed: {} ккал", _goals.get("calories"))
        except Exception as e:
            logger.warning("goals refresh error: {}", e)


async def _streak_check_loop() -> None:
    """Обновляет стрик раз в день."""
    global _current_streak, _streak_date
    while True:
        await asyncio.sleep(60)
        today = datetime.now().strftime("%Y-%m-%d")
        if today != _streak_date:
            try:
                _current_streak = await obsidian_sync.get_streak(today)
                _streak_date    = today
                logger.info("streak updated: {} дней", _current_streak)
            except Exception as e:
                logger.warning("streak check error: {}", e)


async def _reminder_loop() -> None:
    """Утреннее и вечернее напоминание по расписанию из Obsidian (UTC)."""
    _last_morning = ""
    _last_evening = ""
    while True:
        await asyncio.sleep(60)
        if not _bot_user_id:
            continue
        try:
            now_utc = datetime.utcnow()
            hm      = now_utc.strftime("%H:%M")
            today   = now_utc.strftime("%Y-%m-%d")

            morning_t = str(_goals.get("reminder_morning", "05:00"))
            evening_t = str(_goals.get("reminder_evening", "18:00"))

            if hm == morning_t and _last_morning != today:
                _last_morning = today
                asyncio.create_task(_send_morning_reminder())

            if hm == evening_t and _last_evening != today:
                _last_evening = today
                asyncio.create_task(_send_evening_summary(today))
        except Exception as e:
            logger.warning("reminder loop error: {}", e)


async def _send_morning_reminder() -> None:
    cal, p, f, c = _targets()
    try:
        await bot.send_message(
            _bot_user_id,
            f"🌅 *Доброе утро\\!* ☀️\n\n"
            f"📊 Цель сегодня: *{_esc(str(cal))} ккал*\n"
            f"🥩 Белки: *{_esc(str(p))} г*  "
            f"🥑 Жиры: *{_esc(str(f))} г*  "
            f"🍞 Углеводы: *{_esc(str(c))} г*\n"
            f"🔥 Стрик: *{_esc(str(_current_streak))} дней*\n\n"
            f"💪 Удачного дня\\! Не забывай записывать еду 🎙️",
            parse_mode="MarkdownV2",
        )
        logger.info("morning reminder sent")
    except Exception as e:
        logger.warning("morning reminder error: {}", e)


async def _send_evening_summary(date_str: str) -> None:
    if not _bot_user_id:
        return
    cal, _, _, _ = _targets()
    try:
        foods  = get_today_foods(_bot_user_id, date_str)
        burned = get_today_burned(_bot_user_id, date_str)
        total  = sum(f.get("calories", 0) for f in foods)
        net    = total - burned
        rem    = cal - net
        mood   = "🎯" if rem >= 0 else "⚠️"
        rem_text = (
            f"Осталось: *{_esc(f'{abs(rem):.0f}')}* ккал"
            if rem >= 0
            else f"Превышение: *{_esc(f'{abs(rem):.0f}')}* ккал"
        )
        await bot.send_message(
            _bot_user_id,
            f"🌙 *Итог дня* {mood}\n\n"
            f"🔥 Калории: *{_esc(f'{total:.0f}')}* / {_esc(str(cal))} ккал\n"
            f"`{_bar(total, cal, 14)}`\n"
            f"{rem_text}\n\n"
            f"Запиши ужин или скажи что было вечером 🎙️",
            parse_mode="MarkdownV2",
        )
        logger.info("evening summary sent: {:.0f} ккал", total)
    except Exception as e:
        logger.warning("evening summary error: {}", e)


# ── Веб-сервер для health-check (Render) ─────────────────────────────────────

PORT = int(os.environ.get("PORT", 10000))


async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ── Точка входа ───────────────────────────────────────────────────────────────

async def main():
    global _goals, _current_streak, _streak_date, _bot_user_id

    init_db()
    logger.info("SQLite initialised")

    # 1. Загрузить базу продуктов из GitHub
    logger.info("Preloading food DB...")
    await asyncio.to_thread(food_db.preload)

    # 2. Загрузить цели из Obsidian
    logger.info("Loading goals from Obsidian...")
    try:
        _goals = await obsidian_sync.load_goals()
        logger.info("Goals: {} ккал", _goals.get("calories"))
    except Exception as e:
        logger.warning("Could not load goals, using defaults: {}", e)

    # 3. Убедиться что прогресс.md существует
    try:
        await obsidian_sync.ensure_progress()
    except Exception as e:
        logger.warning("ensure_progress error: {}", e)

    # 4. Вычислить стрик
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        _current_streak = await obsidian_sync.get_streak(today)
        _streak_date    = today
        logger.info("Streak: {} дней", _current_streak)
    except Exception as e:
        logger.warning("streak init error: {}", e)

    # 5. Восстановить SQLite из Obsidian (после редеплоя)
    if _bot_user_id:
        try:
            restore_data = await obsidian_sync.read_daily_log(today)
            if restore_data:
                foods_obs, burned_obs = restore_data
                n = restore_from_obsidian(_bot_user_id, today, foods_obs)
                if n:
                    logger.info("Restored {} entries from Obsidian for {}", n, today)
        except Exception as e:
            logger.warning("Obsidian restore error: {}", e)

    # 6. Запустить фоновые задачи
    asyncio.create_task(food_db.refresh_loop())
    asyncio.create_task(_goals_refresh_loop())
    asyncio.create_task(_streak_check_loop())
    asyncio.create_task(_reminder_loop())

    # 7. Веб-сервер (health check для Render)
    app = web.Application()
    app.add_routes([web.get("/", _ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("Web server on port {}", PORT)

    # 8. Запустить polling
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
