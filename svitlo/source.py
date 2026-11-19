"""Источники данных инвертора.

Интерфейс `Source` — один метод `read()`, возвращающий нормализованный `Reading`
(единицы СИ, знаки как в контракте). Любой сбой связи — `SourceError`.
Так позже можно добавить, например, `DeyeCloudSource` без изменений в остальном коде.

ВАЖНО: `SolarmanSource` только читает регистры. Никаких write_* здесь нет и не должно быть.
"""

import logging
import math
import time
from dataclasses import dataclass, field, replace
from typing import Callable, List, Optional, Sequence

log = logging.getLogger(__name__)


class SourceError(Exception):
    """Нет связи с источником (логгер недоступен, таймаут, битый ответ)."""


@dataclass(frozen=True)
class DayStats:
    """Счётчики за сутки, кВт·ч (сбрасываются инвертором в полночь)."""

    batt_charge_kwh: float
    batt_discharge_kwh: float
    grid_import_kwh: float
    load_kwh: float
    grid_hz: float = 0.0       # рег. 79, частота сети, Гц (0 — сети нет)
    inverter_c: Optional[float] = None  # рег. 91, температура радиатора инвертора, °C


@dataclass(frozen=True)
class Reading:
    ts: float              # unix-время чтения
    grid_v: float          # напряжение сети, В
    grid_w: int            # мощность сети, Вт (signed)
    load_w: int            # нагрузка, Вт
    batt_temp_c: float     # температура батареи, °C
    batt_v: float          # напряжение батареи, В
    soc: int               # заряд, %
    batt_w: int            # мощность батареи, Вт: + разряд, − заряд
    batt_a: float          # ток батареи, А (signed)
    grid_relay: int        # реле сети: 0 — отключено от сети
    capacity_ah: int       # рег. 204, А·ч — только для справки (там неверное значение, ёмкость — BATTERY_KWH)
    cutoff_soc: int        # SOC отключения (настройка), %
    max_charge_a: int      # максимальный ток заряда (настройка), А
    day: Optional[DayStats] = None


class Source:
    """Базовый интерфейс источника."""

    name = "source"

    def read(self) -> Reading:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- Solarman / Deye

MAIN_START, MAIN_COUNT = 150, 70   # 150..219: сеть, нагрузка, батарея, настройки батареи
DAY_START, DAY_COUNT = 60, 32      # 60..91: дневные счётчики, частота сети (79), температуры (90, 91)


def _s16(x: int) -> int:
    return x - 0x10000 if x & 0x8000 else x


def decode_main(regs: Sequence[int], ts: float) -> Reading:
    """Разбор блока holding-регистров 150..219 однофазного гибрида Deye."""
    if len(regs) < MAIN_COUNT:
        raise SourceError("короткий ответ: %d регистров" % len(regs))

    def r(addr: int) -> int:
        return regs[addr - MAIN_START]

    return Reading(
        ts=ts,
        grid_v=r(150) * 0.1,
        grid_w=_s16(r(169)),
        load_w=r(178),
        batt_temp_c=round((r(182) - 1000) * 0.1, 1),
        batt_v=r(183) * 0.01,
        soc=r(184),
        batt_w=_s16(r(190)),
        batt_a=_s16(r(191)) * 0.01,
        grid_relay=r(194),
        capacity_ah=r(204),
        cutoff_soc=r(217),
        max_charge_a=r(210),
    )


def decode_day(regs: Sequence[int]) -> DayStats:
    if len(regs) < DAY_COUNT:
        raise SourceError("короткий ответ: %d регистров" % len(regs))

    def r(addr: int) -> float:
        return regs[addr - DAY_START] * 0.1

    return DayStats(
        batt_charge_kwh=round(r(70), 1),
        batt_discharge_kwh=round(r(71), 1),
        grid_import_kwh=round(r(76), 1),
        load_kwh=round(r(84), 1),
        grid_hz=round(regs[79 - DAY_START] * 0.01, 2),
        inverter_c=round((regs[91 - DAY_START] - 1000) * 0.1, 1),
    )


class SolarmanSource(Source):
    """Локальный логгер Solarman (протокол V5, TCP 8899) через pysolarmanv5.

    Одно постоянное соединение; за один опрос — один блок регистров (150..219).
    Дневной блок (60..91: счётчики за сутки, частота сети, температура инвертора) читается вторым
    запросом по `read(with_day=True)` или раз в `day_every` секунд, если он задан (бот: раз в 5 мин,
    пока открыт виджет «Инвертор» — раз в 30 с). Значение кэшируется и прикладывается к каждому `Reading`.
    При ошибке соединение закрывается, следующая попытка — после паузы с
    экспоненциальным ростом (10 с … 5 мин); до её истечения `read()` сразу
    бросает `SourceError`, не трогая логгер.
    """

    name = "solarman"

    def __init__(self, host: str, serial: int, port: int = 8899, slave: int = 1,
                 timeout: float = 10.0, day_every: Optional[float] = None,
                 clock: Callable[[], float] = time.time):
        self.host, self.serial, self.port, self.slave = host, serial, port, slave
        self.timeout = timeout
        self.day_every = day_every
        self.clock = clock
        self._conn = None
        self._retry_at = 0.0
        self._backoff = 0.0
        self._day: Optional[DayStats] = None
        self._day_at = 0.0

    def _connect(self):
        from pysolarmanv5 import PySolarmanV5  # импорт здесь: тесты не требуют библиотеки

        return PySolarmanV5(self.host, self.serial, port=self.port, mb_slave_id=self.slave,
                            socket_timeout=self.timeout, logger=log)

    def _fail(self, exc: Exception) -> SourceError:
        self.close()
        self._backoff = min(300.0, self._backoff * 2) if self._backoff else 10.0
        self._retry_at = self.clock() + self._backoff
        return SourceError("%s: %s (повтор через %.0f с)" % (type(exc).__name__, exc, self._backoff))

    def read(self, with_day: bool = False) -> Reading:
        now = self.clock()
        if now < self._retry_at:
            raise SourceError("пауза после ошибки ещё %.0f с" % (self._retry_at - now))
        try:
            if self._conn is None:
                self._conn = self._connect()
            regs = self._conn.read_holding_registers(MAIN_START, MAIN_COUNT)
            reading = decode_main(regs, self.clock())
            if with_day or (self.day_every is not None and now - self._day_at >= self.day_every):
                try:
                    time.sleep(0.5)
                    self._day = decode_day(self._conn.read_holding_registers(DAY_START, DAY_COUNT))
                    self._day_at = now
                except Exception as exc:  # счётчики не критичны
                    log.warning("дневные счётчики не прочитаны: %s", exc)
        except SourceError as exc:
            raise self._fail(exc)
        except Exception as exc:
            raise self._fail(exc)
        self._backoff = 0.0
        return replace(reading, day=self._day)

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:  # pragma: no cover
                pass


# ---------------------------------------------------------------- FakeSource


@dataclass
class Segment:
    """Отрезок сценария: `duration` секунд с заданным состоянием."""

    duration: float
    grid: bool = True
    load_w: int = 400
    offline: bool = False        # логгер недоступен: read() бросает SourceError
    charge_w: int = 3000         # мощность заряда при сети (до 90%, дальше спадает)
    grid_v: Optional[float] = None


@dataclass
class FakeSource(Source):
    """Сценарий для тестов и демо. Физика упрощённая: SOC интегрируется по мощности.

    Время берётся из `clock` (в тестах и демо — виртуальное).
    """

    segments: List[Segment]
    clock: Callable[[], float]
    soc: float = 80.0
    capacity_wh: float = 10240.0   # реальная энергия батареи в сценарии
    capacity_ah: int = 200         # что «показывает» рег. 204
    cutoff_soc: int = 10
    loop: bool = False
    name: str = "fake"
    _t0: Optional[float] = field(default=None, repr=False)
    _last: Optional[float] = field(default=None, repr=False)

    def _segment(self, now: float) -> Segment:
        if self._t0 is None:
            self._t0 = now
        t = now - self._t0
        total = sum(s.duration for s in self.segments)
        if self.loop and total > 0:
            t = t % total
        for seg in self.segments:
            if t < seg.duration:
                return seg
            t -= seg.duration
        return self.segments[-1]

    def _power(self, seg: Segment) -> int:
        """Мощность батареи (+разряд) для текущего SOC."""
        if seg.grid:
            if self.soc >= 100:
                return 0
            taper = 1.0 if self.soc < 90 else max(0.15, (100 - self.soc) / 10.0)
            return -int(seg.charge_w * taper)
        if self.soc <= self.cutoff_soc:
            return 0
        return seg.load_w

    def read(self) -> Reading:
        now = self.clock()
        seg = self._segment(now)
        if self._last is not None:
            # интегрируем SOC по предыдущему отрезку времени
            dt = max(0.0, now - self._last)
            wh = self._power(seg) * dt / 3600.0
            # без сети инвертор разряжает батарею только до порога отключения — не ниже
            floor = min(self.soc, float(self.cutoff_soc)) if not seg.grid else 0.0
            self.soc = min(100.0, max(floor, self.soc - wh / self.capacity_wh * 100))
        self._last = now
        if seg.offline:
            raise SourceError("fake: логгер недоступен")
        batt_w = self._power(seg)
        grid_v = seg.grid_v if seg.grid_v is not None else (230.0 if seg.grid else 4.7)
        return Reading(
            ts=now, grid_v=grid_v, grid_w=(seg.load_w - batt_w) if seg.grid else 0,
            load_w=seg.load_w, batt_temp_c=22.0, batt_v=53.0, soc=int(math.floor(self.soc)),
            batt_w=batt_w, batt_a=round(batt_w / 53.0, 2), grid_relay=1 if seg.grid else 0,
            capacity_ah=self.capacity_ah, cutoff_soc=self.cutoff_soc, max_charge_a=100,
        )
