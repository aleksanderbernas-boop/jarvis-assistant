"""Server-side speech-to-text and text-to-speech. API keys never reach the browser."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from jarvis.config import GROQ_BASE_URL, OPENAI_BASE_URL, Settings

log = logging.getLogger(__name__)

# Whisper tends to "hear" these in silence or noise.
_HALLUCINATIONS = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching!", "you", "bye", "bye.",
    "thank you for watching", "subtitles by the amara.org community", "dziękuję", "dziękuję.", ".", "",
}


class SpeechError(Exception):
    pass


@dataclass
class Transcript:
    text: str
    language: str | None = None


class SpeechToText:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        voice = settings.voice
        self.provider = voice.stt_provider
        if self.provider == "groq":
            self._url, key = f"{GROQ_BASE_URL}/audio/transcriptions", voice.groq_api_key
        elif self.provider == "openai":
            self._url, key = f"{OPENAI_BASE_URL}/audio/transcriptions", voice.openai_api_key
        else:
            self._url, key = "", None
        self._key = key.get_secret_value() if key else ""
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0))

    @property
    def server_side(self) -> bool:
        return self.provider in {"groq", "openai"}

    def status(self) -> tuple[bool, str]:
        if self._settings.mock:
            return True, "mock mode: transcription is simulated"
        if self.provider == "browser":
            return True, "browser speech recognition"
        if self.provider == "none":
            return False, "voice input disabled"
        if not self._key:
            return False, f"{self.provider} STT needs an API key ({'GROQ_API_KEY' if self.provider == 'groq' else 'OPENAI_API_KEY'})"
        return True, f"{self.provider} {self._settings.voice.stt_model}"

    async def close(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio: bytes, filename: str = "speech.wav", mime: str = "audio/wav") -> Transcript:
        if self._settings.mock:
            return Transcript(text="")
        ok, reason = self.status()
        if not ok or not self.server_side:
            raise SpeechError(reason if not ok else "server-side transcription is not configured")
        data = {"model": self._settings.voice.stt_model, "response_format": "json", "temperature": "0",
                "prompt": "JARVIS, open Chrome. Hey JARVIS, what's the weather?"}
        if self._settings.voice.stt_language:
            data["language"] = self._settings.voice.stt_language
        try:
            resp = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                data=data,
                files={"file": (filename, audio, mime)},
            )
        except httpx.HTTPError as exc:
            raise SpeechError(f"speech-to-text request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SpeechError(f"speech-to-text failed (HTTP {resp.status_code}): {resp.text[:200]}")
        text = str(resp.json().get("text", "")).strip()
        if re.sub(r"[^\w\s.!]", "", text.lower()).strip() in _HALLUCINATIONS:
            text = ""
        return Transcript(text=text, language=resp.json().get("language"))


class TextToSpeech:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.provider = settings.voice.tts_provider
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=8.0))

    @property
    def server_side(self) -> bool:
        # Only route speech through the server when the provider is fully configured;
        # otherwise the HUD uses the browser voice instead of going silent.
        return self.provider in {"groq", "elevenlabs", "openai"} and not self._settings.mock and self.status()[0]

    def status(self) -> tuple[bool, str]:
        voice = self._settings.voice
        if self.provider == "browser" or self._settings.mock:
            return True, "browser speech synthesis"
        if self.provider == "elevenlabs" and not (voice.elevenlabs_api_key and voice.elevenlabs_voice_id):
            return False, "ElevenLabs needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID"
        if self.provider == "openai" and not voice.openai_api_key:
            return False, "OpenAI TTS needs OPENAI_API_KEY"
        if self.provider == "groq" and not voice.groq_api_key:
            return False, "Groq TTS needs GROQ_API_KEY"
        if self.provider == "groq" and not (voice.tts_model and voice.tts_voice):
            return False, "Groq TTS needs JARVIS_TTS_MODEL and JARVIS_TTS_VOICE"
        return True, f"{self.provider} {voice.tts_model or voice.tts_voice}".strip()

    async def close(self) -> None:
        await self._client.aclose()

    async def synthesize(self, text: str) -> tuple[bytes, str]:
        """Return (audio bytes, mime type)."""
        ok, reason = self.status()
        if not ok or not self.server_side:
            raise SpeechError(reason if not ok else "server-side TTS is not configured")
        voice = self._settings.voice
        text = text[:1500]
        try:
            if self.provider == "elevenlabs":
                assert voice.elevenlabs_api_key
                resp = await self._client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice.elevenlabs_voice_id}",
                    params={"output_format": "mp3_44100_128"},
                    headers={"xi-api-key": voice.elevenlabs_api_key.get_secret_value()},
                    json={"text": text, "model_id": voice.tts_model or "eleven_flash_v2_5"},
                )
                mime = "audio/mpeg"
            elif self.provider == "openai":
                assert voice.openai_api_key
                resp = await self._client.post(
                    f"{OPENAI_BASE_URL}/audio/speech",
                    headers={"Authorization": f"Bearer {voice.openai_api_key.get_secret_value()}"},
                    json={"model": voice.tts_model or "gpt-4o-mini-tts", "voice": voice.tts_voice or "fable",
                          "input": text, "response_format": "mp3"},
                )
                mime = "audio/mpeg"
            else:
                assert voice.groq_api_key
                resp = await self._client.post(
                    f"{GROQ_BASE_URL}/audio/speech",
                    headers={"Authorization": f"Bearer {voice.groq_api_key.get_secret_value()}"},
                    json={"model": voice.tts_model, "voice": voice.tts_voice, "input": text,
                          "response_format": "wav"},
                )
                mime = "audio/wav"
        except httpx.HTTPError as exc:
            raise SpeechError(f"text-to-speech request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SpeechError(f"text-to-speech failed (HTTP {resp.status_code}): {resp.text[:200]}")
        return resp.content, resp.headers.get("content-type", mime).split(";")[0]
