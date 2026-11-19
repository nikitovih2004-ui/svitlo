"""Сквозные тесты бота: FakeSource + подставной Telegram, виртуальное время."""

import asyncio
import json
from datetime import datetime

import pytest

from svitlo import texts
from svitlo.bot import Bot
from svitlo.config import Config
from svitlo.source import FakeSource, Segment
from svitlo.state import Engine
from svitlo.storage import Store
from svitlo.telegram import NetworkError, TelegramError
from tests.conftest import KYIV, STUB_WIDGET

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=KYIV).timestamp()
OWNER = 111
STRANGER = 222


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Crash(BaseException):
    """Имитация падения процесса: не ловится обработчиками бота."""


class FakeTG:
    """Подставной Bot API. `fail[method]` — исключение для следующего вызова метода;
    `crash_after[method]` — вызов выполняется, затем процесс «падает» (ответ не записан)."""

    def __init__(self):
        self.log = []
        self.next_id = 500
        self.fail = {}
        self.crash_after = set()
        self.alive = set()    # виджеты-дашборды, которые сейчас видны в чате
        self.existing = set()  # все сообщения бота, которые не удалены

    def _maybe_fail(self, method):
        exc = self.fail.pop(method, None)
        if exc is not None:
            raise exc

    def _maybe_crash(self, method):
        if method in self.crash_after:
            self.crash_after.discard(method)
            raise Crash(method)

    async def send_widget(self, chat_id, html, chat_data=None, caption=None, disable_notification=False, **kw):
        self._maybe_fail("sendWidget")
        self.next_id += 1
        self.alive.add(self.next_id)
        self.existing.add(self.next_id)
        self.log.append(("sendWidget", chat_id, self.next_id, json.loads(chat_data), caption, disable_notification))
        self._maybe_crash("sendWidget")
        return {"message_id": self.next_id}

    async def edit_widget_data(self, chat_id, message_id, chat_data):
        self._maybe_fail("edit")
        self.log.append(("edit", chat_id, message_id, json.loads(chat_data)))
        return True

    async def delete_message(self, chat_id, message_id):
        self._maybe_fail("deleteMessage")
        if message_id not in self.existing:
            raise TelegramError("deleteMessage", 400, "Bad Request: message to delete not found")
        self.alive.discard(message_id)
        self.existing.discard(message_id)
        self.log.append(("delete", chat_id, message_id))
        self._maybe_crash("deleteMessage")
        return True

    async def send_message(self, chat_id, text, disable_notification=False, **kw):
        self._maybe_fail("sendMessage")
        self.next_id += 1
        self.existing.add(self.next_id)
        self.log.append(("sendMessage", chat_id, self.next_id, text, disable_notification, kw))
        self._maybe_crash("sendMessage")
        return {"message_id": self.next_id}

    async def answer_widget_query(self, qid, data=None):
        self.log.append(("answer", qid, data))
        return True

    def of(self, kind):
        return [x for x in self.log if x[0] == kind]

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, parse_mode=None):
        if parse_mode == "HTML":  # правка текстового дашборда / «Инвертора»
            self._maybe_fail("editHTML")
            if message_id not in self.existing:
                raise TelegramError("editMessageText", 400, "Bad Request: message to edit not found")
            self.log.append(("editHTML", chat_id, message_id, text))
            return True
        self._maybe_fail("editText")
        self.log.append(("editText", chat_id, message_id, text, reply_markup))
        return True

    async def answer_callback_query(self, qid, text=None):
        self.log.append(("answerCb", qid, text))
        return True

    def to(self, chat_id):
        """Тексты, отправленные в чат chat_id."""
        return [m[3] for m in self.of("sendMessage") if m[1] == chat_id]

    def events(self, chat_id=None):
        return [m[3] for m in self.of("sendMessage")
                if m[3].startswith(("🟢 Свет ", "🔴 Свет ")) and chat_id in (None, m[1])]

    def chat_order(self, chat=None):
        """Что видно в чате сверху вниз (виджеты и тексты), без удалённых; chat — только этот чат."""
        out = []
        for x in self.log:
            if chat is not None and x[0] in ("sendWidget", "sendMessage") and x[1] != chat:
                continue
            if x[0] == "sendWidget" and x[2] in self.alive:
                out.append(("W", x[2]))
            elif x[0] == "sendMessage" and x[2] in self.existing:
                out.append(("T", x[3].split("\n")[0]))
        return out


def make_bot(tmp_path, clock, segments, tg=None, soc=80, **cfg_kw):
    cfg_kw.setdefault("battery_kwh", 10.24)
    cfg_kw.setdefault("allowed_ids", frozenset({OWNER}))
    cfg = Config(data_dir=str(tmp_path), **cfg_kw)
    store = Store(cfg.db_path)
    engine = Engine(store, KYIV, cfg.grid_on_v, cfg.battery_kwh, cfg.stale_after)
    src = segments if isinstance(segments, FakeSource) else FakeSource(segments, clock, soc=soc)
    tg = tg or FakeTG()
    bot = Bot(cfg, store, engine, src, tg, STUB_WIDGET.read_text(), None, KYIV, clock=clock,
              threaded_source=False)
    return bot, tg, src


def run_for(bot, clock, seconds, step=10):
    end = clock.t + seconds
    while clock.t < end:
        asyncio.run(bot.tick(clock.t))
        clock.t += step


def msg(user, text):
    return {"update_id": 1, "message": {"message_id": 1, "from": {"id": user, "first_name": "Н"},
                                        "chat": {"id": user, "type": "private"}, "text": text}}


def start(bot, clock, warmup=30):
    asyncio.run(bot.handle_update(msg(OWNER, "/start")))
    run_for(bot, clock, warmup)


def test_stranger_denied_with_id_when_no_owner(tmp_path):
    """Владелец не задан (ALLOWED_IDS пуст) — как раньше: отказ с id, без запросов доступа."""
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(600, grid=True)], allowed_ids=frozenset())
    asyncio.run(bot.handle_update(msg(STRANGER, "/start")))
    (m,) = tg.of("sendMessage")
    assert str(STRANGER) in m[3]
    assert bot.store.subscriber(STRANGER) is None


def test_full_cycle(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(1800, grid=True), Segment(10, grid=True, grid_v=30),
                                            Segment(1790, grid=True), Segment(3600, grid=False),
                                            Segment(300, grid=False, offline=True),
                                            Segment(3300, grid=False), Segment(1800, grid=True)])
    asyncio.run(bot.handle_update(msg(OWNER, "/start")))
    assert tg.of("sendMessage")[1][3].startswith("Ещё не знаю")  # до первых чтений
    run_for(bot, clock, 30)
    (first,) = tg.of("sendWidget")
    assert first[3]["g"] == 1 and first[4] == texts.DASHBOARD_CAPTION and first[5] is True  # беззвучно
    assert tg.events() == []                                   # первый запуск — не событие

    run_for(bot, clock, 3470)                                  # провал напряжения — не событие
    assert len(tg.of("sendWidget")) == 1 and tg.events() == []
    revs = [e[3]["r"] for e in tg.of("edit")]
    assert revs == sorted(revs) and len(set(revs)) == len(revs)  # ревизии растут

    run_for(bot, clock, 7200)                                  # отключение + потеря связи
    assert tg.events() == ["🔴 Свет пропал в 13:00."]             # длительность «был» неизвестна
    ev = [m for m in tg.of("sendMessage") if m[3].startswith("🔴 Свет")][0]
    assert ev[4] is False                                      # со звуком
    assert [d[2] for d in tg.of("delete")] == [first[2]]
    second = tg.of("sendWidget")[1]
    assert second[3]["g"] == 0 and second[5] is True
    assert tg.log.index(ev) < tg.log.index(tg.of("delete")[0]) < tg.log.index(second)

    run_for(bot, clock, 600)                                   # свет вернулся
    assert tg.events() == ["🔴 Свет пропал в 13:00.", "🟢 Свет появился в 15:00. Не было 2 ч 00 мин."]
    third = tg.of("sendWidget")[2]
    assert third[3]["g"] == 1 and third[3]["p"] == 7200
    assert tg.alive == {third[2]}                              # один дашборд
    assert tg.chat_order()[-1] == ("W", third[2])              # и он последний в чате


def test_live_edits_throttled(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=False, load_w=500)], soc=90)
    start(bot, clock)
    tg.log.clear()
    run_for(bot, clock, 1200)
    edits = tg.of("edit")
    assert edits, "живые обновления должны идти"
    times = [e[3]["t"] for e in edits]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 60 - 10                                # не чаще раза в минуту
    assert max(gaps) <= 120 + 10                               # и не реже раза в 2 минуты


def test_t_refreshed_every_two_minutes_while_online(tmp_path):
    """Ничего не меняется (полная батарея): правки только ради свежего `t`, раз в 2 минуты;
    без связи правок нет совсем — «нет связи» виджет покажет сам."""
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(900, grid=True, load_w=400),
                                            Segment(1800, grid=True, offline=True)], soc=100)
    start(bot, clock)
    tg.log.clear()
    run_for(bot, clock, 870)
    edits = tg.of("edit")
    assert 6 <= len(edits) <= 8
    ts = [e[3]["t"] for e in edits]
    assert all(115 <= b - a <= 135 for a, b in zip(ts, ts[1:]))
    n = len(tg.log)
    run_for(bot, clock, 300)       # связи нет: максимум одна правка — дослать последнее успешное чтение
    assert len(tg.log) - n <= 1
    n = len(tg.log)
    run_for(bot, clock, 1500)                                  # дальше — ни одной
    assert len(tg.log) == n


def test_restart_keeps_dashboard(tmp_path):
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(1800, grid=True), Segment(7200, grid=False, load_w=500)])
    start(bot, clock, 2400)
    live_id = tg.of("sendWidget")[-1][2]
    n_sends, n_events = len(tg.of("sendWidget")), len(tg.events())
    bot.store.close()

    clock.t += 120                                     # процесс лежал 2 минуты
    bot2, tg2, _ = make_bot(tmp_path, clock, src, tg=tg)
    run_for(bot2, clock, 600)
    assert len(tg.of("sendWidget")) == n_sends and len(tg.events()) == n_events
    later = [e for e in tg.of("edit") if e[2] == live_id]
    assert later and later[-1][3]["r"] > 0             # правится тот же дашборд


@pytest.mark.parametrize("method", ["sendMessage", "deleteMessage", "sendWidget"])
def test_network_failure_mid_chain(tmp_path, method):
    """Сбой сети на любом шаге цепочки «текст → удалить → новый дашборд»: после повтора
    ровно один текст события и ровно один дашборд, последний в чате."""
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(600, grid=True), Segment(3600, grid=False)])
    start(bot, clock, 560)
    old = tg.of("sendWidget")[0][2]
    tg.fail[method] = NetworkError("%s: ConnectError" % method)
    run_for(bot, clock, 120)
    run_for(bot, clock, 300)
    assert tg.events() == ["🔴 Свет пропал в 12:10."]
    assert [d[2] for d in tg.of("delete")] == [old]
    assert len(tg.of("sendWidget")) == 2 and len(tg.alive) == 1
    assert tg.chat_order()[-2:] == [("T", "🔴 Свет пропал в 12:10."), ("W", tg.of("sendWidget")[1][2])]


@pytest.mark.parametrize("crash", [("crash_before", "deleteMessage"), ("crash_before", "sendWidget"),
                                   ("crash_after", "deleteMessage")])
def test_restart_mid_chain(tmp_path, crash):
    """Процесс падает посреди цепочки (до вызова или сразу после успешного deleteMessage,
    ещё до записи в базу). После перезапуска — без дублей и без двух дашбордов."""
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(600, grid=True), Segment(3600, grid=False)])
    start(bot, clock, 560)
    where, method = crash
    if where == "crash_before":
        tg.fail[method] = Crash(method)
    else:
        tg.crash_after.add(method)
    with pytest.raises(Crash):
        run_for(bot, clock, 120)
    bot.store.close()

    clock.t += 30
    bot2, _, _ = make_bot(tmp_path, clock, src, tg=tg)
    run_for(bot2, clock, 300)
    assert tg.events() == ["🔴 Свет пропал в 12:10."]
    assert len(tg.of("delete")) == 1
    assert len(tg.alive) == 1 and tg.chat_order()[-1][0] == "W"


def test_status_recreates_silently(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=False)])
    start(bot, clock)
    first = tg.of("sendWidget")[0]
    asyncio.run(bot.handle_update(msg(OWNER, "hello")))   # пользователь что-то написал — дашборд уже не внизу
    clock.t += 100
    asyncio.run(bot.handle_update(msg(OWNER, "/status")))
    second = tg.of("sendWidget")[1]
    assert [d[2] for d in tg.of("delete")] == [first[2]]
    assert second[5] is True and second[3]["s"] == first[3]["s"]   # беззвучно, то же состояние
    assert tg.events() == []                                       # /status — не событие
    assert tg.alive == {second[2]}


def test_stop_deletes_dashboard(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=False)])
    start(bot, clock)
    asyncio.run(bot.handle_update(msg(OWNER, "/stop")))
    assert bot.store.subscribers() == [] and tg.alive == set()
    n = len(tg.log)
    run_for(bot, clock, 600)
    assert len(tg.log) == n
    asyncio.run(bot.handle_update(msg(OWNER, "/start")))           # вернулся — без «события»
    assert tg.events() == [] and len(tg.alive) == 1


def test_new_subscriber_gets_no_event(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(600, grid=True), Segment(3600, grid=False)])
    run_for(bot, clock, 900)                      # событие было, но подписчиков ещё не было
    asyncio.run(bot.handle_update(msg(OWNER, "/start")))
    run_for(bot, clock, 60)
    assert tg.events() == [] and len(tg.of("sendWidget")) == 1


def test_fallback_to_text_when_widget_fails(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(600, grid=True), Segment(1800, grid=False),
                                            Segment(1800, grid=True)])
    start(bot, clock, 560)
    tg.fail["sendWidget"] = TelegramError("sendWidget", 400, "Bad Request: method not supported")
    run_for(bot, clock, 60)
    fallback = [m for m in tg.of("sendMessage") if m[3].startswith("🔴 <b>Света нет</b>")]
    assert len(fallback) == 1 and "🔋" in fallback[0][3] and fallback[0][4] is True
    assert fallback[0][5]["parse_mode"] == "HTML"
    assert tg.events() == ["🔴 Свет пропал в 12:10."]
    assert bot.store.subscriber(OWNER).live_kind == "text"
    assert bot.widgets_off is None                 # это не отказ INPUT_MEDIA_WIDGET_NOT_ALLOWED — режим не меняется
    run_for(bot, clock, 300)                       # текст правится живым, как виджет
    assert [e for e in tg.of("editHTML") if e[2] == fallback[0][2]]
    run_for(bot, clock, 1800)                      # следующее событие: запасной текст удаляется
    assert [d[2] for d in tg.of("delete")][-1] == fallback[0][2]
    assert bot.store.subscriber(OWNER).live_kind == "widget"


def test_user_deleted_dashboard(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(900, grid=False), Segment(1800, grid=True)])
    start(bot, clock)
    tg.existing -= tg.alive
    tg.alive.clear()
    tg.fail["edit"] = TelegramError("editMessageWidgetData", 400, "Bad Request: message to edit not found")
    run_for(bot, clock, 300)
    assert bot.store.subscriber(OWNER).live_kind == "gone"
    n = len(tg.of("sendWidget"))
    run_for(bot, clock, 300)
    assert len(tg.of("sendWidget")) == n           # сам не пересоздаёт
    run_for(bot, clock, 600)                       # событие — новый дашборд, удалять нечего
    assert len(tg.events()) == 1 and len(tg.of("sendWidget")) == n + 1 and tg.of("delete") == []


def test_dashboard_renewed_before_delete_limit(tmp_path):
    """Дашборд старше renew_hours пересоздаётся беззвучно (deleteMessage работает только до 48 ч)."""
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)], soc=100, renew_hours=0.5)
    start(bot, clock)
    run_for(bot, clock, 1900)
    assert len(tg.of("sendWidget")) == 2 and len(tg.of("delete")) == 1 and len(tg.alive) == 1
    assert tg.of("sendWidget")[1][5] is True and tg.events() == []


def test_widget_query_answered(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=False)])
    start(bot, clock)
    live_id = tg.of("sendWidget")[0][2]
    q = {"update_id": 2, "widget_query": {"id": "q1", "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"},
                                          "message_id": live_id, "chat_instance": "1", "data": "refresh"}}
    asyncio.run(bot.handle_update(q))
    (a,) = tg.of("answer")
    assert json.loads(a[2])["g"] == 0
    q["widget_query"]["message_id"] = 1
    asyncio.run(bot.handle_update(q))
    assert tg.of("answer")[1][2] == ""


def test_no_battery_kwh_no_forecast(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=False, load_w=500)], battery_kwh=0)
    start(bot, clock, 600)
    states = [s[3] for s in tg.of("sendWidget")] + [e[3] for e in tg.of("edit")]
    assert states and all(st["e"] == 0 for st in states)


def test_blocked_user_unsubscribed(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=False)])
    bot.store.subscribe(OWNER, "x", clock.t)
    tg.fail["sendWidget"] = TelegramError("sendWidget", 403, "Forbidden: bot was blocked by the user")
    run_for(bot, clock, 30)
    assert bot.store.subscribers() == []


# ------------------------------------------------------------------ график: сообщения об изменениях
from svitlo.schedule import ScheduleError, parse_yasno  # noqa: E402


def yasno_payload(today, tomorrow=None, d="2026-11-11", d1="2026-11-12"):
    def day(date, sl, st):
        return {"date": date + "T00:00:00+02:00", "status": st,
                "slots": [{"start": a, "end": b, "type": "Definite"} for a, b in sl] or
                [{"start": 0, "end": 1440, "type": "NotPlanned"}]}
    return {"3.1": {"today": day(d, today, "ScheduleApplies"),
                     "tomorrow": day(d1, tomorrow or [], "ScheduleApplies" if tomorrow is not None
                                     else "WaitingForSchedule")}}


class FakeSchedule:
    """Подставной источник графика: `payload` — текущий ответ YASNO, `fail` — ошибка загрузки."""

    def __init__(self, payload):
        self.payload = payload
        self.fail = False
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.fail:
            raise ScheduleError("YASNO: 503")
        return parse_yasno(self.payload, "3.1")


def sched_bot(tmp_path, clock, segments, payload, tg=None, src=None):
    bot, tg, src = make_bot(tmp_path, clock, src or segments, tg=tg)
    fs = FakeSchedule(payload)
    bot.fetch_schedule = fs
    return bot, tg, src, fs


def sched_msgs(tg):
    """Сообщения о графике: (первая строка, беззвучно?)."""
    return [(m[3].split("\n")[0], m[4]) for m in tg.of("sendMessage")
            if "<b>График" in m[3].split("\n")[0] or "Экстренные" in m[3]]


TODAY_CHANGED = "🔄 <b>График на сегодня изменился</b> · ср, 11 ноября"


def test_schedule_change_today_loud_and_dashboard_below(tmp_path):
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(7200, grid=True)], yasno_payload([(0, 90), (900, 1320)]))
    start(bot, clock, 600)                                      # первый запуск: график без сообщений
    assert sched_msgs(tg) == [] and len(tg.of("sendWidget")) == 1
    first = tg.of("sendWidget")[0][2]

    fs.payload = yasno_payload([(0, 90), (960, 1320)])
    run_for(bot, clock, 600)
    assert sched_msgs(tg) == [(TODAY_CHANGED, False)]  # со звуком
    full = tg.of("sendMessage")[-1]
    assert "с <b>16:00</b> до <b>22:00</b> · 6 ч  ← <i>было с 15:00</i>" in full[3]
    assert full[5]["parse_mode"] == "HTML" and full[5]["reply_markup"] == texts.KEYBOARD
    second = tg.of("sendWidget")[1]
    assert second[5] is True and second[3]["o"] == [[0, 90], [960, 1320]]
    assert [d[2] for d in tg.of("delete")] == [first]
    assert tg.chat_order()[-2:] == [("T", TODAY_CHANGED), ("W", second[2])]
    assert tg.events() == []

    run_for(bot, clock, 1200)                                   # без изменений — тишина
    assert len(sched_msgs(tg)) == 1 and len(tg.of("sendWidget")) == 2


def test_schedule_tomorrow_appears_silent(tmp_path):
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(7200, grid=True)], yasno_payload([(0, 90)]))
    start(bot, clock, 600)
    fs.payload = yasno_payload([(0, 90)], [(420, 780)])
    run_for(bot, clock, 600)
    assert sched_msgs(tg) == [("🗓 <b>График на завтра</b> · чт, 12 ноября", True)]   # беззвучно
    assert "с <b>07:00</b> до <b>13:00</b> · 6 ч" in tg.of("sendMessage")[-1][3]
    assert len(tg.of("sendWidget")) == 2 and tg.of("sendWidget")[1][3]["w"] == 1
    assert tg.chat_order()[-1] == ("W", tg.of("sendWidget")[1][2])
    fs.payload = yasno_payload([(0, 90)], [(420, 840)])         # опубликованный график на завтра изменился
    run_for(bot, clock, 600)
    assert sched_msgs(tg)[-1] == ("🔄 <b>График на завтра изменился</b> · чт, 12 ноября", True)
    assert "с <b>07:00</b> до <b>14:00</b> · 7 ч  ← <i>было до 13:00</i>" in tg.of("sendMessage")[-1][3]
    assert len(tg.alive) == 1


def test_schedule_no_messages_on_restart_failure_or_midnight(tmp_path):
    clock = Clock(datetime(2026, 11, 11, 23, 0, tzinfo=KYIV).timestamp())
    p = yasno_payload([(1320, 1440)], [(0, 90)])
    bot, tg, src, fs = sched_bot(tmp_path, clock, [Segment(4 * 3600, grid=True)], p)
    start(bot, clock, 600)
    bot.store.close()
    bot2, _, _, fs2 = sched_bot(tmp_path, clock, None, p, tg=tg, src=src)  # перезапуск, график тот же
    run_for(bot2, clock, 600)
    fs2.fail = True                                             # источник недоступен
    run_for(bot2, clock, 600)
    fs2.fail = False
    fs2.payload = yasno_payload([(0, 90)], None, d="2026-11-12", d1="2026-11-13")  # полночь прошла
    run_for(bot2, clock, 3000)
    assert fs2.calls >= 3 and sched_msgs(tg) == [] and len(tg.of("sendWidget")) == 1
    last = tg.of("edit")[-1][3]
    assert last["o"] == [[0, 90]] and last["d"] == int(datetime(2026, 11, 12, tzinfo=KYIV).timestamp())


def test_schedule_notice_survives_send_failure(tmp_path):
    """Сбой sendMessage сообщения о графике: после повтора — ровно одно сообщение и дашборд под ним."""
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(7200, grid=True)], yasno_payload([(0, 90)]))
    start(bot, clock, 600)
    fs.payload = yasno_payload([(0, 120)])
    tg.fail["sendMessage"] = NetworkError("sendMessage: ConnectError")
    run_for(bot, clock, 600)
    assert sched_msgs(tg) == [(TODAY_CHANGED, False)]
    assert len(tg.alive) == 1 and tg.chat_order()[-1][0] == "W" and len(tg.of("sendWidget")) == 2


def test_schedule_and_grid_event_together(tmp_path):
    """Событие сети и изменение графика в одном цикле: оба текста, затем один новый дашборд."""
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(600, grid=True), Segment(3600, grid=False)],
                               yasno_payload([(0, 90)]))
    start(bot, clock, 560)
    fs.payload = yasno_payload([(0, 90), (720, 900)])
    bot._next_schedule = clock.t + 15                           # график придёт в тот же момент, что и событие
    run_for(bot, clock, 300)
    order = tg.chat_order()
    assert order[-3:] == [("T", "🔴 Свет пропал в 12:10."), ("T", TODAY_CHANGED),
                          ("W", tg.of("sendWidget")[-1][2])] or \
        order[-3:] == [("T", TODAY_CHANGED), ("T", "🔴 Свет пропал в 12:10."),
                       ("W", tg.of("sendWidget")[-1][2])]
    assert len(tg.alive) == 1


def test_new_subscriber_gets_no_old_schedule_notices(tmp_path):
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(7200, grid=True)], yasno_payload([(0, 90)]))
    run_for(bot, clock, 600)
    fs.payload = yasno_payload([(0, 120)])
    run_for(bot, clock, 600)                                    # изменение было, подписчиков ещё нет
    asyncio.run(bot.handle_update(msg(OWNER, "/start")))
    run_for(bot, clock, 60)
    assert sched_msgs(tg) == [] and len(tg.of("sendWidget")) == 1


# ------------------------------------------------------------------ новый HTML виджета → пересоздать дашборд
def test_widget_change_recreates_dashboard_once(tmp_path):
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock, 120)
    first = tg.of("sendWidget")[0][2]
    bot.store.close()

    clock.t += 60                                               # перезапуск с тем же виджетом — ничего
    bot2, _, _ = make_bot(tmp_path, clock, src, tg=tg)
    run_for(bot2, clock, 300)
    assert len(tg.of("sendWidget")) == 1
    bot2.store.close()

    clock.t += 60                                               # перезапуск с новым виджетом
    bot3, _, _ = make_bot(tmp_path, clock, src, tg=tg)
    bot3.html = bot3.html + "<!-- v2 -->"
    from svitlo.bot import widget_hash
    bot3.widget_hash = widget_hash(bot3.html)
    run_for(bot3, clock, 600)
    sends = tg.of("sendWidget")
    assert len(sends) == 2 and sends[1][5] is True              # один раз, беззвучно
    assert [d[2] for d in tg.of("delete")] == [first]
    assert tg.events() == [] and len(tg.alive) == 1
    assert bot3.store.subscriber(OWNER).live_widget == bot3.widget_hash


def test_widget_hash_migration_from_old_db(tmp_path):
    """База от прошлой версии (колонки live_widget нет): дашборд пересоздаётся один раз."""
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock, 120)
    bot.store.db.execute("UPDATE subscribers SET live_widget=NULL, notice_seen=NULL, notice_sent=NULL, "
                         "live_notice=NULL")
    run_for(bot, clock, 300)
    assert len(tg.of("sendWidget")) == 2 and len(tg.alive) == 1
    run_for(bot, clock, 300)
    assert len(tg.of("sendWidget")) == 2
