"""Groq Whisper — транскрипция голосовых сообщений (OGG → текст)."""
import os
from groq import Groq

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        _client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _client


async def transcribe(ogg_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Принимает сырые байты OGG, возвращает распознанный текст."""
    import asyncio
    return await asyncio.to_thread(_transcribe_sync, ogg_bytes, filename)


def _transcribe_sync(ogg_bytes: bytes, filename: str) -> str:
    client = _get_client()
    transcription = client.audio.transcriptions.create(
        file=(filename, ogg_bytes),
        model="whisper-large-v3",
        language="ru",
        response_format="text",
    )
    return transcription.strip()
