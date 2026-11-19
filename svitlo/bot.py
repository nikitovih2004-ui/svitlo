"""Основной цикл бота.

Задачи (asyncio):
* poll   — чтение инвертора раз в POLL_INTERVAL (в отдельном потоке: pysolarmanv5 блокирующий);
* sched  — график отключений раз в SCHEDULE_INTERVAL;
* publish — сверка сообщений у подписчиков (см. `sync`);
* updates — getUpdates (long polling): /start, /status, /stop, /users, кнопки, widget_query, callback_query.

Доставка построена как сверка, а не как «отправить один раз». У каждого подписчика в базе
записано: о каком состоянии сети (его начало `s`) ему уже сообщено (`notified_epoch`) и после
какого состояния отправлен его дашборд (`live_epoch`). Если сеть сменилась — шлётся текст события,
затем старый дашборд удаляется и беззвучно приходит новый. Каждый шаг фиксируется в базе сразу,
поэтому сбой или перезапуск посреди цепочки продолжает её, не теряя и не повторяя шаги.

Сообщения об изменении графика устроены так же: движок кладёт их в таблицу `notices`,
у подписчика записан id последнего обработанного (`notice_seen`) и последнего отправленного
(`notice_sent`); дашборд, отправленный раньше последнего такого сообщения, пересоздаётся ниже.
Дашборд, отправленный с другим HTML виджета (хэш в `live_widget`), тоже пересоздаётся беззвучно —
код виджета в отправленном сообщении изменить нельзя.

Кнопки (постоянная reply-клавиатура): «Дашборд» = /status; «График» — текущий график сегодня/завтра
(прошлый такой ответ удаляется); «Инвертор» — отдельный виджет (один на чат, старый удаляется), бот правит
его `inverter_minutes` минут, ключ `z` — до какого момента. После ответа на кнопку дашборд пересоздаётся
ниже, чтобы он оставался последним в чате; само сообщение-нажатие бот удаляет.
Все действия с сообщениями одного чата идут под его asyncio.Lock: сверка и нажатия не пересекаются.

Текстовый режим. Если Telegram на sendWidget отвечает INPUT_MEDIA_WIDGET_NOT_ALLOWED (виджеты раскатываются
постепенно, список разрешённых чатов — настройка сервера Telegram), бот запоминает это (kv `widgets`) и дальше
сразу шлёт дашборд и «Инвертор» обычными сообщениями (HTML), которые правит editMessageText по тем же правилам,
что и виджеты. Раз в WIDGET_PROBE_MINUTES (30) — одна проба sendWidget на всех: при пересоздании дашборда,
а если его не было — в чате владельца: новый виджет шлётся под текущим текстовым дашбордом, и только при
успехе старый текст удаляется (отказ в чате ничего не меняет). Успех — режим виджетов: текстовые
дашборды у всех беззвучно пересоздаются виджетами. В журнал — по строке при переходе туда и обратно.

Доступ: владелец (OWNER_ID или первый из ALLOWED_IDS) и ALLOWED_IDS — изначальный список; остальные
пишут боту → владельцу приходит запрос с inline-кнопками «Разрешить» / «Отклонить» (таблица `access`).
Разрешённый получает клавиатуру и дашборд, дальше — как все подписчики. Отклонённые и убранные (/users)
игнорируются молча. Один незакрытый запрос на человека, не больше `access_per_hour` новых запросов в час.
Сообщение-запрос владельцу — новое сообщение в его чате, поэтому его дашборд после него пересоздаётся ниже.
Инвертор опрашивается один раз на всех; подписчики сверяются по очереди (правки ≤ 1 в минуту на чат).
"""

import asyncio
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable, Dict, Optional

from . import texts
from .config import Config
from .schedule import Schedule
from .source import Source, SourceError
from .state import Engine, encode_inverter, parse, same_content
from .storage import Store, Subscriber
from .telegram import NetworkError, Telegram, TelegramError

log = logging.getLogger(__name__)

HOUR = 3600.0
RETRY_DELAY = 30.0       # пауза для подписчика после сбоя доставки
SCHEDULE_RETRY = 60.0    # повтор загрузки графика после ошибки
NOTICE_TTL = 6 * 3600.0  # сообщение о графике, не доставленное за это время, уже не шлём
DAY_EVERY = 300.0        # дневной блок регистров (счётчики, частота, температура инвертора) — раз в 5 мин
DAY_EVERY_LIVE = 30.0    # … пока открыт виджет «Инвертор»
PROBE_NET_RETRY = 300.0  # проба виджетов сорвалась по сети — повтор через 5 мин


def widget_hash(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]


class Bot:
    def __init__(self, cfg: Config, store: Store, engine: Engine, source: Source, tg: Telegram,
                 html: str, fetch_schedule: Optional[Callable[[], Awaitable[Schedule]]], tz,
                 clock: Callable[[], float] = time.time, threaded_source: bool = True,
                 inverter_html: Optional[str] = None):
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self.source = source
        self.tg = tg
        self.html = html
        self.widget_hash = widget_hash(html)
        self.fetch_schedule = fetch_schedule
        self.tz = tz
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source") if threaded_source else None
        self._next_schedule = 0.0
        self._retry_at: Dict[int, float] = {}
        self._wake: Optional[asyncio.Event] = None  # создаётся в run(): в 3.9 Event привязан к циклу
        self.inverter_html = inverter_html
        self._locks: Dict[int, asyncio.Lock] = {}
        self._locks_loop = None
        # режим виджетов: widgets_off — с какого момента Telegram их не разрешает (None — разрешает),
        # probe_at — когда пробовать sendWidget снова
        mode = store.get("widgets")
        self._unconfirmed = False
        if mode is not None:
            self.widgets_off: Optional[float] = mode.get("off")
            self.probe_at: float = float(mode.get("probe") or 0.0)
        else:
            # первая версия с текстовым режимом: если дашборды уже текстовые (отказ был до неё) — считаем,
            # что виджеты запрещены, и сразу пробуем один раз
            texty = any(sub.live_kind == "text" and sub.live_msg_id is not None for sub in store.subscribers())
            self.widgets_off, self.probe_at = (clock(), 0.0) if texty else (None, 0.0)
            self._unconfirmed = texty

    def _lock(self, chat_id: int) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if loop is not self._locks_loop:  # в 3.9 Lock привязан к циклу (тесты запускают новый цикл)
            self._locks, self._locks_loop = {}, loop
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    # ================================================================= входные данные
    async def poll_once(self) -> None:
        try:
            if self._pool is not None:
                r = await asyncio.get_running_loop().run_in_executor(self._pool, self.source.read)
            else:
                r = self.source.read()
        except SourceError as exc:
            self.engine.read_failed()
            n = self.engine.errors
            if n == 1 or n % 30 == 0:
                log.warning("инвертор не отвечает (%d подряд): %s", n, exc)
            return
        if self.engine.errors:
            log.info("связь с инвертором восстановлена (было %d ошибок)", self.engine.errors)
        tr = self.engine.observe(r)
        if tr is not None and self._wake is not None:
            self._wake.set()

    async def refresh_schedule(self, now: float) -> None:
        if self.fetch_schedule is None or now < self._next_schedule:
            return
        try:
            sched = await self.fetch_schedule()
        except Exception as exc:
            log.warning("график не загружен: %s", exc)
            self._next_schedule = now + SCHEDULE_RETRY
            return
        if self.engine.set_schedule(sched, now) and self._wake is not None:
            self._wake.set()
        self._next_schedule = now + self.cfg.schedule_interval
        log.debug("график обновлён (%s)", sched.source)

    # ================================================================= сверка сообщений
    async def sync_all(self, now: float) -> None:
        subs = self.store.subscribers()
        if hasattr(self.source, "day_every"):
            live = any(s.inv_msg_id is not None and (s.inv_until or 0) > now for s in subs)
            self.source.day_every = DAY_EVERY_LIVE if live else DAY_EVERY
        if not self.engine.ready:
            return
        await self._probe(now)
        for sub in self.store.subscribers():
            if now < self._retry_at.get(sub.chat_id, 0.0) or not self.allowed(sub.chat_id):
                continue
            try:
                async with self._lock(sub.chat_id):
                    await self.sync(sub.chat_id, now)
            except (NetworkError, TelegramError) as exc:
                log.warning("чат %s: %s — повтор через %.0f с", sub.chat_id, exc, RETRY_DELAY)
                self._retry_at[sub.chat_id] = now + RETRY_DELAY

    async def sync(self, chat_id: int, now: float, force: bool = False) -> None:
        """Привести чат к нужному виду. Каждый шаг записывается в базу сразу после успеха,
        поэтому сбой или перезапуск посреди цепочки просто продолжает её с того же места.

        1. Событие: если подписчику ещё не сообщено о текущем состоянии сети — текст со звуком.
        2. Изменения графика: ещё не отправленные сообщения из `notices` (сегодня — со звуком,
           завтра — беззвучно).
        3. Дашборд: если он показан до события или сообщения о графике (или его нет, или
           force=/status, или он старше renew_hours, или отправлен с другим HTML виджета) —
           удалить старый и беззвучно прислать новый.
        4. Иначе — живая правка chat_data.
        """
        epoch = self.engine.epoch
        if epoch is None or not self.engine.ready:
            return
        sub = self.store.subscriber(chat_id)
        if sub is None or not sub.active:
            return

        if sub.notified_epoch != epoch:
            # notified_epoch пуст — подписчик только что подписался: событие ему не шлём
            text = self.engine.event_text() if sub.notified_epoch is not None else None
            if text:
                try:
                    await self.tg.send_message(chat_id, text, reply_markup=texts.KEYBOARD)
                except TelegramError as exc:
                    if exc.blocked:
                        self._drop(chat_id)
                        return
                    raise
                log.info("чат %s: событие «%s»", chat_id, text)
            self.store.set_notified(chat_id, epoch)
            sub = self.store.subscriber(chat_id)

        if not await self._send_notices(sub, now):
            return
        sub = self.store.subscriber(chat_id)

        if sub.kb_sent is None:
            # клавиатуры у подписчика ещё нет (подписался до появления кнопок): одно беззвучное сообщение
            if not await self._say(chat_id, texts.KEYBOARD_HELLO):
                return
            self.store.set_fields(chat_id, kb_sent=1, live_epoch=None)  # дашборд пересоздаётся ниже
            log.info("чат %s: показана клавиатура", chat_id)
            sub = self.store.subscriber(chat_id)

        renew = (self.cfg.renew_hours > 0 and sub.live_msg_id is not None and sub.live_sent_at is not None
                 and now - sub.live_sent_at >= self.cfg.renew_hours * 3600)
        missing = sub.live_msg_id is None and sub.live_kind != "gone"
        noticed = (sub.notice_sent or 0) > (sub.live_notice or 0)
        outdated = sub.live_kind == "widget" and sub.live_msg_id is not None and sub.live_widget != self.widget_hash
        if outdated:
            log.info("чат %s: виджет изменился — пересоздаю дашборд", chat_id)
        if force or renew or missing or noticed or outdated or sub.live_epoch != epoch:
            if sub.live_msg_id is not None and not await self._delete(sub):
                return
            await self._send_dashboard(chat_id, now)
        elif sub.live_kind == "widget" and sub.live_msg_id is not None:
            await self._edit(sub, now)
        elif sub.live_kind == "text" and sub.live_msg_id is not None:
            await self._edit_text_dashboard(sub, now)
        await self._inverter_live(chat_id, now)

    async def _send_notices(self, sub: Subscriber, now: float) -> bool:
        """Отправить подписчику ещё не отправленные сообщения об изменении графика.
        False — чат недоступен (подписчик снят)."""
        if sub.notice_seen is None:
            # новый подписчик (или первый запуск этой версии): старые сообщения ему не шлём
            self.store.set_notice_seen(sub.chat_id, self.store.last_notice_id())
            return True
        for n in self.store.notices_after(sub.notice_seen):
            if now - n.ts > NOTICE_TTL:
                self.store.set_notice_seen(sub.chat_id, n.id)
                continue
            try:
                await self.tg.send_message(sub.chat_id, n.text, disable_notification=not n.loud,
                                           parse_mode="HTML", reply_markup=texts.KEYBOARD)
            except TelegramError as exc:
                if exc.blocked:
                    self._drop(sub.chat_id)
                    return False
                raise
            self.store.set_notice_seen(sub.chat_id, n.id, sent=n.id)
            log.info("чат %s: график «%s»", sub.chat_id, n.text.split("\n", 1)[0])
        return True

    async def _edit(self, sub: Subscriber, now: float) -> None:
        """Правка не чаще live_interval и только при изменении; пока связь есть — не реже
        fresh_interval, чтобы `t` оставался свежим (без связи `t` не меняется — правок нет)."""
        current = self.engine.peek(now)
        if current is None:
            return
        age = now - (sub.live_at or 0.0)
        changed = not same_content(sub.live_data, current)
        t_moved = sub.live_data is None or parse(sub.live_data).get("t") != parse(current).get("t")
        if not ((changed and age >= self.cfg.live_interval) or (t_moved and age >= self.cfg.fresh_interval)):
            return
        data = self.engine.chat_data(now)
        try:
            await self.tg.edit_widget_data(sub.chat_id, sub.live_msg_id, data)
        except TelegramError as exc:
            if exc.gone:
                log.info("чат %s: дашборд удалён пользователем — новый придёт со следующим событием или /status",
                         sub.chat_id)
                self.store.clear_live(sub.chat_id, "gone")
                return
            if exc.blocked:
                self._drop(sub.chat_id)
                return
            raise
        self.store.update_live_data(sub.chat_id, data, now)

    async def _delete(self, sub: Subscriber) -> bool:
        """Удалить старый дашборд. False — чат недоступен (подписчик снят)."""
        try:
            await self.tg.delete_message(sub.chat_id, sub.live_msg_id)
        except TelegramError as exc:
            if exc.blocked:
                self._drop(sub.chat_id)
                return False
            if exc.code == 429 or exc.code >= 500:
                raise
            # уже удалён или старше 48 ч — дальше не ждём, иначе чат застрянет
            log.warning("чат %s: дашборд #%s не удалён: %s", sub.chat_id, sub.live_msg_id, exc.description)
        self.store.clear_live(sub.chat_id)
        return True

    async def _send_dashboard(self, chat_id: int, now: float) -> None:
        data = self.engine.chat_data(now)
        msg, kind = None, "text"
        if self._try_widgets(now):
            try:
                msg = await self.tg.send_widget(chat_id, self.html, chat_data=data, caption=texts.DASHBOARD_CAPTION,
                                                width=self.cfg.widget_width, height=self.cfg.widget_height,
                                                allow_queries=self.cfg.widget_queries, disable_notification=True)
            except TelegramError as exc:
                if exc.blocked:
                    self._drop(chat_id)
                    return
                if exc.code == 429 or exc.code >= 500:
                    raise
                if exc.widgets_refused:
                    self._widgets_refused(now, exc)
                else:
                    log.warning("чат %s: sendWidget не прошёл (%s) — отправляю текстом", chat_id, exc)
            else:
                kind = "widget"
                self._widgets_allowed(now)
        if msg is None:
            text = texts.dashboard_text(parse(data), now, self.tz, self.engine.since_known)
            try:
                msg = await self.tg.send_message(chat_id, text, disable_notification=True, parse_mode="HTML")
            except TelegramError as exc2:
                if exc2.blocked:
                    self._drop(chat_id)
                    return
                raise
        sub = self.store.subscriber(chat_id)
        self.store.set_live(chat_id, msg["message_id"], kind, self.engine.epoch, data, now,
                            widget=self.widget_hash if kind == "widget" else texts.TEXT_FORMAT,
                            notice=sub.notice_sent if sub is not None else None)
        log.info("чат %s: дашборд %s #%s", chat_id, kind, msg["message_id"])

    async def _edit_text_dashboard(self, sub: Subscriber, now: float) -> None:
        """Живая правка текстового дашборда (editMessageText) по правилам виджета: при изменении текста
        (кроме строки «обновлено») — не чаще live_interval, при новом `t` — не реже fresh_interval;
        старый формат текста (TEXT_FORMAT) правится сразу."""
        current = self.engine.peek(now)
        if current is None:
            return
        known = self.engine.since_known
        cur = parse(current)
        prev = parse(sub.live_data) if sub.live_data else None
        age = now - (sub.live_at or 0.0)
        old_format = sub.live_widget != texts.TEXT_FORMAT
        changed = prev is None or (texts.dashboard_text(cur, now, self.tz, known, footer=False)
                                   != texts.dashboard_text(prev, sub.live_at or now, self.tz, known, footer=False))
        t_moved = prev is None or prev.get("t") != cur.get("t")
        if not (old_format or (changed and age >= self.cfg.live_interval)
                or (t_moved and age >= self.cfg.fresh_interval)):
            return
        data = self.engine.chat_data(now)
        text = texts.dashboard_text(parse(data), now, self.tz, known)
        try:
            await self.tg.edit_message_text(sub.chat_id, sub.live_msg_id, text, parse_mode="HTML")
        except TelegramError as exc:
            if exc.gone:
                log.info("чат %s: дашборд удалён пользователем — новый придёт со следующим событием или /status",
                         sub.chat_id)
                self.store.clear_live(sub.chat_id, "gone")
                return
            if exc.blocked:
                self._drop(sub.chat_id)
                return
            raise
        self.store.update_live_data(sub.chat_id, data, now, widget=texts.TEXT_FORMAT)

    # ================================================================= режим виджетов
    def _save_mode(self) -> None:
        self.store.set("widgets", {"off": self.widgets_off, "probe": self.probe_at})

    def _try_widgets(self, now: float) -> bool:
        """Пробовать ли sendWidget: виджеты разрешены или подошло время пробы."""
        return self.widgets_off is None or now >= self.probe_at

    def _widgets_refused(self, now: float, exc: TelegramError) -> None:
        """Telegram ответил INPUT_MEDIA_WIDGET_NOT_ALLOWED: текстовый режим, следующая проба — через
        widget_probe_minutes. В журнал — одна строка при переходе."""
        if self.widgets_off is None or self._unconfirmed:
            log.warning("Telegram не разрешает виджеты (%s) — дашборд и «Инвертор» идут текстом, "
                        "проверка раз в %.0f мин", exc.description, self.cfg.widget_probe_minutes)
            self.widgets_off = self.widgets_off or now
            self._unconfirmed = False
        else:
            log.debug("проба виджетов: всё ещё %s", exc.description)
        self.probe_at = now + self.cfg.widget_probe_minutes * 60
        self._save_mode()

    def _widgets_allowed(self, now: float) -> None:
        """sendWidget прошёл. Если был текстовый режим — выйти из него: текстовые дашборды у всех
        пересоздаются виджетами при ближайшей сверке (live_epoch=None)."""
        if self.widgets_off is None:
            return
        log.info("Telegram снова разрешает виджеты (текстом с %s) — текстовые дашборды заменяются виджетами",
                 time.strftime("%d.%m %H:%M", time.localtime(self.widgets_off)))
        self.widgets_off, self.probe_at, self._unconfirmed = None, 0.0, False
        self._save_mode()
        for sub in self.store.subscribers():
            if sub.live_kind == "text" and sub.live_msg_id is not None:
                self.store.set_fields(sub.chat_id, live_epoch=None)
        if self._wake is not None:
            self._wake.set()

    def _probe_candidate(self, now: float) -> Optional[Subscriber]:
        """Чат для плановой пробы: текстовый дашборд, который сверка сейчас не пересоздаёт (иначе проба
        пройдёт при пересоздании). Сначала владелец."""
        epoch = self.engine.epoch
        subs = sorted(self.store.subscribers(), key=lambda x: x.chat_id != self.cfg.owner)
        for sub in subs:
            if (sub.live_kind == "text" and sub.live_msg_id is not None and self.allowed(sub.chat_id)
                    and now >= self._retry_at.get(sub.chat_id, 0.0) and sub.notified_epoch == epoch
                    and sub.live_epoch == epoch and sub.kb_sent is not None
                    and (sub.notice_sent or 0) <= (sub.live_notice or 0) and sub.notice_seen is not None
                    and not self.store.notices_after(sub.notice_seen)):
                return sub
        return None

    async def _probe(self, now: float) -> None:
        """Плановая проба в текстовом режиме: новый виджет-дашборд шлётся под текущим текстовым,
        и только если Telegram его принял, текст удаляется. Отказ в чате ничего не меняет."""
        if self.widgets_off is None or now < self.probe_at:
            return
        sub = self._probe_candidate(now)
        if sub is None:
            # кандидата нет: либо пересоздание с пробой случится при сверке, либо текстовых дашбордов нет
            if not any(x.live_kind == "text" and x.live_msg_id is not None for x in self.store.subscribers()):
                self.probe_at = now + self.cfg.widget_probe_minutes * 60
                self._save_mode()
            return
        chat_id = sub.chat_id
        async with self._lock(chat_id):
            data = self.engine.chat_data(now)
            try:
                msg = await self.tg.send_widget(chat_id, self.html, chat_data=data, caption=texts.DASHBOARD_CAPTION,
                                                width=self.cfg.widget_width, height=self.cfg.widget_height,
                                                allow_queries=self.cfg.widget_queries, disable_notification=True)
            except TelegramError as exc:
                if exc.widgets_refused:
                    self._widgets_refused(now, exc)
                elif exc.blocked:
                    self._drop(chat_id)
                elif exc.code == 429 or exc.code >= 500:
                    log.info("проба виджетов: %s — повтор через %.0f с", exc, PROBE_NET_RETRY)
                    self.probe_at = now + PROBE_NET_RETRY
                else:
                    log.warning("проба виджетов: %s — следующая через %.0f мин", exc, self.cfg.widget_probe_minutes)
                    self.probe_at = now + self.cfg.widget_probe_minutes * 60
                    self._save_mode()
                return
            except NetworkError as exc:
                log.info("проба виджетов: %s — повтор через %.0f с", exc, PROBE_NET_RETRY)
                self.probe_at = now + PROBE_NET_RETRY
                return
            old = sub.live_msg_id
            self._widgets_allowed(now)
            sub = self.store.subscriber(chat_id)
            self.store.set_live(chat_id, msg["message_id"], "widget", self.engine.epoch, data, now,
                                widget=self.widget_hash, notice=sub.notice_sent)
            log.info("чат %s: дашборд widget #%s (проба), текстовый #%s удаляется", chat_id, msg["message_id"], old)
            await self._delete_quiet(chat_id, old)

    def _drop(self, chat_id: int) -> None:
        log.warning("чат %s недоступен (бот заблокирован?) — отписываю", chat_id)
        self.store.unsubscribe(chat_id)

    # ================================================================= виджет «Инвертор»
    def _inverter_snapshot(self, now: float, until: float):
        return self.engine.inverter_snapshot(now, int(until))

    async def _send_inverter(self, chat_id: int, now: float) -> None:
        """Кнопка «Инвертор»: удалить прошлый, прислать новый беззвучно (виджет, а в текстовом режиме —
        сообщение); дальше его правит `_inverter_live` до `z`. Дашборд после этого пересоздаёт вызывающий."""
        sub = self.store.subscriber(chat_id)
        if sub.inv_msg_id is not None:
            await self._delete_quiet(chat_id, sub.inv_msg_id)
            self.store.set_fields(chat_id, inv_msg_id=None, inv_until=None, inv_data=None, inv_at=None, inv_kind=None)
        until = now + self.cfg.inverter_minutes * 60
        data = encode_inverter(dict(self._inverter_snapshot(now, until), r=1))
        msg, kind = None, "text"
        if self._try_widgets(now):
            try:
                msg = await self.tg.send_widget(chat_id, self.inverter_html, chat_data=data,
                                                caption=texts.INVERTER_CAPTION, disable_notification=True)
            except TelegramError as exc:
                if exc.blocked:
                    self._drop(chat_id)
                    return
                if exc.code == 429 or exc.code >= 500:
                    raise
                if exc.widgets_refused:
                    self._widgets_refused(now, exc)
                else:
                    log.warning("чат %s: виджет «Инвертор» не отправлен (%s) — отправляю текстом", chat_id, exc)
            else:
                kind = "widget"
                self._widgets_allowed(now)
        if msg is None:
            # без reply_markup, как и текстовый дашборд: клавиатура в чате и так постоянная
            try:
                msg = await self.tg.send_message(chat_id, texts.inverter_text(parse(data), now, self.tz),
                                                 disable_notification=True, parse_mode="HTML")
            except TelegramError as exc:
                if exc.blocked:
                    self._drop(chat_id)
                    return
                raise
        self.store.set_fields(chat_id, inv_msg_id=msg["message_id"], inv_until=until, inv_data=data, inv_at=now,
                              inv_kind=kind, live_epoch=None)  # дашборд пересоздаётся ниже
        if hasattr(self.source, "day_every"):
            self.source.day_every = DAY_EVERY_LIVE
        log.info("чат %s: «Инвертор» %s #%s до %s", chat_id, kind, msg["message_id"],
                 time.strftime("%H:%M", time.localtime(until)))

    async def _inverter_live(self, chat_id: int, now: float) -> None:
        """Живые правки «Инвертора» до `z`: при изменении — не чаще inverter_interval, `t` — не реже
        fresh_interval (пока связь есть). Текстовый после `z` правится последний раз: «Снимок на HH:MM»."""
        sub = self.store.subscriber(chat_id)
        if sub is None or sub.inv_msg_id is None or not sub.inv_until:
            return
        text_kind = sub.inv_kind == "text"
        final = now > sub.inv_until
        if final and not (text_kind and (sub.inv_at or 0.0) <= sub.inv_until):
            return
        snap = self._inverter_snapshot(now, sub.inv_until)
        if snap is None:
            return
        prev = parse(sub.inv_data) if sub.inv_data else {}
        age = now - (sub.inv_at or 0.0)
        if text_kind:
            changed = (not prev or texts.inverter_text(snap, now, self.tz, footer=False)
                       != texts.inverter_text(prev, sub.inv_at or now, self.tz, footer=False))
        else:
            changed = ({k: v for k, v in snap.items() if k != "t"}
                       != {k: v for k, v in prev.items() if k not in ("r", "t")})
        t_moved = prev.get("t") != snap["t"]
        if not (final or (changed and age >= self.cfg.inverter_interval)
                or (t_moved and age >= self.cfg.fresh_interval)):
            return
        data = encode_inverter(dict(snap, r=int(prev.get("r", 0)) + 1))
        try:
            if text_kind:
                await self.tg.edit_message_text(chat_id, sub.inv_msg_id, texts.inverter_text(parse(data), now, self.tz),
                                                parse_mode="HTML")
            else:
                await self.tg.edit_widget_data(chat_id, sub.inv_msg_id, data)
        except TelegramError as exc:
            if exc.gone:
                log.info("чат %s: «Инвертор» удалён пользователем", chat_id)
                self.store.set_fields(chat_id, inv_msg_id=None, inv_until=None, inv_data=None, inv_at=None,
                                      inv_kind=None)
                return
            if exc.blocked:
                self._drop(chat_id)
                return
            raise
        self.store.set_fields(chat_id, inv_data=data, inv_at=now)

    # ================================================================= команды и кнопки
    def allowed(self, user_id: int) -> bool:
        """Владелец — всегда; решение владельца в базе (`access`) важнее ALLOWED_IDS из .env."""
        if user_id is None:
            return False
        if self.cfg.owner and user_id == self.cfg.owner:
            return True
        acc = self.store.access(user_id)
        if acc is not None and acc.status == "allowed":
            return True
        if acc is not None and acc.status in ("denied", "revoked"):
            return False
        return user_id in self.cfg.allowed_ids

    async def _say(self, chat_id: int, text: str, html: bool = False) -> Optional[Dict]:
        """Беззвучное сообщение с клавиатурой. None — чат недоступен (подписчик снят)."""
        try:
            return await self.tg.send_message(chat_id, text, disable_notification=True,
                                              parse_mode="HTML" if html else None, reply_markup=texts.KEYBOARD)
        except TelegramError as exc:
            if exc.blocked:
                self._drop(chat_id)
                return None
            raise

    async def _delete_quiet(self, chat_id: int, message_id: Optional[int]) -> None:
        """Удалить сообщение, если получится (старше 48 ч, уже удалено — не важно)."""
        if message_id is None:
            return
        try:
            await self.tg.delete_message(chat_id, message_id)
        except (NetworkError, TelegramError) as exc:
            log.debug("чат %s: сообщение #%s не удалено: %s", chat_id, message_id, exc)

    async def handle_update(self, upd: Dict) -> None:
        now = self.clock()
        if "widget_query" in upd:
            await self._widget_query(upd["widget_query"], now)
            return
        if "callback_query" in upd:
            await self._callback(upd["callback_query"], now)
            return
        msg = upd.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            return
        chat_id = msg["chat"]["id"]
        user = msg.get("from") or {}
        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if not self.allowed(user.get("id", chat_id)):
            await self._stranger(chat_id, user, cmd, now)
            return
        button = {texts.BTN_DASHBOARD: "/status", texts.BTN_SCHEDULE: "schedule",
                  texts.BTN_INVERTER: "inverter"}.get(text)
        async with self._lock(chat_id):
            if button or cmd == "/status":
                # нажатие кнопки (и /status) — служебное, в чате его не оставляем
                await self._delete_quiet(chat_id, msg.get("message_id"))
            await self._command(chat_id, button or cmd, user, now)

    async def _command(self, chat_id: int, cmd: str, user: Dict, now: float) -> None:
        if cmd == "/start":
            name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            new = self.store.subscribe(chat_id, name, now)
            await self.tg.send_message(chat_id, texts.START_OK if new else texts.START_AGAIN,
                                       reply_markup=texts.KEYBOARD)
            self.store.set_fields(chat_id, kb_sent=1)
            await self._send_current(chat_id, now)
            return
        if cmd == "/stop":
            sub = self.store.subscriber(chat_id)
            if sub is not None:
                await self._delete_quiet(chat_id, sub.live_msg_id)
                await self._delete_quiet(chat_id, sub.inv_msg_id)
                self.store.unsubscribe(chat_id)
            await self.tg.send_message(chat_id, texts.STOP_OK, reply_markup={"remove_keyboard": True})
            return
        owner = bool(self.cfg.owner) and user.get("id", chat_id) == self.cfg.owner
        if cmd == "/users" and owner:
            await self._send_users(chat_id, now)
            return
        if cmd not in ("/status", "schedule", "inverter"):
            await self.tg.send_message(chat_id, texts.HELP_OWNER if owner else texts.HELP,
                                       reply_markup=texts.KEYBOARD)
            return
        sub = self.store.subscriber(chat_id)
        if sub is None or not sub.active:
            await self.tg.send_message(chat_id, texts.NOT_SUBSCRIBED)
            return
        if cmd == "schedule":
            await self._send_schedule(chat_id, now)
            if not self.engine.ready:
                return  # дашборда ещё нет — пересоздавать нечего
        elif cmd == "inverter":
            if not self.engine.ready or self.inverter_html is None:
                await self._say(chat_id, texts.NOT_READY)
                return
            self._retry_at.pop(chat_id, None)
            await self._send_inverter(chat_id, now)
        await self._send_current(chat_id, now)

    async def _send_schedule(self, chat_id: int, now: float) -> None:
        """Кнопка «График»: сегодня и завтра в формате сообщения «График», беззвучно; прошлый такой
        ответ удаляется (автоматические сообщения об изменениях остаются)."""
        sub = self.store.subscriber(chat_id)
        await self._delete_quiet(chat_id, sub.ask_msg_id)
        if self.engine.schedule is None:
            text, html = texts.SCHEDULE_NOT_LOADED, False
        else:
            text, html = texts.current_schedule(self.engine.schedule_days(now)), True
        msg = await self._say(chat_id, text, html=html)
        self.store.set_fields(chat_id, ask_msg_id=msg["message_id"] if msg else None, live_epoch=None)

    async def _send_current(self, chat_id: int, now: float) -> None:
        """Пересоздать дашборд внизу чата (под замком чата)."""
        if not self.engine.ready:
            await self.tg.send_message(chat_id, texts.NOT_READY)
            return
        self._retry_at.pop(chat_id, None)
        await self.sync(chat_id, now, force=True)

    # ================================================================= доступ
    @staticmethod
    def _full_name(user: Dict) -> str:
        return " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()

    async def _stranger(self, chat_id: int, user: Dict, cmd: str, now: float) -> None:
        """Сообщение от того, у кого нет доступа: запрос владельцу (один незакрытый на человека,
        не больше access_per_hour новых в час). Отклонённые и убранные — молча."""
        uid = user.get("id", chat_id)
        owner = self.cfg.owner
        if not owner:
            # владелец не задан (ALLOWED_IDS пуст) — как раньше: отказ с id, чтобы его вписать в .env
            log.info("отказ: id=%s @%s %s", uid, user.get("username", ""), user.get("first_name", ""))
            await self.tg.send_message(chat_id, texts.DENIED.format(id=uid))
            return
        acc = self.store.access(uid)
        if acc is not None and acc.status in ("pending", "denied", "revoked"):
            log.debug("id=%s (%s): сообщение без доступа проигнорировано", uid, acc.status)
            return
        if cmd == "/stop":
            return
        if self.store.requests_since(now - HOUR) >= self.cfg.access_per_hour:
            log.info("запрос доступа от id=%s пропущен: уже %d за час", uid, self.cfg.access_per_hour)
            return
        name, username = self._full_name(user), user.get("username") or ""
        self.store.request_access(uid, name, username, now)
        log.info("запрос доступа: id=%s @%s %s", uid, username, name)
        async with self._lock(owner):
            try:
                m = await self.tg.send_message(owner, texts.access_request(name, username, uid),
                                               reply_markup=self._request_kb(uid))
            except (NetworkError, TelegramError) as exc:
                log.warning("запрос доступа от id=%s не дошёл до владельца: %s", uid, exc)
                self.store.set_access(uid, "failed", now)
                return
            self.store.set_access_msg(uid, m["message_id"])
            await self._dashboard_below(owner, now)
        try:
            await self.tg.send_message(chat_id, texts.ACCESS_REQUESTED)
        except (NetworkError, TelegramError) as exc:
            log.info("id=%s: ответ о запросе не доставлен: %s", uid, exc)

    async def _dashboard_below(self, chat_id: int, now: float) -> None:
        """В чат подписчика пришло новое сообщение — дашборд пересоздать ниже (под замком чата).
        Если сейчас не вышло, live_epoch=None — пересоздаст ближайшая сверка."""
        sub = self.store.subscriber(chat_id)
        if sub is None or not sub.active:
            return
        self.store.set_fields(chat_id, live_epoch=None)
        if not self.engine.ready:
            return
        self._retry_at.pop(chat_id, None)
        try:
            await self.sync(chat_id, now)
        except (NetworkError, TelegramError) as exc:
            log.warning("чат %s: дашборд не пересоздан: %s — повтор через %.0f с", chat_id, exc, RETRY_DELAY)
            self._retry_at[chat_id] = now + RETRY_DELAY

    @staticmethod
    def _request_kb(uid: int) -> Dict:
        return {"inline_keyboard": [[{"text": texts.BTN_ALLOW, "callback_data": "ok:%d" % uid},
                                     {"text": texts.BTN_REJECT, "callback_data": "no:%d" % uid}]]}

    async def _answer(self, query_id: Optional[str], text: Optional[str] = None) -> None:
        if not query_id:
            return
        try:
            await self.tg.answer_callback_query(query_id, text)
        except (NetworkError, TelegramError) as exc:
            log.info("answerCallbackQuery: %s", exc)

    async def _edit_text(self, chat_id: int, message_id: Optional[int], text: str,
                         markup: Optional[Dict] = None) -> None:
        if message_id is None:
            return
        try:
            await self.tg.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        except (NetworkError, TelegramError) as exc:
            log.info("чат %s: сообщение #%s не исправлено: %s", chat_id, message_id, exc)

    def _name_of(self, uid: int) -> str:
        acc = self.store.access(uid)
        if acc is not None and acc.name:
            return acc.name
        sub = self.store.subscriber(uid)
        return sub.name if sub is not None and sub.name else ""

    async def _callback(self, q: Dict, now: float) -> None:
        """Inline-кнопки: запрос доступа («ok»/«no») и список /users («u-» убрать, «u+» вернуть).
        Нажимать может только владелец."""
        qid = q.get("id")
        uid = (q.get("from") or {}).get("id")
        owner = self.cfg.owner
        if not owner or uid != owner:
            log.info("кнопка доступа от id=%s — не владелец, отказ", uid)
            await self._answer(qid, texts.OWNER_ONLY)
            return
        action, _, target = (q.get("data") or "").partition(":")
        msg = q.get("message") or {}
        msg_id = msg.get("message_id")
        try:
            target = int(target)
        except ValueError:
            await self._answer(qid)
            return
        if action in ("ok", "no"):
            acc = self.store.access(target)
            if acc is None or acc.status != "pending":
                await self._answer(qid, texts.ALREADY_DECIDED)
                if acc is not None and acc.status in ("allowed", "denied", "revoked"):
                    done = texts.access_granted if acc.status == "allowed" else texts.access_rejected
                    await self._edit_text(owner, msg_id, done(acc.name))
                return
            if action == "ok":
                await self._answer(qid, texts.access_granted(acc.name))
                await self._edit_text(owner, msg_id, texts.access_granted(acc.name))
                await self._grant(target, now)
            else:
                self.store.set_access(target, "denied", now)
                log.info("доступ отклонён: id=%s %s", target, acc.name)
                await self._answer(qid, texts.access_rejected(acc.name))
                await self._edit_text(owner, msg_id, texts.access_rejected(acc.name))
                try:
                    await self.tg.send_message(target, texts.ACCESS_DENIED)
                except (NetworkError, TelegramError) as exc:
                    log.info("id=%s: «доступ не выдан» не доставлено: %s", target, exc)
            return
        if action in ("u-", "u+"):
            name = self._name_of(target)
            if action == "u-":
                if target == owner:
                    await self._answer(qid)
                    return
                await self._revoke(target, now)
                await self._answer(qid, texts.access_removed(name))
            else:
                acc = self.store.access(target)
                if not self.allowed(target):
                    await self._grant(target, now)
                if acc is not None and acc.status == "pending":  # у запроса убрать кнопки
                    await self._edit_text(owner, acc.owner_msg_id, texts.access_granted(name))
                await self._answer(qid, texts.access_granted(name))
            text, markup = self._users_view()
            await self._edit_text(owner, msg_id, text, markup)
            return
        await self._answer(qid)

    async def _grant(self, uid: int, now: float) -> None:
        """Доступ выдан: подписка, клавиатура и дашборд сразу — дальше как у всех."""
        self.store.set_access(uid, "allowed", now)
        name = self._name_of(uid)
        log.info("доступ выдан: id=%s %s", uid, name)
        async with self._lock(uid):
            self.store.subscribe(uid, name, now)
            try:
                await self.tg.send_message(uid, texts.ACCESS_GRANTED, reply_markup=texts.KEYBOARD)
            except TelegramError as exc:
                if exc.blocked:
                    self._drop(uid)
                    return
                log.warning("чат %s: приветствие не доставлено: %s — кнопки и дашборд пришлёт сверка", uid, exc)
                return
            except NetworkError as exc:
                log.warning("чат %s: приветствие не доставлено: %s — кнопки и дашборд пришлёт сверка", uid, exc)
                return
            self.store.set_fields(uid, kb_sent=1)
            if not self.engine.ready:
                return  # дашборд пришлёт сверка, когда появятся данные
            self._retry_at.pop(uid, None)
            try:
                await self.sync(uid, now, force=True)
            except (NetworkError, TelegramError) as exc:
                log.warning("чат %s: дашборд не отправлен: %s — повтор через %.0f с", uid, exc, RETRY_DELAY)
                self._retry_at[uid] = now + RETRY_DELAY

    async def _revoke(self, uid: int, now: float) -> None:
        """Доступ убран: сообщения больше не идут, дашборд и виджет «Инвертор» удаляются (если получится)."""
        name = self._name_of(uid)
        self.store.set_access(uid, "revoked", now, name=name or None)
        log.info("доступ убран: id=%s %s", uid, name)
        async with self._lock(uid):
            sub = self.store.subscriber(uid)
            if sub is not None:
                await self._delete_quiet(uid, sub.live_msg_id)
                await self._delete_quiet(uid, sub.inv_msg_id)
                self.store.unsubscribe(uid)
        self._retry_at.pop(uid, None)

    def _users_view(self):
        """Текст и inline-клавиатура для /users."""
        owner = self.cfg.owner
        allowed_ids = [owner] + sorted(i for i in self.cfg.allowed_ids if i != owner)
        allowed_ids += [a.user_id for a in self.store.accesses("allowed") if a.user_id not in allowed_ids]
        allowed, rows = [], []
        for uid in allowed_ids:
            if not self.allowed(uid):
                continue
            acc = self.store.access(uid)
            name, username = self._name_of(uid), acc.username if acc is not None else ""
            allowed.append((name, username, uid, uid == owner))
            if uid != owner:
                rows.append([{"text": "%s · %s" % (texts.BTN_REMOVE, (name or str(uid))[:32]),
                              "callback_data": "u-:%d" % uid}])
        closed, pending = [], []
        for a in self.store.accesses("pending", "denied", "revoked"):
            if a.user_id == owner:
                continue
            (pending if a.status == "pending" else closed).append((a.name, a.username, a.user_id))
            rows.append([{"text": "%s · %s" % (texts.BTN_ALLOW, (a.name or str(a.user_id))[:32]),
                          "callback_data": "u+:%d" % a.user_id}])
        return texts.users_list(allowed, closed, pending), ({"inline_keyboard": rows} if rows else None)

    async def _send_users(self, chat_id: int, now: float) -> None:
        text, markup = self._users_view()
        await self.tg.send_message(chat_id, text, disable_notification=True, reply_markup=markup)
        await self._dashboard_below(chat_id, now)

    async def _widget_query(self, q: Dict, now: float) -> None:
        """Виджет просит свежее состояние: отвечаем текущим chat_data (если это живое сообщение)."""
        data = ""
        chat_id = (q.get("chat") or {}).get("id")
        sub = self.store.subscriber(chat_id) if chat_id is not None else None
        if sub and sub.active and sub.live_msg_id == q.get("message_id"):
            data = self.engine.chat_data(now) or ""
        try:
            await self.tg.answer_widget_query(q["id"], data)
        except TelegramError as exc:
            log.info("answerWidgetQuery: %s", exc)

    # ================================================================= циклы
    async def tick(self, now: Optional[float] = None) -> None:
        """Один шаг без ожиданий (демо и тесты)."""
        now = self.clock() if now is None else now
        await self.poll_once()
        await self.refresh_schedule(now)
        await self.sync_all(now)

    async def _poll_loop(self) -> None:
        while True:
            started = self.clock()
            await self.poll_once()
            await asyncio.sleep(max(1.0, self.cfg.poll_interval - (self.clock() - started)))

    async def _schedule_loop(self) -> None:
        while True:
            await self.refresh_schedule(self.clock())
            self._wake.set()
            await asyncio.sleep(max(5.0, self._next_schedule - self.clock()))

    async def _publish_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self.sync_all(self.clock())

    async def _updates_loop(self) -> None:
        offset = self.store.get("offset")
        allowed = ["message", "widget_query", "callback_query"]
        while True:
            try:
                updates = await self.tg.get_updates(offset, timeout=25, allowed_updates=allowed)
            except (NetworkError, TelegramError) as exc:
                log.warning("getUpdates: %s", exc)
                await asyncio.sleep(5.0)
                continue
            if not updates:
                await asyncio.sleep(0.5)  # защита от горячего цикла, если сервер ответил сразу
            for upd in updates:
                offset = upd["update_id"] + 1
                self.store.set("offset", offset)
                try:
                    await self.handle_update(upd)
                except Exception:
                    log.exception("ошибка обработки апдейта %s", upd.get("update_id"))

    async def run(self) -> None:
        self._wake = asyncio.Event()
        me = await self.tg.get_me()
        if self.widgets_off is None:
            mode = "виджеты"
        elif self._unconfirmed:
            mode = "текст (дашборды уже текстовые — сразу проверяю, разрешены ли виджеты)"
        else:
            mode = "текст (Telegram не разрешает виджеты с %s, проверка в %s)" % (
                time.strftime("%d.%m %H:%M", time.localtime(self.widgets_off)),
                time.strftime("%H:%M", time.localtime(max(self.probe_at, self.clock()))))
        log.info("бот @%s запущен; подписчиков: %d; виджет %s (%s), инвертор %s; владелец %s, выдано доступов: %d; "
                 "режим: %s", me.get("username"), len(self.store.subscribers()), self.cfg.widget_file.name,
                 self.widget_hash, "есть" if self.inverter_html else "нет", self.cfg.owner or "не задан",
                 len(self.store.accesses("allowed")), mode)
        tasks = [asyncio.ensure_future(c) for c in (self._poll_loop(), self._schedule_loop(),
                                                    self._publish_loop(), self._updates_loop())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                t.result()
        finally:
            for t in tasks:
                t.cancel()
            if self._pool is not None:
                self._pool.shutdown(wait=False)
            self.source.close()
