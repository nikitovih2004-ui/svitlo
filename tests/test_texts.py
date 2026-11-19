"""Сообщение «График» и разбор новых регистров."""

from datetime import date

from svitlo import texts
from svitlo.schedule import Change
from svitlo.source import DAY_COUNT, DAY_START, decode_day

TODAY, TOMORROW = date(2026, 11, 12), date(2026, 11, 13)
T0 = [(0, 90), (420, 780), (900, 1320)]
T1 = [(120, 420), (600, 990), (1110, 1440)]


def test_tomorrow_appeared_exactly_as_design():
    assert texts.schedule_message(TOMORROW, T1, tomorrow=True) == (
        "🗓 <b>График на завтра</b> · пт, 13 ноября\n\n"
        "с <b>02:00</b> до <b>07:00</b> · 5 ч\n"
        "с <b>10:00</b> до <b>16:30</b> · 6 ч 30 мин\n"
        "с <b>18:30</b> до <b>24:00</b> · 5 ч 30 мин\n\n"
        "Всего без света 17 ч")


def test_changed_marks_as_design():
    assert texts.schedule_message(TOMORROW, [(120, 420), (780, 900), (1140, 1440)], True, old=T1) == (
        "🔄 <b>График на завтра изменился</b> · пт, 13 ноября\n\n"
        "с <b>02:00</b> до <b>07:00</b> · 5 ч\n"
        "с <b>13:00</b> до <b>15:00</b> · 2 ч  ← <i>было 10:00–16:30</i>\n"
        "с <b>19:00</b> до <b>24:00</b> · 5 ч  ← <i>было с 18:30</i>\n\n"
        "Всего без света 12 ч (было 17 ч)")
    cancelled = texts.schedule_message(TODAY, [(0, 90), (900, 1320), (1380, 1440)], False, old=T0)
    assert "<s>с 07:00 до 13:00</s>  ← <i>отменено</i>\n" in cancelled and "· 1 ч  ← <i>новое</i>" in cancelled
    assert "← <i>было 15:00–22:00</i>" in texts.schedule_message(TODAY, [(0, 90), (420, 780), (960, 1200)],
                                                                False, old=T0)


def test_empty_and_emergency():
    assert texts.schedule_message(TOMORROW, [], True) == "🗓 <b>График на завтра</b> · пт, 13 ноября\nОтключений нет."
    assert texts.schedule_message(TODAY, [], False, old=T0).endswith("\nОтключений больше нет.")
    assert texts.change_message(Change(TODAY, False, [], emergency=True)) == (
        "🚨 <b>Экстренные отключения</b> · чт, 12 ноября\n"
        "График на сегодня не действует — свет отключают без расписания.")


def test_span_and_label():
    assert [texts.span(m) for m in (45, 60, 390, 1440)] == ["45 мин", "1 ч", "6 ч 30 мин", "24 ч"]
    assert texts.day_label(date(2026, 11, 16)) == "пн, 16 ноября" and texts.clock(1440) == "24:00"


def test_current_schedule_emergency_and_waiting():
    text = texts.current_schedule([(TODAY, False, "emergency", []), (TOMORROW, True, "unknown", [])])
    assert text.startswith("🚨 <b>Экстренные отключения</b> · чт, 12 ноября")
    assert text.endswith("🗓 <b>График на завтра</b> · пт, 13 ноября\nЕщё не опубликован.")


def test_old_plain_functions_gone():
    assert not hasattr(texts, "schedule_text") and not hasattr(texts, "intervals_text")


def test_decode_day_live_values():
    """Сырые значения с живого инвертора 12.11 13:28 (сети нет): 79 = 0, 91 = 1406 → 40.6 °C."""
    regs = [0] * DAY_COUNT
    live = {70: 19, 71: 13, 76: 77, 79: 0, 84: 71, 90: 1560, 91: 1406}
    for a, v in live.items():
        regs[a - DAY_START] = v
    d = decode_day(regs)
    assert (d.batt_charge_kwh, d.batt_discharge_kwh, d.grid_import_kwh, d.load_kwh) == (1.9, 1.3, 7.7, 7.1)
    assert d.grid_hz == 0 and d.inverter_c == 40.6
    regs[79 - DAY_START] = 5001
    assert decode_day(regs).grid_hz == 50.01
    # баланс за сутки без солнца: из сети ≈ потребление + заряд − разряд
    assert abs(d.grid_import_kwh - (d.load_kwh + d.batt_charge_kwh - d.batt_discharge_kwh)) < 0.2


def test_event_texts_with_emoji(tz):
    from datetime import datetime
    on = datetime(2026, 11, 12, 15, 48, tzinfo=tz).timestamp()
    off = datetime(2026, 11, 12, 12, 46, tzinfo=tz).timestamp()
    assert texts.event_text(True, on, 3 * 3600 + 2 * 60, tz) == "🟢 Свет появился в 15:48. Не было 3 ч 02 мин."
    assert texts.event_text(False, off, 9 * 3600 + 17 * 60, tz) == "🔴 Свет пропал в 12:46. Был 9 ч 17 мин."
    assert texts.event_text(False, off, 0, tz) == "🔴 Свет пропал в 12:46."
