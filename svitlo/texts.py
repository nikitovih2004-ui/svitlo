"""Все тексты бота в одном месте (их правит тимлид; логика их не трогает).

События (свет пропал/появился) — отдельные короткие сообщения со звуком; дашборд-виджет
присылается беззвучно, его подпись постоянная.
Все функции получают уже готовые числа; время форматируется в Europe/Kyiv.
"""

from datetime import datetime


def hm(ts: int, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def duration(sec: int) -> str:
    """6 ч 04 мин · 45 мин · 2 д 3 ч"""
    m = max(0, int(sec)) // 60
    d, m = divmod(m, 1440)
    h, m = divmod(m, 60)
    if d:
        return "%d д %d ч" % (d, h)
    if h:
        return "%d ч %02d мин" % (h, m)
    return "%d мин" % m


# ------------------------------------------------------------------ события и дашборд


def event_text(grid: bool, since: int, prev: int, tz) -> str:
    """Отдельное сообщение со звуком при смене состояния сети.
    prev — сколько длилось прошлое состояние, с (0 — неизвестно: вторую фразу не пишем)."""
    if grid:
        text = "🟢 Свет появился в %s." % hm(since, tz)
        if prev:
            text += " Не было %s." % duration(prev)
        return text
    text = "🔴 Свет пропал в %s." % hm(since, tz)
    if prev:
        text += " Был %s." % duration(prev)
    return text


# Подпись дашборда: видна только в клиентах без виджетов и в списке чатов (состояние показывает виджет).
DASHBOARD_CAPTION = "Дашборд"


# ------------------------------------------------------------------ текстовый режим (parse_mode=HTML)
# Пока Telegram не разрешает боту виджеты (INPUT_MEDIA_WIDGET_NOT_ALLOWED), дашборд и «Инвертор» — обычные
# сообщения, которые бот правит (editMessageText). Текст сам не тикает, поэтому вместо обратных отсчётов —
# абсолютное время, внизу «обновлено HH:MM». Логика повторяет виджеты widgets/a.html и widgets/inverter.html.
# Сменился формат — поднять TEXT_FORMAT: бот исправит уже отправленные текстовые дашборды на месте.

TEXT_FORMAT = "text-2"
STALE_SEC = 300  # данные старше — «нет связи с инвертором» (как в виджетах)
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _at(m: float) -> str:
    """Минуты от полуночи → HH:MM (ровно 1440 — «24:00», иначе по модулю суток)."""
    m = int(m)
    return "24:00" if m == 1440 else "%02d:%02d" % divmod(m % 1440, 60)


def _num(x: float, k: int) -> str:
    """53.3 → «53,3»; −4.6 → «−4,6»."""
    return ("%.*f" % (k, x)).replace(".", ",").replace("-", "−")


def _hold(sec: float) -> str:
    """Сколько хватит: «45 мин» до 1,5 ч, дальше «~19 ч» (как в виджете)."""
    return "%d мин" % round(sec / 60) if sec < 5400 else "~%d ч" % round(sec / 3600)


def _day_short(d0: int, k: int, tz) -> str:
    x = datetime.fromtimestamp(d0 + k * 86400 + 43200, tz)
    return "%s %02d.%02d" % (WEEKDAYS_SHORT[x.weekday()], x.day, x.month)


def dashboard_text(d: dict, now: float, tz, known: bool = True, footer: bool = True) -> str:
    """Текстовый дашборд («Табло» текстом). d — chat_data по контракту, now — момент правки.
    known=False — неизвестно, с какого времени текущее состояние сети. footer=False — без строки
    «обновлено …» (для сравнения: изменилось ли что-то, кроме неё)."""
    n = (now - d["d"]) / 60.0
    g, mask = d["g"], d.get("m", 0)
    day = int(n // 1440)
    z = day * 1440
    horizon = 2880 if d.get("w") else 1440
    o = [v for v in d.get("o") or [] if v[0] < horizon] if n < horizon else []
    cur = next((v for v in o if v[0] <= n < v[1]), None)
    nxt = next((v for v in o if v[0] > n), None)

    def when(v: int) -> str:
        return ("завтра " if v - z > 1440 else "") + "<b>%s</b>" % _at(v - z)

    lines = []
    if now - d["t"] > STALE_SEC:
        lines.append("⚠️ Нет связи с инвертором · данные на <b>%s</b>" % hm(d["t"], tz))
    head = "🟢 <b>Свет есть</b>" if g else "🔴 <b>Света нет</b>"
    if known:
        since = _day_short(d["d"], int((d["s"] - d["d"]) // 86400), tz) if now - d["s"] > 80000 else hm(d["s"], tz)
        head += " с <b>%s</b>" % since
    lines.append(head)
    if not g and cur:
        lines.append("По графику включат в %s" % when(cur[1]))
    elif not g:
        lines.append("Отключили вне графика — когда включат, неизвестно")
    elif mask >> day & 1:
        lines.append("🚨 Аварийные отключения — график не действует")
    elif cur:
        lines.append("По графику света нет до %s, но свет пока есть" % when(cur[1]))
    elif nxt:
        lines.append("По графику отключат в %s до %s" % (when(nxt[0]), when(nxt[1])))
    else:
        lines.append("До конца %s без отключений" % ("завтра" if horizon - n > 1440 else "суток"))

    lines.append("")
    for k, title in ((0, "Сегодня"), (1, "Завтра")):
        a = z + k * 1440
        label = "%s, %s: " % (title, _day_short(d["d"], day + k, tz))
        if mask >> (day + k) & 1:
            lines.append(label + "🚨 аварийные отключения, график не действует")
        elif a + 1440 > horizon:
            if k == 0:
                lines.append(label + "график ещё не опубликован")
        else:
            parts = []
            for v in o:
                if v[1] <= a or v[0] >= a + 1440:
                    continue
                b, c = max(v[0], a), min(v[1], a + 1440)
                iv = "%s–%s" % (_at(b - a), _at(c - a))
                parts.append(iv if c <= n else "<b>%s</b>" % iv)  # прошедшие — обычным шрифтом
            lines.append(label + (", ".join(parts) or "отключений нет"))

    lines.append("")
    hold = d["e"] - now if not g and d.get("e", 0) > now else d.get("h", 0)
    batt = "%s <b>%d%%</b>" % ("🪫" if d["c"] < 20 else "🔋", d["c"])
    if hold > 0:
        batt += " · %s <b>%s</b>" % ("без сети хватит на" if g else "хватит на", _hold(hold))
        if not g and d.get("e", 0) > now:
            batt += ", до <b>%s</b>" % hm(d["e"], tz)
    elif d["c"] > 98:
        batt += " · батарея полная"
    if d["b"] < -50 and d.get("e", 0) > now:
        batt += " · полная к <b>%s</b>" % hm(d["e"], tz)
    lines.append(batt)
    load = "🏠 Нагрузка <b>%d Вт</b>" % d["l"]
    if g and d.get("u"):
        load += " · сеть <b>%d В</b>" % d["u"]
    lines.append(load)
    if footer:
        lines.append("")
        lines.append("<i>обновлено %s</i>" % hm(now, tz))
    return "\n".join(lines)


def inverter_text(d: dict, now: float, tz, footer: bool = True) -> str:
    """Текстовый «Инвертор» (те же данные, что в виджете). d — chat_data виджета «Инвертор»
    (ключ `z` — до какого момента бот его правит; после — «Снимок на HH:MM»)."""
    snapshot = bool(d.get("z")) and now > d["z"]
    g = d["g"]
    lines = ["⚡ <b>Инвертор</b>"]
    if now - d["t"] > STALE_SEC and not snapshot:
        lines.append("⚠️ Нет связи с инвертором · данные на <b>%s</b>" % hm(d["t"], tz))
    if g:
        grid = "🔌 Сеть: <b>%s В</b>" % _num(d["u"], 1)
        if d.get("f"):
            grid += " · %s Гц" % _num(d["f"], 2)
        if d.get("n"):
            grid += " · %s <b>%d Вт</b>" % ("из сети" if d["n"] > 0 else "в сеть", abs(d["n"]))
    else:
        grid = "🔌 Сеть: <b>нет напряжения</b>"
    lines.append(grid)
    lines.append("🏠 Дом: <b>%d Вт</b>" % d["l"])
    batt = "%s Батарея: <b>%d%%</b>" % ("🪫" if d["c"] < 20 else "🔋", d["c"])
    if d["b"] < -20:
        batt += " · заряд <b>%d Вт</b>" % -d["b"]
    elif d["b"] > 20:
        batt += " · разряд <b>%d Вт</b>" % d["b"]
    lines.append(batt)
    lines.append("      %s В · %s А · %s °C" % (_num(d["v"], 1), _num(abs(d["a"]), 1), _num(d["k"], 1)))
    hold = d["e"] - now if not g and d.get("e", 0) > now else d.get("h", 0)
    extra = []
    if hold > 0:
        extra.append("%s %s" % ("без сети хватит на" if g else "хватит на", _hold(hold)))
    if d["c"] < d["x"] + 15 or not hold > 0:
        extra.append("отключится при %d%%" % d["x"])
    if d["b"] < -20 and d.get("e", 0) > now:
        extra.append("полная к <b>%s</b>" % hm(d["e"], tz))
    if extra:
        lines.append("      " + " · ".join(extra))
    if d.get("i"):
        lines.append("🌡 Инвертор: <b>%s °C</b>" % _num(d["i"], 1))
    q = d.get("q") or []
    if len(q) == 4:
        lines.append("📊 За сегодня, кВт·ч: потребили <b>%s</b> · из сети <b>%s</b> · заряд <b>%s</b> · разряд <b>%s</b>"
                     % (_num(q[3], 1), _num(q[2], 1), _num(q[0], 1), _num(q[1], 1)))
    if footer:
        lines.append("")
        if snapshot:
            lines.append("<i>Снимок на %s · свежие данные — кнопка «Инвертор»</i>" % hm(d["t"], tz))
        elif d.get("z"):
            lines.append("<i>обновлено %s · обновляется до %s</i>" % (hm(now, tz), hm(d["z"], tz)))
        else:
            lines.append("<i>обновлено %s</i>" % hm(now, tz))
    return "\n".join(lines)


# ------------------------------------------------------------------ команды и кнопки

# Постоянная клавиатура под полем ввода (reply keyboard). Нажатие присылает боту ровно этот текст.
BTN_DASHBOARD = "Дашборд"
BTN_SCHEDULE = "График"
BTN_INVERTER = "Инвертор"
KEYBOARD = {"keyboard": [[{"text": BTN_DASHBOARD}, {"text": BTN_SCHEDULE}, {"text": BTN_INVERTER}]],
            "resize_keyboard": True, "is_persistent": True}

START_OK = "Готово, вы подписаны. Пришлю сообщение, когда свет пропадёт или появится."
START_AGAIN = "Вы уже подписаны. Текущее состояние — ниже."
KEYBOARD_HELLO = "Кнопки внизу: «Дашборд» — свежий дашборд, «График» — график отключений, «Инвертор» — схема и цифры инвертора."
DENIED = ("Извините, это частный бот для одной квартиры.\n"
          "Если вас должны добавить, передайте владельцу ваш id: {id}")  # владелец не задан (ALLOWED_IDS пуст)
STOP_OK = "Подписка отключена. Вернуться — /start."
NOT_SUBSCRIBED = "Вы не подписаны. Подписаться — /start."
NOT_READY = "Ещё не знаю состояние сети — жду данных от инвертора. Попробуйте через минуту."
SCHEDULE_NOT_LOADED = "График ещё не загружен — попробуйте через минуту."
INVERTER_CAPTION = "Инвертор"
HELP = ("Кнопки внизу:\n"
        "«Дашборд» — пересоздать дашборд внизу чата (то же, что /status)\n"
        "«График» — график отключений на сегодня и завтра\n"
        "«Инвертор» — схема и цифры инвертора, обновляются 15 минут\n"
        "/stop — отписаться, /start — подписаться")
HELP_OWNER = HELP + "\n/users — кто имеет доступ (убрать или вернуть доступ)"


# ------------------------------------------------------------------ сообщение «График» (parse_mode=HTML)
# intervals — [(a, b), …] минуты от полуночи ЭТОГО дня,
# отсортированы и склеены, b ≤ 1440 (1440 → «24:00»).

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")


def clock(m: int) -> str:
    return "24:00" if m >= 1440 else "%02d:%02d" % divmod(int(m), 60)


def span(m: int) -> str:
    """5 ч · 6 ч 30 мин · 45 мин"""
    h, mm = divmod(int(m), 60)
    if not h:
        return "%d мин" % mm
    return "%d ч %d мин" % (h, mm) if mm else "%d ч" % h


def day_label(day) -> str:
    return "%s, %d %s" % (WEEKDAYS[day.weekday()], day.day, MONTHS[day.month - 1])


def _note(iv, old) -> str:
    """Что изменилось у интервала по сравнению со старым графиком ('' — не изменился)."""
    a, b = iv
    over = [tuple(o) for o in old if o[0] < b and a < o[1]]
    if not over:
        return "новое"
    if over == [tuple(iv)]:
        return ""
    if len(over) == 1:
        oa, ob = over[0]
        if oa == a:
            return "было до %s" % clock(ob)
        if ob == b:
            return "было с %s" % clock(oa)
    return "было " + ", ".join("%s–%s" % (clock(x), clock(y)) for x, y in over)


def emergency_message(day, tomorrow: bool) -> str:
    return ("🚨 <b>Экстренные отключения</b> · %s\n"
            "График %s не действует — свет отключают без расписания."
            % (day_label(day), "на завтра" if tomorrow else "на сегодня"))


def schedule_message(day, intervals, tomorrow: bool, old=None) -> str:
    """Сообщение «График»: появился (old=None) или изменился (old — прошлые интервалы того же дня)."""
    intervals = [tuple(x) for x in intervals]
    changed = old is not None
    title = "График на %s%s" % ("завтра" if tomorrow else "сегодня", " изменился" if changed else "")
    head = "%s <b>%s</b> · %s" % ("🔄" if changed else "🗓", title, day_label(day))
    old = [tuple(x) for x in (old or [])]
    if not intervals:
        return head + "\n" + ("Отключений больше нет." if changed and old else "Отключений нет.")
    lines = []
    for a, b in intervals:
        note = _note((a, b), old) if changed else ""
        line = "с <b>%s</b> до <b>%s</b> · %s" % (clock(a), clock(b), span(b - a))
        lines.append((a, line + ("  ← <i>%s</i>" % note if note else "")))
    if changed:
        for oa, ob in old:
            if not any(a < ob and oa < b for a, b in intervals):
                lines.append((oa, "<s>с %s до %s</s>  ← <i>отменено</i>" % (clock(oa), clock(ob))))
    lines.sort(key=lambda x: x[0])
    total = sum(b - a for a, b in intervals)
    was = sum(b - a for a, b in old)
    summary = "Всего без света %s" % span(total)
    if changed and was != total:
        summary += " (было %s)" % span(was)
    return "%s\n\n%s\n\n%s" % (head, "\n".join(text for _, text in lines), summary)


def change_message(change) -> str:
    """Текст для schedule.Change (автоматическое сообщение об изменении графика)."""
    if change.emergency:
        return emergency_message(change.day, change.tomorrow)
    return schedule_message(change.day, change.intervals, change.tomorrow, change.old)


def not_published(day, tomorrow: bool) -> str:
    return "🗓 <b>График на %s</b> · %s\nЕщё не опубликован." % ("завтра" if tomorrow else "сегодня",
                                                               day_label(day))


def current_schedule(days) -> str:
    """Ответ на кнопку «График»: days — [(дата, завтра?, статус, интервалы)], статус из schedule.py
    ('applies' | 'waiting' | 'emergency' | 'unknown'/None — нет данных)."""
    parts = []
    for day, tomorrow, status, intervals in days:
        if status == "emergency":
            parts.append(emergency_message(day, tomorrow))
        elif status == "applies":
            parts.append(schedule_message(day, intervals, tomorrow))
        else:
            parts.append(not_published(day, tomorrow))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ доступ (запрос → подтверждение владельцем)

ACCESS_REQUESTED = "Запрос отправлен владельцу. Как только он подтвердит — бот заработает."
ACCESS_DENIED = "Доступ не выдан."
ACCESS_GRANTED = ("Доступ открыт. Пришлю сообщение, когда свет пропадёт или появится; "
                  "кнопки внизу: «Дашборд», «График», «Инвертор».")
BTN_ALLOW = "Разрешить"
BTN_REJECT = "Отклонить"
BTN_REMOVE = "Убрать"
OWNER_ONLY = "Это может только владелец бота."
ALREADY_DECIDED = "Уже решено."


def person(name: str, username: str = "", user_id: int = 0) -> str:
    """«Имя Фамилия (@username)»; без username — «Имя Фамилия (id 123)»."""
    name = name or "Без имени"
    if username:
        return "%s (@%s)" % (name, username)
    return "%s (id %d)" % (name, user_id) if user_id else name


def access_request(name: str, username: str, user_id: int) -> str:
    return "%s просит доступ к боту" % person(name, username, user_id)


def access_granted(name: str) -> str:
    return "Доступ выдан: %s" % (name or "Без имени")


def access_rejected(name: str) -> str:
    return "Отклонено: %s" % (name or "Без имени")


def access_removed(name: str) -> str:
    return "Доступ убран: %s" % (name or "Без имени")


def users_list(allowed, closed, pending=()) -> str:
    """Ответ на /users. allowed — [(имя, username, id, владелец?)], closed — [(имя, username, id)]
    (отклонённые и убранные), pending — так же (запрос ждёт ответа)."""
    lines = ["С доступом:"]
    for name, username, uid, owner in allowed:
        lines.append("• %s%s" % (person(name, username, uid), " — владелец" if owner else ""))
    if pending:
        lines.append("")
        lines.append("Ждут ответа:")
        lines.extend("• %s" % person(name, username, uid) for name, username, uid in pending)
    if closed:
        lines.append("")
        lines.append("Без доступа (отклонены или убраны):")
        lines.extend("• %s" % person(name, username, uid) for name, username, uid in closed)
    return "\n".join(lines)

