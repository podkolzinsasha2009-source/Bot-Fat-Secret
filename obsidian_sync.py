"""
Obsidian/GitHub интеграция дневника питания.
────────────────────────────────────────────
• load_goals()        — читает wiki/areas/питание/цели.md (создаёт если нет)
• write_daily_log()   — пишет YYYY-MM-DD.md при каждом логе (в фоне)
• read_daily_log()    — читает дневник для восстановления SQLite после редеплоя
• read_week()         — параллельно читает 7 дней, возвращает list[dict]
• get_streak()        — стрик = вчерашний стрик + 1
• ensure_progress()   — создаёт прогресс.md с Dataview если нет
"""
import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests
from loguru import logger

OBSIDIAN_REPO   = os.environ.get("OBSIDIAN_REPO",   "podkolzinsasha2009-source/baza-znanii")
OBSIDIAN_BRANCH = os.environ.get("OBSIDIAN_BRANCH", "main")
GOALS_FILE      = "wiki/areas/питание/цели.md"
DIARY_DIR       = "wiki/areas/питание/дневники"
PROGRESS_FILE   = "wiki/areas/питание/прогресс.md"

_MEAL_ICONS = {"Завтрак": "🌅", "Обед": "☀️", "Полдник": "🌤", "Ужин": "🌙", "Перекус": "🍎"}
_MEAL_ORDER = ["Завтрак", "Обед", "Полдник", "Ужин", "Перекус"]
_DAY_RU     = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTH_RU   = ["января","февраля","марта","апреля","мая","июня",
               "июля","августа","сентября","октября","ноября","декабря"]

DEFAULT_GOALS = {
    "calories": 2000, "protein": 140, "fat": 80, "carbs": 220,
    "reminder_morning": "08:00", "reminder_evening": "21:00",
}

# asyncio.Lock создаётся при первом использовании (event-loop-safe)
_write_lock: asyncio.Lock | None = None

def _get_lock() -> asyncio.Lock:
    global _write_lock
    if _write_lock is None:
        _write_lock = asyncio.Lock()
    return _write_lock


# ── GitHub HTTP helpers ───────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_get(path: str) -> tuple[str, str] | None:
    """Получить (content, sha) или None если файл не существует."""
    url = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents/{quote(path, safe='/')}"
    r = requests.get(url, headers=_gh_headers(),
                     params={"ref": OBSIDIAN_BRANCH}, timeout=20)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def _gh_put(path: str, content: str, sha: str | None, message: str) -> None:
    """Создать или обновить файл."""
    url = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents/{quote(path, safe='/')}"
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": OBSIDIAN_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    requests.put(url, headers=_gh_headers(), json=payload, timeout=20).raise_for_status()


# ── YAML frontmatter ──────────────────────────────────────────────────────────

def _parse_fm(content: str) -> dict:
    result: dict = {}
    if not content.startswith("---"):
        return result
    end = content.find("\n---", 3)
    if end == -1:
        return result
    for line in content[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        val = raw.strip().strip('"').strip("'")
        try:
            result[key] = float(val) if "." in val else int(val)
        except ValueError:
            result[key] = val
    return result


# ── Goals ─────────────────────────────────────────────────────────────────────

def _load_goals_sync() -> dict:
    result = _gh_get(GOALS_FILE)
    if result is None:
        _create_goals_sync(DEFAULT_GOALS)
        return DEFAULT_GOALS.copy()
    fm = _parse_fm(result[0])
    goals = DEFAULT_GOALS.copy()
    for k in DEFAULT_GOALS:
        if k in fm:
            goals[k] = fm[k]
    logger.info("obsidian_sync: цели {} ккал Б:{} Ж:{} У:{}",
                goals["calories"], goals["protein"], goals["fat"], goals["carbs"])
    return goals


def _create_goals_sync(g: dict) -> None:
    content = (
        f"---\ncalories: {g['calories']}\nprotein: {g['protein']}\n"
        f"fat: {g['fat']}\ncarbs: {g['carbs']}\n"
        f'reminder_morning: "{g["reminder_morning"]}"\n'
        f'reminder_evening: "{g["reminder_evening"]}"\n---\n\n'
        f"# 🎯 Цели питания\n\n"
        f"> Файл создан ботом FatSecret. Меняй значения в frontmatter выше.\n"
        f"> **Важно:** время напоминаний указывай в UTC (Москва = UTC+3, вычти 3ч).\n\n"
        f"| Параметр | Цель |\n|---|---|\n"
        f"| 🔥 Калории | {g['calories']} ккал |\n"
        f"| 🥩 Белки | {g['protein']} г |\n"
        f"| 🥑 Жиры | {g['fat']} г |\n"
        f"| 🍞 Углеводы | {g['carbs']} г |\n"
        f"| ⏰ Утреннее напоминание (UTC) | {g['reminder_morning']} |\n"
        f"| 🌙 Вечернее напоминание (UTC) | {g['reminder_evening']} |\n"
    )
    _gh_put(GOALS_FILE, content, None, "🤖 Создан файл целей питания")
    logger.info("obsidian_sync: создан {}", GOALS_FILE)


async def load_goals() -> dict:
    """Загрузить цели из Obsidian. Создать файл если нет."""
    return await asyncio.to_thread(_load_goals_sync)


# ── Progress bars (для .md) ───────────────────────────────────────────────────

def _bar(cur: float, tot: float, n: int = 12) -> str:
    f = int(n * min(cur, tot) / tot) if tot > 0 else 0
    return "█" * f + "░" * (n - f)


def _pct(cur: float, tot: float) -> int:
    return min(int(cur / tot * 100), 100) if tot > 0 else 0


# ── Daily diary write ─────────────────────────────────────────────────────────

def _write_daily_log_sync(
    date_str: str, foods: list[dict], burned: float,
    goals: dict, streak: int,
) -> None:
    total_cal = sum(f.get("calories", 0) for f in foods)
    total_p   = sum(f.get("p", 0) for f in foods)
    total_f   = sum(f.get("f", 0) for f in foods)
    total_c   = sum(f.get("c", 0) for f in foods)

    dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_label = f"{_DAY_RU[dt.weekday()]}, {dt.day} {_MONTH_RU[dt.month - 1]} {dt.year}"

    cal  = int(goals.get("calories", 2000))
    prot = int(goals.get("protein",  140))
    fat  = int(goals.get("fat",      80))
    carb = int(goals.get("carbs",    220))

    # JSON для восстановления SQLite после редеплоя
    bot_json = json.dumps(
        {"foods": [{"meal_type": f.get("meal_type", "Перекус"), "name": f.get("name", ""),
                    "weight": f.get("weight", 0), "calories": f.get("calories", 0),
                    "p": f.get("p", 0), "f": f.get("f", 0), "c": f.get("c", 0)}
                   for f in foods],
         "burned": burned},
        ensure_ascii=False,
    )

    # Группировка по приёмам
    by_meal: dict[str, list[dict]] = {}
    for food in foods:
        by_meal.setdefault(food.get("meal_type") or "Перекус", []).append(food)
    ordered = [m for m in _MEAL_ORDER if m in by_meal] + \
              [m for m in by_meal if m not in _MEAL_ORDER]

    meals_md = ""
    for meal in ordered:
        items    = by_meal[meal]
        meal_cal = sum(i.get("calories", 0) for i in items)
        icon     = _MEAL_ICONS.get(meal, "🍽")
        meals_md += f"\n## {icon} {meal} — {meal_cal:.0f} ккал\n\n"
        meals_md += "| Продукт | Вес | Ккал | Б | Ж | У |\n|---|---|---|---|---|---|\n"
        for item in items:
            meals_md += (
                f"| {item.get('name','')} | {int(item.get('weight',0))}г "
                f"| {item.get('calories',0):.0f} | {item.get('p',0):.1f} "
                f"| {item.get('f',0):.1f} | {item.get('c',0):.1f} |\n"
            )

    streak_word = "день" if streak == 1 else ("дня" if 2 <= streak <= 4 else "дней")

    content = (
        f"---\ndate: {date_str}\n"
        f"calories_total: {total_cal:.1f}\nprotein_total: {total_p:.1f}\n"
        f"fat_total: {total_f:.1f}\ncarbs_total: {total_c:.1f}\n"
        f"burned: {burned:.1f}\ngoal_calories: {cal}\ngoal_protein: {prot}\n"
        f"goal_fat: {fat}\ngoal_carbs: {carb}\nstreak: {streak}\n---\n\n"
        f"<!-- BOT_JSON: {bot_json} -->\n\n"
        f"# 📅 {date_label}\n\n"
        f"## 📊 Итог дня\n\n"
        f"🔥 **Калории:** {total_cal:.0f} / {cal} ккал "
        f"`{_bar(total_cal, cal)}` {_pct(total_cal, cal)}%  \n"
        f"🥩 **Белки:** {total_p:.1f} / {prot} г "
        f"`{_bar(total_p, prot)}` {_pct(total_p, prot)}%  \n"
        f"🥑 **Жиры:** {total_f:.1f} / {fat} г "
        f"`{_bar(total_f, fat)}` {_pct(total_f, fat)}%  \n"
        f"🍞 **Углеводы:** {total_c:.1f} / {carb} г "
        f"`{_bar(total_c, carb)}` {_pct(total_c, carb)}%  \n"
        f"💪 **Сожжено:** {burned:.0f} ккал  \n"
        f"🔥 **Стрик:** {streak} {streak_word}  \n\n---\n"
        f"{meals_md}"
    )

    file_path = f"{DIARY_DIR}/{date_str}.md"
    result    = _gh_get(file_path)
    sha       = result[1] if result else None
    _gh_put(file_path, content, sha, f"🤖 Дневник {date_str} — {total_cal:.0f} ккал")
    logger.info("obsidian_sync: дневник {} обновлён ({:.0f} ккал)", date_str, total_cal)


async def write_daily_log(
    date_str: str, foods: list[dict], burned: float,
    goals: dict, streak: int,
) -> None:
    """Записать дневной лог в Obsidian (с блокировкой от параллельных записей)."""
    async with _get_lock():
        try:
            await asyncio.to_thread(
                _write_daily_log_sync, date_str, foods, burned, goals, streak
            )
        except Exception as e:
            logger.warning("obsidian_sync: ошибка записи дневника: {}", e)


# ── Restore from Obsidian ─────────────────────────────────────────────────────

def _read_daily_log_sync(date_str: str) -> tuple[list[dict], float] | None:
    result = _gh_get(f"{DIARY_DIR}/{date_str}.md")
    if not result:
        return None
    m = re.search(r"<!-- BOT_JSON: (.+?) -->", result[0])
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data.get("foods", []), float(data.get("burned", 0))
    except Exception as e:
        logger.warning("obsidian_sync: ошибка BOT_JSON: {}", e)
        return None


async def read_daily_log(date_str: str) -> tuple[list[dict], float] | None:
    """Прочитать foods + burned из дневника Obsidian."""
    return await asyncio.to_thread(_read_daily_log_sync, date_str)


# ── Week stats (parallel) ─────────────────────────────────────────────────────

def _read_fm_only(date_str: str) -> dict:
    """Быстро читает только frontmatter одного дня."""
    result = _gh_get(f"{DIARY_DIR}/{date_str}.md")
    if not result:
        return {"date": date_str, "empty": True}
    fm = _parse_fm(result[0])
    fm["date"] = date_str
    fm["empty"] = False
    return fm


async def read_week() -> list[dict]:
    """Параллельно читает frontmatter 7 дней из Obsidian."""
    today = datetime.now()
    dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    results = await asyncio.gather(*[asyncio.to_thread(_read_fm_only, d) for d in dates])
    return list(results)


# ── Streak ────────────────────────────────────────────────────────────────────

def _get_streak_sync(date_str: str) -> int:
    """Стрик = вчерашний стрик + 1. Если вчера нет записи — начало нового стрика (1)."""
    yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    result = _gh_get(f"{DIARY_DIR}/{yesterday}.md")
    if not result:
        return 1
    return int(_parse_fm(result[0]).get("streak", 0)) + 1


async def get_streak(date_str: str) -> int:
    return await asyncio.to_thread(_get_streak_sync, date_str)


# ── Progress.md (Dataview) ────────────────────────────────────────────────────

def _ensure_progress_sync() -> None:
    if _gh_get(PROGRESS_FILE):
        return
    content = (
        "---\ntags: [питание, прогресс]\n---\n\n"
        "# 📊 Прогресс питания\n\n"
        "> Автоматически обновляется ботом FatSecret.\n"
        "> Установи плагин **Dataview** в Obsidian для работы таблицы.\n\n"
        "```dataview\n"
        "TABLE WITHOUT ID\n"
        '  link(file.name, dateformat(date(file.name), "dd MMM")) as "📅 Дата",\n'
        '  calories_total + " / " + goal_calories + " ккал" as "🔥 Ккал",\n'
        '  protein_total + "г" as "🥩 Б",\n'
        '  fat_total + "г" as "🥑 Ж",\n'
        '  carbs_total + "г" as "🍞 У",\n'
        '  burned + " ккал" as "💪 Сожжено",\n'
        '  streak as "🔥 Стрик"\n'
        'FROM "wiki/areas/питание/дневники"\n'
        "SORT file.name DESC\nLIMIT 30\n"
        "```\n"
    )
    _gh_put(PROGRESS_FILE, content, None, "🤖 Создан файл прогресса питания")
    logger.info("obsidian_sync: создан прогресс.md")


async def ensure_progress() -> None:
    await asyncio.to_thread(_ensure_progress_sync)
