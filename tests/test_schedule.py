import asyncio
import json
from datetime import date, datetime

import httpx
import pytest

from svitlo.schedule import (APPLIES, EMERGENCY, WAITING, ScheduleError, ScheduleFetcher, combine,
                             contract_view, merge, parse_fallback, parse_yasno)
from tests.conftest import KYIV

D = date(2026, 11, 11)
D1 = date(2026, 11, 12)


def yasno(today_slots, tomorrow_slots=(), today_status="ScheduleApplies",
          tomorrow_status="WaitingForSchedule", today=D, tomorrow=D1):
    def day(d, slots, status):
        return {"slots": [{"start": a, "end": b, "type": t} for a, b, t in slots],
                "date": "%sT00:00:00+02:00" % d.isoformat(), "status": status}
    return {"3.1": {"today": day(today, today_slots, today_status),
                     "tomorrow": day(tomorrow, tomorrow_slots, tomorrow_status),
                     "updatedOn": "2026-11-10T20:00:02+00:00"},
            "1.1": {"today": day(today, [], "ScheduleApplies")}}


LIVE = [(0, 120, "Definite"), (120, 450, "NotPlanned"), (450, 810, "Definite"), (810, 930, "NotPlanned"),
        (930, 1320, "Definite"), (1320, 1440, "NotPlanned")]


def test_merge():
    assert merge([]) == []
    assert merge([(5, 10), (0, 5), (20, 30), (25, 40), (50, 50)]) == [(0, 10), (20, 40)]
    assert merge([(0, 100), (10, 20)]) == [(0, 100)]


def test_parse_live_sample():
    s = parse_yasno(yasno(LIVE), "3.1")
    assert s.days[D].status == APPLIES
    assert s.days[D].intervals == [(0, 120), (450, 810), (930, 1320)]
    assert s.days[D1].status == WAITING and s.days[D1].intervals == []
    o, w = contract_view(s, D)
    assert o == [[0, 120], [450, 810], [930, 1320]]
    assert w == 0


def test_adjacent_slots_glued():
    s = parse_yasno(yasno([(0, 60, "Definite"), (60, 90, "Definite"), (90, 1440, "NotPlanned")]), "3.1")
    assert contract_view(s, D)[0] == [[0, 90]]


def test_midnight_glue_and_tomorrow_offset():
    s = parse_yasno(yasno([(1320, 1440, "Definite")], [(0, 120, "Definite"), (600, 660, "Definite")],
                          tomorrow_status="ScheduleApplies"), "3.1")
    o, w = contract_view(s, D)
    assert o == [[1320, 1560], [2040, 2100]]
    assert w == 1


def test_after_midnight_rebased_without_refetch():
    """Сразу после полуночи (ещё без нового ответа) вчерашнее «завтра» становится «сегодня»."""
    s = parse_yasno(yasno([(1320, 1440, "Definite")], [(0, 120, "Definite")],
                          tomorrow_status="ScheduleApplies"), "3.1")
    o, w = contract_view(s, D1)
    assert o == [[0, 120]]
    assert w == 0


def test_stale_today_and_combine():
    """YASNO в 00:01 ещё отдаёт вчерашний день как today — старые данные на сегодня не теряются."""
    old = parse_yasno(yasno([(0, 60, "Definite")], [(300, 360, "Definite")],
                            tomorrow_status="ScheduleApplies"), "3.1")
    new = parse_yasno(yasno([(0, 60, "Definite")], [], today=D, tomorrow=D1,
                            tomorrow_status="WaitingForSchedule"), "3.1")
    # день, который есть в новом ответе, берётся из нового (он свежее)
    merged = combine(old, new, D1)
    assert merged.days[D1].status == WAITING
    # а если в новом ответе дня нет вовсе — берётся старый
    new2 = parse_yasno(yasno([(0, 60, "Definite")], today=D, tomorrow=D), "3.1")
    merged2 = combine(old, new2, D1)
    assert contract_view(merged2, D1)[0] == [[300, 360]]
    # прошлые дни из старого графика выбрасываются
    assert D not in combine(old, parse_yasno(yasno([], today=D1, tomorrow=D1), "3.1"), D1).days


def test_emergency_and_unknown_types(caplog):
    s = parse_yasno(yasno([(0, 600, "Definite")], [(0, 60, "Definite"), (60, 120, "Possible"),
                                                   (120, 180, "Weird")],
                          today_status="EmergencyShutdowns", tomorrow_status="ScheduleApplies"), "3.1")
    assert s.days[D].status == EMERGENCY and s.days[D].intervals == []
    assert s.days[D1].intervals == [(0, 60)]
    assert "Weird" in caplog.text


def test_unknown_status_keeps_definite():
    s = parse_yasno(yasno([(0, 60, "Definite")], today_status="SomethingNew"), "3.1")
    assert contract_view(s, D)[0] == [[0, 60]]


def test_missing_group():
    with pytest.raises(ScheduleError):
        parse_yasno(yasno([]), "99.9")


def fallback_payload():
    mid_d = int(datetime(2026, 11, 11, tzinfo=KYIV).timestamp())
    mid_d1 = int(datetime(2026, 11, 12, tzinfo=KYIV).timestamp())
    day = {str(h): "yes" for h in range(1, 25)}
    day.update({"1": "no", "2": "first", "8": "no", "9": "no", "24": "second", "12": "maybe", "13": "mfirst"})
    day2 = {str(h): "yes" for h in range(1, 25)}
    day2.update({"1": "no", "5": "msecond"})
    return {"fact": {"data": {str(mid_d): {"GPV3.1": day, "GPV1.1": {}},
                              str(mid_d1): {"GPV3.1": day2}},
                     "update": "10.11.2026 21:59", "today": mid_d}}


def test_parse_fallback():
    s = parse_fallback(fallback_payload(), "3.1", KYIV)
    assert s.days[D].intervals == [(0, 90), (420, 540), (1410, 1440)]
    o, w = contract_view(s, D)
    assert o == [[0, 90], [420, 540], [1410, 1500]]  # склейка через полночь, maybe не считается
    assert w == 1


def test_fetcher_falls_back():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert "Mozilla" in request.headers["User-Agent"]
        if "yasno" in request.url.host:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=fallback_payload())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ScheduleFetcher(client, "3.1", KYIV).fetch()

    s = asyncio.run(run())
    assert s.source == "outage-data-ua"
    assert len(calls) == 2


def test_fetcher_yasno_ok_and_json_roundtrip():
    def handler(request):
        return httpx.Response(200, json=yasno(LIVE))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await ScheduleFetcher(client, "3.1", KYIV).fetch()

    s = asyncio.run(run())
    assert s.source == "yasno"
    s2 = type(s).from_json(s.to_json())
    assert contract_view(s2, D) == contract_view(s, D)
    json.loads(s.to_json())


# ------------------------------------------------------------------ изменения графика (п. 5 «Жизненного цикла»)
from datetime import timedelta  # noqa: E402

from svitlo import texts  # noqa: E402
from svitlo.schedule import schedule_changes  # noqa: E402

D2 = D1 + timedelta(days=1)
A = [(0, 90, "Definite"), (90, 420, "NotPlanned"), (420, 780, "Definite"), (780, 1440, "NotPlanned")]
B = [(0, 90, "Definite"), (90, 600, "NotPlanned"), (600, 780, "Definite"), (780, 1440, "NotPlanned")]
T = [(0, 240, "Definite"), (240, 1440, "NotPlanned")]


def steps(*payloads, today=D):
    """Прогоняет последовательность ответов YASNO; возвращает тексты сообщений по шагам."""
    base, out = None, []
    for p in payloads:
        d = today
        if isinstance(p, tuple):
            p, d = p
        sched = p if not isinstance(p, dict) else parse_yasno(p, "3.1")
        base, ch = schedule_changes(base, sched, d)
        out.append([brief(c) for c in ch])
    return out


def brief(c):
    """Краткая запись изменения для сравнения в тестах (полный текст проверяет test_texts.py)."""
    when = "завтра" if c.tomorrow else "сегодня"
    if c.emergency:
        return "экстренные " + when
    ivs = ", ".join("%s–%s" % (texts.clock(a), texts.clock(b)) for a, b in c.intervals) or "нет"
    return "%s %s: %s" % (when, "изменился" if c.old is not None else "новый", ivs)


def test_first_run_and_unchanged_are_silent():
    assert steps(yasno(A), yasno(A), yasno(A)) == [[], [], []]
    applies = yasno(A, T, tomorrow_status="ScheduleApplies")
    assert steps(applies, applies) == [[], []]      # при первом запуске график на завтра уже был


def test_today_changed():
    assert steps(yasno(A), yasno(B)) == [[], ["сегодня изменился: 00:00–01:30, 10:00–13:00"]]


def test_today_changed_to_nothing():
    no = [(0, 1440, "NotPlanned")]
    assert steps(yasno(A), yasno(no))[1] == ["сегодня изменился: нет"]


def test_tomorrow_appears_then_changes():
    r = steps(yasno(A), yasno(A, T, tomorrow_status="ScheduleApplies"),
              yasno(A, T, tomorrow_status="ScheduleApplies"),
              yasno(A, A, tomorrow_status="ScheduleApplies"))
    assert r == [[], ["завтра новый: 00:00–04:00"], [], ["завтра изменился: 00:00–01:30, 07:00–13:00"]]


def test_glued_intervals_compared_not_slots():
    """Слоты разбиты по-другому, но после склейки интервалы те же — сообщения нет."""
    split = [(0, 60, "Definite"), (60, 90, "Definite"), (90, 420, "NotPlanned"), (420, 780, "Definite"),
             (780, 1440, "NotPlanned")]
    assert steps(yasno(A), yasno(split)) == [[], []]


def test_midnight_rollover_is_silent():
    """«Завтра» стало «сегодня»: сначала YASNO ещё отдаёт вчерашний ответ, потом новый — без сообщений."""
    before = yasno(A, T, tomorrow_status="ScheduleApplies")
    after = yasno(T, [], today=D1, tomorrow=D2)
    assert steps(before, (before, D1), (after, D1)) == [[], [], []]


def test_tomorrow_change_seen_after_midnight_is_today_change():
    before = yasno(A, T, tomorrow_status="ScheduleApplies")
    after = yasno(A, [], today=D1, tomorrow=D2)
    assert steps(before, (after, D1))[1] == ["сегодня изменился: 00:00–01:30, 07:00–13:00"]


def test_waiting_emergency_and_missing_are_not_no_outages():
    r = steps(yasno(A),
              yasno([], today_status="WaitingForSchedule"),          # ждём график — не «отключений нет»
              yasno(A, today_status="EmergencyShutdowns"),           # экстренные — одно сообщение со звуком
              yasno(A, today_status="EmergencyShutdowns"),           # те же экстренные — тишина
              {"3.1": {"updatedOn": ""}},                           # пустой ответ
              yasno(A))                                              # график снова действует
    assert r == [[], [], ["экстренные сегодня"], [], [], ["сегодня новый: 00:00–01:30, 07:00–13:00"]]


def test_emergency_change_is_loud_and_not_on_first_run():
    base, ch = schedule_changes(None, parse_yasno(yasno(A, today_status="EmergencyShutdowns"), "3.1"), D)
    assert ch == [] and base[D.isoformat()]["s"] == "emergency"
    _, ch = schedule_changes(base, parse_yasno(yasno(A, T, tomorrow_status="EmergencyShutdowns"), "3.1"), D)
    assert [brief(c) for c in ch] == ["сегодня новый: 00:00–01:30, 07:00–13:00", "экстренные завтра"]
    assert all(c.loud for c in ch)


def test_change_loudness_and_old_intervals():
    _, ch = schedule_changes({}, parse_yasno(yasno(A, T, tomorrow_status="ScheduleApplies"), "3.1"), D)
    assert [(brief(c), c.loud, c.old) for c in ch] == [("завтра новый: 00:00–04:00", False, None)]
    base, _ = schedule_changes(None, parse_yasno(yasno(A, T, tomorrow_status="ScheduleApplies"), "3.1"), D)
    _, ch = schedule_changes(base, parse_yasno(yasno(B, A, tomorrow_status="ScheduleApplies"), "3.1"), D)
    assert [(c.tomorrow, c.loud, c.old) for c in ch] == [(False, True, [(0, 90), (420, 780)]),
                                                         (True, False, [(0, 240)])]


def test_today_published_after_midnight_is_new_and_loud():
    waiting = yasno(A, [], tomorrow_status="WaitingForSchedule")
    after = yasno([], [], today=D1, tomorrow=D2, today_status="WaitingForSchedule")
    published = yasno(T, [], today=D1, tomorrow=D2)
    base, _ = schedule_changes(None, parse_yasno(waiting, "3.1"), D)
    base, ch = schedule_changes(base, parse_yasno(after, "3.1"), D1)
    assert ch == []
    _, ch = schedule_changes(base, parse_yasno(published, "3.1"), D1)
    assert [(brief(c), c.loud) for c in ch] == [("сегодня новый: 00:00–04:00", True)]


def test_emergency_mask():
    from svitlo.schedule import emergency_mask
    s = parse_yasno(yasno(A, T, today_status="EmergencyShutdowns", tomorrow_status="EmergencyShutdowns"), "3.1")
    assert emergency_mask(s, D) == 3 and emergency_mask(s, D1) == 1 and emergency_mask(s, D - timedelta(days=1)) == 2 and emergency_mask(None, D) == 0
    assert emergency_mask(parse_yasno(yasno(A), "3.1"), D) == 0


def test_applies_without_slots_is_skipped(caplog):
    s = parse_yasno(yasno([]), "3.1")
    assert D not in s.days and "без слотов" in caplog.text
    assert steps(yasno(A), yasno([]), yasno(A)) == [[], [], []]


def test_tomorrow_waiting_then_published():
    r = steps(yasno(A), yasno(A, [], tomorrow_status="WaitingForSchedule"),
              yasno(A, T, tomorrow_status="ScheduleApplies"))
    assert r == [[], [], ["завтра новый: 00:00–04:00"]]


def test_source_switch_is_silent_both_ways():
    """YASNO и запасной источник расходятся: переключение источника сообщений не даёт,
    изменение в пределах одного источника — даёт."""
    y = parse_yasno(yasno(A), "3.1")
    fb = parse_fallback(fallback_payload(), "3.1", KYIV)     # на 11.11 у него другие интервалы
    assert y.days[D].intervals != fb.days[D].intervals
    y2 = parse_yasno(yasno(B), "3.1")
    assert steps(y, fb, y, fb, y, y2) == [[], [], [], [], [],
                                          ["сегодня изменился: 00:00–01:30, 10:00–13:00"]]


def test_base_drops_past_days():
    base, _ = schedule_changes(None, parse_yasno(yasno(A, T, tomorrow_status="ScheduleApplies"), "3.1"), D)
    base, _ = schedule_changes(base, parse_yasno(yasno(T, [], today=D1, tomorrow=D2), "3.1"), D1)
    assert sorted(base) == [D1.isoformat(), D2.isoformat()]


def test_fetcher_keeps_yasno_through_short_outage():
    """После успешного YASNO его сбои до FALLBACK_AFTER не переключают на запасной источник
    (остаётся прошлый график); дольше — переключают."""
    now = [1000.0]
    yasno_up = [True]
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if "yasno" in request.url.host:
            return httpx.Response(200, json=yasno(LIVE)) if yasno_up[0] else httpx.Response(503)
        return httpx.Response(200, json=fallback_payload())

    async def fetch(f):
        return await f.fetch()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            f = ScheduleFetcher(client, "3.1", KYIV, clock=lambda: now[0], fallback_after=1800)
            assert (await f.fetch()).source == "yasno"
            yasno_up[0] = False
            now[0] += 600
            with pytest.raises(ScheduleError):
                await f.fetch()
            assert all("yasno" in h for h in calls)
            now[0] += 1300
            assert (await f.fetch()).source == "outage-data-ua"
            yasno_up[0] = True
            assert (await f.fetch()).source == "yasno"

    asyncio.run(run())
