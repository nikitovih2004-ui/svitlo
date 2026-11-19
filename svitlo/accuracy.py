"""`python -m svitlo accuracy`: насколько точен прогноз «батареи хватит на …».

Журнал пишет Engine (таблица discharge_log): без сети раз в ~10 мин — строка прогноза
(SOC, дробный SOC, сглаженная мощность, опубликованный `e`, порог), и отдельная строка
в каждый момент, когда показание SOC уменьшилось на 1% («тик»).

Метрика. Период без сети почти всегда заканчивается раньше порога (свет дали), поэтому
сравниваем не момент достижения порога, а скорость падения SOC. Прогноз, сделанный в момент t
при дробном SOC s, неявно утверждает скорость v = (s − порог) / (e − t). Фактическую скорость
берём между первым тиком после t (SOC стал a в момент τa) и последним тиком периода (SOC стал b
в τb): между двумя тиками ровно (a − b)% — без ошибки округления целого SOC. Прогноз «проходит»
этот отрезок за (a − b) / v, на деле прошло τb − τa. Ошибка = (прогноз − факт) / факт: это и есть
ошибка горизонта «хватит на …» в процентах, если бы разряд шёл с фактической средней скоростью.
Плюс — прогноз был оптимистичен (обещал больше, чем вышло), минус — пессимистичен.
Отрезки короче MIN_DROP процентов или MIN_SPAN секунд не оцениваются: на них шум SOC BMS
сравним с самой величиной. Тики ниже порога отключения в метрику не входят: прогноз говорит
только о пути до порога, а на пороге инвертор прекращает разряд (дальнейшее падение показания —
округление BMS/саморазряд, а не скорость, которую обещал прогноз).
"""

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from .storage import LogRow, Store
from .texts import duration

MIN_DROP = 3        # минимум процентов SOC на оцениваемом отрезке
MIN_SPAN = 20 * 60  # и минимум времени, с


@dataclass
class Check:
    ts: int            # когда сделан прогноз
    soc: int
    power: float
    horizon: int       # опубликованное «хватит на», с (e − t)
    a: int             # SOC в начале отрезка (первый тик после прогноза)
    b: int             # SOC в конце (последний тик периода)
    actual: int        # τb − τa, с
    predicted: float   # за сколько прогноз проходит a → b, с

    @property
    def error(self) -> float:
        """Относительная ошибка: + прогноз оптимистичен, − пессимистичен."""
        return (self.predicted - self.actual) / self.actual


@dataclass
class Period:
    since: int
    end: int               # последнее записанное время
    reached_cutoff: bool
    checks: List[Check]
    skipped: int           # прогнозов, которые не оценить (нет прогноза, короткий отрезок)


def evaluate(rows: List[LogRow]) -> Period:
    """Строки одного периода (по времени) → проверки прогнозов."""
    all_ticks = [r for r in rows if r.kind == "t"]
    # только путь до порога: тики ниже него прогноз не описывает
    ticks = [t for t in all_ticks if t.cutoff is None or t.soc >= t.cutoff]
    forecasts = [r for r in rows if r.kind == "f"]
    checks: List[Check] = []
    skipped = 0
    last = ticks[-1] if ticks else None
    for f in forecasts:
        if not f.eta or f.eta <= f.ts or f.soc_f is None or f.cutoff is None or f.soc_f <= f.cutoff:
            skipped += 1
            continue
        first = next((t for t in ticks if t.ts >= f.ts), None)
        if first is None or last is None:
            skipped += 1
            continue
        drop, span = first.soc - last.soc, last.ts - first.ts
        if drop < MIN_DROP or span < MIN_SPAN:
            skipped += 1
            continue
        rate = (f.soc_f - f.cutoff) / float(f.eta - f.ts)   # %/с по прогнозу
        checks.append(Check(f.ts, f.soc, f.power or 0.0, f.eta - f.ts, first.soc, last.soc, span, drop / rate))
    reached = any(t.cutoff is not None and t.soc <= t.cutoff for t in all_ticks)
    return Period(rows[0].period if rows else 0, rows[-1].ts if rows else 0, reached, checks, skipped)


def periods(store: Store, include_current: bool = False) -> List[Period]:
    """Завершённые периоды без сети (текущий, если сети нет сейчас, — только по запросу)."""
    by: Dict[int, List[LogRow]] = {}
    for r in store.log_rows():
        by.setdefault(r.period, []).append(r)
    g = store.get("grid")
    current = g["s"] if g and not g["g"] else None
    return [evaluate(rows) for since, rows in sorted(by.items()) if include_current or since != current]


def _pct(x: float) -> str:
    return "%+.0f%%" % (x * 100)


def summary(errors: List[float]) -> str:
    absval = [abs(e) for e in errors]
    return "медиана |ошибки| %.0f%%, максимум %.0f%%, смещение (медиана со знаком) %s" % (
        statistics.median(absval) * 100, max(absval) * 100, _pct(statistics.median(errors)))


def report(store: Store, tz, include_current: bool = False) -> str:
    out: List[str] = []
    ps = periods(store, include_current)
    if not ps:
        return "Журнал пуст: завершённых периодов без сети ещё не было (или не задан BATTERY_KWH)."
    every: List[float] = []

    def hm(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%H:%M")

    for p in ps:
        start = datetime.fromtimestamp(p.since, tz).strftime("%d.%m %H:%M")
        out.append("Без сети %s – %s (%s)%s" % (start, hm(p.end), duration(p.end - p.since),
                                                 ", дошли до порога" if p.reached_cutoff else ""))
        if not p.checks:
            out.append("  оценить нечего (прогнозов без данных/коротких отрезков: %d)" % p.skipped)
            continue
        out.append("  %-5s %4s %5s %11s  %-8s %10s %10s %7s" % ("время", "SOC", "Вт", "хватит на", "отрезок",
                                                               "факт", "прогноз", "ошибка"))
        for c in p.checks:
            out.append("  %-5s %3d%% %5.0f %11s  %3d→%-3s %10s %10s %7s" % (
                hm(c.ts), c.soc, c.power, duration(c.horizon), c.a, "%d%%" % c.b, duration(c.actual),
                duration(int(c.predicted)), _pct(c.error)))
        errs = [c.error for c in p.checks]
        every.extend(errs)
        out.append("  итог: %s; не оценено: %d" % (summary(errs), p.skipped))
    if every:
        out.append("")
        out.append("Всего прогнозов: %d — %s" % (len(every), summary(every)))
        out.append("Плюс — прогноз обещал больше, чем вышло; ошибка — в процентах от горизонта «хватит на».")
    return "\n".join(out)


def main(cfg, include_current: bool = False) -> int:
    from zoneinfo import ZoneInfo

    if not cfg.db_path.exists():
        print("Базы %s ещё нет — бот не запускался." % cfg.db_path)
        return 1
    store = Store(cfg.db_path)
    try:
        print(report(store, ZoneInfo(cfg.tz), include_current))
    finally:
        store.close()
    return 0
