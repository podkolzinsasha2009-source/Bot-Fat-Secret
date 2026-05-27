"""
Gemini 2.5 Flash (google-genai SDK):
  - extract_intent()     — что хочет пользователь (еда / активность / вопрос)
  - estimate_nutrition() — оценка КБЖУ на 100г для неизвестных продуктов
"""
import asyncio
import json
import os
import re

from google import genai
from google.genai import types
from loguru import logger

_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
_MODEL  = "gemini-2.5-flash"

# ── Системный промпт — намерение пользователя ─────────────────────────────────

_INTENT_SYSTEM = """\
Ты — ассистент дневника питания. Анализируй сообщение пользователя и возвращай ТОЛЬКО один валидный JSON-объект без пояснений, без markdown.

⚠️ ВАЖНО: в items указывай ТОЛЬКО name и weight. Калории/белки/жиры/углеводы НЕ нужны — они берутся из нашей базы данных.

─── ТИПЫ ОТВЕТОВ ───────────────────────────────────────────────────

1. Логирование еды — ТОЛЬКО если нет явных слов редактирования/удаления:
{"type":"food","action":"log_food","meal_type":"Завтрак|Обед|Полдник|Ужин|Перекус","items":[{"name":"название продукта","weight":100}]}

Составное блюдо — разбивай на ингредиенты. meal_type — по времени из контекста.

🔴 КРИТИЧЕСКИ ВАЖНО: "съел/выпил/поел X" — ВСЕГДА log_food (новая запись), даже если X уже есть в рационе сегодня. Один продукт может встречаться несколько раз за день — это нормально. НЕ объединяй, НЕ суммируй, НЕ обновляй существующие записи при новом логировании.

2. Удаление записи (слова: "удали", "убери", "удалить", "убрать"):
{"type":"food","action":"delete_food","product_name":"точное название из рациона"}

3. Изменение веса (слова: "не X а Y", "исправь вес", "было X теперь Y", "на самом деле было"):
{"type":"food","action":"change_weight","product_name":"название","new_weight":200}

4. Замена продукта (слова: "вместо", "неправильно", "ошибка", "исправь название", "не то"):
{"type":"food","action":"edit_food","old_name":"старое название","new_name":"правильное название","new_weight":150}

5. Физическая активность (ходьба, бег, тренировка, велосипед, шаги):
{"type":"activity","burned_calories":350,"description":"описание"}

Калории активности: 10000 шагов ≈ 375 ккал; бег 30 мин ≈ 300 ккал; велосипед 1ч ≈ 400 ккал; силовая 1ч ≈ 250 ккал.

6. Вопрос или разговор (всё остальное):
{"type":"question","reply":"краткий ответ с учётом дня пользователя"}

─── ОЦЕНКА ВЕСА ЕСЛИ НЕ УКАЗАН ────────────────────────────────────
стакан/кружка = 200г | тарелка = 300г | миска = 400г
ложка столовая = 15г | ложка чайная = 5г | щепотка = 1г
кусок/ломтик хлеба = 30г | ломтик сыра = 20г | яйцо = 60г
не указан = 100г

─── НАЗВАНИЯ ПРОДУКТОВ ─────────────────────────────────────────────
Сохраняй ВСЕ уточняющие детали из речи пользователя — они важны для точного КБЖУ.

ЧИСЛА И ПРОЦЕНТЫ — всегда включай в name:
  «молоко 3.2» → «молоко 3.2»     (жирность)
  «творог 5%» → «творог 5%»       (жирность)
  «сок 100%» → «сок 100%»         (натуральность)
  «пиво 5%» → «пиво 5%»           (алкоголь)
  «кефир 2.5» → «кефир 2.5»       (жирность)

ТЕКСТОВЫЕ УТОЧНИТЕЛИ — всегда включай в name:
  «кола без сахара» → «кола без сахара»
  «хлеб цельнозерновой» → «хлеб цельнозерновой»
  «рис бурый» → «рис бурый»
  «арахис солёный» → «арахис солёный»
  «масло сливочное 82.5%» → «масло сливочное 82.5%»
  «куриная грудка варёная» → «куриная грудка варёная»
  «гречка варёная» → «гречка варёная»

БЕЗ УТОЧНЕНИЙ — передавай просто:
  «молоко» → «молоко»   «сахар» → «сахар»   «яблоко» → «яблоко»
"""

_ESTIMATE_SYSTEM = """\
Ты — нутрициолог. Оцени КБЖУ продукта на 100 грамм. Верни ТОЛЬКО JSON без пояснений:
{"calories":0,"protein":0,"fat":0,"carbs":0}
Числа — значения с одним знаком после запятой.
"""


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start == -1:
        return text
    depth, in_str, esc = 0, False, False
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
                text = text[start:i + 1]
                break
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"\bTrue\b",  "true",  text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b",  "null",  text)
    return text


def _parse(text: str) -> dict:
    return json.loads(_clean_json(text))


# ── Public API ────────────────────────────────────────────────────────────────

_FORCE_EDIT_PREFIX = """\
⚠️ РЕЖИМ РЕДАКТИРОВАНИЯ: пользователь явно нажал кнопку «Изменить». \
Его сообщение — это КОМАНДА РЕДАКТИРОВАНИЯ существующей записи. \
Верни change_weight, edit_food или delete_food — никогда не log_food. \
Если пользователь написал только название и вес (например «молоко 200г») — это change_weight. \
Если написал «X → Y» или «вместо X Y» — это edit_food. \
Если написал «удали X» — delete_food.\n\n"""


async def extract_intent(user_text: str, context: str, force_edit: bool = False) -> dict:
    prefix = _FORCE_EDIT_PREFIX if force_edit else ""
    prompt = f"{prefix}{_INTENT_SYSTEM}\n\nКонтекст:\n{context}\n\nСообщение пользователя: {user_text}"

    def _call() -> str:
        resp = _client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=4096,
            ),
        )
        return resp.text

    raw = await asyncio.to_thread(_call)
    logger.debug("Gemini intent raw: {}", raw[:300])

    try:
        return _parse(raw)
    except Exception as e:
        logger.warning("JSON parse error: {} | raw: {}", e, raw[:300])
        repair = f"Исправь JSON ниже и верни ТОЛЬКО валидный JSON без пояснений:\n\n{raw}"

        def _repair() -> str:
            r = _client.models.generate_content(
                model=_MODEL,
                contents=repair,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=4096,
                ),
            )
            return r.text

        repaired = await asyncio.to_thread(_repair)
        return _parse(repaired)


async def estimate_nutrition(product_name: str) -> dict:
    prompt = f"{_ESTIMATE_SYSTEM}\n\nПродукт: {product_name}"

    def _call() -> str:
        resp = _client.models.generate_content(
            model=_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                max_output_tokens=200,
            ),
        )
        return resp.text

    raw = await asyncio.to_thread(_call)
    try:
        data = _parse(raw)
        return {
            "calories": float(data.get("calories", 0)),
            "protein":  float(data.get("protein",  0)),
            "fat":      float(data.get("fat",       0)),
            "carbs":    float(data.get("carbs",     0)),
        }
    except Exception:
        return {"calories": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0}
