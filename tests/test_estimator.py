import math

from svitlo.estimator import STEP, ChargeCurve, Estimator
from tests.conftest import reading

T0 = 1_794_400_000.0
CAP_WH = 200 * 51.2


def run_discharge(est, minutes, power, soc0=87.0, t0=T0, spikes=()):
    soc = soc0
    etas = []
    for i in range(int(minutes * 6)):
        ts = t0 + i * 10
        p = power + (2500 if i in spikes else 0)
        etas.append(est.update(reading(ts, grid=False, soc=int(math.floor(soc)), batt_w=p), grid_on=False))
        soc -= p * 10 / 3600 / CAP_WH * 100
    return etas


def test_discharge_formula():
    est = Estimator(CAP_WH)
    eta = est.update(reading(T0, grid=False, soc=87, batt_w=400), grid_on=False)
    expected = T0 + (87 - 10) / 100 * CAP_WH / 400 * 3600
    assert abs(eta - expected) <= STEP / 2
    assert eta % STEP == 0


def test_discharge_stable_over_time():
    """Постоянная нагрузка 400 Вт, SOC целый: прогноз не дёргается."""
    est = Estimator(CAP_WH)
    etas = run_discharge(est, 60, 400)[6:]  # первая минута — прогрев (дробная часть SOC неизвестна)
    distinct = sorted(set(etas))
    assert len(distinct) <= 2, distinct
    assert distinct[-1] - distinct[0] <= STEP


def test_discharge_ignores_single_spike():
    est = Estimator(CAP_WH)
    etas = run_discharge(est, 30, 400, spikes={60})[6:]
    assert max(etas) - min(etas) <= STEP


def test_discharge_follows_sustained_change():
    est = Estimator(CAP_WH)
    run_discharge(est, 30, 400)
    before = est.eta
    etas = run_discharge(est, 30, 800, soc0=est.soc_f, t0=T0 + 1800)
    # нагрузка удвоилась → оставшееся время примерно вдвое меньше
    remaining_before = before - (T0 + 1800)
    remaining_after = etas[-1] - (T0 + 3600)
    assert 0.4 < remaining_after / remaining_before < 0.6


def test_no_forecast_cases():
    est = Estimator(CAP_WH)
    assert est.update(reading(T0, grid=False, soc=9, batt_w=400), grid_on=False) == 0   # ниже порога
    est = Estimator(CAP_WH)
    assert est.update(reading(T0, grid=False, soc=50, batt_w=5), grid_on=False) == 0    # нет нагрузки
    est = Estimator(CAP_WH)
    assert est.update(reading(T0, grid=True, soc=100, batt_w=0), grid_on=True) == 0     # полная
    est = Estimator(CAP_WH)
    assert est.update(reading(T0, grid=True, soc=60, batt_w=20), grid_on=True) == 0     # не заряжается


def test_charge_physical_estimate():
    est = Estimator(CAP_WH)
    eta = est.update(reading(T0, grid=True, soc=50, batt_w=-3000), grid_on=True)
    per = CAP_WH / 100 / 3000 * 3600
    expected = T0 + per * (40 + 5 * 1.6 + 5 * 2.6)
    assert abs(eta - expected) <= STEP / 2


def test_charge_learning_and_use():
    """Зарядка 1% за 120 с: кривая учится, первый (неполный) процент не учитывается."""
    curve = ChargeCurve()
    est = Estimator(CAP_WH, curve)
    soc = 60.5
    for i in range(6 * 25):  # 25 минут
        ts = T0 + i * 10
        est.update(reading(ts, grid=True, soc=int(soc), batt_w=-3000), grid_on=True)
        soc += 10 / 120
    assert curve.get(62) is not None and abs(curve.get(62) - 120) <= 10
    assert curve.get(67) is not None and abs(curve.get(67) - 120) <= 10
    assert curve.get(80) is None
    assert curve.dirty


def test_learned_curve_overrides_physics():
    curve = ChargeCurve({b: (600.0, 3) for b in range(20)})
    est = Estimator(CAP_WH, curve)
    eta = est.update(reading(T0, grid=True, soc=90, batt_w=-3000), grid_on=True)
    assert abs(eta - (T0 + 10 * 600)) <= STEP / 2


def test_learning_rejects_gaps_and_discharge():
    curve = ChargeCurve()
    est = Estimator(CAP_WH, curve)
    est.update(reading(T0, grid=True, soc=60, batt_w=-3000), grid_on=True)
    est.update(reading(T0 + 100, grid=True, soc=61, batt_w=-3000), grid_on=True)   # первая отметка
    est.update(reading(T0 + 1000, grid=True, soc=62, batt_w=-3000), grid_on=True)  # разрыв > 120 с
    assert curve.data == {}
    est.update(reading(T0 + 1010, grid=True, soc=62, batt_w=200), grid_on=True)    # разряд при сети
    est.update(reading(T0 + 1020, grid=True, soc=63, batt_w=-3000), grid_on=True)
    assert curve.data == {}


def test_mode_switch_resets_power():
    est = Estimator(CAP_WH)
    for i in range(30):
        est.update(reading(T0 + i * 10, grid=True, soc=60, batt_w=-3000), grid_on=True)
    est.update(reading(T0 + 300, grid=False, soc=60, batt_w=400), grid_on=False)
    assert est.mode == "discharge" and est.power == 400


def test_no_capacity_no_discharge_forecast():
    """BATTERY_KWH не задан: при разряде e=0, физической оценки заряда тоже нет."""
    est = Estimator(0)
    for i in range(30):
        assert est.update(reading(T0 + i * 10, grid=False, soc=87, batt_w=400), grid_on=False) == 0
    est = Estimator(0)
    assert est.update(reading(T0, grid=True, soc=50, batt_w=-3000), grid_on=True) == 0


def test_no_capacity_no_forecast_even_with_learned_curve():
    """Контракт: ёмкость не задана — `e` всегда 0, даже если кривая заряда уже выучена."""
    curve = ChargeCurve({b: (120.0, 3) for b in range(20)})
    est = Estimator(0, curve)
    for i in range(30):
        assert est.update(reading(T0 + i * 10, grid=True, soc=90, batt_w=-3000), grid_on=True) == 0
    for i in range(30):
        assert est.update(reading(T0 + 300 + i * 10, grid=False, soc=90, batt_w=400), grid_on=False) == 0


def test_ema_about_12_minutes():
    """Скачок нагрузки 400 → 800 Вт: через 12 мин сглаженная мощность прошла ≈63% пути."""
    est = Estimator(CAP_WH)
    run_discharge(est, 30, 400)
    run_discharge(est, 12, 800, soc0=est.soc_f, t0=T0 + 1800)
    share = (est.power - 400) / 400
    assert 0.55 < share < 0.70, share


def test_kettle_softer_than_before_and_recovers():
    """Чайник +2 кВт на 3 мин при фоне 400 Вт: с EMA 12 мин прогноз проседает меньше, чем с
    прежней EMA 5 мин, и через 45 мин возвращается к прежнему значению."""
    def dip(tau):
        est = Estimator(CAP_WH, tau=tau)
        run_discharge(est, 30, 400)
        before = est.eta - (T0 + 1800)
        etas = run_discharge(est, 3, 2400, soc0=est.soc_f, t0=T0 + 1800)
        low = etas[-1] - (T0 + 1800 + 180)
        etas = run_discharge(est, 45, 400, soc0=est.soc_f, t0=T0 + 1980)
        return low / before, etas[-1], before

    low12, eta_back, before = dip(720)
    low5, _, _ = dip(300)
    assert low12 > low5 + 0.1
    # момент «батарея кончится» через 45 мин вернулся к прежнему за вычетом энергии самого чайника (100 Вт·ч ≈ 15 мин)
    assert abs(eta_back - (T0 + 1800 + before - 15 * 60)) <= 0.05 * before


# ------------------------------------------------------------------ `h`: на сколько хватит батареи
from svitlo.estimator import hold_step  # noqa: E402


def test_hold_with_grid_uses_smoothed_load():
    """При сети `h` — «если свет пропадёт сейчас»: (SOC − порог) × ёмкость / сглаженная нагрузка."""
    est = Estimator(CAP_WH)
    for i in range(6):
        est.update(reading(T0 + i * 10, grid=True, soc=100, batt_w=0, load_w=400), grid_on=True)
    raw = (100 - 10) / 100 * CAP_WH / 400 * 3600
    assert est.eta == 0                                   # полная батарея — прогноза заряда нет
    assert abs(est.hold - raw) <= hold_step(raw) / 2 and est.hold % hold_step(est.hold) == 0


def test_hold_matches_eta_when_discharging():
    est = Estimator(CAP_WH)
    run_discharge(est, 20, 400)
    r = est.last
    assert abs((est.eta - r.ts) - est.hold) <= hold_step(est.hold) + STEP


def test_hold_zero_cases():
    est = Estimator(0)                                    # ёмкость не задана
    est.update(reading(T0, grid=True, soc=100, load_w=400), grid_on=True)
    assert est.hold == 0
    est = Estimator(CAP_WH)                               # нагрузки нет
    est.update(reading(T0, grid=True, soc=100, load_w=0), grid_on=True)
    assert est.hold == 0
    est = Estimator(CAP_WH)                               # SOC на пороге
    est.update(reading(T0, grid=True, soc=10, load_w=400, cutoff=10), grid_on=True)
    assert est.hold == 0


def hold_series(loads, soc=100):
    est = Estimator(CAP_WH)
    out = []
    for i, load in enumerate(loads):
        est.update(reading(T0 + i * 10, grid=True, soc=soc, load_w=load), grid_on=True)
        out.append(est.hold)
    return out


def changes(values):
    return [(a, b) for a, b in zip(values, values[1:]) if a != b]


def test_hold_does_not_twitch_on_noise():
    """Нагрузка скачет ±15 % вокруг 400 Вт каждые 10 с: `h` за час не меняется вовсе."""
    for soc in (100, 40, 14):                             # ≈ 2 сут, ≈ 20 ч, ≈ 1 ч
        values = hold_series([400] + [400 + (60 if i % 2 else -60) for i in range(360)], soc)
        assert changes(values) == [], (soc, changes(values))


def test_hold_kettle_moves_in_steps():
    """Чайник 1,8 кВт на 3 минуты (≈ 23 ч → ≈ 12 ч и обратно за ~40 мин по EMA): `h` меняется
    ступенями не меньше шага (≥ 5 мин), в среднем не чаще раза в 2 минуты."""
    values = hold_series([400 + (1800 if 60 <= i < 78 else 0) for i in range(360)])
    ch = changes(values)
    assert 2 <= len(ch) <= 30, ch
    assert all(abs(b - a) >= hold_step(min(a, b)) for a, b in ch)
    assert all(v % hold_step(v) == 0 for v in values)


def test_hold_step_ladder():
    assert [hold_step(x * 3600) for x in (1, 2, 4, 8, 20, 48)] == [300, 600, 900, 1800, 3600, 7200]
