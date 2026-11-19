"""Прогнозы: когда батарея дойдёт до порога отключения (без сети) и когда будет 100% (с сетью).

* Ёмкость (энергия, Вт·ч) берётся только из настройки BATTERY_KWH. Регистр 204 инвертора
  (А·ч) не используется: там вписано вручную неверное значение. Нет настройки — прогноза нет
  совсем (`e = 0` и при разряде, и при заряде; docs/contract.md, «Жизненный цикл», п. 1).
* Разряд: (SOC − порог) × ёмкость / сглаженная мощность разряда (EMA ≈ 12 мин; перед EMA —
  медиана трёх последних чтений, чтобы одиночный выброс в 10 с не сдвигал прогноз на часы,
  а EMA гасит чайник/микроволновку на несколько минут).
* Заряд: для каждой 5%-корзины SOC храним EMA «секунд на 1%» из реальных зарядок этой
  батареи (ёмкость при этом не подбирается). ETA = сумма по процентам до 100%. Корзины без
  данных оцениваются физически: ёмкость / сглаженная мощность заряда, с замедлением после 90%.
* SOC инвертора целый, поэтому внутри процента используем «дробный» SOC — интегрируем
  мощность батареи и держим результат в пределах ±0.5% от показания. Без этого прогноз
  прыгал бы на длительность целого процента (при 400 Вт это ≈25 мин).
* Результат округляется до 5 минут и меняется только при сдвиге не меньше чем на шаг.
* «На сколько хватит» (`h`, считается всегда, и при сети): (SOC − порог) × ёмкость / мощность.
  Мощность — при разряде без сети та же сглаженная мощность батареи, что и в прогнозе `e`
  (поэтому `h` и `e` сходятся; КПД инвертора отдельно не вводится ни там, ни там: мощность
  батареи его уже включает); при сети — нагрузка квартиры с тем же сглаживанием (медиана трёх +
  EMA ≈ 12 мин). Округление ступенчатое (`hold_step`), с гистерезисом в одну ступень: виджет
  показывает минуты до 1,5 ч и целые часы дальше, поэтому мелкие сдвиги правку не вызывают.
"""

import math
from typing import Dict, List, Optional, Tuple

from .source import Reading

BUCKET_PCT = 5
N_BUCKETS = 100 // BUCKET_PCT
STEP = 300            # округление прогноза, с
TAU = 720.0           # постоянная времени EMA мощности, с (≈12 мин)
GAP = 120.0           # разрыв в данных, после которого сглаживание начинается заново
MIN_DISCHARGE_W = 20  # ниже — прогноз разряда бессмыслен (≈ бесконечность)
MIN_CHARGE_W = 50
CHARGE_ALPHA = 0.3    # вес нового замера в кривой заряда
SAMPLE_MIN, SAMPLE_MAX = 5.0, 3 * 3600.0
SOC_SLACK = 0.5       # насколько дробный SOC может отходить от целого показания


HOLD_STEPS = ((5400, 300), (3 * 3600, 600), (6 * 3600, 900), (12 * 3600, 1800), (24 * 3600, 3600))
HOLD_STEP_MAX = 7200


def hold_step(sec: float) -> int:
    """Шаг округления `h` (≈ 3–8 % значения, но не меньше 5 мин): до 1,5 ч — 5 мин (виджет здесь
    пишет минуты), до 3 ч — 10 мин, до 6 ч — 15 мин, до 12 ч — 30 мин, до суток — 1 ч, дальше 2 ч."""
    for limit, step in HOLD_STEPS:
        if sec < limit:
            return step
    return HOLD_STEP_MAX


class Smoothed:
    """Медиана трёх последних значений + EMA с постоянной `tau`; разрыв > GAP — заново."""

    def __init__(self, tau: float = TAU):
        self.tau = tau
        self.value: Optional[float] = None
        self._recent: List[float] = []

    def reset(self, x: float) -> None:
        self._recent = [x]
        self.value = x

    def add(self, x: float, dt: Optional[float]) -> float:
        if self.value is None or dt is None or dt <= 0 or dt > GAP:
            self.reset(x)
            return x
        self._recent = (self._recent + [x])[-3:]
        med = sorted(self._recent)[1] if len(self._recent) == 3 else x
        self.value += (1.0 - math.exp(-dt / self.tau)) * (med - self.value)
        return self.value


def bucket_of(pct: int) -> int:
    return max(0, min(N_BUCKETS - 1, int(pct) // BUCKET_PCT))


def slowdown(pct: int) -> float:
    """Физическая модель LiFePO4: после 90% ток заряда падает (CV-фаза)."""
    if pct < 90:
        return 1.0
    if pct < 95:
        return 1.6
    return 2.6


class ChargeCurve:
    """EMA «секунд на 1%» по 5%-корзинам SOC."""

    def __init__(self, data: Optional[Dict[int, Tuple[float, int]]] = None, alpha: float = CHARGE_ALPHA):
        self.data: Dict[int, Tuple[float, int]] = dict(data or {})
        self.alpha = alpha
        self.dirty = False

    def learn(self, pct: int, seconds: float) -> None:
        if not (SAMPLE_MIN <= seconds <= SAMPLE_MAX):
            return
        b = bucket_of(pct)
        old = self.data.get(b)
        if old is None:
            self.data[b] = (float(seconds), 1)
        else:
            self.data[b] = (old[0] + self.alpha * (seconds - old[0]), old[1] + 1)
        self.dirty = True

    def get(self, pct: int) -> Optional[float]:
        v = self.data.get(bucket_of(pct))
        return v[0] if v else None


class Estimator:
    def __init__(self, capacity_wh: float = 0.0, curve: Optional[ChargeCurve] = None,
                 tau: float = TAU, step: int = STEP):
        self.capacity_wh = max(0.0, float(capacity_wh))  # 0 — ёмкость не задана
        self.curve = curve or ChargeCurve()
        self.tau = tau
        self.step = step
        self.mode: Optional[str] = None      # "discharge" | "charge"
        self.power: Optional[float] = None   # EMA мощности в текущем режиме, Вт (≥0)
        self.soc_f: Optional[float] = None   # дробный SOC
        self.last: Optional[Reading] = None
        self.eta: int = 0
        self.hold: int = 0                    # `h`: на сколько хватит батареи, с (0 — оценки нет)
        self.load = Smoothed(tau)             # сглаженная нагрузка квартиры, Вт
        self._raw: Optional[float] = None
        self._pw = Smoothed(tau)              # сглаженная мощность батареи в текущем режиме
        # обучение кривой: (soc, время, валидна ли отметка)
        self._mark: Optional[Tuple[int, float, bool]] = None

    # ------------------------------------------------------------ обновление
    def update(self, r: Reading, grid_on: bool) -> int:
        prev = self.last
        dt = (r.ts - prev.ts) if prev else None
        gap = dt is None or dt <= 0 or dt > GAP
        mode = "charge" if grid_on else "discharge"
        p_now = float(max(0, -r.batt_w) if mode == "charge" else max(0, r.batt_w))

        if gap or mode != self.mode or self.power is None:
            self._pw.reset(p_now)
            if mode != self.mode:
                self.eta, self._raw = 0, None
        else:
            self._pw.add(p_now, dt)
        self.power = self._pw.value
        self.mode = mode
        self.load.add(float(max(0, r.load_w)), dt)

        self._learn(r, mode, gap)

        # дробный SOC: кулоновский счёт, привязанный к показанию инвертора
        cap_wh = self.capacity_wh
        if cap_wh <= 0 or self.soc_f is None or gap:
            self.soc_f = float(r.soc)
        else:
            self.soc_f -= r.batt_w * dt / 3600.0 / cap_wh * 100.0
        self.soc_f = min(float(r.soc) + SOC_SLACK, max(float(r.soc) - SOC_SLACK, self.soc_f))
        self.soc_f = min(100.0, max(0.0, self.soc_f))

        self.last = r
        self.eta = self._smooth(self._estimate(r))
        self.hold = self._smooth_hold(self._hold(r))
        return self.eta

    def _learn(self, r: Reading, mode: str, gap: bool) -> None:
        charging = mode == "charge" and r.batt_w < -MIN_CHARGE_W
        if not charging or gap or self._mark is None:
            # первая отметка невалидна: неизвестно, сколько процента уже пройдено
            self._mark = (r.soc, r.ts, False) if charging else None
            return
        soc0, t0, valid = self._mark
        if r.soc == soc0:
            return
        if r.soc == soc0 + 1 and valid:
            self.curve.learn(soc0, r.ts - t0)
        # скачок больше 1%, падение SOC и т. п. — просто ставим новую отметку
        self._mark = (r.soc, r.ts, r.soc == soc0 + 1)

    # ------------------------------------------------------------ прогноз
    def _estimate(self, r: Reading) -> Optional[float]:
        cap_wh = self.capacity_wh
        soc = self.soc_f if self.soc_f is not None else float(r.soc)
        if self.power is None or cap_wh <= 0:
            return None  # ёмкость не задана — прогноза нет
        if self.mode == "discharge":
            if self.power < MIN_DISCHARGE_W or soc <= r.cutoff_soc:
                return None
            wh = (soc - r.cutoff_soc) / 100.0 * cap_wh
            return r.ts + wh / self.power * 3600.0
        # заряд
        if r.soc >= 100 or self.power < MIN_CHARGE_W:
            return None  # полная или не заряжается
        p_bulk = self.power * slowdown(int(soc))  # оценка мощности до замедления
        total = 0.0
        pct = int(soc)
        frac_left = 1.0 - (soc - pct)
        while pct < 100:
            sec = self.curve.get(pct)
            if sec is None:
                sec = cap_wh / 100.0 / p_bulk * 3600.0 * slowdown(pct)
            total += sec * frac_left
            frac_left = 1.0
            pct += 1
        return r.ts + total

    def hold_power(self) -> Optional[float]:
        """Мощность для `h`: без сети — та же, что для `e`; иначе (или если батарея почти не
        отдаёт, например днём на солнце) — сглаженная нагрузка квартиры."""
        if self.mode == "discharge" and self.power is not None and self.power >= MIN_DISCHARGE_W:
            return self.power
        return self.load.value

    def _hold(self, r: Reading) -> Optional[float]:
        cap_wh = self.capacity_wh
        p = self.hold_power()
        soc = self.soc_f if self.soc_f is not None else float(r.soc)
        if cap_wh <= 0 or p is None or p < MIN_DISCHARGE_W or soc <= r.cutoff_soc:
            return None
        return (soc - r.cutoff_soc) / 100.0 * cap_wh / p * 3600.0

    def _smooth_hold(self, raw: Optional[float]) -> int:
        if raw is None:
            return 0
        if self.hold and abs(raw - self.hold) < hold_step(self.hold):
            return self.hold
        step = hold_step(raw)
        return int(round(raw / step)) * step

    def _smooth(self, raw: Optional[float]) -> int:
        if raw is None:
            self._raw = None
            return 0
        self._raw = raw
        if self.eta and abs(raw - self.eta) < self.step:
            return self.eta
        return int(round(raw / self.step)) * self.step

    def describe(self) -> str:
        return "mode=%s power=%s load=%s soc_f=%s eta=%s hold=%s" % (
            self.mode, None if self.power is None else round(self.power),
            None if self.load.value is None else round(self.load.value),
            None if self.soc_f is None else round(self.soc_f, 2), self.eta, self.hold)
