import json
import os
import re

import aiohttp

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-flash-lite"

SYSTEM_PROMPT = """Возвращай ТОЛЬКО JSON. Никакого текста вне JSON.
Без приветствий, пожеланий, вводных слов и советов по здоровью.

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

{"type":"food","action":"log_food","data":{"meal_type":"Завтрак|Обед|Ужин|Перекус","items":[{"name":"Название","emoji":"🍗","weight":100,"calories":0,"p":0.0,"f":0.0,"c":0.0}]}}
{"type":"food","action":"delete_food","data":{"product_name":"Точное название из рациона"}}
{"type":"food","action":"edit_food","data":{"old_name":"Старое название из рациона","new_name":"Правильное название","calories":0,"p":0.0,"f":0.0,"c":0.0}}
{"type":"food","action":"change_weight","data":{"product_name":"Название из рациона","new_weight":200,"calories":0,"p":0.0,"f":0.0,"c":0.0}}

TYPE "activity" — шаги, тренировки, физическая нагрузка
Расчёт: 10000 шагов ≈ 350-400 ккал. Ходьба с рюкзаком / в гору / с грузом → +25% к расходу.
{"type":"activity","burned_calories":350,"description":"10000 шагов"}

TYPE "question" — вопросы, советы, рекомендации
Используй контекст дня: съедено, сожжено, чистый баланс, остаток до нормы.
Ответ краткий и конкретный. Начни с релевантного эмодзи.
{"type":"question","reply":"🍦 Мороженое можно! Остаток 500 ккал, белок уже набран."}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip thinking tags (Qwen / o1 style)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


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
        async with session.post(OPENROUTER_URL, headers=headers, json=payload) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"OpenRouter error {response.status}: {error_text}")
            result = await response.json()

    content = result["choices"][0]["message"]["content"]

    try:
        return _extract_json(content)
    except (json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f"Не удалось разобрать ответ ИИ как JSON: {e}\nОтвет: {content}")
