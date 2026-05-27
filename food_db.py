"""
Поиск КБЖУ по базе продуктов из приватного GitHub репо Obsidian.
- При старте: загружает файл через GitHub API
- Каждые 5 минут: обновляет кэш в фоне (подхватывает изменения из Obsidian)
- search(): работает из памяти, не блокирует event loop
"""
import asyncio
import base64
import os
import re
import time
from urllib.parse import quote

import requests
from thefuzz import process, fuzz
from loguru import logger

OBSIDIAN_REPO   = os.environ.get("OBSIDIAN_REPO",   "podkolzinsasha2009-source/baza-znanii")
OBSIDIAN_BRANCH = os.environ.get("OBSIDIAN_BRANCH", "main")
DB_FILE         = os.environ.get("DB_FILE",         "wiki/areas/спорт/база-продуктов.md")
FUZZY_THRESHOLD = 70
CACHE_TTL       = 300  # секунд (5 минут)

# name.lower() → (display_name, K, B, J, U)
_db: dict[str, tuple] = {}
_last_load: float = 0.0

_LINE_RE = re.compile(
    r"^-\s+(.+?)\s+—\s+К:\s*([\d.]+),\s*Б:\s*([\d.]+),\s*Ж:\s*([\d.]+),\s*У:\s*([\d.]+)",
    re.IGNORECASE,
)

# Любые числа в запросе: «3.2», «2,5», «5%», «100», «0»
_NUM_RE = re.compile(r'\b(\d+[.,]\d+|\d+)\s*%?')

# Текстовые уточнители — слова/фразы, которые меняют продукт принципиально
_TEXT_QUALIFIERS = [
    "без сахара", "без соли", "обезжиренный", "обезжиренное", "обезжиренная",
    "цельнозерновой", "цельнозерновое", "цельнозерновая",
    "варёный", "варёная", "варёное", "жареный", "жареная", "жареное",
    "солёный", "солёная", "солёное", "копчёный", "копчёная", "копчёное",
    "сырой", "сырое", "сырая", "свежий", "свежая", "свежее",
    "тёмный", "тёмное", "тёмная", "белый", "белое", "белая",
    "бурый", "бурое", "бурая", "красный", "красное", "красная",
    "диетический", "диетическое", "диетическая",
    "нежирный", "нежирная", "нежирное",
    "цельное", "пастеризованное", "ультрапастеризованное",
    "натуральный", "натуральная", "натуральное",
    "light", "zero", "diet",
]


def _extract_numbers(text: str) -> list[str]:
    """Все числа из текста: «молоко 3.2 200мл» → ['3.2', '200']"""
    return [m.replace(",", ".") for m in _NUM_RE.findall(text)]


def _extract_qualifiers(text: str) -> list[str]:
    """Текстовые уточнители присутствующие в запросе."""
    tl = text.lower()
    return [q for q in _TEXT_QUALIFIERS if q in tl]


def _base_word(text: str) -> str:
    """Первое слово длиной ≥ 3 символов, не число."""
    for w in text.split():
        if len(w) >= 3 and not re.match(r"^\d", w):
            return w
    return text.split()[0] if text else text


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _fetch_and_parse() -> dict:
    """Скачать базу из GitHub и распарсить в dict."""
    path_enc = quote(DB_FILE, safe="/")
    url = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents/{path_enc}"
    r = requests.get(
        url, headers=_gh_headers(),
        params={"ref": OBSIDIAN_BRANCH}, timeout=20,
    )
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"]).decode("utf-8")

    db = {}
    for line in content.splitlines():
        m = _LINE_RE.match(line.strip())
        if m:
            name = m.group(1).strip()
            db[name.lower()] = (
                name,
                float(m.group(2)), float(m.group(3)),
                float(m.group(4)), float(m.group(5)),
            )
    return db


def preload() -> None:
    """Загрузить базу из GitHub. Вызывается при старте (в потоке)."""
    global _db, _last_load
    try:
        _db = _fetch_and_parse()
        _last_load = time.time()
        logger.info("food_db: загружено {} продуктов из GitHub", len(_db))
    except Exception as e:
        logger.error("food_db: ошибка загрузки: {}", e)


async def refresh_loop() -> None:
    """Фоновый таск: обновляет кэш каждые CACHE_TTL секунд."""
    while True:
        await asyncio.sleep(CACHE_TTL)
        try:
            new_db = await asyncio.to_thread(_fetch_and_parse)
            global _db, _last_load
            _db = new_db
            _last_load = time.time()
            logger.info("food_db: кэш обновлён, {} продуктов", len(_db))
        except Exception as e:
            logger.warning("food_db: ошибка обновления кэша: {}", e)


def search(query: str, weight: float) -> dict | None:
    """
    Найти продукт и вернуть КБЖУ для указанного веса.

    Приоритеты поиска:
      1. Точное совпадение ключа — «сахар» → «Сахар»
      2. Ключ ⊂ запрос → берём ДЛИННЕЙШИЙ ключ (наиболее специфичный)
         «молоко лебедянское 3.2» содержит «молоко лебедянское» и «молоко»
         → вернём «молоко лебедянское»
      3. Запрос ⊂ ключ → берём КРАТЧАЙШИЙ ключ (простейший продукт)
         «сахар» есть и в «epica без сахара», и в «сахар-рафинад», и в «сахар»
         → вернём «сахар» (самый короткий)
      4. Fuzzy token_set_ratio ≥ 70
    """
    if not _db:
        return None

    q = query.lower().strip()
    names = list(_db.keys())

    def _result(key: str) -> dict:
        display, k, b, j, u = _db[key]
        f = weight / 100.0
        return {
            "name": display, "weight": weight,
            "calories": round(k * f, 1), "p": round(b * f, 1),
            "f": round(j * f, 1), "c": round(u * f, 1),
            "from_db": True,
        }

    nums       = _extract_numbers(q)      # все числа: ['3.2'] из «молоко лебедянское 3.2»
    qualifiers = _extract_qualifiers(q)   # текстовые: ['без сахара'] из «кола без сахара»
    base       = _base_word(q)            # первое слово-продукт: «молоко», «кола», «сок»

    # ── вспомогательная функция: оценка релевантности ключа ──────────────────
    def _score(key: str) -> int:
        """Сколько уточнителей из запроса совпало в ключе (чем больше — тем лучше)."""
        score = 0
        for n in nums:
            if n in key or n.replace(".", ",") in key:
                score += 2          # число/процент важнее текстового уточнителя
        for qt in qualifiers:
            if qt in key:
                score += 1
        return score

    # 1. Точное совпадение
    if q in _db:
        return _result(q)

    # 2. Ключ ⊂ запрос → самый ДЛИННЫЙ ключ
    #    «молоко лебедянское» ⊂ «молоко лебедянское 3.2» → бренд найден точно
    key_in_query = [k for k in names if len(k) >= 3 and k in q]
    if key_in_query:
        best = max(key_in_query, key=len)
        return _result(best)

    # 3. Базовое слово + уточнители (числа ИЛИ текст)
    #    Примеры:
    #      «молоко лебедянское 3.2» → ищем ключи с «молоко» + «3.2» → «молоко 3.2%»
    #      «кола без сахара»        → ищем ключи с «кола» + «без сахара» → «кола без сахара»
    #      «сок 100%»               → ищем ключи с «сок» + «100» → «сок яблочный 100%»
    #      «хлеб цельнозерновой»    → ищем ключи с «хлеб» + «цельнозерновой»
    if base and (nums or qualifiers):
        qualified = [k for k in names if base in k and _score(k) > 0]
        if qualified:
            # Берём с максимальным score, при ничьей — кратчайший
            best = max(qualified, key=lambda k: (_score(k), -len(k)))
            return _result(best)

    # 4. Запрос ⊂ ключ → самый КОРОТКИЙ ключ
    #    «сахар» ⊂ «epica без сахара» и ⊂ «сахар» → берём «сахар»
    query_in_key = [k for k in names if len(q) >= 3 and q in k]
    if query_in_key:
        best = min(query_in_key, key=len)
        return _result(best)

    # 5. Только базовое слово → кратчайший ключ с ним
    #    Последний резерв до fuzzy: хоть что-то с нужным продуктом
    if base and base != q:
        base_matches = [k for k in names if len(base) >= 3 and base in k]
        if base_matches:
            best = min(base_matches, key=len)
            return _result(best)

    # 6. Fuzzy-поиск
    result = process.extractOne(q, names, scorer=fuzz.token_set_ratio)
    if result and result[1] >= FUZZY_THRESHOLD:
        return _result(result[0])

    return None


def append_product(name: str, k: float, b: float, j: float, u: float) -> None:
    """
    Добавить новый продукт в базу через GitHub API commit.
    Вызывается в фоне — не блокирует бота при ошибке.
    """
    try:
        path_enc = quote(DB_FILE, safe="/")
        url = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents/{path_enc}"

        # Получить текущий файл и его SHA
        r = requests.get(url, headers=_gh_headers(),
                         params={"ref": OBSIDIAN_BRANCH}, timeout=15)
        r.raise_for_status()
        data = r.json()
        sha = data["sha"]
        current = base64.b64decode(data["content"]).decode("utf-8")

        # Добавить строку и закоммитить
        new_line = f"- {name} — К: {k}, Б: {b}, Ж: {j}, У: {u}\n"
        new_content = current + new_line
        payload = {
            "message": f"🤖 Бот добавил продукт: {name}",
            "content": base64.b64encode(new_content.encode("utf-8")).decode(),
            "sha": sha,
            "branch": OBSIDIAN_BRANCH,
        }
        r2 = requests.put(url, headers=_gh_headers(), json=payload, timeout=20)
        r2.raise_for_status()

        # Обновить локальный кэш
        _db[name.lower()] = (name, k, b, j, u)
        logger.info("food_db: добавлен в GitHub: {}", name)
    except Exception as e:
        logger.warning("food_db: не удалось добавить '{}' в GitHub: {}", name, e)
