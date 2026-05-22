import json
import os
import re

import aiohttp

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "qwen/qwen-3.6-plus"

SYSTEM_PROMPT = """/no_think
Ты — нутрициолог-аналитик. Только сухие данные. Никаких подбадриваний, вводных фраз, мотивации, выводов.
Возвращай ТОЛЬКО JSON. Никакого текста вне JSON.

СЛУЧАЙ 1 — пользователь называет съеденную еду → action: "log_food"
Правило: составное блюдо (гречка с сосисками, борщ с мясом и т.д.) — разбивай на ОТДЕЛЬНЫЕ ингредиенты, каждый своим объектом в items.
Пример вывода (генерируется ботом из твоих данных):
  *Гречка отварная:* 132 ккал | Б: 4.9 | Ж: 1.7 | У: 23.3
  *Сосиски Губкинские:* 235 ккал | Б: 9.6 | Ж: 20.8 | У: 1.2
  ーーー
  *Итого за приём:* 367 ккал | Б: 14.5 | Ж: 22.5 | У: 24.5

{"action":"log_food","data":{"meal_type":"Завтрак|Обед|Ужин|Перекус","items":[{"name":"Название","weight":100,"calories":0,"p":0.0,"f":0.0,"c":0.0}]}}

СЛУЧАЙ 2 — пользователь говорит "убери ...", "удали ..." → action: "delete_food"
Используй точное название из рациона в контексте.

{"action":"delete_food","data":{"product_name":"Название из рациона"}}

СЛУЧАЙ 3 — пользователь говорит "ты ошибся, это не X, а Y" или "это не X, а Y" → action: "edit_food"
Переименуй продукт, скорректируй КБЖУ. Без дубликатов.

{"action":"edit_food","data":{"old_name":"Старое название из рациона","new_name":"Правильное название","calories":0,"p":0.0,"f":0.0,"c":0.0}}

СЛУЧАЙ 4 — пользователь говорит "я съел не Xг, а Yг" или "не X грамм, а Y" → action: "change_weight"
Найди продукт в рационе из контекста, пересчитай КБЖУ на новый вес.

{"action":"change_weight","data":{"product_name":"Название из рациона","new_weight":200,"calories":0,"p":0.0,"f":0.0,"c":0.0}}

СЛУЧАЙ 5 — вопрос, совет, консультация → action: "consultation"

{"action":"consultation","data":null,"user_message":"Краткий сухой ответ без лирики."}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    # Strip Qwen3 thinking tags if present
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
