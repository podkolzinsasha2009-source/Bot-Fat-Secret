import json
import os
import re

import aiohttp
from loguru import logger

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash"

SYSTEM_PROMPT = """Ты — встроенный КБЖУ-калькулятор. Используй свои актуальные знания и встроенные возможности поиска для точного расчёта КБЖУ под указанный вес (weight / 100). Ответ дай строго в формате JSON.

⚠️ КРИТИЧЕСКОЕ ПРАВИЛО ПЕРЕСЧЁТА ПОРЦИЙ:
Все базы данных (Calorizator, FatSecret, USDA) указывают КБЖУ СТРОГО НА 100 ГРАММ продукта.
Ты ОБЯЗАН взять эти 100-граммовые данные из сети и МАТЕМАТИЧЕСКИ ПЕРЕСЧИТАТЬ под реальный вес.
Формула: Значение_на_100г * (weight / 100)
Примеры для самопроверки:
  Масло сливочное 10г:    748 * (10/100) =  74.8 ккал  ← НЕ 748!
  Масло подсолнечное 15г: 899 * (15/100) = 134.9 ккал  ← НЕ 899!
  Топинг карамельный 61г: 293 * (61/100) = 178.7 ккал
  Грудка куриная 150г:    165 * (150/100) = 247.5 ккал
Значения КБЖУ в итоговом JSON физически не могут быть больше, чем вес самой порции в граммах.

⚠️ ЖЁСТКОЕ РАЗДЕЛЕНИЕ: log_food vs edit_food
  log_food  → пользователь называет продукт и вес ("молоко 250г", "гречка 200г").
              ВСЕГДА новая отдельная запись, даже если этот продукт уже был сегодня —
              повторная порция НЕ заменяет предыдущую, это самостоятельная строка!
  edit_food → ТОЛЬКО при явных словах-маркерах изменения: "измени", "исправь",
              "поменяй", "ошибка", "не 120, а 250г", "вместо X — Y".
              Нет таких слов → строго log_food, не edit_food.

Возвращай ТОЛЬКО один валидный JSON-объект {}. Никакого текста вне JSON, без цитат и ссылок.
Числа (calories, p, f, c, burned_calories, weight, new_weight) — только числовые значения JSON, без единиц и кавычек.
НИКОГДА не пиши два {} блока подряд — вложи всё в один объект.

Тип запроса:
  еда → type "food"
  активность (шаги, бег, тренировка, велосипед) → type "activity"
  вопрос или совет → type "question"

TYPE "food" — actions: log_food | delete_food | edit_food | change_weight
Составное блюдо → разбей на ингредиенты (каждый отдельным объектом в items).
Для каждого продукта: emoji (подходящий эмодзи), weight (граммы, обязательно > 0).

{"type":"food","action":"log_food","data":{"meal_type":"Завтрак|Обед|Ужин|Перекус","items":[{"name":"Название","emoji":"🍗","weight":100,"calories":200,"p":15.0,"f":8.0,"c":4.0}]}}
{"type":"food","action":"delete_food","data":{"product_name":"Точное название из рациона"}}
{"type":"food","action":"edit_food","data":{"old_name":"Старое название","new_name":"Правильное название","calories":200,"p":15.0,"f":8.0,"c":4.0}}
{"type":"food","action":"change_weight","data":{"product_name":"Название","new_weight":200,"calories":264,"p":9.8,"f":3.4,"c":46.6}}

TYPE "activity" — 10000 шагов ≈ 350-400 ккал; с грузом/в гору → +25%.
{"type":"activity","burned_calories":350,"description":"10000 шагов"}

TYPE "question" — краткий ответ с учётом контекста дня (съедено, сожжено, баланс).
{"type":"question","reply":"🍦 Мороженое можно! Остаток 500 ккал, белок уже набран."}"""


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_first_object(text: str) -> str:
    """Возвращает первый синтаксически полный {...} из строки.

    Использует балансовый счётчик скобок с учётом строк и escaping,
    поэтому корректно обрабатывает вложенные объекты и значения-строки
    с { или } внутри. Это защита от сценария, когда модель выдаёт
    два JSON-объекта подряд: {…}{…} → берём только первый.
    """
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # Скобки не сбалансированы — возвращаем всё от первой { (парсер разберётся)
    return text[start:]


def _clean_json_str(text: str) -> str:
    """Нормализует строку ответа модели к валидному JSON."""
    text = text.strip()
    # 1. Убираем thinking-теги (Qwen / Gemini thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # 2. Убираем markdown-обёртку ```json ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 3. Извлекаем первый полный {...} — защита от склейки нескольких объектов
    #    (rfind("}")  было бы неправильно: оно брало последнюю скобку и включало
    #    весь мусор между двумя объектами)
    text = _extract_first_object(text)
    # 4. Trailing comma перед } или ] — частая ошибка Gemini
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # 5. Python-литералы → JSON
    text = re.sub(r"\bTrue\b",  "true",  text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b",  "null",  text)
    return text


def _parse_json(text: str) -> dict:
    return json.loads(_clean_json_str(text))


async def _repair_json(
    bad_text: str,
    session: aiohttp.ClientSession,
    headers: dict,
) -> dict:
    """Fallback: просим модель вернуть исправленный JSON."""
    repair_payload = {
        "model": MODEL,
        "max_tokens": 600,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Исправь синтаксис JSON ниже и верни ТОЛЬКО исправленный валидный JSON "
                    "без пояснений, без markdown:\n\n" + bad_text
                ),
            }
        ],
    }
    async with session.post(OPENROUTER_URL, headers=headers, json=repair_payload) as resp:
        if resp.status != 200:
            error = await resp.text()
            raise RuntimeError(f"Repair call failed {resp.status}: {error}")
        data = await resp.json()
    repaired = data["choices"][0]["message"]["content"]
    logger.warning("JSON repaired by model. Original: %s | Repaired: %s", bad_text[:200], repaired[:200])
    return _parse_json(repaired)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def ask_gemini(user_text: str, context_info: str) -> dict:
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nutrition-bot",
        "X-Title": "NutritionBot",
    }

    full_user_message = f"{context_info}\n\nСообщение пользователя: {user_text}"

    payload = {
        "model": MODEL,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": full_user_message},
        ],
        "response_format": {"type": "json_object"},
    }

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # ── Основной запрос ──────────────────────────────────────────────
        async with session.post(OPENROUTER_URL, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"OpenRouter error {response.status}: {error_text}")
            result = await response.json()

        content = result["choices"][0]["message"]["content"]

        # ── Попытка 1: прямой парсинг с очисткой ────────────────────────
        try:
            return _parse_json(content)
        except (json.JSONDecodeError, ValueError) as first_err:
            logger.warning("JSON parse failed (attempt 1): %s | raw: %s", first_err, content[:300])

        # ── Попытка 2: repair-запрос к модели ───────────────────────────
        try:
            return await _repair_json(content, session, headers)
        except Exception as repair_err:
            raise RuntimeError(
                f"Не удалось разобрать ответ ИИ даже после repair-запроса.\n"
                f"Ошибка: {repair_err}\n"
                f"Оригинал: {content[:500]}"
            )
