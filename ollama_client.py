"""Ollama API 클라이언트 (env var 기반).

OLLAMA_URL : Ollama 베이스 URL. 비어있으면 LLM 호출 없이 None 반환 (graceful degradation).
             예) http://localhost:11434  또는  https://macmini.tail16c823.ts.net/worker/ollama
OLLAMA_MODEL: 사용할 모델 (기본 qwen3:8b)
OLLAMA_AUTH_HEADER: "Header-Name: value" 형식 (선택). Mac Mini를 통과시킬 때 X-Worker-Secret 등.
"""
import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("geo_audit.ollama")

_OLLAMA_URL = os.environ.get("OLLAMA_URL", "").rstrip("/")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
_OLLAMA_AUTH = os.environ.get("OLLAMA_AUTH_HEADER", "")
_OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT_SEC", "60"))


def is_enabled() -> bool:
    return bool(_OLLAMA_URL)


def _build_headers() -> dict:
    if not _OLLAMA_AUTH:
        return {}
    if ":" not in _OLLAMA_AUTH:
        log.warning("OLLAMA_AUTH_HEADER 형식 오류 — 'Name: value' 형태여야 함")
        return {}
    name, _, value = _OLLAMA_AUTH.partition(":")
    return {name.strip(): value.strip()}


async def chat(prompt: str, system: Optional[str] = None) -> Optional[str]:
    """Ollama에 단일 turn 채팅 요청. 실패 시 None 반환."""
    if not _OLLAMA_URL:
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_TIMEOUT) as client:
            resp = await client.post(
                f"{_OLLAMA_URL}/api/chat",
                json={"model": _OLLAMA_MODEL, "messages": messages, "stream": False},
                headers=_build_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            return content.strip() or None
    except Exception as e:
        log.warning("Ollama 호출 실패: %s", e)
        return None
