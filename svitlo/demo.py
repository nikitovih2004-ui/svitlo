"""`python -m svitlo demo`: ускоренный сценарий на FakeSource без Telegram.

Виртуальное время, шаг 10 с. Показывает: заряд с сетью, ложный провал напряжения
(одно чтение — события нет), отключение (текст события + новый дашборд), потерю связи
с логгером (события нет, правок нет), перезапуск процесса (событий нет, дашборд тот же),
включение и дозаряд до 100%, в конце — отчёт о точности прогноза разряда.
Вызовы Bot API печатаются вместо отправки. Ёмкость — BATTERY_KWH или 10.24 кВт·ч сценария.
"""

import dataclasses
import json
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import texts
from .accuracy import report
from .bot import Bot
from .schedule import contract_view, parse_yasno
from .source import FakeSource, Segment
from .state import Engine, intervals_text
from .storage import Store

DEMO_CHAT = 1001
STEP = 10


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


class PrintTelegram:
    """Подставной клиент: печатает вызовы вместо отправки."""

    def __init__(self, clock, tz, verbose_edits: bool = False):
        self.clock, self.tz, self.verbose_edits = clock, tz, verbose_edits
        self.msg_id = 100
        self.calls = {}

    def _t(self) -> str:
        return datetime.fromtimestamp(self.clock(), self.tz).strftime("%H:%M:%S")

    def _count(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1

    async def send_widget(self, chat_id, html, chat_data=None, caption=None, **kw):
        self._count("sendWidget")
        self.msg_id += 1
        print("%s  ► sendWidget #%d  «%s»%s\n            chat_data=%s" % (
            self._t(), self.msg_id, caption, " (беззвучно)" if kw.get("disable_notification") else "", chat_data))
        return {"message_id": self.msg_id}

    async def edit_widget_data(self, chat_id, message_id, chat_data):
        self._count("editMessageWidgetData")
        st = json.loads(chat_data)
        if self.verbose_edits:
            print("%s    edit #%d r=%d c=%d%% b=%d e=%s" % (self._t(), message_id, st["r"], st["c"], st["b"], st["e"]))
        return True

    async def delete_message(self, chat_id, message_id):
        self._count("deleteMessage")
        print("%s  ► deleteMessage #%d" % (self._t(), message_id))
        return True

    async def send_message(self, chat_id, text, **kw):
        self._count("sendMessage")
        self.msg_id += 1
        print("%s  ► sendMessage%s «%s»" % (self._t(), " (беззвучно)" if kw.get("disable_notification") else "",
                                           text))
        return {"message_id": self.msg_id}

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self._count("editMessageText")
        if self.verbose_edits:
            print("%s    editText #%d «%s»" % (self._t(), message_id, text.split("\n", 1)[0]))
        return True

    async def answer_widget_query(self, *a, **kw):
        return True


def demo_schedule(day):
    tomorrow = day + timedelta(days=1)
    payload = {"3.1": {
        "today": {"date": "%sT00:00:00+02:00" % day.isoformat(), "status": "ScheduleApplies",
                  "slots": [{"start": 0, "end": 60, "type": "Definite"},
                            {"start": 60, "end": 810, "type": "NotPlanned"},
                            {"start": 810, "end": 1050, "type": "Definite"},
                            {"start": 1050, "end": 1320, "type": "NotPlanned"},
                            {"start": 1320, "end": 1440, "type": "Definite"}]},
        "tomorrow": {"date": "%sT00:00:00+02:00" % tomorrow.isoformat(), "status": "ScheduleApplies",
                     "slots": [{"start": 0, "end": 120, "type": "Definite"},
                               {"start": 120, "end": 1440, "type": "NotPlanned"}]},
    }}
    return parse_yasno(payload, "3.1")


def scenario():
    return [
        Segment(60 * 60, grid=True, load_w=500, charge_w=3000),        # 12:00 заряд
        Segment(STEP, grid=True, load_w=500, grid_v=60.0),              # 13:00 провал на одно чтение
        Segment(60 * 60 - STEP, grid=True, load_w=500, charge_w=3000),  # дозаряд, уходит в замедление
        Segment(60 * 60, grid=False, load_w=450),                       # 14:00 отключение
        Segment(4 * 60, grid=False, load_w=450, offline=True),          # нет связи с логгером
        Segment(56 * 60, grid=False, load_w=600),
        Segment(80 * 60, grid=True, load_w=400, charge_w=3000),         # 16:00 свет дали
    ]


async def demo(cfg) -> None:
    tz = ZoneInfo(cfg.tz)
    start_day = datetime.now(tz).date()
    t0 = datetime(start_day.year, start_day.month, start_day.day, 12, 0, tzinfo=tz).timestamp()
    clock = Clock(t0)
    tmp = tempfile.mkdtemp(prefix="svitlo-demo-")
    db = os.path.join(tmp, "demo.sqlite3")
    kwh = cfg.battery_kwh or 10.24
    source = FakeSource(scenario(), clock, soc=62.0, capacity_wh=kwh * 1000)
    tg = PrintTelegram(clock, tz)
    sched = demo_schedule(start_day)

    async def fetch():
        return sched

    demo_cfg = dataclasses.replace(cfg, allowed_ids=frozenset({DEMO_CHAT}), owner_id=DEMO_CHAT)

    def make_bot():
        store = Store(db)
        engine = Engine(store, tz, cfg.grid_on_v, kwh, cfg.stale_after)
        return store, engine, Bot(demo_cfg, store, engine, source, tg, "<!-- demo -->", fetch, tz,
                                  clock=clock, threaded_source=False)

    store, engine, bot = make_bot()
    store.subscribe(DEMO_CHAT, "demo", clock())
    print("Демо: виртуальное время с 12:00, шаг %d с; график: %s" % (
        STEP, intervals_text(contract_view(sched, start_day)[0])))
    print("%-8s  %-4s %-4s %-6s %-6s %-7s  %s" % ("время", "сеть", "SOC", "бат,Вт", "нагр", "прогноз", "ревизия"))
    total = sum(s.duration for s in scenario())
    restarted = False
    steps = int(total // STEP)
    for i in range(steps + 1):
        now = clock()
        if not restarted and now >= t0 + 2.5 * 3600:
            restarted = True
            store.close()
            print("%s  ── перезапуск процесса (состояние из sqlite) ──" %
                  datetime.fromtimestamp(now, tz).strftime("%H:%M:%S"))
            store, engine, bot = make_bot()
        await bot.tick(now)
        if i % 60 == 0 and engine.ready:
            st = engine.snapshot(now)
            e = datetime.fromtimestamp(st["e"], tz).strftime("%H:%M") if st["e"] else "—"
            stale = " (нет связи)" if engine.is_stale(now) else ""
            print("%s  %-4s %3d%% %6d %6d %7s  r=%d%s" % (
                datetime.fromtimestamp(now, tz).strftime("%H:%M:%S"), "есть" if st["g"] else "нет",
                st["c"], st["b"], st["l"], e, engine.rev, stale))
        clock.t += STEP
    print()
    print("События в базе:")
    for ts, grid, since, prev, soc in reversed(store.events()):
        print("  %s  сеть %s, прошлое состояние длилось %s, SOC %s%%" % (
            datetime.fromtimestamp(since, tz).strftime("%H:%M:%S"), "есть" if grid else "нет",
            texts.duration(prev) if prev else "неизвестно", soc))
    print("Кривая заряда (корзина: сек/1%%, замеров): %s" % {
        "%d–%d%%" % (b * 5, b * 5 + 5): (round(s), n) for b, (s, n) in sorted(store.load_curve().items())})
    print("Вызовы Bot API: %s" % tg.calls)
    print()
    print("Точность прогноза разряда (python -m svitlo accuracy):")
    print(report(store, tz))
    store.close()
