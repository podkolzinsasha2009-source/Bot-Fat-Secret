"""
Gemini 2.5 Flash (google-genai SDK):
  - extract_intent()     — что хочет пользователь (еда / активность / вопрос)
  - estimate_nutrition() — оценка КБЖУ на 100г для неизвестных продуктов
  - analyze_photo()      — анализ фото еды или штрихкода
  - lookup_barcode()     — поиск продукта по штрихкоду (Open Food Facts)

Retry: 3 попытки с экспоненциальной задержкой при 503/RESOURCE_EXHAUSTED.
"""
import asyncio
import base64
import json
import os
import re

import requests
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

_PHOTO_SYSTEM = """\
Ты — ассистент дневника питания. Проанализируй фотографию и верни ТОЛЬКО один JSON-объект без пояснений.

Если на фото ШТРИХКОД (EAN-13, EAN-8, QR-код, любой баркод):
{"type":"barcode","barcode":"ТОЛЬКО_ЦИФРЫ"}
Штрихкод — только цифры, точно как написаны на упаковке.

Если на фото ЕДА, БЛЮДО, ПРОДУКТЫ — верни список ингредиентов:
{"type":"food","items":[{"name":"название на русском","weight":ВЕС_ГРАММЫ}],"meal_type":"Завтрак|Обед|Полдник|Ужин|Перекус"}

Правила для ЕДЫ:
- Если пользователь написал вес в подписи к фото — используй это значение для всей порции
- Иначе оцени визуально: стандартная порция ~100-200г, тарелка ~300г, миска ~400г
- Разбивай блюда на ингредиенты (борщ → свёкла, капуста, картофель, мясо и т.д.)
- Называй конкретно: не «блюдо», а «куриная грудка», «рис варёный», «огурец»
- meal_type определи по виду еды или поставь «Перекус»
- Верни ТОЛЬКО JSON
"""

_FORCE_EDIT_PREFIX = """\
⚠️ РЕЖИМ РЕДАКТИРОВАНИЯ: пользователь явно нажал кнопку «Изменить». \
Его сообщение — это КОМАНДА РЕДАКТИРОВАНИЯ существующей записи. \
Верни change_weight, edit_food или delete_food — никогда не log_food. \
Если пользователь написал только название и вес (например «молоко 200г») — это change_weight. \
Если написал «X → Y» или «вместо X Y» — это edit_food. \
Если написал «удали X» — delete_food.\n\n"""


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    
    start_brace = text.find("{")
    start_bracket = text.find("[")
    
    start = -1
    end_char = ""
    if start_brace != -1 and (start_bracket == -1 or start_brace < start_bracket):
        start = start_brace
        end_char = "}"
    elif start_bracket != -1:
        start = start_bracket
        end_char = "]"
        
    if start == -1:
        return text
        
    end = text.rfind(end_char)
    if end != -1:
        text = text[start:end+1]
    else:
        text = text[start:]
        
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r"\bTrue\b",  "true",  text)
    text = re.sub(r"\bFalse\b", "false", text)
    text = re.sub(r"\bNone\b",  "null",  text)
    return text


def _parse(text: str) -> dict:
    return json.loads(_clean_json(text))


# ── Retry helper ──────────────────────────────────────────────────────────────

_TRANSIENT_KEYWORDS = (
    "503", "resource_exhausted", "unavailable", "overloaded",
    "internal", "service_unavailable", "quota", "rate_limit",
)


async def _with_retry(coro_factory, attempts: int = 3) -> str:
    """Retry с экспоненциальной задержкой при временных ошибках Gemini."""
    last_err: Exception = RuntimeError("no attempts")
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(k in msg for k in _TRANSIENT_KEYWORDS) and attempt < attempts - 1:
                delay = 2.0 * (2 ** attempt)   # 2s → 4s
                logger.warning(
                    "Gemini transient error (attempt {}/{}), retry in {:.0f}s: {}",
                    attempt + 1, attempts, delay, e,
                )
                await asyncio.sleep(delay)
            else:
                raise
    raise last_err


# ── Public API ────────────────────────────────────────────────────────────────

async def extract_intent(user_text: str, context: str, force_edit: bool = False) -> dict:
    prefix = _FORCE_EDIT_PREFIX if force_edit else ""
    prompt = f"{prefix}{_INTENT_SYSTEM}\n\nКонтекст:\n{context}\n\nСообщение пользователя: {user_text}"

    async def _call():
        def _sync():
            resp = _client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=4096,
                ),
            )
            return resp.text
        return await asyncio.to_thread(_sync)

    raw = await _with_retry(_call)
    logger.debug("Gemini intent raw: {}", raw[:300])

    try:
        return _parse(raw)
    except Exception as e:
        logger.warning("JSON parse error: {} | raw: {}", e, raw[:300])
        repair = f"Исправь JSON ниже и верни ТОЛЬКО валидный JSON без пояснений:\n\n{raw}"

        async def _repair_call():
            def _sync():
                r = _client.models.generate_content(
                    model=_MODEL,
                    contents=repair,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        max_output_tokens=4096,
                    ),
                )
                return r.text
            return await asyncio.to_thread(_sync)

        repaired = await _with_retry(_repair_call)
        return _parse(repaired)


async def estimate_nutrition(product_name: str) -> dict:
    prompt = f"{_ESTIMATE_SYSTEM}\n\nПродукт: {product_name}"

    async def _call():
        def _sync():
            resp = _client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=200,
                ),
            )
            return resp.text
        return await asyncio.to_thread(_sync)

    raw = await _with_retry(_call)
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


async def analyze_photo(image_bytes: bytes, caption: str = "") -> dict:
    """
    Анализирует фото еды или штрихкода через Gemini Vision.
    Возвращает {"type":"food","items":[...],"meal_type":"..."}
    или {"type":"barcode","barcode":"..."}.
    """
    prompt_text = _PHOTO_SYSTEM
    if caption.strip():
        prompt_text += f"\n\nПодпись пользователя к фото: {caption.strip()}"

    img_b64 = base64.b64encode(image_bytes).decode()

    async def _call():
        def _sync():
            resp = _client.models.generate_content(
                model=_MODEL,
                contents=[
                    {
                        "role": "user",
                        "parts": [
                            {"text": prompt_text},
                            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}},
                        ],
                    }
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    max_output_tokens=1024,
                ),
            )
            return resp.text
        return await asyncio.to_thread(_sync)

    raw = await _with_retry(_call)
    logger.debug("Gemini photo raw: {}", raw[:300])
    try:
        return _parse(raw)
    except Exception as e:
        logger.warning("Photo JSON parse error: {} | raw: {}", e, raw[:200])
        return {"type": "food", "items": [], "meal_type": "Перекус"}


# ── Barcode lookup (Open Food Facts) ─────────────────────────────────────────

def _lookup_barcode_sync(barcode: str) -> dict | None:
    url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
    r   = requests.get(
        url, timeout=10,
        headers={"User-Agent": "FatSecretBot/1.0 (contact: bot@fatsecret.local)"},
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != 1:
        return None
    p = data["product"]
    n = p.get("nutriments", {})
    name = (p.get("product_name_ru") or p.get("product_name") or "").strip() or barcode
    return {
        "name":     name,
        "calories": float(n.get("energy-kcal_100g") or n.get("energy_100g", 0) or 0),
        "protein":  float(n.get("proteins_100g",        0) or 0),
        "fat":      float(n.get("fat_100g",              0) or 0),
        "carbs":    float(n.get("carbohydrates_100g",    0) or 0),
    }


async def lookup_barcode(barcode: str) -> dict | None:
    """Поиск продукта по штрихкоду через Open Food Facts."""
    try:
        return await asyncio.to_thread(_lookup_barcode_sync, barcode)
    except Exception as e:
        logger.warning("barcode lookup {}: {}", barcode, e)
        return None
