"""
Groq Whisper — транскрипция голосовых сообщений (OGG → текст).
Retry: 3 попытки с экспоненциальной задержкой при 503/rate_limit.
context_hint: подсказка Whisper (последние продукты) для лучшего распознавания.
"""
import asyncio
import os

from groq import Groq
from loguru import logger

_client: Groq | None = None

_TRANSIENT_KEYWORDS = (
    "503", "rate_limit", "overloaded", "unavailable",
    "service_unavailable", "timeout",
)


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


def _transcribe_sync(ogg_bytes: bytes, filename: str, context_hint: str) -> str:
    client = _get_client()
    kwargs: dict = dict(
        file=(filename, ogg_bytes),
        model="whisper-large-v3",
        language="ru",
        response_format="text",
    )
    # Подсказка помогает Whisper лучше распознавать названия продуктов
    if context_hint:
        kwargs["prompt"] = context_hint
    result = client.audio.transcriptions.create(**kwargs)
    return result.strip() if isinstance(result, str) else str(result).strip()


async def transcribe(
    ogg_bytes: bytes,
    filename: str = "voice.ogg",
    context_hint: str = "",
) -> str:
    """
    Принимает сырые байты OGG, возвращает распознанный текст.
    context_hint — строка с названиями продуктов из рациона дня (для подсказки Whisper).
    """
    last_err: Exception = RuntimeError("no attempts")
    for attempt in range(3):
        try:
            return await asyncio.to_thread(_transcribe_sync, ogg_bytes, filename, context_hint)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if any(k in msg for k in _TRANSIENT_KEYWORDS) and attempt < 2:
                delay = 2.0 * (2 ** attempt)  # 2s → 4s
                logger.warning(
                    "Groq transient error (attempt {}/3), retry in {:.0f}s: {}",
                    attempt + 1, delay, e,
                )
                await asyncio.sleep(delay)
            else:
                raise
    raise last_err
