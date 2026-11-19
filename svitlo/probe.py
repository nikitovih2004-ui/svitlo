"""`python -m svitlo probe`: один раз прочитать инвертор и график, напечатать по-человечески
и показать итоговый chat_data. Ничего не пишет ни в инвертор, ни в базу бота."""

import asyncio
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .schedule import (FALLBACK_URL, USER_AGENT, ScheduleFetcher, contract_view, parse_fallback)
from .source import SolarmanSource, SourceError
from .state import Engine, encode_inverter, intervals_text, local_date
from .storage import Store
from .texts import DASHBOARD_CAPTION, dashboard_text, duration, inverter_text


def _fmt_day(d, tz, now) -> str:
    today = local_date(now, tz)
    label = {0: "сегодня", 1: "завтра"}.get((d - today).days, "")
    return ("%s %s" % (label, d.strftime("%d.%m"))).strip()


async def _schedules(cfg, tz):
    async with httpx.AsyncClient(timeout=20.0) as http:
        fetcher = ScheduleFetcher(http, cfg.group, tz, cfg.yasno_region, cfg.yasno_dso)
        main = fallback = None
        errors = []
        try:
            main = await fetcher.fetch()
        except Exception as exc:
            errors.append("основной: %s" % exc)
        try:
            resp = await http.get(FALLBACK_URL, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            fallback = parse_fallback(resp.json(), cfg.group, tz)
        except Exception as exc:
            errors.append("запасной: %s" % exc)
        return main, fallback, errors


def probe(cfg, with_schedule: bool = True) -> int:
    tz = ZoneInfo(cfg.tz)
    rc = 0
    print("Инвертор %s:%s (логгер %s, slave %s)" % (cfg.logger_host, cfg.logger_port,
                                                  cfg.logger_serial, cfg.logger_slave))
    src = SolarmanSource(cfg.logger_host, cfg.logger_serial, cfg.logger_port, cfg.logger_slave)
    r = None
    try:
        r = src.read(with_day=True)
    except SourceError as exc:
        print("  ОШИБКА: %s" % exc)
        rc = 1
    finally:
        src.close()

    store = Store(":memory:")
    engine = Engine(store, tz, cfg.grid_on_v, cfg.battery_kwh)
    engine.tracker.confirm = 1  # одно чтение — без подтверждения
    if r is not None:
        engine.observe(r)
        on = engine.tracker.raw(r)
        flow = "разряд" if r.batt_w > 0 else ("заряд" if r.batt_w < 0 else "покой")
        print("  Время чтения:  %s" % datetime.fromtimestamp(r.ts, tz).strftime("%d.%m.%Y %H:%M:%S"))
        print("  Сеть:          %.1f В → %s (порог %.0f В), реле %s, мощность сети %d Вт"
              % (r.grid_v, "ЕСТЬ" if on else "НЕТ", cfg.grid_on_v, r.grid_relay, r.grid_w))
        print("  Нагрузка:      %d Вт" % r.load_w)
        print("  Батарея:       %d%% · %.2f В · %.2f А · %d Вт (%s) · %.1f °C"
              % (r.soc, r.batt_v, r.batt_a, r.batt_w, flow, r.batt_temp_c))
        print("  Настройки:     отключение при %d%% (рег. 217), макс. ток заряда %d А; рег. 204 = %d А·ч "
              "(не используется)" % (r.cutoff_soc, r.max_charge_a, r.capacity_ah))
        print("  Ёмкость:       %s" % ("%.2f кВт·ч (BATTERY_KWH)" % cfg.battery_kwh if cfg.battery_kwh
                                       else "не задана (BATTERY_KWH) — прогноза нет, e = 0"))
        if r.day:
            print("  За сегодня:    заряд %.1f кВт·ч, разряд %.1f, из сети %.1f, потребление %.1f"
                  % (r.day.batt_charge_kwh, r.day.batt_discharge_kwh, r.day.grid_import_kwh, r.day.load_kwh))
            print("  Инвертор:      частота сети %.2f Гц (рег. 79), радиатор %.1f °C (рег. 91)"
                  % (r.day.grid_hz, r.day.inverter_c if r.day.inverter_c is not None else float("nan")))
        e = engine.estimator.eta
        if e:
            what = "до %d%%" % r.cutoff_soc if not on else "до 100%"
            print("  Прогноз %s:  ≈ %s (через %s; по одному чтению, без сглаживания)"
                  % (what, datetime.fromtimestamp(e, tz).strftime("%H:%M"), duration(e - r.ts)))
        else:
            print("  Прогноз:       нет (%s)" % ("нет заряда/разряда или батарея полная" if cfg.battery_kwh
                                              else "BATTERY_KWH не задан"))

    if with_schedule:
        main, fallback, errors = asyncio.run(_schedules(cfg, tz))
        now = datetime.now(tz).timestamp()
        for title, sched in (("График", main), ("Запасной источник", fallback)):
            if sched is None:
                continue
            print("%s (группа %s, %s, обновлён %s):" % (title, cfg.group, sched.source, sched.updated_on or "?"))
            for d in sorted(sched.days):
                if d < local_date(now, tz) - timedelta(days=1):
                    continue
                day = sched.days[d]
                o = [[a, b] for a, b in day.intervals]
                print("  %-14s %-9s %s" % (_fmt_day(d, tz, now), day.status, intervals_text(o)))
        for err in errors:
            print("  ОШИБКА графика — %s" % err)
            rc = rc or 2
        if main is None and fallback is None:
            rc = rc or 2
        sched = main or fallback
        if sched is not None:
            engine.set_schedule(sched, now)
            o, w = contract_view(engine.schedule, local_date(now, tz))
            print("Для виджета:     o = %s (%s), w = %d" % (json.dumps(o), intervals_text(o), w))

    if engine.ready:
        now = datetime.now(tz).timestamp()
        data = engine.chat_data(now)
        print("Подпись дашборда: «%s»" % DASHBOARD_CAPTION)
        plain = lambda html: re.sub(r"<[^>]+>", "", html)  # noqa: E731
        print("Текстовый дашборд (если Telegram не разрешает виджеты):\n%s" % plain(
            dashboard_text(json.loads(data), now, tz, known=False)))
        print("chat_data (%d байт):" % len(data.encode("utf-8")))
        print(data)
        inv = encode_inverter(dict(engine.inverter_snapshot(now), r=1))
        print("Текстовый «Инвертор»:\n%s" % plain(inverter_text(json.loads(inv), now, tz)))
        print("chat_data виджета «Инвертор» (%d байт):" % len(inv.encode("utf-8")))
        print(inv)
    store.close()
    return rc
