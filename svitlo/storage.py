"""Хранилище sqlite: подписчики, дашборды, доступ (запросы и решения владельца), состояние сети,
история событий, кривая заряда, журнал точности прогноза разряда."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    live_msg_id    INTEGER,          -- id дашборда (NULL — дашборда нет)
    live_kind      TEXT,             -- 'widget' | 'text' (запасной) | 'gone' (удалён пользователем)
    live_epoch     INTEGER,          -- начало состояния сети (s), после события которого отправлен дашборд
    live_data      TEXT,             -- последний отправленный chat_data
    live_at        REAL,             -- когда последний раз отправляли/правили
    created_at     REAL NOT NULL,
    notified_epoch INTEGER,          -- состояние сети (s), о котором подписчику уже сообщено (или не нужно)
    live_sent_at   REAL,             -- когда дашборд отправлен (для пересоздания до 48 ч)
    live_widget    TEXT,             -- хэш HTML виджета, с которым отправлен дашборд (текстовый — версия формата)
    notice_seen    INTEGER,          -- id последнего обработанного сообщения о графике (NULL — ещё не начато)
    notice_sent    INTEGER,          -- id последнего реально отправленного сообщения о графике
    live_notice    INTEGER,          -- notice_sent на момент отправки дашборда
    kb_sent        INTEGER,          -- 1 — клавиатура с кнопками уже показана
    ask_msg_id     INTEGER,          -- последний ответ на кнопку «График» (удаляется при следующем)
    inv_msg_id     INTEGER,          -- «Инвертор»: виджет или текст (NULL — нет)
    inv_until      REAL,             -- до какого момента он обновляется (ключ `z`)
    inv_data       TEXT,             -- его последний chat_data
    inv_at         REAL,             -- когда его последний раз отправляли/правили
    inv_kind       TEXT              -- 'widget' | 'text' (NULL — виджет)
);
CREATE TABLE IF NOT EXISTS notices (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,           -- когда обнаружено изменение графика
    text  TEXT NOT NULL,
    loud  INTEGER NOT NULL         -- 1 — со звуком (сегодня, экстренные), 0 — беззвучно (завтра); текст — HTML
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,    -- когда подтверждено
    grid      INTEGER NOT NULL,    -- новое состояние: 1 есть, 0 нет
    since     INTEGER NOT NULL,    -- начало нового состояния
    prev_dur  INTEGER NOT NULL,    -- длительность прошлого, с (0 — неизвестно)
    soc       INTEGER
);
CREATE TABLE IF NOT EXISTS charge_curve (
    bucket      INTEGER PRIMARY KEY,
    sec_per_pct REAL NOT NULL,
    samples     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS discharge_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    period  INTEGER NOT NULL,      -- начало периода без сети (s)
    ts      INTEGER NOT NULL,
    kind    TEXT NOT NULL,         -- 'f' — прогноз (раз в ~10 мин), 't' — показание SOC упало на 1%
    soc     INTEGER NOT NULL,      -- SOC инвертора (для 't' — новое значение)
    soc_f   REAL,                  -- дробный SOC, от которого считался прогноз
    power   REAL,                  -- сглаженная мощность разряда, Вт
    eta     INTEGER,               -- опубликованный прогноз `e` (0 — прогноза нет)
    cutoff  INTEGER                -- порог отключения, %
);
CREATE INDEX IF NOT EXISTS discharge_log_period ON discharge_log(period, ts);
CREATE TABLE IF NOT EXISTS access (
    user_id      INTEGER PRIMARY KEY,
    status       TEXT NOT NULL,     -- 'pending' (ждёт владельца) | 'allowed' | 'denied' (отклонён) | 'revoked' (убран)
                                    -- | 'failed' (запрос не дошёл до владельца; можно запросить снова)
    name         TEXT NOT NULL DEFAULT '',
    username     TEXT NOT NULL DEFAULT '',
    requested_at REAL,              -- когда пришёл последний запрос (для лимита запросов в час)
    decided_at   REAL,              -- когда владелец решил
    owner_msg_id INTEGER            -- сообщение-запрос у владельца (правится после решения)
);
"""

# колонки, добавленные после первой версии (ALTER TABLE для старых баз)
MIGRATIONS = (("subscribers", "notified_epoch", "INTEGER"), ("subscribers", "live_sent_at", "REAL"),
              ("subscribers", "live_widget", "TEXT"), ("subscribers", "notice_seen", "INTEGER"),
              ("subscribers", "notice_sent", "INTEGER"), ("subscribers", "live_notice", "INTEGER"),
              ("subscribers", "kb_sent", "INTEGER"), ("subscribers", "ask_msg_id", "INTEGER"),
              ("subscribers", "inv_msg_id", "INTEGER"), ("subscribers", "inv_until", "REAL"),
              ("subscribers", "inv_data", "TEXT"), ("subscribers", "inv_at", "REAL"),
              ("subscribers", "inv_kind", "TEXT"))
NOTICE_KEEP = 7 * 86400  # сколько хранить сообщения о графике в базе, с


@dataclass
class Subscriber:
    chat_id: int
    name: str
    active: bool
    live_msg_id: Optional[int]
    live_kind: Optional[str]
    live_epoch: Optional[int]
    live_data: Optional[str]
    live_at: Optional[float]
    notified_epoch: Optional[int]
    live_sent_at: Optional[float]
    live_widget: Optional[str] = None
    notice_seen: Optional[int] = None
    notice_sent: Optional[int] = None
    live_notice: Optional[int] = None
    kb_sent: Optional[int] = None
    ask_msg_id: Optional[int] = None
    inv_msg_id: Optional[int] = None
    inv_until: Optional[float] = None
    inv_data: Optional[str] = None
    inv_at: Optional[float] = None
    inv_kind: Optional[str] = None   # 'widget' | 'text' (текстовый режим); NULL — виджет (старые записи)


@dataclass
class Access:
    user_id: int
    status: str
    name: str
    username: str
    requested_at: Optional[float]
    decided_at: Optional[float]
    owner_msg_id: Optional[int]


@dataclass
class Notice:
    id: int
    ts: float
    text: str
    loud: bool


@dataclass
class LogRow:
    period: int
    ts: int
    kind: str
    soc: int
    soc_f: Optional[float]
    power: Optional[float]
    eta: Optional[int]
    cutoff: Optional[int]


class Store:
    def __init__(self, path: Union[str, Path]):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)  # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(%s)" % table)}
            if col not in cols:
                self.db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- kv
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    # ---------------------------------------------------------------- подписчики
    _COLS = ("chat_id, name, active, live_msg_id, live_kind, live_epoch, live_data, live_at, "
             "notified_epoch, live_sent_at, live_widget, notice_seen, notice_sent, live_notice, "
             "kb_sent, ask_msg_id, inv_msg_id, inv_until, inv_data, inv_at, inv_kind")

    def _sub(self, row) -> Subscriber:
        return Subscriber(row[0], row[1], bool(row[2]), *row[3:])

    def subscriber(self, chat_id: int) -> Optional[Subscriber]:
        row = self.db.execute("SELECT %s FROM subscribers WHERE chat_id=?" % self._COLS, (chat_id,)).fetchone()
        return self._sub(row) if row else None

    def subscribers(self, active_only: bool = True) -> List[Subscriber]:
        q = "SELECT %s FROM subscribers" % self._COLS + (" WHERE active=1" if active_only else "")
        return [self._sub(r) for r in self.db.execute(q + " ORDER BY chat_id")]

    def subscribe(self, chat_id: int, name: str, now: float) -> bool:
        """True — подписка новая или возобновлена."""
        sub = self.subscriber(chat_id)
        if sub is None:
            self.db.execute("INSERT INTO subscribers(chat_id, name, active, created_at) VALUES(?,?,1,?)",
                            (chat_id, name, now))
            return True
        self.db.execute("UPDATE subscribers SET active=1, name=? WHERE chat_id=?", (name, chat_id))
        return not sub.active

    def unsubscribe(self, chat_id: int) -> None:
        """Отписка: дашборд и отметка о событиях забываются."""
        self.db.execute("UPDATE subscribers SET active=0, live_msg_id=NULL, live_kind=NULL, live_epoch=NULL, "
                        "live_data=NULL, live_at=NULL, live_sent_at=NULL, notified_epoch=NULL, "
                        "live_widget=NULL, notice_seen=NULL, notice_sent=NULL, live_notice=NULL, ask_msg_id=NULL, "
                        "inv_msg_id=NULL, inv_until=NULL, inv_data=NULL, inv_at=NULL, inv_kind=NULL "
                        "WHERE chat_id=?", (chat_id,))

    def set_notified(self, chat_id: int, epoch: Optional[int]) -> None:
        self.db.execute("UPDATE subscribers SET notified_epoch=? WHERE chat_id=?", (epoch, chat_id))

    def set_live(self, chat_id: int, msg_id: int, kind: str, epoch: int, data: str, at: float,
                 widget: Optional[str] = None, notice: Optional[int] = None) -> None:
        """Новый дашборд отправлен (widget — хэш HTML, notice — notice_sent на этот момент)."""
        self.db.execute("UPDATE subscribers SET live_msg_id=?, live_kind=?, live_epoch=?, live_data=?, "
                        "live_at=?, live_sent_at=?, live_widget=?, live_notice=? WHERE chat_id=?",
                        (msg_id, kind, epoch, data, at, at, widget, notice, chat_id))

    def set_notice_seen(self, chat_id: int, seen: int, sent: Optional[int] = None) -> None:
        """Сообщение о графике обработано (sent — если оно действительно отправлено)."""
        if sent is None:
            self.db.execute("UPDATE subscribers SET notice_seen=? WHERE chat_id=?", (seen, chat_id))
        else:
            self.db.execute("UPDATE subscribers SET notice_seen=?, notice_sent=? WHERE chat_id=?",
                            (seen, sent, chat_id))

    def clear_live(self, chat_id: int, kind: Optional[str] = None) -> None:
        """Дашборда больше нет (удалили мы — kind=None, удалил пользователь — kind='gone')."""
        self.db.execute("UPDATE subscribers SET live_msg_id=NULL, live_kind=? WHERE chat_id=?", (kind, chat_id))

    def set_fields(self, chat_id: int, **fields) -> None:
        """Обновить отдельные колонки подписчика (kb_sent, ask_msg_id, inv_*; live_epoch=None — дашборд
        оказался выше нового сообщения бота и при следующей сверке пересоздаётся, даже если сейчас не выйдет)."""
        allowed = {"kb_sent", "ask_msg_id", "inv_msg_id", "inv_until", "inv_data", "inv_at", "inv_kind", "live_epoch"}
        assert fields and set(fields) <= allowed, fields
        cols = sorted(fields)
        self.db.execute("UPDATE subscribers SET %s WHERE chat_id=?" % ", ".join("%s=?" % c for c in cols),
                        [fields[c] for c in cols] + [chat_id])

    def update_live_data(self, chat_id: int, data: str, at: float, widget: Optional[str] = None) -> None:
        """Дашборд исправлен (widget — заодно новая отметка формата/хэша, для текстового дашборда)."""
        if widget is None:
            self.db.execute("UPDATE subscribers SET live_data=?, live_at=? WHERE chat_id=?", (data, at, chat_id))
        else:
            self.db.execute("UPDATE subscribers SET live_data=?, live_at=?, live_widget=? WHERE chat_id=?",
                            (data, at, widget, chat_id))

    # ---------------------------------------------------------------- доступ
    def access(self, user_id: int) -> Optional[Access]:
        row = self.db.execute("SELECT user_id, status, name, username, requested_at, decided_at, owner_msg_id "
                              "FROM access WHERE user_id=?", (user_id,)).fetchone()
        return Access(*row) if row else None

    def accesses(self, *statuses: str) -> List[Access]:
        q = "SELECT user_id, status, name, username, requested_at, decided_at, owner_msg_id FROM access"
        if statuses:
            q += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
        return [Access(*r) for r in self.db.execute(q + " ORDER BY user_id", statuses)]

    def request_access(self, user_id: int, name: str, username: str, now: float) -> None:
        """Новый запрос (или повторный после того, как прошлый не дошёл до владельца)."""
        self.db.execute("INSERT INTO access(user_id, status, name, username, requested_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, name=excluded.name, "
                        "username=excluded.username, requested_at=excluded.requested_at, decided_at=NULL, "
                        "owner_msg_id=NULL", (user_id, "pending", name, username, now))

    def set_access(self, user_id: int, status: str, now: float, name: Optional[str] = None,
                   username: Optional[str] = None) -> None:
        """Решение владельца (allowed / denied / revoked). Строки может не быть (id из ALLOWED_IDS)."""
        self.db.execute("INSERT INTO access(user_id, status, name, username, decided_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, decided_at=excluded.decided_at",
                        (user_id, status, name or "", username or "", now))
        if name is not None:
            self.db.execute("UPDATE access SET name=? WHERE user_id=?", (name, user_id))
        if username is not None:
            self.db.execute("UPDATE access SET username=? WHERE user_id=?", (username, user_id))

    def set_access_msg(self, user_id: int, msg_id: Optional[int]) -> None:
        self.db.execute("UPDATE access SET owner_msg_id=? WHERE user_id=?", (msg_id, user_id))

    def requests_since(self, ts: float) -> int:
        """Сколько запросов доступа пришло после ts (для лимита в час)."""
        return self.db.execute("SELECT COUNT(*) FROM access WHERE requested_at >= ?", (ts,)).fetchone()[0]

    # ---------------------------------------------------------------- график
    def save_schedule(self, schedule_json: str, base: Dict, notices: List[Tuple[float, str, bool]]) -> None:
        """Новый график, «что уже известно владельцу» и сообщения об изменениях — одной транзакцией,
        чтобы сбой между ними не породил повтор сообщения после перезапуска."""
        self.db.execute("BEGIN")
        try:
            self.set("schedule", schedule_json)
            self.set("sched_base", base)
            for ts, text, loud in notices:
                self.db.execute("INSERT INTO notices(ts, text, loud) VALUES(?,?,?)", (ts, text, int(loud)))
            if notices:
                self.db.execute("DELETE FROM notices WHERE ts < ?", (notices[-1][0] - NOTICE_KEEP,))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def notices_after(self, notice_id: int) -> List[Notice]:
        return [Notice(i, ts, text, bool(loud)) for i, ts, text, loud in self.db.execute(
            "SELECT id, ts, text, loud FROM notices WHERE id > ? ORDER BY id", (notice_id,))]

    def last_notice_id(self) -> int:
        row = self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='notices'").fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------------------------- события
    def add_event(self, ts: int, grid: bool, since: int, prev_dur: int, soc: Optional[int]) -> None:
        self.db.execute("INSERT INTO events(ts, grid, since, prev_dur, soc) VALUES(?,?,?,?,?)",
                        (ts, int(grid), since, prev_dur, soc))

    def events(self, limit: int = 20) -> List[Tuple[int, int, int, int, Optional[int]]]:
        return list(self.db.execute("SELECT ts, grid, since, prev_dur, soc FROM events "
                                    "ORDER BY id DESC LIMIT ?", (limit,)))

    # ---------------------------------------------------------------- кривая заряда
    def load_curve(self) -> Dict[int, Tuple[float, int]]:
        return {b: (s, n) for b, s, n in self.db.execute("SELECT bucket, sec_per_pct, samples FROM charge_curve")}

    def save_curve(self, data: Dict[int, Tuple[float, int]]) -> None:
        with self.db:
            for b, (s, n) in data.items():
                self.db.execute("INSERT INTO charge_curve(bucket, sec_per_pct, samples) VALUES(?,?,?) "
                                "ON CONFLICT(bucket) DO UPDATE SET sec_per_pct=excluded.sec_per_pct, "
                                "samples=excluded.samples", (b, s, n))

    # ---------------------------------------------------------------- журнал разряда
    def add_log(self, row: LogRow) -> None:
        self.db.execute("INSERT INTO discharge_log(period, ts, kind, soc, soc_f, power, eta, cutoff) "
                        "VALUES(?,?,?,?,?,?,?,?)", (row.period, row.ts, row.kind, row.soc, row.soc_f,
                                                    row.power, row.eta, row.cutoff))

    def log_rows(self) -> List[LogRow]:
        return [LogRow(*r) for r in self.db.execute(
            "SELECT period, ts, kind, soc, soc_f, power, eta, cutoff FROM discharge_log ORDER BY period, ts, id")]
