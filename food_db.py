"""
Поиск КБЖУ по базе продуктов из приватного GitHub репо Obsidian.
- При старте: загружает файл через GitHub API
- Каждые 5 минут: обновляет кэш (ETag — только если файл изменился)
- search(): умный каскадный поиск + кэш 14 часов
- append_product(): async, не блокирует event loop
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

OBSIDIAN_REPO    = os.environ.get("OBSIDIAN_REPO",   "podkolzinsasha2009-source/baza-znanii")
OBSIDIAN_BRANCH  = os.environ.get("OBSIDIAN_BRANCH", "main")
DB_FILE          = os.environ.get("DB_FILE",         "wiki/areas/спорт/база-продуктов.md")
FUZZY_THRESHOLD  = 70
CACHE_TTL        = 300          # секунд (5 минут)
SEARCH_CACHE_TTL = 14 * 3600   # 14 часов

# name.lower() → (display_name, K, B, J, U)
_db: dict[str, tuple] = {}
_last_load: float = 0.0
_etag: str = ""

# Кэш поиска: normalized_query → (db_key | None, timestamp)
_search_cache: dict[str, tuple[str | None, float]] = {}

_LINE_RE = re.compile(
    r"^-\s+(.+?)\s+—\s+К:\s*([\d.]+),\s*Б:\s*([\d.]+),\s*Ж:\s*([\d.]+),\s*У:\s*([\d.]+)",
    re.IGNORECASE,
)

# Числа в запросе: «3.2», «2,5», «5%», «100»
_NUM_RE = re.compile(r'\b(\d+[.,]\d+|\d+)\s*%?')

# Текстовые уточнители
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


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _extract_numbers(text: str) -> list[str]:
    return [m.replace(",", ".") for m in _NUM_RE.findall(text)]


def _extract_qualifiers(text: str) -> list[str]:
    tl = text.lower()
    return [q for q in _TEXT_QUALIFIERS if q in tl]


def _base_word(text: str) -> str:
    for w in text.split():
        if len(w) >= 3 and not re.match(r"^\d", w):
            return w
    return text.split()[0] if text else text


def _fetch_and_parse() -> dict | None:
    """Скачать базу из GitHub. Возвращает None если не изменилась (ETag 304)."""
    global _etag
    path_enc = quote(DB_FILE, safe="/")
    url = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents/{path_enc}"
    headers = _gh_headers()
    if _etag:
        headers["If-None-Match"] = _etag
    r = requests.get(url, headers=headers, params={"ref": OBSIDIAN_BRANCH}, timeout=20)
    if r.status_code == 304:
        logger.debug("food_db: ETag 304 — файл не изменился")
        return None
    r.raise_for_status()
    _etag = r.headers.get("ETag", "")
    content = base64.b64decode(r.json()["content"]).decode("utf-8")
    db: dict[str, tuple] = {}
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
    global _db, _last_load
    try:
        db = _fetch_and_parse()
        if db is not None:
            _db = db
            _last_load = time.time()
            logger.info("food_db: загружено {} продуктов из GitHub", len(_db))
    except Exception as e:
        logger.error("food_db: ошибка загрузки: {}", e)


async def refresh_loop() -> None:
    while True:
        await asyncio.sleep(CACHE_TTL)
        try:
            new_db = await asyncio.to_thread(_fetch_and_parse)
            if new_db is not None:
                global _db, _last_load, _search_cache
                _db = new_db
                _last_load = time.time()
                _search_cache.clear()  # сбросить кэш поиска при обновлении базы
                logger.info("food_db: кэш обновлён, {} продуктов", len(_db))
        except Exception as e:
            logger.warning("food_db: ошибка обновления кэша: {}", e)


def search(query: str, weight: float) -> dict | None:
    """
    Каскадный поиск с кэшем 14ч:
      1. Точное совпадение
      2. Ключ ⊂ запрос → самый длинный ключ (специфичный)
      3. Базовое слово + уточнители (числа + текст) → max score
      4. Запрос ⊂ ключ → самый короткий ключ
      5. Базовое слово → самый короткий ключ
      6. Fuzzy token_set_ratio ≥ 70
    """
    if not _db:
        return None

    q = query.lower().strip()

    # ── Кэш поиска ────────────────────────────────────────────────────────────
    if q in _search_cache:
        cached_key, cached_ts = _search_cache[q]
        if time.time() - cached_ts < SEARCH_CACHE_TTL:
            if cached_key is None:
                return None
            if cached_key in _db:
                display, k, b, j, u = _db[cached_key]
                f = weight / 100.0
                return {"name": display, "weight": weight,
                        "calories": round(k * f, 1), "p": round(b * f, 1),
                        "f": round(j * f, 1), "c": round(u * f, 1), "from_db": True}

    names = list(_db.keys())
    nums       = _extract_numbers(q)
    qualifiers = _extract_qualifiers(q)
    base       = _base_word(q)

    def _score(key: str) -> int:
        s = 0
        for n in nums:
            if n in key or n.replace(".", ",") in key:
                s += 2
        for qt in qualifiers:
            if qt in key:
                s += 1
        return s

    def _make(key: str) -> dict:
        display, k, b, j, u = _db[key]
        f = weight / 100.0
        _search_cache[q] = (key, time.time())
        return {"name": display, "weight": weight,
                "calories": round(k * f, 1), "p": round(b * f, 1),
                "f": round(j * f, 1), "c": round(u * f, 1), "from_db": True}

    # 1. Точное совпадение
    if q in _db:
        return _make(q)

    # 2. Ключ ⊂ запрос → длиннейший
    key_in_q = [k for k in names if len(k) >= 3 and k in q]
    if key_in_q:
        return _make(max(key_in_q, key=len))

    # 3. Базовое слово + уточнители
    if base and (nums or qualifiers):
        qualified = [k for k in names if base in k and _score(k) > 0]
        if qualified:
            return _make(max(qualified, key=lambda k: (_score(k), -len(k))))

    # 4. Запрос ⊂ ключ → кратчайший
    q_in_key = [k for k in names if len(q) >= 3 and q in k]
    if q_in_key:
        return _make(min(q_in_key, key=len))

    # 5. Базовое слово в ключах → кратчайший
    if base and base != q:
        base_m = [k for k in names if len(base) >= 3 and base in k]
        if base_m:
            return _make(min(base_m, key=len))

    # 6. Fuzzy
    result = process.extractOne(q, names, scorer=fuzz.token_set_ratio)
    if result and result[1] >= FUZZY_THRESHOLD:
        return _make(result[0])

    _search_cache[q] = (None, time.time())
    return None


def _append_product_sync(name: str, k: float, b: float, j: float, u: float) -> None:
    """Добавить продукт в базу через GitHub API commit (синхронная часть)."""
    try:
        path_enc = quote(DB_FILE, safe="/")
        url = f"https://api.github.com/repos/{OBSIDIAN_REPO}/contents/{path_enc}"
        r = requests.get(url, headers=_gh_headers(),
                         params={"ref": OBSIDIAN_BRANCH}, timeout=15)
        r.raise_for_status()
        data = r.json()
        sha     = data["sha"]
        current = base64.b64decode(data["content"]).decode("utf-8")
        new_content = current + f"- {name} — К: {k}, Б: {b}, Ж: {j}, У: {u}\n"
        payload = {
            "message": f"🤖 Бот добавил продукт: {name}",
            "content": base64.b64encode(new_content.encode("utf-8")).decode(),
            "sha": sha, "branch": OBSIDIAN_BRANCH,
        }
        requests.put(url, headers=_gh_headers(), json=payload, timeout=20).raise_for_status()
        _db[name.lower()] = (name, k, b, j, u)
        _search_cache.pop(name.lower(), None)
        logger.info("food_db: добавлен в GitHub: {}", name)
    except Exception as e:
        logger.warning("food_db: не удалось добавить '{}': {}", name, e)


async def append_product(name: str, k: float, b: float, j: float, u: float) -> None:
    """Async: добавить продукт (не блокирует event loop)."""
    await asyncio.to_thread(_append_product_sync, name, k, b, j, u)
