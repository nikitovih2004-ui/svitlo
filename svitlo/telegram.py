"""Тонкий асинхронный клиент Bot API на httpx (Bot API 10.4: виджеты).

Ретраи: сетевые ошибки и 5xx — с экспоненциальной паузой; 429 — ждём `retry_after`.
Исключение — отправка новых сообщений (sendMessage, sendWidget): у Bot API нет ключа
идемпотентности, поэтому повторяем только то, что точно не дошло (ошибка соединения, 429).
Таймаут ответа или 5xx для них — сразу `NetworkError`/`TelegramError`, повтор решает сверка
в bot.py (через 30 с), а не три немедленных дубля здесь.
Остальные ошибки API — `TelegramError`. «message is not modified» при правках — успех.
Токен не попадает в логи (логгер httpx нужно держать на WARNING — см. __main__).
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

log = logging.getLogger(__name__)

NOT_MODIFIED = "message is not modified"
NON_IDEMPOTENT = frozenset({"sendMessage", "sendWidget"})
SAFE_TRANSPORT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)  # запрос не ушёл


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: Optional[int] = None):
        super().__init__("%s: %s %s" % (method, code, description))
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def not_modified(self) -> bool:
        return NOT_MODIFIED in self.description.lower()

    @property
    def gone(self) -> bool:
        """Сообщение удалено/недоступно — править его больше нельзя."""
        d = self.description.lower()
        return self.code == 400 and ("message to edit not found" in d or "message can't be edited" in d
                                     or "message_id_invalid" in d)

    @property
    def widgets_refused(self) -> bool:
        """Telegram не разрешает боту виджеты в этом чате (серверная настройка, Bot API 10.4 раскатывается
        постепенно): INPUT_MEDIA_WIDGET_NOT_ALLOWED."""
        return self.code == 400 and "WIDGET_NOT_ALLOWED" in self.description.upper()

    @property
    def blocked(self) -> bool:
        """Пользователь заблокировал бота или чат недоступен."""
        return self.code == 403 or "chat not found" in self.description.lower()


class NetworkError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, base: str = "https://api.telegram.org",
                 client: Optional[httpx.AsyncClient] = None, retries: int = 3, test_env: bool = False,
                 max_retry_after: float = 120.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.token = token
        self.base = base.rstrip("/")
        self.test_env = test_env  # тестовый сервер Telegram: /bot<token>/test/<method>
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self.retries = retries
        self.max_retry_after = max_retry_after
        self.sleep = sleep

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, http_timeout: Optional[float] = None, **params: Any) -> Any:
        url = "%s/bot%s/%s%s" % (self.base, self.token, "test/" if self.test_env else "", method)
        payload = {k: v for k, v in params.items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            last = attempt > self.retries
            try:
                kwargs = {"json": payload}
                if http_timeout is not None:
                    kwargs["timeout"] = http_timeout
                resp = await self.client.post(url, **kwargs)
            except httpx.TransportError as exc:
                if last or (method in NON_IDEMPOTENT and not isinstance(exc, SAFE_TRANSPORT)):
                    raise NetworkError("%s: %s" % (method, type(exc).__name__))
                await self.sleep(min(30.0, 2.0 ** attempt))
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {"ok": False, "error_code": resp.status_code, "description": resp.text[:200]}
            if data.get("ok"):
                return data.get("result")
            code = int(data.get("error_code") or resp.status_code)
            desc = str(data.get("description") or "")
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and retry_after is not None and not last and retry_after <= self.max_retry_after:
                log.warning("%s: 429, жду %s с", method, retry_after)
                await self.sleep(float(retry_after))
                continue
            if code >= 500 and not last and method not in NON_IDEMPOTENT:
                await self.sleep(min(30.0, 2.0 ** attempt))
                continue
            raise TelegramError(method, code, desc, retry_after)

    # ---------------------------------------------------------------- методы
    async def get_me(self) -> Dict:
        return await self.call("getMe")

    async def get_updates(self, offset: Optional[int], timeout: int = 25,
                          allowed_updates: Optional[List[str]] = None) -> List[Dict]:
        return await self.call("getUpdates", http_timeout=timeout + 15.0, offset=offset, timeout=timeout,
                               allowed_updates=allowed_updates)

    async def send_widget(self, chat_id: int, html: str, chat_data: Optional[str] = None,
                          caption: Optional[str] = None, width: int = 0, height: int = 0,
                          allow_queries: bool = False, disable_notification: bool = False) -> Dict:
        return await self.call("sendWidget", chat_id=chat_id, html=html, chat_data=chat_data,
                               caption=caption, width=width or None, height=height or None,
                               allow_queries=True if allow_queries else None,
                               disable_notification=True if disable_notification else None)

    async def edit_widget_data(self, chat_id: int, message_id: int, chat_data: str) -> bool:
        try:
            await self.call("editMessageWidgetData", chat_id=chat_id, message_id=message_id, chat_data=chat_data)
        except TelegramError as exc:
            if exc.not_modified:
                return True
            raise
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        return await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    async def send_message(self, chat_id: int, text: str, disable_notification: bool = False,
                           parse_mode: Optional[str] = None, reply_markup: Optional[Dict] = None) -> Dict:
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode=parse_mode,
                               reply_markup=reply_markup,
                               disable_notification=True if disable_notification else None)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str,
                                reply_markup: Optional[Dict] = None, parse_mode: Optional[str] = None) -> bool:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                            parse_mode=parse_mode, reply_markup=reply_markup)
        except TelegramError as exc:
            if exc.not_modified:
                return True
            raise
        return True

    async def answer_callback_query(self, query_id: str, text: Optional[str] = None) -> bool:
        return await self.call("answerCallbackQuery", callback_query_id=query_id, text=text)

    async def answer_widget_query(self, query_id: str, data: Optional[str] = None) -> bool:
        return await self.call("answerWidgetQuery", widget_query_id=query_id, data=data)
