"""Журнал точности прогноза разряда и метрика (svitlo/accuracy.py) на синтетических сценариях."""

from datetime import datetime

import pytest

from svitlo.accuracy import evaluate, periods, report
from svitlo.source import FakeSource, Segment, SourceError
from svitlo.state import Engine
from svitlo.storage import LogRow, Store
from tests.conftest import KYIV

T0 = datetime(2026, 11, 11, 12, 0, tzinfo=KYIV).timestamp()
REAL_KWH = 10.24


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def simulate(tmp_path, segments, kwh, soc=90.0, seconds=None):
    clock = Clock(T0)
    store = Store(tmp_path / "db.sqlite3")
    engine = Engine(store, KYIV, battery_kwh=kwh)
    src = FakeSource(segments, clock, soc=soc, capacity_wh=REAL_KWH * 1000)
    end = T0 + (seconds if seconds is not None else sum(s.duration for s in segments))
    while clock.t < end:
        try:
            engine.observe(src.read())
        except SourceError:
            engine.read_failed()
        clock.t += 10
    return store


def test_log_written_during_discharge_only(tmp_path):
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(3 * 3600, grid=False, load_w=500),
                                Segment(600, grid=True)], REAL_KWH)
    rows = store.log_rows()
    assert rows and len({r.period for r in rows}) == 1
    f = [r for r in rows if r.kind == "f"]
    t = [r for r in rows if r.kind == "t"]
    assert 17 <= len(f) <= 19                                 # раз в ~10 минут за 3 часа
    assert all(b.ts - a.ts == 600 for a, b in zip(f, f[1:]))
    assert all(r.eta > r.ts and r.power > 0 and r.soc_f is not None for r in f)
    # 500 Вт × 3 ч = 1.5 кВт·ч ≈ 14.6% батареи 10.24 кВт·ч — тики по 1%
    assert 13 <= len(t) <= 15 and [r.soc for r in t] == list(range(t[0].soc, t[0].soc - len(t), -1))
    assert all(r.ts >= rows[0].period for r in rows)


def test_accurate_forecast_scores_near_zero(tmp_path):
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(4 * 3600, grid=False, load_w=500),
                                Segment(600, grid=True)], REAL_KWH)
    (p,) = periods(store)
    assert p.checks and not p.reached_cutoff
    assert all(abs(c.error) < 0.05 for c in p.checks)
    text = report(store, KYIV)
    assert "Без сети 11.11 12:10" in text and "медиана |ошибки|" in text


def test_wrong_capacity_shows_optimism(tmp_path):
    """В настройке вдвое большая ёмкость, чем у батареи: прогноз обещает вдвое больше → ошибка ≈ +100%."""
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(4 * 3600, grid=False, load_w=500),
                                Segment(600, grid=True)], REAL_KWH * 2)
    (p,) = periods(store)
    assert all(0.9 < c.error < 1.1 for c in p.checks)


def test_load_increase_makes_early_forecasts_optimistic(tmp_path):
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(2 * 3600, grid=False, load_w=400),
                                Segment(2 * 3600, grid=False, load_w=800), Segment(600, grid=True)], REAL_KWH)
    (p,) = periods(store)
    first, last = p.checks[0], p.checks[-1]
    assert first.error > 0.25                       # при 400 Вт обещал больше, чем вышло при 400→800
    assert abs(last.error) < 0.1                    # после смены нагрузки прогноз верный


def test_reaching_cutoff(tmp_path):
    """Разряд до порога 10%. За 10 мин с сетью батарея заряжается 30 → ≈34.8% (3 кВт), дальше
    800 Вт: до порога ≈24.8% × 10.24 кВт·ч / 800 Вт ≈ 3 ч 10 мин — поэтому без сети 4 часа
    (за 3 часа порог не достигается: останавливается на ≈11.4%)."""
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(4 * 3600, grid=False, load_w=800),
                                Segment(600, grid=True)], REAL_KWH, soc=30.0)
    (p,) = periods(store)
    assert p.reached_cutoff and p.checks and all(abs(c.error) < 0.05 for c in p.checks)
    assert min(r.soc for r in store.log_rows()) == 10          # ниже порога инвертор не разряжает


def test_ticks_below_cutoff_not_scored():
    """Показание SOC упало ниже порога (округление BMS после отключения): этот отрезок прогноз
    не описывает, в метрику он не входит, но порог считается достигнутым."""
    t = 1_000_000
    rows = [LogRow(t, t, "f", 16, 16.5, 500.0, t + int(6.5 * 3600), 10),     # 1%/ч
            LogRow(t, t + 1800, "t", 15, None, None, None, 10)]
    rows += [LogRow(t, t + 1800 + k * 3600, "t", 15 - k, None, None, None, 10) for k in range(1, 6)]
    rows.append(LogRow(t, t + 1800 + 5 * 3600 + 60, "t", 9, None, None, None, 10))
    p = evaluate(rows)
    (c,) = p.checks
    assert (c.a, c.b, c.actual) == (15, 10, 5 * 3600) and c.error == pytest.approx(0.0)
    assert p.reached_cutoff


def test_current_period_excluded(tmp_path):
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(4 * 3600, grid=False, load_w=500)], REAL_KWH)
    assert periods(store) == []
    assert len(periods(store, include_current=True)) == 1
    assert "Журнал пуст" in report(store, KYIV)


def test_no_capacity_nothing_to_score(tmp_path):
    store = simulate(tmp_path, [Segment(600, grid=True), Segment(3 * 3600, grid=False, load_w=500),
                                Segment(600, grid=True)], 0)
    (p,) = periods(store)
    assert p.checks == [] and p.skipped > 0
    assert all(r.eta == 0 for r in store.log_rows() if r.kind == "f")   # журнал всё равно ведётся


def test_metric_formula():
    """Прогноз в 12:00 при SOC 80.5% и пороге 10%: e через 70.5 ч → 1%/ч. Факт: 70→60% за 5 ч (2%/ч).
    Прогноз проходит 10% за 10 ч, на деле 5 ч → ошибка +100%."""
    t = 1_000_000
    rows = [LogRow(t, t, "f", 80, 80.5, 500.0, t + int(70.5 * 3600), 10),
            LogRow(t, t + 1800, "t", 70, None, None, None, 10),
            LogRow(t, t + 1800 + 5 * 3600, "t", 60, None, None, None, 10)]
    p = evaluate(rows)
    (c,) = p.checks
    assert (c.a, c.b, c.actual) == (70, 60, 5 * 3600)
    assert c.predicted == pytest.approx(10 * 3600)
    assert c.error == pytest.approx(1.0)


def test_short_stretch_skipped():
    t = 1_000_000
    rows = [LogRow(t, t, "f", 80, 80.0, 500.0, t + 70 * 3600, 10),
            LogRow(t, t + 600, "t", 79, None, None, None, 10),
            LogRow(t, t + 3600, "t", 78, None, None, None, 10)]
    p = evaluate(rows)
    assert p.checks == [] and p.skipped == 1
