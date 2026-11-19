"""Кнопки «Дашборд» / «График» / «Инвертор», клавиатура и виджет «Инвертор»."""

import asyncio
import json
from datetime import datetime

import pytest

from svitlo import state, texts
from svitlo.source import DayStats, FakeSource, Segment
from svitlo.state import INV_KEY_ORDER
from tests.conftest import KYIV
from tests.test_bot import (OWNER, STRANGER, T0, Clock, make_bot, run_for, sched_bot, start,
                            yasno_payload)

INV_HTML = "<div id=w></div><script>window.TelegramWidget</script><!-- inverter -->"


def press(bot, tg, text, user=OWNER, message_id=None):
    """Нажатие кнопки: сообщение пользователя существует в чате, бот должен его удалить."""
    tg.next_id += 1
    mid = message_id or tg.next_id
    tg.existing.add(mid)
    upd = {"update_id": 1, "message": {"message_id": mid, "from": {"id": user, "first_name": "Н"},
                                       "chat": {"id": user, "type": "private"}, "text": text}}
    asyncio.run(bot.handle_update(upd))
    return mid


def inv_bot(tmp_path, clock, segments, **kw):
    bot, tg, src = make_bot(tmp_path, clock, segments, **kw)
    bot.inverter_html = INV_HTML
    return bot, tg, src


def widgets(tg, caption):
    return [w for w in tg.of("sendWidget") if w[4] == caption]


def test_start_shows_keyboard(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(600, grid=True)])
    start(bot, clock)
    first = tg.of("sendMessage")[0]
    assert first[3] == texts.START_OK and first[5]["reply_markup"] == texts.KEYBOARD
    kb = texts.KEYBOARD["keyboard"][0]
    assert [b["text"] for b in kb] == ["Дашборд", "График", "Инвертор"] and texts.KEYBOARD["is_persistent"]
    assert bot.store.subscriber(OWNER).kb_sent == 1
    run_for(bot, clock, 300)
    assert not [m for m in tg.of("sendMessage") if m[3] == texts.KEYBOARD_HELLO]


def test_existing_subscriber_gets_keyboard_once_and_dashboard_stays_last(tmp_path):
    """Подписчик из прошлой версии (kb_sent пуст): одно беззвучное сообщение с кнопками, дашборд ниже."""
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    bot.store.db.execute("UPDATE subscribers SET kb_sent=NULL")
    run_for(bot, clock, 600)
    hello = [m for m in tg.of("sendMessage") if m[3] == texts.KEYBOARD_HELLO]
    assert len(hello) == 1 and hello[0][4] is True and hello[0][5]["reply_markup"] == texts.KEYBOARD
    assert len(tg.alive) == 1 and tg.chat_order()[-1][0] == "W"
    assert tg.chat_order()[-2] == ("T", texts.KEYBOARD_HELLO)
    run_for(bot, clock, 600)
    assert len([m for m in tg.of("sendMessage") if m[3] == texts.KEYBOARD_HELLO]) == 1


def test_event_message_keeps_keyboard(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(300, grid=True), Segment(600, grid=False)])
    start(bot, clock)
    run_for(bot, clock, 600)
    (ev,) = [m for m in tg.of("sendMessage") if m[3].startswith("🔴 Свет пропал")]
    assert ev[4] is False and ev[5]["reply_markup"] == texts.KEYBOARD


def test_dashboard_button_recreates_and_deletes_press(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    first = tg.of("sendWidget")[0][2]
    mid = press(bot, tg, "Дашборд")
    assert ("delete", OWNER, mid) in tg.log                     # нажатие удалено
    assert ("delete", OWNER, first) in tg.log                   # старый дашборд удалён
    assert len(tg.of("sendWidget")) == 2 and len(tg.alive) == 1
    assert tg.of("sendWidget")[1][5] is True                    # беззвучно
    assert tg.events() == []


def test_status_command_also_deleted(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    mid = press(bot, tg, "/status")
    assert ("delete", OWNER, mid) in tg.log and len(tg.of("sendWidget")) == 2


def test_schedule_button(tmp_path):
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(7200, grid=True)],
                               yasno_payload([(0, 90), (900, 1320)], [(120, 420)]))
    start(bot, clock, 60)
    press(bot, tg, "График")
    reply = tg.of("sendMessage")[-1]
    assert reply[4] is True and reply[5]["parse_mode"] == "HTML" and reply[5]["reply_markup"] == texts.KEYBOARD
    assert reply[3] == (
        "🗓 <b>График на сегодня</b> · ср, 11 ноября\n\n"
        "с <b>00:00</b> до <b>01:30</b> · 1 ч 30 мин\n"
        "с <b>15:00</b> до <b>22:00</b> · 7 ч\n\n"
        "Всего без света 8 ч 30 мин\n\n"
        "🗓 <b>График на завтра</b> · чт, 12 ноября\n\n"
        "с <b>02:00</b> до <b>07:00</b> · 5 ч\n\n"
        "Всего без света 5 ч")
    assert tg.chat_order()[-1][0] == "W" and len(tg.alive) == 1   # дашборд снова последний
    assert bot.store.subscriber(OWNER).ask_msg_id == reply[2]

    press(bot, tg, "График")                                      # прошлый ответ удаляется
    assert ("delete", OWNER, reply[2]) in tg.log
    assert [t for k, t in tg.chat_order() if k == "T" and t.startswith("🗓")] == [
        "🗓 <b>График на сегодня</b> · ср, 11 ноября"]
    assert len(tg.alive) == 1


def test_schedule_button_tomorrow_unknown_and_not_loaded(tmp_path):
    clock = Clock(T0)
    bot, tg, _, fs = sched_bot(tmp_path, clock, [Segment(7200, grid=True)], yasno_payload([(0, 90)]))
    asyncio.run(bot.handle_update({"update_id": 1, "message": {
        "message_id": 1, "from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}, "text": "/start"}}))
    press(bot, tg, "График")                                      # график ещё не загружен
    assert tg.of("sendMessage")[-1][3] == texts.SCHEDULE_NOT_LOADED
    run_for(bot, clock, 60)
    press(bot, tg, "График")
    assert tg.of("sendMessage")[-1][3].endswith("🗓 <b>График на завтра</b> · чт, 12 ноября\nЕщё не опубликован.")


def test_buttons_denied_for_strangers(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = inv_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    n = len(tg.log)
    press(bot, tg, "Инвертор", user=STRANGER)
    new = tg.log[n:]
    assert not [x for x in new if x[0] == "sendWidget" and x[1] == STRANGER]   # кнопка не сработала
    assert [x[3] for x in new if x[0] == "sendMessage" and x[1] == STRANGER] == [texts.ACCESS_REQUESTED]
    assert bot.store.subscriber(STRANGER) is None and bot.store.access(STRANGER).status == "pending"


def test_inverter_button_lifecycle(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = inv_bot(tmp_path, clock, [Segment(7200, grid=True, load_w=600)], soc=60)
    start(bot, clock)
    dash0 = tg.of("sendWidget")[0][2]
    pressed_at = clock.t
    mid = press(bot, tg, "Инвертор")
    (w,) = widgets(tg, texts.INVERTER_CAPTION)
    data = w[3]
    assert w[5] is True and ("delete", OWNER, mid) in tg.log
    assert list(data) == [k for k in INV_KEY_ORDER if k in data]
    assert data["z"] == int(pressed_at + 15 * 60) and data["g"] == 1 and data["c"] == 60
    assert data["b"] < 0 and data["a"] < 0 and data["n"] > 0 and data["x"] == 10
    assert ("delete", OWNER, dash0) in tg.log                   # дашборд пересоздан под инвертором
    order = tg.chat_order()
    assert order[-2] == ("W", w[2]) and order[-1][0] == "W" and order[-1][1] != w[2]

    run_for(bot, clock, 14 * 60)                                # живые правки ~ раз в 30 с … 2 мин
    inv_edits = [e for e in tg.of("edit") if e[2] == w[2]]
    assert inv_edits and all(e[3]["z"] == data["z"] for e in inv_edits)
    revs = [e[3]["r"] for e in inv_edits]
    assert revs == list(range(2, 2 + len(revs)))
    ts = [e[3]["t"] for e in inv_edits]
    assert min(b - a for a, b in zip(ts, ts[1:])) >= 30 - 10 and max(b - a for a, b in zip(ts, ts[1:])) <= 130
    run_for(bot, clock, 30 * 60)                                # после z — больше не правится
    late = [e for e in tg.of("edit") if e[2] == w[2] and e[3]["t"] > data["z"]]
    assert late == []

    press(bot, tg, "Инвертор")                                  # повтор: старый удалён, новый один
    assert ("delete", OWNER, w[2]) in tg.log
    alive_inv = [x for x in widgets(tg, texts.INVERTER_CAPTION) if x[2] in tg.alive]
    assert len(alive_inv) == 1 and len(tg.alive) == 2
    assert tg.chat_order()[-1][1] not in [x[2] for x in widgets(tg, texts.INVERTER_CAPTION)]


def test_inverter_day_block_and_day_every(tmp_path):
    """Дневной блок (частота, температура, счётчики) попадает в виджет; пока он живой — опрос раз в 30 с."""
    clock = Clock(T0)
    src = FakeSource([Segment(7200, grid=True)], clock, soc=70)
    src.day_every = None
    day = DayStats(1.9, 1.3, 7.7, 7.1, grid_hz=50.01, inverter_c=40.6)
    orig = src.read
    src.read = lambda: orig().__class__(**dict(orig().__dict__, day=day))
    bot, tg, _ = inv_bot(tmp_path, clock, src)
    start(bot, clock)
    asyncio.run(bot.sync_all(clock.t))
    assert src.day_every == 300
    press(bot, tg, "Инвертор")
    (w,) = widgets(tg, texts.INVERTER_CAPTION)
    assert w[3]["f"] == 50.01 and w[3]["i"] == 40.6 and w[3]["q"] == [1.9, 1.3, 7.7, 7.1]
    assert src.day_every == 30
    run_for(bot, clock, 20 * 60)
    assert src.day_every == 300


def test_inverter_snapshot_without_grid_and_without_day(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = inv_bot(tmp_path, clock, [Segment(3600, grid=False, load_w=180)], soc=98)
    start(bot, clock)
    snap = bot.engine.inverter_snapshot(clock.t, int(clock.t) + 900)
    assert snap["g"] == 0 and snap["u"] == 0 and snap["n"] == 0 and snap["b"] == 180
    assert "f" not in snap and "q" not in snap and "i" not in snap
    hidden = bot.engine.inverter_snapshot(clock.t, None, grid_power=False, grid_hz=False)
    assert "n" not in hidden and "z" not in hidden
    assert len(json.dumps(snap, separators=(",", ":")).encode()) < 300


def test_inverter_widget_deleted_by_user(tmp_path):
    from svitlo.telegram import TelegramError

    clock = Clock(T0)
    bot, tg, _ = inv_bot(tmp_path, clock, [Segment(7200, grid=False, load_w=400)])
    start(bot, clock)
    press(bot, tg, "Инвертор")
    inv_id = bot.store.subscriber(OWNER).inv_msg_id
    edit = tg.edit_widget_data

    async def gone(chat_id, message_id, chat_data):
        if message_id == inv_id:
            raise TelegramError("editMessageWidgetData", 400, "Bad Request: message to edit not found")
        return await edit(chat_id, message_id, chat_data)

    tg.edit_widget_data = gone
    run_for(bot, clock, 240)
    sub = bot.store.subscriber(OWNER)
    assert sub.inv_msg_id is None and sub.active and sub.live_kind == "widget"


def test_stop_removes_keyboard_and_inverter(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = inv_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    press(bot, tg, "Инвертор")
    (w,) = widgets(tg, texts.INVERTER_CAPTION)
    press(bot, tg, "/stop")
    assert ("delete", OWNER, w[2]) in tg.log and tg.alive == set()
    assert tg.of("sendMessage")[-1][5]["reply_markup"] == {"remove_keyboard": True}


def test_inverter_not_ready(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = inv_bot(tmp_path, clock, [Segment(600, grid=True)])
    bot.store.subscribe(OWNER, "Н", clock.t)
    press(bot, tg, "Инвертор")
    assert tg.of("sendMessage")[-1][3] == texts.NOT_READY and widgets(tg, texts.INVERTER_CAPTION) == []


def test_emergency_in_dashboard_chat_data(tmp_path):
    from svitlo.schedule import parse_yasno

    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    p = yasno_payload([(0, 90)])
    p["3.1"]["today"]["status"] = "EmergencyShutdowns"
    bot.engine.set_schedule(parse_yasno(p, "3.1"), clock.t)
    data = json.loads(bot.engine.chat_data(clock.t))
    assert data["m"] == 1 and data["o"] == [] and list(data)[-2:] == ["m", "t"]
    assert datetime.fromtimestamp(data["d"], KYIV).hour == 0


def test_grid_power_and_hz_shown_by_default(tmp_path):
    """Рег. 169 и 79 сверены при сети 12.11.2026: `n` и `f` шлются без дополнительных настроек."""
    assert state.INV_GRID_POWER and state.INV_GRID_HZ
    clock = Clock(T0)
    src = FakeSource([Segment(7200, grid=True, load_w=600)], clock, soc=70)
    day = DayStats(3.1, 2.2, 9.2, 8.3, grid_hz=50.01, inverter_c=47.1)
    orig = src.read
    src.read = lambda: orig().__class__(**dict(orig().__dict__, day=day))
    bot, tg, _ = inv_bot(tmp_path, clock, src)
    start(bot, clock)
    snap = bot.engine.inverter_snapshot(clock.t, int(clock.t) + 900)
    assert snap["n"] > 0 and snap["n"] == snap["l"] - snap["b"]    # из сети = нагрузка + заряд
    assert snap["f"] == 50.01
    assert len(state.encode_inverter(dict(snap, r=1)).encode()) <= state.CHAT_DATA_LIMIT


def test_live_registers_with_grid_decode():
    """Сырые регистры живого инвертора 12.11.2026 16:07:56 (сеть есть, заряд): знаки и масштаб."""
    from svitlo.source import MAIN_COUNT, MAIN_START, decode_main

    regs = [0] * MAIN_COUNT
    for addr, val in {150: 2245, 169: 850, 178: 595, 182: 1230, 183: 5679, 184: 100,
                      190: 0x10000 - 262, 191: 0x10000 - 463, 194: 1, 217: 10}.items():
        regs[addr - MAIN_START] = val
    r = decode_main(regs, 0)
    assert r.grid_w == 850 and r.load_w == 595 and r.batt_w == -262 and r.batt_a == pytest.approx(-4.63)
    assert abs(r.grid_w - (r.load_w - r.batt_w)) <= 15                # потери 7–12 Вт
    assert r.grid_v == pytest.approx(224.5) and r.batt_temp_c == 23.0


def test_unconfirmed_registers_can_be_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "INV_GRID_POWER", False)
    monkeypatch.setattr(state, "INV_GRID_HZ", False)
    clock = Clock(T0)
    src = FakeSource([Segment(7200, grid=True)], clock, soc=70)
    day = DayStats(1.9, 1.3, 7.7, 7.1, grid_hz=50.01, inverter_c=40.6)
    orig = src.read
    src.read = lambda: orig().__class__(**dict(orig().__dict__, day=day))
    bot, tg, _ = inv_bot(tmp_path, clock, src)
    start(bot, clock)
    snap = bot.engine.inverter_snapshot(clock.t, int(clock.t) + 900)
    assert "n" not in snap and "f" not in snap and snap["i"] == 40.6
