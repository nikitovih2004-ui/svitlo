import json
from datetime import datetime

from svitlo.estimator import hold_step
from svitlo.schedule import parse_yasno
from svitlo.source import FakeSource, Segment, SourceError
from svitlo.state import CHAT_DATA_LIMIT, KEY_ORDER, Engine, GridTracker, encode, same_content
from svitlo.storage import Store
from tests.conftest import KYIV, reading

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=KYIV).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def drive(engine, source, clock, seconds, step=10):
    """Гоняет движок по сценарию; возвращает список не-начальных переходов."""
    events = []
    end = clock.t + seconds
    while clock.t < end:
        try:
            tr = engine.observe(source.read())
        except SourceError:
            engine.read_failed()
            tr = None
        if tr is not None and not tr.initial:
            events.append(tr)
        clock.t += step
    return events


def test_tracker_needs_confirmation():
    t = GridTracker(170)
    assert t.observe(reading(0, grid=True)) is None
    tr = t.observe(reading(10, grid=True))
    assert tr.initial and tr.grid and tr.since == 0
    # одиночный провал напряжения — не событие
    assert t.observe(reading(20, grid=True, grid_v=40)) is None
    assert t.observe(reading(30, grid=True)) is None
    assert t.state.grid
    # два чтения подряд — событие, время = первое чтение
    assert t.observe(reading(40, grid=False)) is None
    tr = t.observe(reading(50, grid=False))
    assert not tr.grid and tr.since == 40 and tr.prev == 0  # прошлое началось неизвестно когда
    assert t.observe(reading(60, grid=True)) is None
    tr = t.observe(reading(70, grid=True))
    assert tr.grid and tr.since == 60 and tr.prev == 20


def test_threshold_uses_voltage_not_relay():
    t = GridTracker(170)
    assert not t.raw(reading(0, grid=True, grid_v=169.9))
    assert t.raw(reading(0, grid=False, grid_v=171))


def test_no_event_on_connection_loss(tmp_path):
    clock = Clock(T0)
    store = Store(tmp_path / "db.sqlite3")
    engine = Engine(store, KYIV)
    src = FakeSource([Segment(600, grid=False), Segment(1200, grid=False, offline=True),
                      Segment(600, grid=False)], clock, soc=70)
    events = drive(engine, src, clock, 2400)
    assert events == []
    assert engine.tracker.state.grid is False
    assert store.events() == []


def test_stale_flag(tmp_path):
    clock = Clock(T0)
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV, stale_after=60)
    src = FakeSource([Segment(60, grid=True), Segment(600, grid=True, offline=True)], clock)
    drive(engine, src, clock, 60)
    assert not engine.is_stale(clock.t)
    drive(engine, src, clock, 300)
    assert engine.is_stale(clock.t)
    assert engine.ready  # состояние есть, просто устарело


def test_scenario_events(tmp_path):
    clock = Clock(T0)
    store = Store(tmp_path / "db.sqlite3")
    engine = Engine(store, KYIV)
    src = FakeSource([Segment(1800, grid=True), Segment(10, grid=True, grid_v=50),  # провал
                      Segment(590, grid=True), Segment(3600, grid=False), Segment(300, grid=False, offline=True),
                      Segment(3600, grid=False), Segment(1800, grid=True)], clock, soc=80)
    events = drive(engine, src, clock, 2400 + 7500 + 1800)
    assert [e.grid for e in events] == [False, True]
    assert events[1].prev == 7500
    assert len(store.events()) == 2


def test_restart_keeps_state_and_no_false_event(tmp_path):
    db = tmp_path / "db.sqlite3"
    clock = Clock(T0)
    store = Store(db)
    engine = Engine(store, KYIV)
    src = FakeSource([Segment(600, grid=True), Segment(7200, grid=False)], clock, soc=80)
    events = drive(engine, src, clock, 1200)
    assert len(events) == 1
    since = engine.tracker.state.since
    rev = engine.rev
    store.close()

    store2 = Store(db)
    engine2 = Engine(store2, KYIV)
    assert not engine2.ready  # до первого чтения ничего не публикуем
    assert engine2.tracker.state.since == since and engine2.tracker.state.grid is False
    events = drive(engine2, src, clock, 600)
    assert events == []
    assert engine2.epoch == since
    assert engine2.chat_data(clock.t) is not None and engine2.rev > rev  # ревизия монотонна


def test_restart_after_real_change_during_downtime(tmp_path):
    db = tmp_path / "db.sqlite3"
    clock = Clock(T0)
    store = Store(db)
    engine = Engine(store, KYIV)
    src = FakeSource([Segment(600, grid=True), Segment(1200, grid=False), Segment(3600, grid=True)], clock)
    drive(engine, src, clock, 900)          # сеть пропала, событие было
    store.close()
    clock.t = T0 + 2400                      # бот лежал, свет вернулся
    engine2 = Engine(Store(db), KYIV)
    events = drive(engine2, src, clock, 60)
    assert len(events) == 1 and events[0].grid is True
    assert events[0].prev == int(T0 + 2400) - int(T0 + 600)


def test_chat_data_contract(tmp_path):
    clock = Clock(T0)
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV, battery_kwh=10.24)
    sched = parse_yasno({"3.1": {"today": {"date": "2026-11-11T00:00:00+02:00", "status": "ScheduleApplies",
                                            "slots": [{"start": 1320, "end": 1440, "type": "Definite"}]},
                                  "tomorrow": {"date": "2026-11-12T00:00:00+02:00",
                                               "status": "ScheduleApplies",
                                               "slots": [{"start": 0, "end": 90, "type": "Definite"}]}}},
                        "3.1")
    engine.set_schedule(sched, clock.t)
    src = FakeSource([Segment(3600, grid=False, load_w=420)], clock, soc=87)
    drive(engine, src, clock, 30)
    data = engine.chat_data(clock.t)
    st = json.loads(data)
    assert list(st.keys()) == [k for k in KEY_ORDER if k in st]
    assert set(st) == {"r", "g", "s", "p", "c", "b", "l", "u", "e", "h", "o", "d", "w", "m", "t"}
    assert st["t"] == int(clock.t - 10)   # последнее успешное чтение
    assert st["g"] == 0 and st["u"] == 0 and st["c"] in (86, 87)
    assert st["b"] == 420 and st["l"] == 420
    assert st["o"] == [[1320, 1530]] and st["w"] == 1
    assert st["d"] == int(datetime(2026, 11, 11, tzinfo=KYIV).timestamp())
    assert st["e"] > clock.t and st["e"] % 300 == 0
    assert abs(st["e"] - (st["t"] + st["h"])) <= hold_step(st["h"]) + 300  # без сети `h` и `e` — одна оценка
    assert len(data.encode()) <= CHAT_DATA_LIMIT
    assert " " not in data


def test_revision_only_on_change(tmp_path):
    clock = Clock(T0)
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)
    engine.observe(reading(T0, grid=True, soc=100))
    engine.observe(reading(T0 + 10, grid=True, soc=100))
    a = json.loads(engine.chat_data(T0 + 10))["r"]
    b = json.loads(engine.chat_data(T0 + 20))["r"]
    assert a == b
    engine.observe(reading(T0 + 30, grid=True, soc=100, load_w=900))
    assert json.loads(engine.chat_data(T0 + 30))["r"] == a + 1
    assert engine.peek(T0 + 40) == engine.chat_data(T0 + 40)  # peek ревизию не поднимает


def test_midnight_rolls_d_and_o(tmp_path):
    clock = Clock(datetime(2026, 11, 11, 23, 58, tzinfo=KYIV).timestamp())
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)
    sched = parse_yasno({"3.1": {"today": {"date": "2026-11-11T00:00:00+02:00", "status": "ScheduleApplies",
                                            "slots": [{"start": 1320, "end": 1440, "type": "Definite"}]},
                                  "tomorrow": {"date": "2026-11-12T00:00:00+02:00",
                                               "status": "ScheduleApplies",
                                               "slots": [{"start": 0, "end": 90, "type": "Definite"},
                                                         {"start": 600, "end": 660, "type": "Definite"}]}}},
                        "3.1")
    engine.set_schedule(sched, clock.t)
    engine.observe(reading(clock.t, grid=False))
    engine.observe(reading(clock.t + 10, grid=False))
    before = json.loads(engine.chat_data(clock.t + 10))
    after = json.loads(engine.chat_data(clock.t + 300))  # 00:03 следующего дня
    assert before["o"] == [[1320, 1530], [2040, 2100]] and before["w"] == 1
    assert after["d"] - before["d"] == 86400
    assert after["o"] == [[0, 90], [600, 660]] and after["w"] == 0
    assert after["r"] == before["r"] + 1


def test_encode_limit_trims_schedule():
    st = {"r": 1, "g": 0, "s": 1, "p": 0, "c": 50, "b": 400, "l": 400, "u": 0, "e": 0,
          "o": [[i * 10, i * 10 + 5] for i in range(288)], "d": 0, "w": 1}
    text = encode(st)
    assert len(text.encode()) <= CHAT_DATA_LIMIT
    o = json.loads(text)["o"]
    assert o and o[0] == [0, 5] and len(o) < 288


def test_no_capacity_means_no_eta(tmp_path):
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)   # BATTERY_KWH не задан
    for i in range(20):
        engine.observe(reading(T0 + i * 10, grid=False, soc=87, batt_w=420))
    assert json.loads(engine.chat_data(T0 + 200))["e"] == 0


def test_t_tracks_last_successful_read(tmp_path):
    clock = Clock(T0)
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)
    src = FakeSource([Segment(60, grid=True), Segment(600, grid=True, offline=True)], clock, soc=100)
    drive(engine, src, clock, 60)
    t_ok = json.loads(engine.chat_data(clock.t))["t"]
    assert t_ok == int(clock.t - 10)
    drive(engine, src, clock, 400)
    assert json.loads(engine.peek(clock.t))["t"] == t_ok        # связи нет — t стоит


def test_same_content_ignores_r_and_t(tmp_path):
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)
    engine.observe(reading(T0, grid=True, soc=100))
    engine.observe(reading(T0 + 10, grid=True, soc=100))
    a = engine.chat_data(T0 + 10)
    engine.observe(reading(T0 + 20, grid=True, soc=100))
    b = engine.chat_data(T0 + 20)
    assert json.loads(b)["t"] == json.loads(a)["t"] + 10 and json.loads(b)["r"] > json.loads(a)["r"]
    assert same_content(a, b)
    assert not same_content(a, b.replace('"c":100', '"c":99'))


def test_event_text(tmp_path):
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)
    t = datetime(2026, 11, 11, 13, 0, tzinfo=KYIV).timestamp()
    engine.observe(reading(t, grid=True))
    engine.observe(reading(t + 10, grid=True))
    assert engine.event_text() is None                      # первый запуск: неизвестно с какого времени
    engine.observe(reading(t + 7320, grid=False))           # 15:02
    engine.observe(reading(t + 7330, grid=False))
    assert engine.event_text() == "🔴 Свет пропал в 15:02."    # длительность прошлого неизвестна
    engine.observe(reading(t + 7320 + 22140, grid=True))    # через 6 ч 09 мин
    engine.observe(reading(t + 7330 + 22140, grid=True))
    assert engine.event_text() == "🟢 Свет появился в 21:11. Не было 6 ч 09 мин."
    engine.observe(reading(t + 36600, grid=False))
    engine.observe(reading(t + 36610, grid=False))
    assert engine.event_text() == "🔴 Свет пропал в 23:10. Был 1 ч 59 мин."


def test_chat_data_worst_case_fits_with_margin(tmp_path):
    """Худший реалистичный случай: 6 отдельных отключений в сутки на сегодня и на завтра
    (12 интервалов, четырёхзначные минуты), все числа максимальной длины, есть `h`."""
    day = [(s, s + 90, "Definite") for s in (0, 240, 480, 720, 960, 1200)]
    gaps = [(s + 90, s + 240, "NotPlanned") for s in (0, 240, 480, 720, 960, 1200)]
    slots = sorted(day + gaps)[:-1] + [(1290, 1440, "NotPlanned")]
    sched = parse_yasno({"3.1": {"today": {"date": "2026-11-11T00:00:00+02:00", "status": "ScheduleApplies",
                                            "slots": [{"start": a, "end": b, "type": t} for a, b, t in slots]},
                                  "tomorrow": {"date": "2026-11-12T00:00:00+02:00", "status": "ScheduleApplies",
                                               "slots": [{"start": a, "end": b, "type": t} for a, b, t in slots]}}},
                        "3.1")
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV, battery_kwh=14.34)
    engine.set_schedule(sched, T0)
    engine.observe(reading(T0, grid=False, soc=100, batt_w=-1, load_w=1))
    engine.observe(reading(T0 + 10, grid=False, soc=100, batt_w=-1, load_w=1))
    snap = engine.snapshot(T0 + 10)
    assert len(snap["o"]) == 12 and snap["o"][-1] == [2640, 2730]
    # подставляем максимальные по длине значения остальных ключей
    snap.update({"s": 9999999999, "p": 9999999, "c": 100, "b": -10000, "l": 10000, "u": 255,
                 "e": 9999999999, "h": 9999999, "d": 9999999999, "t": 9999999999})
    text = encode(dict(snap, r=99999999))
    assert json.loads(text)["o"] == snap["o"]            # ничего не обрезано
    assert set(json.loads(text)) == set(KEY_ORDER)
    assert len(text.encode("utf-8")) <= CHAT_DATA_LIMIT // 2, len(text.encode("utf-8"))


def test_h_present_with_grid(tmp_path):
    """Свет есть, батарея полная: `e` = 0, а `h` — «без сети хватит на …» по сглаженной нагрузке."""
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV, battery_kwh=14.34)
    for i in range(10):
        engine.observe(reading(T0 + i * 10, grid=True, soc=100, batt_w=0, load_w=400))
    st = json.loads(engine.chat_data(T0 + 100))
    assert st["g"] == 1 and st["e"] == 0
    raw = 0.9 * 14340 / 400 * 3600
    assert abs(st["h"] - raw) <= hold_step(raw)
    assert list(st) == [k for k in KEY_ORDER if k in st]


def test_h_zero_without_capacity(tmp_path):
    engine = Engine(Store(tmp_path / "db.sqlite3"), KYIV)
    for i in range(10):
        engine.observe(reading(T0 + i * 10, grid=True, soc=100, load_w=400))
    assert json.loads(engine.chat_data(T0 + 100))["h"] == 0


def test_set_schedule_queues_notices_once(tmp_path):
    """Изменение графика ставится в очередь один раз и переживает перезапуск без повтора."""
    db = tmp_path / "db.sqlite3"

    def y(slots, tomorrow=()):
        def day(d, sl, st):
            return {"date": d, "status": st, "slots": [{"start": a, "end": b, "type": "Definite"} for a, b in sl]}
        return parse_yasno({"3.1": {"today": day("2026-11-11T00:00:00+02:00", slots, "ScheduleApplies"),
                                     "tomorrow": day("2026-11-12T00:00:00+02:00", tomorrow,
                                                     "ScheduleApplies" if tomorrow else "WaitingForSchedule")}},
                           "3.1")

    store = Store(db)
    engine = Engine(store, KYIV)
    assert engine.set_schedule(y([(0, 90)]), T0) == []                 # первый запуск
    assert engine.set_schedule(y([(0, 90)]), T0 + 300) == []
    store.close()
    store = Store(db)
    engine = Engine(store, KYIV)                                        # перезапуск
    assert engine.set_schedule(y([(0, 90)]), T0 + 600) == []
    today = ("🔄 <b>График на сегодня изменился</b> · ср, 11 ноября\n\n"
             "с <b>00:00</b> до <b>02:00</b> · 2 ч  ← <i>было до 01:30</i>\n\n"
             "Всего без света 2 ч (было 1 ч 30 мин)")
    tomorrow = "🗓 <b>График на завтра</b> · чт, 12 ноября\n\nс <b>01:00</b> до <b>02:00</b> · 1 ч\n\nВсего без света 1 ч"
    assert engine.set_schedule(y([(0, 120)]), T0 + 900) == [today]
    assert engine.set_schedule(y([(0, 120)], [(60, 120)]), T0 + 1200) == [tomorrow]
    assert [(n.text, n.loud) for n in store.notices_after(0)] == [(today, True), (tomorrow, False)]
    assert store.last_notice_id() == 2
