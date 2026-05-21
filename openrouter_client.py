import json
import os
import re

import aiohttp

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "google/gemini-2.5-pro"

SYSTEM_PROMPT = """Вы - эксперт-нутрициолог и фитнес-тренер с доступом к поиску в интернете. Ваша цель - находить точные данные КБЖУ для любых продуктов (даже редких брендов или сложных описаний блюд), используя веб-поиск. Вы общаетесь дружелюбно, как личный тренер, поддерживаете и мотивируете пользователя. Используйте дефис вместо длинного тире.

Контекст дня пользователя передается в каждом запросе.
Вы ОБЯЗАНЫ возвращать ответ СТРОГО в формате JSON с двумя ветками: 'action', 'data' (для записи в БД) и 'user_message' (текст для пользователя).

Если пользователь записывает еду:
{
  "action": "log_food",
  "data": {
    "meal_type": "Автоматически определи на основе контекста времени (Завтрак/Обед/Ужин/Перекус)",
    "items": [{"name": "Продукт", "weight": 100, "calories": 0, "p": 0, "f": 0, "c": 0}]
  },
  "user_message": "Текст тренера: эмоциональное подтверждение записи, анализ БЖУ блюда, сколько калорий осталось на сегодня, слова поддержки и мотивации."
}

Если пользователь просто задает вопрос, просит совет, план или спрашивает разрешение съесть продукт:
{
  "action": "consultation",
  "data": null,
  "user_message": "Текст тренера: развернутый, аргументированный ответ с точки зрения диетологии, одобрение или предложение более здоровой альтернативы согласно его целям."
}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
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
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": full_user_message},
        ],
        "plugins": [{"id": "google_search"}],
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
