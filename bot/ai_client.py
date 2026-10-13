"""Explicitly-invoked OpenRouter helpers for text commands and OCR."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from bot.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL
from bot.kill_import import canonical_mp, parse_iso_date
from bot.utils import GANGS, valid_nick

logger = logging.getLogger(__name__)

_COMMAND_ACTIONS = {
    "add",
    "remove",
    "move",
    "ss",
    "ld",
    "add_kills",
    "answer",
    "unknown",
}
_OCR_WRAPPER_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)


class AIServiceError(RuntimeError):
    """A friendly, non-secret-bearing error raised for AI service failures."""


def _provider_error_code(exc: Any) -> str | None:
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if not code and isinstance(body, dict):
        payload = body.get("error", body)
        if isinstance(payload, dict):
            code = payload.get("code") or payload.get("type")
    if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", code):
        return code.lower()
    return None


def _friendly_provider_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    code = _provider_error_code(exc)
    if status == 429:
        if code in {
            "insufficient_quota",
            "billing_hard_limit_reached",
            "credit_balance_exhausted",
            "insufficient_credits",
        }:
            return (
                "OpenRouter отклонил запрос: закончились кредиты API-аккаунта. "
                "Пополните баланс OpenRouter."
            )
        return (
            "OpenRouter временно ограничил запрос (HTTP 429). Проверьте лимиты "
            "аккаунта и попробуйте позже."
        )
    if status == 402 or code in {"insufficient_quota", "credit_balance_exhausted", "insufficient_credits"}:
        return "На балансе OpenRouter недостаточно кредитов. Пополните баланс API-аккаунта."
    if status == 401:
        return "OpenRouter отклонил API-ключ (HTTP 401). Проверьте OPENROUTER_API_KEY в секретах."
    if status == 403:
        return "OpenRouter запретил запрос (HTTP 403). Проверьте доступ ключа и модели."
    if isinstance(exc, asyncio.TimeoutError):
        return "OpenRouter не ответил вовремя. Попробуйте ещё раз позже."
    if isinstance(exc, json.JSONDecodeError):
        return "OpenRouter вернул ответ в неверном формате. Попробуйте ещё раз."
    return "Не удалось обработать запрос через OpenRouter. Проверьте логи бота."


class AIClient:
    def __init__(self) -> None:
        self.model = OPENROUTER_MODEL
        self._client = (
            AsyncOpenAI(
                api_key=OPENROUTER_API_KEY,
                base_url=OPENROUTER_BASE_URL,
                timeout=25,
                max_retries=1,
                default_headers={"X-Title": "Grand Mobile Kill-List Bot"},
            )
            if OPENROUTER_API_KEY
            else None
        )

    @property
    def available(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client:
            await self._client.close()

    def _require_client(self) -> AsyncOpenAI:
        if not self._client:
            raise AIServiceError(
                "AI недоступен: задайте переменную окружения OPENROUTER_API_KEY."
            )
        return self._client

    async def parse_command(self, text: str) -> dict[str, Any]:
        client = self._require_client()
        today = datetime.now(UTC).date().isoformat()
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model,
                    temperature=0.3,
                    max_tokens=700,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Ты помощник Discord-сервера Grand Mobile. "
                                "Определи намерение пользователя и верни только JSON-объект "
                                "с полями action, user, opg, answer, mp, date. "
                                "Для явной просьбы изменить состав используй action add, "
                                "remove, move, ss или ld; доступные ОПГ: tambov, orekh, "
                                "kurgan, kavkaz. В user укажи только явно названный ник, "
                                "opg — только явно названную ОПГ; если данных нет, "
                                "используй пустую строку для user и null для opg. "
                                "Не придумывай ники и не меняй состав, если пользователь "
                                "лишь спрашивает, как это сделать, или говорит об этом "
                                "гипотетически. Если пользователь явно просит добавить "
                                "одну или несколько строк kill log в килл-лист, используй "
                                "action add_kills. Не выдумывай строки: они должны быть "
                                "присланы пользователем в сообщении в формате времени, "
                                "убийцы, оружия и жертвы. В mp верни только gs, vch или "
                                "fura, если ОПГ прямо указана; иначе null. В date верни "
                                "дату в формате ГГГГ-ММ-ДД, если она указана или "
                                "однозначно следует из запроса; не считай лог просьбой "
                                "добавить его, если пользователь явно этого не просил. "
                                "Текущая UTC-дата: "
                                f"{today}. Для остальных действий mp и date должны быть null. "
                                "Для любого обычного вопроса, приветствия, "
                                "просьбы объяснить или темы вне управления составом "
                                "используй action answer и дай полезный краткий ответ "
                                "на языке пользователя в поле answer. Не утверждай, что "
                                "выполнил действие. Не выдумывай текущие данные сервера. "
                                "Для действий поле answer оставь пустым."
                            ),
                        },
                        {"role": "user", "content": text[:2000]},
                    ],
                ),
                timeout=10,
            )
            content = response.choices[0].message.content or "{}"
            payload = json.loads(content)
        except (asyncio.TimeoutError, json.JSONDecodeError, OpenAIError) as exc:
            code = _provider_error_code(exc) if isinstance(exc, OpenAIError) else None
            logger.warning(
                "OpenRouter command parse failed: error=%s status=%s code=%s",
                type(exc).__name__,
                getattr(exc, "status_code", None),
                code,
            )
            raise AIServiceError(_friendly_provider_error(exc)) from exc

        action = str(payload.get("action", "unknown")).strip().lower()
        opg = payload.get("opg")
        opg_key = str(opg).strip().lower() if opg is not None else None
        if action not in _COMMAND_ACTIONS:
            action = "unknown"
        if opg_key not in GANGS:
            opg_key = None
        user = str(payload.get("user", "")).strip()[:32]
        if user and not valid_nick(user):
            user = ""
        answer = payload.get("answer", "")
        if not isinstance(answer, str):
            answer = ""
        answer = answer.strip()[:1600]
        mp = canonical_mp(str(payload.get("mp", "")))
        selected_date = payload.get("date")
        if not isinstance(selected_date, str):
            selected_date = None
        parsed_date = parse_iso_date(selected_date)
        return {
            "action": action,
            "user": user,
            "opg": opg_key,
            "answer": answer,
            "mp": mp,
            "date": parsed_date.isoformat() if parsed_date else None,
        }

    async def extract_nicks(self, image: bytes, media_type: str) -> list[str]:
        client = self._require_client()
        encoded = base64.b64encode(image).decode("ascii")
        data_url = f"data:{media_type};base64,{encoded}"
        prompt = (
            "Ты OCR для скриншотов Grand Mobile, сервер 15. Найди все видимые "
            "игровые никнеймы. Оставляй только буквы латиницы/кириллицы, цифры "
            "и подчёркивание; метки вида [ID] не включай. Не считай заголовки, "
            "кнопки и подписи никами. Верни JSON-объект вида "
            '{"nicks":["Nick1","Nick2"]}; если ников нет, верни {"nicks":[]}.'
        )
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model,
                    max_tokens=800,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url, "detail": "high"},
                                },
                            ],
                        }
                    ],
                ),
                timeout=25,
            )
            content = response.choices[0].message.content or "{}"
            # Accept an array too, so the parser remains compatible with OCR output
            # from OpenAI model variants that ignore JSON-object mode.
            cleaned = _OCR_WRAPPER_RE.sub("", content).strip()
            payload = json.loads(cleaned)
            values = payload if isinstance(payload, list) else payload.get("nicks", [])
            if not isinstance(values, list):
                raise ValueError("OCR result is not a list")
            result: list[str] = []
            seen: set[str] = set()
            for value in values:
                if not isinstance(value, str):
                    continue
                nick = value.strip()
                key = nick.casefold()
                if valid_nick(nick) and key not in seen:
                    result.append(nick)
                    seen.add(key)
            return result
        except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, OpenAIError) as exc:
            code = _provider_error_code(exc) if isinstance(exc, OpenAIError) else None
            logger.warning(
                "OpenRouter OCR failed: error=%s status=%s code=%s",
                type(exc).__name__,
                getattr(exc, "status_code", None),
                code,
            )
            raise AIServiceError(_friendly_provider_error(exc)) from exc

