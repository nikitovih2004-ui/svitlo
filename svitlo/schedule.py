"""График отключений: YASNO (основной) + outage-data-ua (запасной) → нормализованные интервалы.

Внутреннее представление — по календарным дням (локальная дата Europe/Kyiv) с интервалами
в минутах от полуночи этого дня, как и в источниках. `contract_view()` превращает его
в поля контракта `o` и `w` относительно «сегодня»: завтра = +1440, интервалы отсортированы,
соседние и перекрывающиеся склеены (в том числе через полночь).
"""

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

YASNO_URL = ("https://app.yasno.ua/api/blackout-service/public/shutdowns/"
             "regions/{region}/dsos/{dso}/planned-outages")
FALLBACK_URL = "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data/kyiv.json"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Статусы дня
APPLIES = "applies"        # график действует
WAITING = "waiting"        # график ещё не опубликован
EMERGENCY = "emergency"    # экстренные отключения: график не действует
UNKNOWN = "unknown"

YASNO_STATUS = {
    "ScheduleApplies": APPLIES,
    "WaitingForSchedule": WAITING,
    "EmergencyShutdowns": EMERGENCY,
}
# Типы слотов YASNO: Definite — отключение по графику, NotPlanned — свет есть.
# Possible/неизвестные — не считаем отключением (в контракте только плановые), но логируем.
YASNO_OUTAGE_TYPES = {"Definite"}
YASNO_KNOWN_TYPES = {"Definite", "NotPlanned", "Possible"}

# Запасной источник: значения по часам ("1" = 00:00–01:00).
# no — света нет весь час; first/second — нет первые/вторые 30 мин;
# maybe/mfirst/msecond — «возможно», в контракт не попадает; yes — свет есть.
FALLBACK_HALVES = {
    "no": ((0, 60),),
    "first": ((0, 30),),
    "second": ((30, 60),),
}
FALLBACK_KNOWN = {"yes", "no", "first", "second", "maybe", "mfirst", "msecond"}

# Запасной источник берётся, только если YASNO не отвечает дольше этого времени; до тех пор
# остаётся последний график YASNO. Так единичные сбои YASNO не переключают дашборд на источник,
# который может отставать или расходиться с ним.
FALLBACK_AFTER = 1800.0

Interval = Tuple[int, int]


class ScheduleError(Exception):
    pass


@dataclass
class DaySchedule:
    status: str
    intervals: List[Interval] = field(default_factory=list)  # минуты от полуночи этого дня

    def to_json(self) -> dict:
        return {"s": self.status, "i": [list(x) for x in self.intervals]}


@dataclass
class Schedule:
    days: Dict[date, DaySchedule]
    source: str
    fetched_at: float
    updated_on: str = ""

    def to_json(self) -> str:
        return json.dumps({
            "days": {d.isoformat(): v.to_json() for d, v in self.days.items()},
            "source": self.source, "fetched_at": self.fetched_at, "updated_on": self.updated_on,
        })

    @classmethod
    def from_json(cls, text: str) -> "Schedule":
        raw = json.loads(text)
        days = {date.fromisoformat(k): DaySchedule(v["s"], [tuple(x) for x in v["i"]])
                for k, v in raw["days"].items()}
        return cls(days, raw["source"], raw["fetched_at"], raw.get("updated_on", ""))


def merge(intervals: List[Interval]) -> List[Interval]:
    """Сортировка и склейка перекрывающихся/соседних интервалов; пустые выбрасываются."""
    out: List[List[int]] = []
    for a, b in sorted((int(a), int(b)) for a, b in intervals if b > a):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _parse_iso_date(value: str) -> date:
    v = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(v).date()  # дата в собственном (киевском) смещении строки


def parse_yasno(payload: dict, group: str, fetched_at: Optional[float] = None) -> Schedule:
    if group not in payload:
        raise ScheduleError("YASNO: группы %s нет в ответе" % group)
    g = payload[group]
    days: Dict[date, DaySchedule] = {}
    for key in ("today", "tomorrow"):
        day = g.get(key)
        if not day:
            continue
        try:
            d = _parse_iso_date(day["date"])
        except (KeyError, ValueError) as exc:
            raise ScheduleError("YASNO: нет даты в %s: %s" % (key, exc))
        raw_status = day.get("status", "")
        status = YASNO_STATUS.get(raw_status, UNKNOWN)
        if status == UNKNOWN:
            log.warning("YASNO: неизвестный статус %r (%s)", raw_status, key)
        if status == APPLIES and not day.get("slots"):
            # «график действует», но слотов нет вовсе (даже NotPlanned) — битый/неполный ответ,
            # а не «отключений нет»: день пропускаем, остаётся то, что было известно раньше
            log.warning("YASNO: %s без слотов — день пропущен", key)
            continue
        intervals: List[Interval] = []
        for slot in day.get("slots") or []:
            t = slot.get("type")
            if t not in YASNO_KNOWN_TYPES:
                log.warning("YASNO: неизвестный тип слота %r", t)
            if t in YASNO_OUTAGE_TYPES:
                a, b = int(slot["start"]), int(slot["end"])
                intervals.append((max(0, a), min(1440, b)))
        if status == EMERGENCY:
            intervals = []  # при экстренных график не действует
        days[d] = DaySchedule(status, merge(intervals))
    return Schedule(days, "yasno", fetched_at if fetched_at is not None else time.time(),
                    str(g.get("updatedOn", "")))


def parse_fallback(payload: dict, group: str, tz, fetched_at: Optional[float] = None) -> Schedule:
    key = group if group.startswith("GPV") else "GPV" + group
    try:
        data = payload["fact"]["data"]
    except (KeyError, TypeError):
        raise ScheduleError("outage-data-ua: нет fact.data")
    days: Dict[date, DaySchedule] = {}
    for ts, groups in data.items():
        hours = (groups or {}).get(key)
        if not hours:
            continue
        d = datetime.fromtimestamp(int(ts), tz).date()
        intervals: List[Interval] = []
        for h, value in hours.items():
            if value not in FALLBACK_KNOWN:
                log.warning("outage-data-ua: неизвестное значение %r", value)
            base = (int(h) - 1) * 60
            for a, b in FALLBACK_HALVES.get(value, ()):
                intervals.append((base + a, base + b))
        days[d] = DaySchedule(APPLIES, merge(intervals))
    if not days:
        raise ScheduleError("outage-data-ua: группы %s нет" % key)
    upd = str(payload.get("fact", {}).get("update") or payload.get("lastUpdated") or "")
    return Schedule(days, "outage-data-ua", fetched_at if fetched_at is not None else time.time(), upd)


def contract_view(schedule: Optional[Schedule], today: date) -> Tuple[List[List[int]], int]:
    """Поля контракта `o` (интервалы на сегодня и завтра, завтра = +1440) и `w`."""
    if schedule is None:
        return [], 0
    tomorrow = today + timedelta(days=1)
    raw: List[Interval] = []
    for offset, d in ((0, today), (1440, tomorrow)):
        day = schedule.days.get(d)
        if day is None:
            continue
        raw.extend((a + offset, b + offset) for a, b in day.intervals)
    t = schedule.days.get(tomorrow)
    published = 1 if (t is not None and t.status == APPLIES) else 0
    return [[a, b] for a, b in merge(raw)], published


def combine(old: Optional[Schedule], new: Schedule, today: date) -> Schedule:
    """Новый график поверх старого: дни, которых нет в новом ответе (например, около
    полуночи YASNO ещё отдаёт вчерашнее «сегодня»), берутся из старого, если не в прошлом."""
    if old is None:
        return new
    days = {d: v for d, v in old.days.items() if d >= today and d not in new.days}
    days.update(new.days)
    return Schedule(days, new.source, new.fetched_at, new.updated_on)


@dataclass
class Change:
    """Что сообщить владельцу о графике одного дня (texts.schedule_message)."""

    day: date
    tomorrow: bool
    intervals: List[Interval]
    old: Optional[List[Interval]] = None  # None — график дня появился; иначе — каким он был
    emergency: bool = False               # экстренные отключения: график дня не действует

    @property
    def loud(self) -> bool:
        """Со звуком: всё про сегодня и экстренные; про завтра — беззвучно."""
        return self.emergency or not self.tomorrow


def schedule_changes(base: Optional[Dict], new: Schedule, today: date) -> Tuple[Dict, List[Change]]:
    """Что сообщить владельцу об изменении графика (docs/contract.md, «Жизненный цикл», п. 5).

    `base` — что уже известно владельцу по календарным дням (хранится в базе):
    {"YYYY-MM-DD": {"s": статус, "i": [[a, b], …], "src": источник}}; None — первый запуск.
    Сравниваются склеенные интервалы конкретного дня (сегодня и завтра). Возвращает новый `base`
    и список изменений.

    Сообщается: график на завтра появился или изменился (беззвучно); график на сегодня изменился или
    впервые опубликован уже после полуночи (со звуком); день стал «экстренные отключения» (со звуком).
    Не сообщается: первый запуск; смена суток («завтра» стало «сегодня» — база хранится по датам);
    день, которого нет в ответе (временная недоступность); «ждём график»/неизвестный статус после
    опубликованного — такой ответ не значит «отключений больше нет»; график на сегодня, о котором раньше
    не было ничего известно (бот лежал); смена источника (YASNO ↔ запасной) — новые данные молча
    становятся базой, иначе расхождение источников давало бы сообщения туда-сюда.
    """
    first = base is None
    out: Dict = {k: v for k, v in (base or {}).items() if date.fromisoformat(k) >= today}
    changes: List[Change] = []
    for tomorrow, d in ((False, today), (True, today + timedelta(days=1))):
        day = new.days.get(d)
        if day is None:
            continue
        key = d.isoformat()
        old = out.get(key)
        if day.status == WAITING:
            if old is None:
                out[key] = {"s": WAITING, "i": [], "src": new.source}
            continue
        if day.status == EMERGENCY:
            out[key] = {"s": EMERGENCY, "i": [], "src": new.source}
            if not first and (old is None or old.get("s") != EMERGENCY):
                changes.append(Change(d, tomorrow, [], emergency=True))
            continue
        if day.status != APPLIES:
            continue
        ivs = [list(x) for x in day.intervals]
        out[key] = {"s": APPLIES, "i": ivs, "src": new.source}
        if first:
            continue
        if old is None:
            if tomorrow:
                changes.append(Change(d, True, list(day.intervals)))  # завтрашний день впервые в ответе
            continue
        if old.get("src") != new.source:
            if old.get("s") == APPLIES and old.get("i") != ivs:
                log.info("график %s: %s и %s расходятся (%s / %s) — без сообщения",
                         key, old.get("src"), new.source, old.get("i"), ivs)
            continue
        if old.get("s") != APPLIES:
            # опубликован после «ждём график»/экстренных: на завтра — как обычно, на сегодня — уже после полуночи
            changes.append(Change(d, tomorrow, list(day.intervals)))
            continue
        if old.get("i") != ivs:
            changes.append(Change(d, tomorrow, list(day.intervals), [tuple(x) for x in old.get("i") or []]))
    return out, changes


def emergency_mask(schedule: Optional[Schedule], today: date) -> int:
    """Поле контракта `m`: бит 0 — сегодня экстренные отключения, бит 1 — завтра."""
    if schedule is None:
        return 0
    m = 0
    for bit, d in ((1, today), (2, today + timedelta(days=1))):
        day = schedule.days.get(d)
        if day is not None and day.status == EMERGENCY:
            m |= bit
    return m


def day_status(schedule: Optional[Schedule], d: date) -> str:
    if schedule is None or d not in schedule.days:
        return UNKNOWN
    return schedule.days[d].status


class ScheduleFetcher:
    """Скачивает график: сначала YASNO, при сбое — запасной источник."""

    def __init__(self, client, group: str, tz, region: int = 25, dso: int = 902,
                 clock: Callable[[], float] = time.time, fallback_after: float = FALLBACK_AFTER):
        self.client = client  # httpx.AsyncClient
        self.group = group
        self.tz = tz
        self.yasno_url = YASNO_URL.format(region=region, dso=dso)
        self.clock = clock
        self.fallback_after = fallback_after
        self._yasno_ok_at: Optional[float] = None

    async def _get_json(self, url: str) -> dict:
        resp = await self.client.get(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/json"}, timeout=20.0)
        resp.raise_for_status()
        return resp.json()

    async def fetch(self) -> Schedule:
        errors = []
        try:
            sched = parse_yasno(await self._get_json(self.yasno_url), self.group)
            self._yasno_ok_at = self.clock()
            return sched
        except Exception as exc:
            errors.append("YASNO: %s" % exc)
            if self._yasno_ok_at is not None and self.clock() - self._yasno_ok_at < self.fallback_after:
                raise ScheduleError("YASNO: %s — пока остаётся его прошлый график" % exc)
            log.warning("YASNO недоступен: %s — пробую запасной источник", exc)
        try:
            return parse_fallback(await self._get_json(FALLBACK_URL), self.group, self.tz)
        except Exception as exc:
            errors.append("outage-data-ua: %s" % exc)
        raise ScheduleError("; ".join(errors))
