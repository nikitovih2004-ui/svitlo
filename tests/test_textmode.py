"""Текстовый режим: Telegram отвечает на sendWidget INPUT_MEDIA_WIDGET_NOT_ALLOWED.

Дашборд и «Инвертор» — живые текстовые сообщения (editMessageText), отказ запоминается, раз в 30 минут —
одна проба sendWidget на всех; разрешили — текстовые дашборды у всех заменяются виджетами."""

import asyncio
import json
import logging
from datetime import datetime

import httpx

from svitlo import texts
from svitlo.source import Segment
from svitlo.telegram import Telegram, TelegramError
from tests.conftest import KYIV
from tests.test_bot import OWNER, T0, Clock, FakeTG, make_bot, msg, run_for, sched_bot, start, yasno_payload
from tests.test_buttons import INV_HTML, press

DAD = 333
REFUSED = "Bad Request: INPUT_MEDIA_WIDGET_NOT_ALLOWED"
PROBE = 30 * 60


class RefusingTG(FakeTG):
    """Telegram, который (пока refuse=True) не разрешает боту виджеты."""

    def __init__(self, refuse=True):
        super().__init__()
        self.refuse = refuse

    async def send_widget(self, chat_id, html, chat_data=None, caption=None, disable_notification=False, **kw):
        if self.refuse:
            self.log.append(("refused", chat_id, caption))
            raise TelegramError("sendWidget", 400, REFUSED)
        return await super().send_widget(chat_id, html, chat_data, caption, disable_notification, **kw)


def two_users(tmp_path, clock, segments, tg, **kw):
    bot, tg, src = make_bot(tmp_path, clock, segments, tg=tg, allowed_ids=frozenset({OWNER, DAD}),
                            owner_id=OWNER, **kw)
    bot.inverter_html = INV_HTML
    start(bot, clock)
    asyncio.run(bot.handle_update(msg(DAD, "/start")))
    run_for(bot, clock, 30)
    return bot, tg, src


def attempts(tg):
    """Все попытки sendWidget (и отказанные, и прошедшие)."""
    return [x for x in tg.log if x[0] in ("refused", "sendWidget")]


def text_dashboards(tg, chat):
    return [m for m in tg.of("sendMessage") if m[1] == chat and m[3].startswith(("🟢 <b>Свет", "🔴 <b>Свет"))]


def mode_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if "не разрешает виджеты" in r.getMessage() or "снова разрешает" in r.getMessage()]


# ------------------------------------------------------------------ тексты

D0 = int(datetime(2026, 11, 13, tzinfo=KYIV).timestamp())
NOW = datetime(2026, 11, 13, 17, 42, tzinfo=KYIV).timestamp()
OFF = int(datetime(2026, 11, 13, 17, 21, 56, tzinfo=KYIV).timestamp())
ON = int(datetime(2026, 11, 13, 11, 1, 40, tzinfo=KYIV).timestamp())
DASH = dict(r=5, g=0, s=OFF, p=22816, c=87, b=420, l=410, u=0, e=int(NOW + 19 * 3600), h=68000,
            o=[[480, 660], [1020, 1230], [1740, 1980]], d=D0, w=1, m=0, t=int(NOW - 8))


def test_dashboard_text_outage_by_schedule_exact():
    assert texts.dashboard_text(DASH, NOW, KYIV) == (
        "🔴 <b>Света нет</b> с <b>17:21</b>\n"
        "По графику включат в <b>20:30</b>\n"
        "\n"
        "Сегодня, пт 13.11: 08:00–11:00, <b>17:00–20:30</b>\n"
        "Завтра, сб 14.11: <b>05:00–09:00</b>\n"
        "\n"
        "🔋 <b>87%</b> · хватит на <b>~19 ч</b>, до <b>12:42</b>\n"
        "🏠 Нагрузка <b>410 Вт</b>\n"
        "\n"
        "<i>обновлено 17:42</i>")


def test_dashboard_text_grid_on_next_outage_stale_and_tomorrow_unknown():
    d = dict(DASH, g=1, s=ON, u=229, b=-260, c=96, e=int(NOW + 3600), h=70000, o=[[480, 660], [1260, 1440]],
             w=0, t=int(NOW - 400))
    text = texts.dashboard_text(d, NOW, KYIV)
    lines = text.split("\n")
    assert lines[0] == "⚠️ Нет связи с инвертором · данные на <b>17:35</b>"
    assert lines[1] == "🟢 <b>Свет есть</b> с <b>11:01</b>"
    assert lines[2] == "По графику отключат в <b>21:00</b> до <b>24:00</b>"
    assert "Сегодня, пт 13.11: 08:00–11:00, <b>21:00–24:00</b>" in lines and not any("Завтра" in x for x in lines)
    assert "🔋 <b>96%</b> · без сети хватит на <b>~19 ч</b> · полная к <b>18:42</b>" in lines
    assert "🏠 Нагрузка <b>410 Вт</b> · сеть <b>229 В</b>" in lines
    assert texts.dashboard_text(d, NOW, KYIV, footer=False) == "\n".join(lines[:-2])


def test_dashboard_text_other_headlines():
    unplanned = texts.dashboard_text(dict(DASH, o=[]), NOW, KYIV, known=False)
    assert unplanned.split("\n")[:2] == ["🔴 <b>Света нет</b>", "Отключили вне графика — когда включат, неизвестно"]
    assert "Сегодня, пт 13.11: отключений нет" in unplanned
    on_in_slot = texts.dashboard_text(dict(DASH, g=1, s=ON, u=230, e=0), NOW, KYIV)
    assert on_in_slot.split("\n")[1] == "По графику света нет до <b>20:30</b>, но свет пока есть"
    emergency = texts.dashboard_text(dict(DASH, g=1, s=ON, u=230, o=[], m=1, e=0), NOW, KYIV)
    assert emergency.split("\n")[1] == "🚨 Аварийные отключения — график не действует"
    assert "Сегодня, пт 13.11: 🚨 аварийные отключения, график не действует" in emergency
    free = texts.dashboard_text(dict(DASH, g=1, s=ON, u=230, o=[], w=0, e=0, h=0, c=100), NOW, KYIV)
    assert free.split("\n")[1] == "До конца суток без отключений" and "🔋 <b>100%</b> · батарея полная" in free
    tomorrow = texts.dashboard_text(dict(DASH, g=1, s=ON, u=230, o=[[1500, 1600]], e=0), NOW, KYIV)
    assert tomorrow.split("\n")[1] == "По графику отключат в завтра <b>01:00</b> до завтра <b>02:40</b>"
    long_ago = texts.dashboard_text(dict(DASH, s=OFF - 2 * 86400), NOW, KYIV)
    assert long_ago.startswith("🔴 <b>Света нет</b> с <b>ср 11.11</b>")
    low = texts.dashboard_text(dict(DASH, c=15, e=int(NOW + 50 * 60)), NOW, KYIV)
    assert "🪫 <b>15%</b> · хватит на <b>50 мин</b>, до <b>18:32</b>" in low


INV = dict(r=3, t=int(NOW - 5), d=D0, z=int(NOW + 600), g=1, u=224.5, f=50.01, n=850, l=600, c=100, b=-260,
           a=-4.6, v=56.8, k=23.0, x=10, h=86400, e=0, i=47.1, q=[3.1, 2.2, 9.2, 8.3])


def test_inverter_text_live_exact():
    assert texts.inverter_text(INV, NOW, KYIV) == (
        "⚡ <b>Инвертор</b>\n"
        "🔌 Сеть: <b>224,5 В</b> · 50,01 Гц · из сети <b>850 Вт</b>\n"
        "🏠 Дом: <b>600 Вт</b>\n"
        "🔋 Батарея: <b>100%</b> · заряд <b>260 Вт</b>\n"
        "      56,8 В · 4,6 А · 23,0 °C\n"
        "      без сети хватит на ~24 ч\n"
        "🌡 Инвертор: <b>47,1 °C</b>\n"
        "📊 За сегодня, кВт·ч: потребили <b>8,3</b> · из сети <b>9,2</b> · заряд <b>3,1</b> · разряд <b>2,2</b>\n"
        "\n"
        "<i>обновлено 17:42 · обновляется до 17:52</i>")


def test_inverter_text_no_grid_stale_and_snapshot():
    d = dict(INV, g=0, u=0, n=0, b=180, a=3.4, c=18, e=int(NOW + 3 * 3600), h=0)
    d.pop("f"), d.pop("i"), d.pop("q")
    live = texts.inverter_text(d, NOW + 400, KYIV)
    assert "⚠️ Нет связи с инвертором · данные на <b>17:41</b>" in live and "🔌 Сеть: <b>нет напряжения</b>" in live
    assert "🪫 Батарея: <b>18%</b> · разряд <b>180 Вт</b>" in live and "хватит на ~3 ч · отключится при 10%" in live
    assert "🌡" not in live and "📊" not in live
    snap = texts.inverter_text(d, NOW + 700, KYIV)
    assert snap.endswith("<i>Снимок на 17:41 · свежие данные — кнопка «Инвертор»</i>") and "Нет связи" not in snap


# ------------------------------------------------------------------ Telegram

def test_widgets_refused_flag_and_edit_parse_mode():
    assert TelegramError("sendWidget", 400, REFUSED).widgets_refused
    assert not TelegramError("sendWidget", 400, "Bad Request: INPUT_MEDIA_WIDGET_TOO_LARGE").widgets_refused
    assert not TelegramError("sendWidget", 403, "Forbidden: INPUT_MEDIA_WIDGET_NOT_ALLOWED").widgets_refused
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": True})

    tg = Telegram("1:x", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(tg.edit_message_text(5, 9, "<b>x</b>", parse_mode="HTML"))
    assert seen == {"chat_id": 5, "message_id": 9, "text": "<b>x</b>", "parse_mode": "HTML"}


# ------------------------------------------------------------------ бот

def test_refusal_remembered_text_mode_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="svitlo.bot")
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = two_users(tmp_path, clock, [Segment(600, grid=True), Segment(3600, grid=False)], tg)
    assert len(attempts(tg)) == 1                      # отказ запомнен: второму чату сразу текст
    assert bot.widgets_off is not None and bot.store.get("widgets")["probe"] == bot.probe_at
    assert bot.probe_at >= T0 + PROBE
    run_for(bot, clock, 600)                           # событие «свет пропал» у обоих
    press(bot, tg, "Дашборд")
    press(bot, tg, "Дашборд", user=DAD)
    run_for(bot, clock, 300)
    assert len(attempts(tg)) == 1                      # ни события, ни кнопки не дёргают sendWidget
    for chat in (OWNER, DAD):
        assert tg.events(chat) == ["🔴 Свет пропал в 12:10."]
        dash = text_dashboards(tg, chat)
        assert all(m[4] is True and m[5]["parse_mode"] == "HTML" for m in dash)
        alive = [m for m in dash if m[2] in tg.existing]
        assert len(alive) == 1 and tg.chat_order(chat)[-1] == ("T", alive[0][3].split("\n")[0])
        sub = bot.store.subscriber(chat)
        assert sub.live_kind == "text" and sub.live_widget == texts.TEXT_FORMAT
    assert len(mode_lines(caplog)) == 1
    assert not [r for r in caplog.records if "sendWidget не прошёл" in r.getMessage()]


def test_text_dashboard_live_edits_like_widget(tmp_path):
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(1200, grid=True, load_w=600),
                                            Segment(900, grid=True, offline=True)], tg=tg)
    start(bot, clock)
    live = bot.store.subscriber(OWNER).live_msg_id
    run_for(bot, clock, 1170)
    edits = [e for e in tg.of("editHTML") if e[2] == live]
    assert len(edits) >= 8 and all("<i>обновлено " in e[3] for e in edits)
    assert json.loads(bot.store.subscriber(OWNER).live_data)["t"] >= clock.t - 140   # t свежий
    # частота (отдельный бот, по шагам): не чаще раза в минуту, не реже раза в ~2 минуты
    stamps = []
    clock2 = Clock(T0)
    bot2, tg2, _ = make_bot(tmp_path / "b", clock2, [Segment(1200, grid=True, load_w=600)], tg=RefusingTG())
    start(bot2, clock2)
    for _ in range(100):
        n = len(tg2.of("editHTML"))
        asyncio.run(bot2.tick(clock2.t))
        if len(tg2.of("editHTML")) > n:
            stamps.append(clock2.t)
        clock2.t += 10
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps and min(gaps) >= 60 and max(gaps) <= 130
    # связь пропала: через 5 минут одна правка «нет связи», дальше текст стоит
    run_for(bot, clock, 900)
    edits = [e for e in tg.of("editHTML") if e[2] == live]
    stale = [i for i, e in enumerate(edits) if e[3].startswith("⚠️ Нет связи с инвертором")]
    assert stale and stale[0] == len(edits) - 1
    assert tg.chat_order()[-1][0] == "T" and len(attempts(tg)) == 2   # + одна плановая проба через 30 мин


def test_probe_refused_changes_nothing_in_chats(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="svitlo.bot")
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = two_users(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg)
    first_probe = bot.probe_at
    run_for(bot, clock, first_probe - clock.t - 20)
    assert len(attempts(tg)) == 1
    before = [x for x in tg.log if x[0] in ("sendMessage", "delete")]
    run_for(bot, clock, 60)
    assert len(attempts(tg)) == 2 and attempts(tg)[-1][1] == OWNER   # одна проба на всех, у владельца
    assert [x for x in tg.log if x[0] in ("sendMessage", "delete")] == before
    assert first_probe + PROBE - 10 <= bot.probe_at <= first_probe + PROBE + 70
    run_for(bot, clock, PROBE)
    assert len(attempts(tg)) == 3                                      # и ещё одна через 30 мин
    assert [x for x in tg.log if x[0] in ("sendMessage", "delete")] == before
    assert len(mode_lines(caplog)) == 1


def test_probe_success_replaces_text_dashboards_everywhere(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="svitlo.bot")
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = two_users(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg)
    old = {c: bot.store.subscriber(c).live_msg_id for c in (OWNER, DAD)}
    run_for(bot, clock, 600)
    tg.refuse = False                                  # Telegram снова разрешил
    sent_before = len(tg.of("sendMessage"))
    run_for(bot, clock, bot.probe_at - clock.t + 30)
    assert bot.widgets_off is None and bot.store.get("widgets")["off"] is None
    widgets = tg.of("sendWidget")
    assert [w[1] for w in widgets] == [OWNER, DAD] and all(w[5] is True for w in widgets)
    log = tg.log
    i_w = log.index(widgets[0])
    assert log.index(("delete", OWNER, old[OWNER])) > i_w                  # у владельца: сначала виджет, потом удаление
    assert log.index(("delete", DAD, old[DAD])) < log.index(widgets[1])
    assert len(tg.of("sendMessage")) == sent_before                        # ни одного лишнего сообщения
    for chat in (OWNER, DAD):
        sub = bot.store.subscriber(chat)
        assert sub.live_kind == "widget" and tg.chat_order(chat)[-1] == ("W", sub.live_msg_id)
        assert not [m for m in text_dashboards(tg, chat) if m[2] in tg.existing]
    run_for(bot, clock, 600)
    assert len(tg.of("sendWidget")) == 2 and tg.of("edit")                 # дальше живые правки виджетов
    lines = mode_lines(caplog)
    assert len(lines) == 2 and "снова разрешает" in lines[1]


def test_inverter_text_mode_lifecycle(tmp_path):
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True, load_w=600)], tg=tg, soc=60)
    bot.inverter_html = INV_HTML
    start(bot, clock)
    pressed = clock.t
    press(bot, tg, "Инвертор")
    assert len(attempts(tg)) == 1                      # без пробы: отказ свежий
    (inv,) = [m for m in tg.of("sendMessage") if m[3].startswith("⚡ <b>Инвертор</b>")]
    assert inv[4] is True and inv[5]["parse_mode"] == "HTML" and "🔌 Сеть: <b>230,0 В</b>" in inv[3]
    assert "обновляется до %s" % texts.hm(pressed + 900, KYIV) in inv[3]
    sub = bot.store.subscriber(OWNER)
    assert sub.inv_kind == "text" and sub.inv_msg_id == inv[2]
    order = tg.chat_order()
    assert order[-2] == ("T", "⚡ <b>Инвертор</b>") and order[-1][1].startswith("🟢 <b>Свет есть</b>")
    run_for(bot, clock, 14 * 60)
    inv_edits = [e for e in tg.of("editHTML") if e[2] == inv[2]]
    assert len(inv_edits) >= 6 and all("обновляется до" in e[3] for e in inv_edits)
    run_for(bot, clock, 10 * 60)                      # после z — одна последняя правка «Снимок»
    inv_edits = [e for e in tg.of("editHTML") if e[2] == inv[2]]
    snaps = [e for e in inv_edits if "Снимок на" in e[3]]
    assert len(snaps) == 1 and snaps[0] == inv_edits[-1]
    run_for(bot, clock, 5 * 60)
    assert len([e for e in tg.of("editHTML") if e[2] == inv[2]]) == len(inv_edits)
    press(bot, tg, "Инвертор")                        # повтор: прошлый удалён, новый один
    assert ("delete", OWNER, inv[2]) in tg.log
    assert len([m for m in tg.of("sendMessage") if m[3].startswith("⚡") and m[2] in tg.existing]) == 1
    assert tg.chat_order()[-1][1].startswith("🟢 <b>Свет есть</b>")


def test_inverter_button_is_a_probe_when_due(tmp_path):
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg=tg)
    bot.inverter_html = INV_HTML
    start(bot, clock)
    bot.probe_at = clock.t                             # подошло время пробы
    tg.refuse = False
    press(bot, tg, "Инвертор")
    assert [w[4] for w in tg.of("sendWidget")] == [texts.INVERTER_CAPTION, texts.DASHBOARD_CAPTION]
    assert bot.widgets_off is None and bot.store.subscriber(OWNER).inv_kind == "widget"


def test_old_text_dashboard_after_deploy(tmp_path, caplog):
    """Выкатка: в базе текстовые дашборды старого формата, отметки режима ещё нет — сразу одна проба;
    отказ — дашборд исправляется на месте новым текстом, без новых сообщений."""
    caplog.set_level(logging.INFO, logger="svitlo.bot")
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = two_users(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg)
    for chat in (OWNER, DAD):
        bot.store.db.execute("UPDATE subscribers SET live_widget=NULL WHERE chat_id=?", (chat,))
    bot.store.db.execute("DELETE FROM kv WHERE key='widgets'")
    caplog.clear()
    bot2, _, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg=tg,
                          allowed_ids=frozenset({OWNER, DAD}), owner_id=OWNER)
    assert bot2.widgets_off is not None and bot2.probe_at == 0
    before = [x for x in tg.log if x[0] in ("sendMessage", "delete")]
    n = len(attempts(tg))
    run_for(bot2, clock, 30)
    assert len(attempts(tg)) == n + 1 and attempts(tg)[-1][1] == OWNER
    assert [x for x in tg.log if x[0] in ("sendMessage", "delete")] == before
    for chat in (OWNER, DAD):
        sub = bot2.store.subscriber(chat)
        assert sub.live_widget == texts.TEXT_FORMAT
        assert [e for e in tg.of("editHTML") if e[2] == sub.live_msg_id]
    assert len(mode_lines(caplog)) == 1 and "не разрешает" in mode_lines(caplog)[0]


def test_old_text_dashboard_after_deploy_widgets_back(tmp_path):
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = two_users(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg)
    bot.store.db.execute("DELETE FROM kv WHERE key='widgets'")
    tg.refuse = False
    bot2, _, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg=tg,
                          allowed_ids=frozenset({OWNER, DAD}), owner_id=OWNER)
    sent = len(tg.of("sendMessage"))
    run_for(bot2, clock, 40)
    assert [w[1] for w in tg.of("sendWidget")] == [OWNER, DAD] and len(tg.of("sendMessage")) == sent
    assert all(bot2.store.subscriber(c).live_kind == "widget" for c in (OWNER, DAD))


def test_restart_keeps_text_mode(tmp_path):
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg=tg)
    start(bot, clock)
    probe_at = bot.probe_at
    bot2, _, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg=tg)
    assert bot2.widgets_off == bot.widgets_off and bot2.probe_at == probe_at
    run_for(bot2, clock, 30)
    press(bot2, tg, "Дашборд")
    assert len(attempts(tg)) == 1


def test_no_text_dashboards_probe_postponed(tmp_path):
    """Текстовых дашбордов нет (пользователь удалил) — плановая проба не шлёт лишний виджет."""
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _ = make_bot(tmp_path, clock, [Segment(3 * 3600, grid=True)], tg=tg)
    start(bot, clock)
    bot.store.clear_live(OWNER, "gone")
    run_for(bot, clock, PROBE + 60)
    assert len(attempts(tg)) == 1 and bot.probe_at > clock.t


def test_text_dashboard_shows_schedule(tmp_path):
    clock = Clock(T0)
    tg = RefusingTG()
    bot, tg, _, _ = sched_bot(tmp_path, clock, [Segment(3600, grid=True)],
                              yasno_payload([(780, 900)], [(120, 300)]), tg=tg)
    start(bot, clock)
    (dash,) = text_dashboards(tg, OWNER)
    assert "По графику отключат в <b>13:00</b> до <b>15:00</b>" in dash[3]
    assert "Сегодня, ср 11.11: <b>13:00–15:00</b>" in dash[3] and "Завтра, чт 12.11: <b>02:00–05:00</b>" in dash[3]
