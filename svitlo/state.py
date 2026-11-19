"""Машина состояний сети и сборка `chat_data` по контракту (docs/contract.md)."""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import texts
from .estimator import GAP, ChargeCurve, Estimator
from .schedule import APPLIES, Schedule, combine, contract_view, day_status, emergency_mask, schedule_changes
from .source import Reading
from .storage import LogRow, Store

log = logging.getLogger(__name__)

CHAT_DATA_LIMIT = 1024
KEY_ORDER = ("r", "g", "s", "p", "c", "b", "l", "u", "e", "h", "o", "d", "w", "m", "t")
# виджет «Инвертор» (docs/contract.md, «Виджет «Инвертор»»)
INV_KEY_ORDER = ("r", "t", "d", "z", "g", "u", "f", "n", "l", "c", "b", "a", "v", "k", "x", "h", "e", "i", "q")
FORECAST_LOG_EVERY = 600   # как часто писать прогноз разряда в журнал точности, с
# Регистры для виджета «Инвертор», сверенные на живом инверторе (docs/contract.md). Неподтверждённое не шлём.
# 12.11.2026 16:08–16:10 при сети: 169 = +750…+850 Вт при нагрузке 595–637 Вт и заряде 150–262 Вт
# (169 ≈ 178 − 190 с точностью 7–12 Вт, «+» = из сети), 79 = 49,98–50,01 Гц (как 192 на выходе).
INV_GRID_POWER = True      # `n`, рег. 169
INV_GRID_HZ = True         # `f`, рег. 79 (без сети = 0 — не шлём)


@dataclass
class GridState:
    grid: bool
    since: int      # unix, начало текущего состояния
    prev: int       # длительность прошлого состояния, с (0 — неизвестно)
    exact: bool = True  # False — начало неизвестно (первый запуск): since = момент запуска


@dataclass
class Transition:
    grid: bool
    since: int
    prev: int
    initial: bool   # первое определение состояния (нет прошлого) — не событие


class GridTracker:
    """«Сеть есть» = напряжение ≥ порога; смена состояния — только после `confirm`
    одинаковых чтений подряд. Пропуски чтений (нет связи) состояние не меняют."""

    def __init__(self, threshold_v: float = 170.0, confirm: int = 2, state: Optional[GridState] = None):
        self.threshold_v = threshold_v
        self.confirm = confirm
        self.state = state
        self._pending: Optional[bool] = None
        self._pending_n = 0
        self._pending_ts = 0.0

    def raw(self, r: Reading) -> bool:
        return r.grid_v >= self.threshold_v

    def observe(self, r: Reading) -> Optional[Transition]:
        on = self.raw(r)
        if (r.grid_relay == 1) != on:
            log.debug("реле сети (%s) расходится с напряжением %.1f В", r.grid_relay, r.grid_v)
        if self.state is not None and on == self.state.grid:
            self._pending, self._pending_n = None, 0
            return None
        if on != self._pending:
            self._pending, self._pending_n, self._pending_ts = on, 0, r.ts
        self._pending_n += 1
        if self._pending_n < self.confirm:
            return None
        since = int(self._pending_ts)
        self._pending, self._pending_n = None, 0
        if self.state is None:
            self.state = GridState(on, since, 0, exact=False)
            return Transition(on, since, 0, True)
        prev = max(0, since - self.state.since) if self.state.exact else 0
        self.state = GridState(on, since, prev)
        return Transition(on, since, prev, False)


def local_midnight(ts: float, tz) -> int:
    d = datetime.fromtimestamp(ts, tz).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


def local_date(ts: float, tz) -> date:
    return datetime.fromtimestamp(ts, tz).date()


def round10(x: float) -> int:
    return int(round(x / 10.0)) * 10


def encode(state: Dict) -> str:
    """JSON без пробелов, ключи в порядке контракта; укладывается в 1024 байта
    (если нет — отбрасываются самые дальние интервалы графика)."""
    st = dict(state)
    while True:
        ordered = {k: st[k] for k in KEY_ORDER if k in st}
        text = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
        if len(text.encode("utf-8")) <= CHAT_DATA_LIMIT:
            return text
        if not st.get("o"):
            raise ValueError("chat_data не помещается в %d байт" % CHAT_DATA_LIMIT)
        st["o"] = st["o"][:-1]
        log.warning("chat_data длиннее лимита — отброшен последний интервал графика")


class Engine:
    """Всё состояние бота кроме Telegram: сеть, батарея, прогноз, график, ревизии."""

    def __init__(self, store: Store, tz, grid_on_v: float = 170.0, battery_kwh: float = 0.0,
                 stale_after: float = 60.0):
        self.store = store
        self.tz = tz
        self.stale_after = stale_after
        g = store.get("grid")
        state = GridState(bool(g["g"]), int(g["s"]), int(g["p"]), bool(g.get("x", 1))) if g else None
        self.tracker = GridTracker(grid_on_v, 2, state)
        self.estimator = Estimator(battery_kwh * 1000.0, ChargeCurve(store.load_curve()))
        sched = store.get("schedule")
        self.schedule: Optional[Schedule] = Schedule.from_json(sched) if sched else None
        self.rev: int = int(store.get("rev", 0))
        self._last_snap: Optional[Dict] = None
        self.last: Optional[Reading] = None
        self.last_ok: Optional[float] = None
        self.errors = 0
        self._log_period: Optional[int] = None
        self._log_next = 0.0

    # ---------------------------------------------------------------- входные данные
    def observe(self, r: Reading) -> Optional[Transition]:
        tr = self.tracker.observe(r)
        if tr is not None:
            st = self.tracker.state
            self.store.set("grid", {"g": int(st.grid), "s": st.since, "p": st.prev, "x": int(st.exact)})
            if not tr.initial:
                self.store.add_event(int(r.ts), tr.grid, tr.since, tr.prev, r.soc)
            log.info("сеть: %s с %s (прошлое состояние %s с)%s", "есть" if tr.grid else "нет",
                     datetime.fromtimestamp(tr.since, self.tz).strftime("%d.%m %H:%M:%S"), tr.prev,
                     " [первое определение]" if tr.initial else "")
        grid = self.tracker.state.grid if self.tracker.state else self.tracker.raw(r)
        prev = self.last
        self.estimator.update(r, grid)
        if self.estimator.curve.dirty:
            self.store.save_curve(self.estimator.curve.data)
            self.estimator.curve.dirty = False
        self._log_discharge(r, prev)
        self.last, self.last_ok, self.errors = r, r.ts, 0
        return tr

    def _log_discharge(self, r: Reading, prev: Optional[Reading]) -> None:
        """Журнал точности (см. accuracy.py): без сети раз в ~10 мин — прогноз, и каждый момент,
        когда показание SOC уменьшилось на 1% (между двумя чтениями без разрыва)."""
        st = self.tracker.state
        if st is None or st.grid:
            self._log_period = None
            return
        period = st.since
        if period != self._log_period:
            self._log_period, self._log_next = period, 0.0
        est = self.estimator
        if (prev is not None and prev.ts >= period and 0 < r.ts - prev.ts <= GAP
                and r.soc == prev.soc - 1):
            self.store.add_log(LogRow(period, int(r.ts), "t", r.soc, None, None, None, r.cutoff_soc))
        if r.ts >= self._log_next:
            self._log_next = r.ts + FORECAST_LOG_EVERY
            self.store.add_log(LogRow(period, int(r.ts), "f", r.soc,
                                      None if est.soc_f is None else round(est.soc_f, 3),
                                      None if est.power is None else round(est.power, 1),
                                      int(est.eta), r.cutoff_soc))

    def read_failed(self) -> None:
        self.errors += 1

    def set_schedule(self, schedule: Schedule, now: float) -> List[str]:
        """Принять свежий график. Сравнивает его по календарным дням с тем, что уже известно
        владельцу (`sched_base` в базе), и ставит в очередь сообщения об изменениях.
        Возвращает тексты новых сообщений (для журнала и тестов)."""
        today = local_date(now, self.tz)
        self.schedule = combine(self.schedule, schedule, today)
        base, changes = schedule_changes(self.store.get("sched_base"), schedule, today)
        notices = [(now, texts.change_message(c), c.loud) for c in changes]
        self.store.save_schedule(self.schedule.to_json(), base, notices)
        for _, text, loud in notices:
            log.info("график: %s (%s)", text.replace("\n", " / "), "со звуком" if loud else "беззвучно")
        return [n[1] for n in notices]

    # ---------------------------------------------------------------- состояние
    @property
    def ready(self) -> bool:
        return self.tracker.state is not None and self.last is not None

    @property
    def since_known(self) -> bool:
        return bool(self.tracker.state and self.tracker.state.exact)

    @property
    def epoch(self) -> Optional[int]:
        return self.tracker.state.since if self.tracker.state else None

    def is_stale(self, now: float) -> bool:
        return self.last_ok is None or now - self.last_ok > self.stale_after

    def event_text(self) -> Optional[str]:
        """Текст события о текущем состоянии сети. None — это не событие (первое определение
        состояния после самого первого запуска: неизвестно, с какого времени)."""
        st = self.tracker.state
        if st is None or not st.exact:
            return None
        return texts.event_text(st.grid, st.since, st.prev, self.tz)

    def snapshot(self, now: float) -> Optional[Dict]:
        """Поля контракта без `r`. None — состояние ещё не определено."""
        if not self.ready:
            return None
        st, r = self.tracker.state, self.last
        o, w = contract_view(self.schedule, local_date(now, self.tz))
        return {
            "g": int(st.grid), "s": st.since, "p": st.prev,
            "c": int(r.soc), "b": round10(r.batt_w), "l": round10(r.load_w),
            "u": int(round(r.grid_v)) if st.grid else 0,
            "e": int(self.estimator.eta), "h": int(self.estimator.hold), "o": o, "d": local_midnight(now, self.tz), "w": w,
            "m": emergency_mask(self.schedule, local_date(now, self.tz)), "t": int(self.last_ok),
        }

    def schedule_days(self, now: float):
        """Сегодня и завтра для кнопки «График»: [(дата, завтра?, статус, интервалы)]."""
        today = local_date(now, self.tz)
        out = []
        for tomorrow, d in ((False, today), (True, today + timedelta(days=1))):
            st = day_status(self.schedule, d)
            ivs = list(self.schedule.days[d].intervals) if st == APPLIES else []
            out.append((d, tomorrow, st, ivs))
        return out

    def inverter_snapshot(self, now: float, until: Optional[int] = None,
                          grid_power: Optional[bool] = None, grid_hz: Optional[bool] = None) -> Optional[Dict]:
        """Поля виджета «Инвертор» без `r` (необязательные `z`, `f`, `i`, `q` — только если известны).
        grid_power/grid_hz=False — не передавать `n`/`f` (регистры не подтверждены на живом инверторе)."""
        if not self.ready:
            return None
        grid_power = INV_GRID_POWER if grid_power is None else grid_power
        grid_hz = INV_GRID_HZ if grid_hz is None else grid_hz
        st, r = self.tracker.state, self.last
        snap = {
            "t": int(self.last_ok), "d": local_midnight(now, self.tz),
            "g": int(st.grid), "u": round(r.grid_v, 1) if st.grid else 0,
            "l": round10(r.load_w), "c": int(r.soc), "b": round10(r.batt_w),
            "a": round(r.batt_a, 1), "v": round(r.batt_v, 1), "k": round(r.batt_temp_c, 1),
            "x": int(r.cutoff_soc), "h": int(self.estimator.hold), "e": int(self.estimator.eta),
        }
        if until:
            snap["z"] = int(until)
        if grid_power:
            snap["n"] = round10(r.grid_w) if st.grid else 0
        day = r.day
        if day is not None:
            if grid_hz and st.grid and 45 <= day.grid_hz <= 65:
                snap["f"] = round(day.grid_hz, 2)
            if day.inverter_c is not None and -30 < day.inverter_c < 120:
                snap["i"] = round(day.inverter_c, 1)
            snap["q"] = [round(day.batt_charge_kwh, 1), round(day.batt_discharge_kwh, 1),
                         round(day.grid_import_kwh, 1), round(day.load_kwh, 1)]
        return snap

    def next_rev(self) -> int:
        self.rev += 1
        self.store.set("rev", self.rev)
        return self.rev

    def peek(self, now: float) -> Optional[str]:
        """Текущее состояние с текущей ревизией — для сравнения, ревизию не поднимает."""
        snap = self.snapshot(now)
        return None if snap is None else encode(dict(snap, r=self.rev))

    def chat_data(self, now: float) -> Optional[str]:
        """Состояние к отправке: ревизия поднимается, если содержимое изменилось."""
        snap = self.snapshot(now)
        if snap is None:
            return None
        if snap != self._last_snap:
            self.next_rev()
            self._last_snap = snap
        return encode(dict(snap, r=self.rev))


def encode_inverter(state: Dict) -> str:
    ordered = {k: state[k] for k in INV_KEY_ORDER if k in state}
    text = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    if len(text.encode("utf-8")) > CHAT_DATA_LIMIT:  # pragma: no cover - ~200 байт
        raise ValueError("chat_data инвертора не помещается в %d байт" % CHAT_DATA_LIMIT)
    return text


def same_content(a: Optional[str], b: Optional[str], ignore=("r", "t")) -> bool:
    """Совпадают ли два chat_data без учёта ревизии и времени чтения `t`."""
    if a is None or b is None:
        return a == b
    da, db = json.loads(a), json.loads(b)
    for k in ignore:
        da.pop(k, None)
        db.pop(k, None)
    return da == db


def parse(data: str) -> Dict:
    return json.loads(data)


def intervals_text(o: List[List[int]]) -> str:
    """Человекочитаемый вид `o` (для probe/демо): «07:00–13:00, завтра 00:00–01:30»."""
    def hm(m: int, end: bool = False) -> str:
        day, mm = divmod(m, 1440)
        if end and mm == 0 and day > 0:
            day, mm = day - 1, 1440
        return "%s%02d:%02d" % ("завтра " if day == 1 else ("+%dд " % day if day else ""), mm // 60, mm % 60)
    return ", ".join("%s–%s" % (hm(a), hm(b, True)) for a, b in o) or "нет"
