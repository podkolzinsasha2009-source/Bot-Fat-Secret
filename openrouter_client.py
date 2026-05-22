import json
import logging
import os
import re

import aiohttp

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-lite"

SYSTEM_PROMPT = """Возвращай ТОЛЬКО валидный JSON. Никакого текста вне JSON — ни до, ни после.
Без приветствий, пожеланий, вводных слов и советов по здоровью.
Все числа (calories, p, f, c, burned_calories, weight, new_weight) — строго числовые значения JSON
(целые или дробные: 4 или 4.0). Никаких единиц измерения, кавычек вокруг чисел и лишних символов.

Определи тип запроса:
- описание съеденной еды → type "food"
- активность (шаги, тренировка, бег, велосипед, прогулка) → type "activity"
- вопрос или просьба совета → type "question"

TYPE "food" — action: log_food | delete_food | edit_food | change_weight
Составное блюдо → разбей на отдельные ингредиенты (каждый своим объектом в items).
Для каждого продукта подбери подходящий эмодзи (поле "emoji").
Формат вывода (бот строит из данных):
  [emoji] *Название:* X ккал | Б: X | Ж: X | У: X
  ーーー
  📊 *Итого за приём:* X ккал | Б: X | Ж: X | У: X

{"type":"food","action":"log_food","data":{"meal_type":"Завтрак|Обед|Ужин|Перекус","items":[{"name":"Название","emoji":"🍗","weight":100,"calories":200,"p":15.0,"f":8.0,"c":4.0}]}}
{"type":"food","action":"delete_food","data":{"product_name":"Точное название из рациона"}}
{"type":"food","action":"edit_food","data":{"old_name":"Старое название из рациона","new_name":"Правильное название","calories":200,"p":15.0,"f":8.0,"c":4.0}}
{"type":"food","action":"change_weight","data":{"product_name":"Название из рациона","new_weight":200,"calories":264,"p":9.8,"f":3.4,"c":46.6}}

TYPE "activity" — шаги, тренировки, физическая нагрузка
Расчёт: 10000 шагов ≈ 350-400 ккал. Ходьба с рюкзаком / в гору / с грузом → +25% к расходу.
{"type":"activity","burned_calories":350,"description":"10000 шагов"}

TYPE "question" — вопросы, советы, рекомендации
Используй контекст дня: съедено, сожжено, чистый баланс, остаток до нормы.
Ответ краткий и конкретный. Начни с релевантного эмодзи.
{"type":"question","reply":"🍦 Мороженое можно! Остаток 500 ккал, белок уже набран."}"""


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _clean_json_str(text: str) -> str:
    """Нормализует строку ответа модели к валидному JSON."""
    text = text.strip()
    # 1. Убираем thinking-теги (Qwen / Gemini thinking mode)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # 2. Убираем markdown-обёртку ```json ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 3. Если вокруг JSON есть лишний текст — вырезаем первый {...}
    start = text.find("{")
    end   = text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
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

    timeout = aiohttp.ClientTimeout(total=60)
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
