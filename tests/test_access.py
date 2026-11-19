"""Доступ по запросу: незнакомый пишет боту → владелец подтверждает inline-кнопкой; /users; несколько подписчиков."""

import asyncio

from svitlo import texts
from svitlo.config import load_config
from svitlo.source import Segment
from svitlo.storage import Store
from svitlo.telegram import TelegramError
from tests.test_bot import OWNER, STRANGER, T0, Clock, make_bot, run_for, start
from tests.test_buttons import INV_HTML, press, widgets

MOM, DAD, AUNT = 301, 302, 303


def write(bot, user, text="/start", first="Мама", last="Иванова", username="mama_example"):
    frm = {"id": user, "first_name": first}
    if last:
        frm["last_name"] = last
    if username:
        frm["username"] = username
    asyncio.run(bot.handle_update({"update_id": 1, "message": {
        "message_id": 9000 + user, "from": frm, "chat": {"id": user, "type": "private"}, "text": text}}))


def click(bot, data, by=OWNER, message_id=None):
    asyncio.run(bot.handle_update({"update_id": 2, "callback_query": {
        "id": "cb%d" % by, "from": {"id": by, "first_name": "X"}, "data": data,
        "message": {"message_id": message_id or 1, "chat": {"id": by, "type": "private"}}}}))


def request_msg(tg, user=MOM):
    """Сообщение-запрос владельцу про user."""
    (m,) = [m for m in tg.of("sendMessage") if m[1] == OWNER and m[5].get("reply_markup", {}).get(
        "inline_keyboard", [[{}]])[0][0].get("callback_data") == "ok:%d" % user]
    return m


def test_stranger_request_goes_to_owner_and_dashboard_stays_last(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    owner_dash = tg.of("sendWidget")[0][2]
    write(bot, MOM)
    req = request_msg(tg)
    assert req[3] == "Мама Иванова (@mama_example) просит доступ к боту"
    assert req[4] is False                                         # со звуком
    assert req[5]["reply_markup"] == {"inline_keyboard": [[{"text": "Разрешить", "callback_data": "ok:301"},
                                                           {"text": "Отклонить", "callback_data": "no:301"}]]}
    assert tg.to(MOM) == ["Запрос отправлен владельцу. Как только он подтвердит — бот заработает."]
    assert not [w for w in tg.of("sendWidget") if w[1] == MOM]
    # у владельца дашборд пересоздан ниже запроса
    assert ("delete", OWNER, owner_dash) in tg.log
    order = tg.chat_order(OWNER)
    assert order[-2][1] == req[3] and order[-1][0] == "W" and len(tg.alive) == 1
    acc = bot.store.access(MOM)
    assert acc.status == "pending" and acc.owner_msg_id == req[2] and acc.name == "Мама Иванова"


def test_request_without_username_shows_id(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(600, grid=True)])
    start(bot, clock)
    write(bot, MOM, "привет", first="Мама", last=None, username=None)
    assert request_msg(tg)[3] == "Мама (id 301) просит доступ к боту"


def test_repeated_messages_do_not_create_new_requests(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    write(bot, MOM)
    n = len(tg.log)
    for text in ("/start", "Дашборд", "ну что там?", "/start"):
        clock.t += 60
        write(bot, MOM, text)
    assert tg.log[n:] == []                                         # ни владельцу, ни ей — тишина
    request_msg(tg)                                                 # запрос по-прежнему один


def test_approve_gives_keyboard_and_dashboard_and_events(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(1800, grid=True), Segment(3600, grid=False)])
    start(bot, clock)
    write(bot, MOM)
    req = request_msg(tg)
    click(bot, "ok:%d" % MOM, message_id=req[2])
    assert ("answerCb", "cb%d" % OWNER, "Доступ выдан: Мама Иванова") in tg.log
    assert ("editText", OWNER, req[2], "Доступ выдан: Мама Иванова", None) in tg.log  # кнопки убраны
    (hello,) = [m for m in tg.of("sendMessage") if m[1] == MOM and m[3] == texts.ACCESS_GRANTED]
    assert hello[5]["reply_markup"] == texts.KEYBOARD
    (dash,) = [w for w in tg.of("sendWidget") if w[1] == MOM]
    assert dash[5] is True and tg.chat_order(MOM)[-1] == ("W", dash[2])
    sub = bot.store.subscriber(MOM)
    assert sub.active and sub.kb_sent == 1 and sub.name == "Мама Иванова"
    assert bot.store.access(MOM).status == "allowed" and bot.allowed(MOM)
    assert tg.events() == []                                        # выдача доступа — не событие

    run_for(bot, clock, 1800)                                       # свет пропал — событие обоим
    assert tg.events(OWNER) == tg.events(MOM) == ["🔴 Свет пропал в 12:30."]
    for chat in (OWNER, MOM):
        assert tg.chat_order(chat)[-1][0] == "W"
    assert len(tg.alive) == 2

    press(bot, tg, "График", user=MOM)                              # кнопки работают
    assert [m for m in tg.of("sendMessage") if m[1] == MOM][-1][3] == texts.SCHEDULE_NOT_LOADED
    assert tg.chat_order(MOM)[-1][0] == "W"


def test_approve_survives_restart(tmp_path):
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(1800, grid=True), Segment(3600, grid=False)])
    start(bot, clock)
    write(bot, MOM)
    click(bot, "ok:%d" % MOM)
    bot.store.close()
    bot2, _, _ = make_bot(tmp_path, clock, src, tg=tg)              # ALLOWED_IDS всё ещё только владелец
    assert bot2.allowed(MOM)
    run_for(bot2, clock, 1800)
    assert tg.events(MOM) == ["🔴 Свет пропал в 12:30."]


def test_deny_then_silence(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    write(bot, MOM)
    req = request_msg(tg)
    click(bot, "no:%d" % MOM, message_id=req[2])
    assert ("editText", OWNER, req[2], "Отклонено: Мама Иванова", None) in tg.log
    assert tg.to(MOM)[-1] == "Доступ не выдан."
    assert bot.store.access(MOM).status == "denied" and bot.store.subscriber(MOM) is None
    n = len(tg.log)
    for _ in range(3):
        clock.t += 3600
        write(bot, MOM, "/start")
    assert tg.log[n:] == []                                         # владельца больше не беспокоят


def test_only_owner_can_press(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    write(bot, MOM)
    click(bot, "ok:%d" % MOM, by=MOM)                               # сама себе
    click(bot, "ok:%d" % MOM, by=STRANGER)
    assert ("answerCb", "cb%d" % MOM, texts.OWNER_ONLY) in tg.log
    assert ("answerCb", "cb%d" % STRANGER, texts.OWNER_ONLY) in tg.log
    assert bot.store.access(MOM).status == "pending" and not bot.allowed(MOM)
    assert not tg.of("editText") and not [w for w in tg.of("sendWidget") if w[1] == MOM]


def test_double_press_does_not_grant_twice(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    write(bot, MOM)
    click(bot, "ok:%d" % MOM)
    click(bot, "ok:%d" % MOM)
    click(bot, "no:%d" % MOM)                                       # поздний «Отклонить» не отменяет
    assert ("answerCb", "cb%d" % OWNER, texts.ALREADY_DECIDED) in tg.log
    assert len([m for m in tg.to(MOM) if m == texts.ACCESS_GRANTED]) == 1
    assert len([w for w in tg.of("sendWidget") if w[1] == MOM]) == 1
    assert bot.store.access(MOM).status == "allowed"


def test_rate_limit_per_hour(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(9000, grid=True)])
    start(bot, clock)
    for uid in (401, 402, 403, 404, 405):
        write(bot, uid, first="U%d" % uid, last=None, username=None)
    reqs = [m for m in tg.of("sendMessage") if m[1] == OWNER and "просит доступ" in m[3]]
    assert [m[3] for m in reqs] == ["U401 (id 401) просит доступ к боту", "U402 (id 402) просит доступ к боту",
                                    "U403 (id 403) просит доступ к боту"]
    assert tg.to(404) == [] and tg.to(405) == [] and bot.store.access(404) is None
    clock.t += 3601                                                 # через час — снова можно
    write(bot, 404, first="U404", last=None, username=None)
    assert tg.to(404) == [texts.ACCESS_REQUESTED]


def test_owner_unreachable_request_can_be_repeated(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock)
    tg.fail["sendMessage"] = TelegramError("sendMessage", 500, "Internal Server Error")
    write(bot, MOM)
    assert bot.store.access(MOM).status == "failed" and tg.to(MOM) == []
    write(bot, MOM)
    request_msg(tg)
    assert tg.to(MOM) == [texts.ACCESS_REQUESTED]


def test_users_list_remove_and_restore(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(1800, grid=True), Segment(1800, grid=False),
                                            Segment(3600, grid=True)])
    bot.inverter_html = INV_HTML
    start(bot, clock)
    write(bot, MOM)
    click(bot, "ok:%d" % MOM)
    press(bot, tg, "Инвертор", user=MOM)
    mom_dash = bot.store.subscriber(MOM).live_msg_id
    (mom_inv,) = [w for w in widgets(tg, texts.INVERTER_CAPTION) if w[1] == MOM]

    owner_dash = bot.store.subscriber(OWNER).live_msg_id
    write(bot, OWNER, "/users", first="Оля", last=None, username=None)
    lst = tg.of("sendMessage")[-1]
    assert lst[1] == OWNER and lst[4] is True
    assert lst[3] == "С доступом:\n• Н (id 111) — владелец\n• Мама Иванова (@mama_example)"
    assert lst[5]["reply_markup"] == {"inline_keyboard": [[{"text": "Убрать · Мама Иванова",
                                                            "callback_data": "u-:301"}]]}
    assert ("delete", OWNER, owner_dash) in tg.log and tg.chat_order(OWNER)[-1][0] == "W"  # дашборд ниже списка

    click(bot, "u-:%d" % OWNER, message_id=lst[2])                  # себя убрать нельзя
    assert bot.allowed(OWNER)
    click(bot, "u-:%d" % MOM, message_id=lst[2])
    assert ("answerCb", "cb%d" % OWNER, "Доступ убран: Мама Иванова") in tg.log
    assert ("delete", MOM, mom_dash) in tg.log and ("delete", MOM, mom_inv[2]) in tg.log
    edited = tg.of("editText")[-1]
    assert edited[2] == lst[2] and edited[3] == ("С доступом:\n• Н (id 111) — владелец\n\n"
                                                 "Без доступа (отклонены или убраны):\n• Мама Иванова (@mama_example)")
    assert edited[4] == {"inline_keyboard": [[{"text": "Разрешить · Мама Иванова", "callback_data": "u+:301"}]]}
    assert not bot.allowed(MOM) and not bot.store.subscriber(MOM).active

    n_mom = len([x for x in tg.log if x[1] == MOM])
    run_for(bot, clock, 1800)                                       # событие — только владельцу
    write(bot, MOM, "Дашборд")                                      # и на её сообщения — тишина
    assert tg.events(OWNER) == ["🔴 Свет пропал в 12:30."] and tg.events(MOM) == []
    assert len([x for x in tg.log if x[1] == MOM]) == n_mom

    click(bot, "u+:%d" % MOM, message_id=lst[2])                    # вернуть
    assert bot.allowed(MOM) and bot.store.subscriber(MOM).active
    assert tg.to(MOM)[-1] == texts.ACCESS_GRANTED and tg.chat_order(MOM)[-1][0] == "W"


def test_env_user_can_be_removed(tmp_path):
    """ALLOWED_IDS — изначальный список; решение владельца в базе важнее."""
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)], allowed_ids=frozenset({OWNER, DAD}),
                          owner_id=OWNER)
    start(bot, clock)
    write(bot, DAD, "/start", first="Папа", last=None, username=None)
    assert bot.store.subscriber(DAD).active
    click(bot, "u-:%d" % DAD)
    assert not bot.allowed(DAD) and not bot.store.subscriber(DAD).active
    n = len(tg.log)
    write(bot, DAD, "/start", first="Папа", last=None, username=None)
    assert tg.log[n:] == []


def test_users_is_owner_only(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    write(bot, MOM)
    click(bot, "ok:%d" % MOM)
    write(bot, MOM, "/users")
    assert tg.to(MOM)[-1] == texts.HELP


def test_four_chats_one_poll_and_independent_widgets(tmp_path):
    """Владелец + трое: один опрос инвертора на тик, у каждого одно событие и свой дашборд внизу,
    правки не чаще раза в минуту на чат, виджет «Инвертор» — у каждого свой."""
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(1800, grid=True), Segment(3600, grid=False, load_w=500)],
                            soc=90)
    bot.inverter_html = INV_HTML
    reads = []
    real_read = src.read
    src.read = lambda: reads.append(clock.t) or real_read()
    start(bot, clock)
    users = (MOM, DAD, AUNT)
    for uid in users:
        write(bot, uid, first="U%d" % uid, last=None, username="u%d" % uid)
        click(bot, "ok:%d" % uid)
    chats = (OWNER,) + users
    assert all(bot.store.subscriber(c).active for c in chats)

    press(bot, tg, "Инвертор", user=MOM)
    press(bot, tg, "Инвертор", user=DAD)
    press(bot, tg, "Инвертор", user=MOM)                            # повтор удаляет только её прошлый
    inv = widgets(tg, texts.INVERTER_CAPTION)
    assert [w[1] for w in inv] == [MOM, DAD, MOM]
    assert ("delete", MOM, inv[0][2]) in tg.log and ("delete", DAD, inv[1][2]) not in tg.log

    reads.clear()
    tg.log.clear()
    ticks = 0
    end = clock.t + 3000
    while clock.t < end:
        asyncio.run(bot.tick(clock.t))
        clock.t += 10
        ticks += 1
    assert len(reads) == ticks                                      # один опрос на всех
    for c in chats:
        assert tg.events(c) == ["🔴 Свет пропал в 12:30."]
        assert tg.chat_order(c)[-1][0] == "W"
        live = bot.store.subscriber(c).live_msg_id
        times = [e[3]["t"] for e in tg.of("edit") if e[1] == c and e[2] == live]
        assert times and min(b - a for a, b in zip(times, times[1:])) >= 50
    dashboards = {bot.store.subscriber(c).live_msg_id for c in chats}
    assert len(dashboards) == 4 and dashboards <= tg.alive


def test_migration_keeps_subscriber_and_dashboard(tmp_path):
    """База прошлой версии (без таблицы access): подписчик и дашборд на месте, лишнего дашборда нет."""
    clock = Clock(T0)
    bot, tg, src = make_bot(tmp_path, clock, [Segment(7200, grid=True)])
    start(bot, clock, 120)
    live = bot.store.subscriber(OWNER).live_msg_id
    bot.store.db.execute("DROP TABLE access")
    bot.store.close()
    n_widgets = len(tg.of("sendWidget"))
    bot2, _, _ = make_bot(tmp_path, clock, src, tg=tg)
    run_for(bot2, clock, 600)
    assert bot2.store.subscriber(OWNER).live_msg_id == live and len(tg.of("sendWidget")) == n_widgets
    assert bot2.store.accesses() == []


def test_config_owner(tmp_path):
    env = tmp_path / "none.env"
    cfg = load_config(env, {"ALLOWED_IDS": "100500, 7"})
    assert cfg.owner == 100500 and cfg.allowed_ids == {100500, 7} and cfg.access_per_hour == 3
    cfg = load_config(env, {"ALLOWED_IDS": "7", "OWNER_ID": "9", "ACCESS_PER_HOUR": "5"})
    assert cfg.owner == 9 and 9 in cfg.allowed_ids and cfg.access_per_hour == 5
    assert load_config(env, {"ALLOWED_IDS": ""}).owner == 0
    assert Store(":memory:").accesses() == []


def test_users_shows_pending_and_allows_from_list(tmp_path):
    clock = Clock(T0)
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3600, grid=True)])
    start(bot, clock)
    write(bot, MOM)
    req = request_msg(tg)
    write(bot, OWNER, "/users")
    lst = tg.of("sendMessage")[-1]
    assert lst[3] == "С доступом:\n• Н (id 111) — владелец\n\nЖдут ответа:\n• Мама Иванова (@mama_example)"
    click(bot, "u+:%d" % MOM, message_id=lst[2])
    assert bot.allowed(MOM) and tg.to(MOM)[-1] == texts.ACCESS_GRANTED
    assert ("editText", OWNER, req[2], "Доступ выдан: Мама Иванова", None) in tg.log  # кнопки запроса убраны
