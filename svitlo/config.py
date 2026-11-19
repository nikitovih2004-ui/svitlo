"""Конфигурация из окружения и файла .env (без внешних зависимостей)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent


def read_env_file(path: Path) -> Dict[str, str]:
    """Простой парсер .env: KEY=VALUE, комментарии #, необязательные кавычки."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def _id_list(value: str) -> Tuple[int, ...]:
    """Telegram id через запятую/пробел/точку с запятой — в порядке записи, без повторов."""
    result: List[int] = []
    for part in value.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part and int(part) not in result:
            result.append(int(part))
    return tuple(result)


def _ids(value: str) -> FrozenSet[int]:
    return frozenset(_id_list(value))


@dataclass(frozen=True)
class Config:
    bot_token: str = ""
    api_base: str = "https://api.telegram.org"
    api_test: bool = False
    logger_host: str = ""            # IP логгера Solarman в локальной сети (LOGGER_HOST)
    logger_port: int = 8899
    logger_serial: int = 0           # серийный номер логгера (LOGGER_SERIAL)
    logger_slave: int = 1
    group: str = ""                   # черга YASNO, например 3.1 (GROUP)
    yasno_region: int = 25
    yasno_dso: int = 902
    widget: str = "a"
    widget_path: Optional[str] = None
    widget_width: int = 0
    widget_height: int = 0
    widget_queries: bool = False
    inverter_path: Optional[str] = None   # HTML виджета «Инвертор»; по умолчанию widgets/inverter.html
    inverter_minutes: float = 15.0        # сколько минут бот обновляет виджет «Инвертор» после нажатия
    inverter_interval: float = 30.0       # правки виджета «Инвертор» не чаще, с
    widget_probe_minutes: float = 30.0    # текстовый режим: как часто пробовать, не разрешил ли Telegram виджеты
    allowed_ids: FrozenSet[int] = field(default_factory=frozenset)  # изначальный список доступа (.env)
    owner_id: int = 0                 # кто подтверждает запросы доступа; 0 — OWNER_ID или первый из ALLOWED_IDS
    access_per_hour: int = 3          # новых запросов доступа в час (сверх — тихо игнорируются)
    battery_kwh: float = 0.0          # паспортная энергия батареи, кВт·ч; 0 — не задана (прогноза разряда нет)
    data_dir: str = str(ROOT / "data")
    tz: str = "Europe/Kyiv"
    poll_interval: float = 10.0
    schedule_interval: float = 300.0
    live_interval: float = 60.0       # правки дашборда не чаще, с
    fresh_interval: float = 120.0     # и не реже (пока связь есть), чтобы `t` оставался свежим, с
    renew_hours: float = 47.0         # пересоздать дашборд, если он старше (deleteMessage работает до 48 ч); 0 — нет
    grid_on_v: float = 170.0
    stale_after: float = 60.0
    log_level: str = "INFO"

    @property
    def owner(self) -> int:
        """Владелец бота: OWNER_ID, иначе первый id из ALLOWED_IDS (load_config кладёт его в owner_id);
        если Config собран вручную с одним id — он. 0 — владельца нет (запросы доступа не принимаются)."""
        if self.owner_id:
            return self.owner_id
        return next(iter(self.allowed_ids)) if len(self.allowed_ids) == 1 else 0

    @property
    def widget_file(self) -> Path:
        if self.widget_path:
            return Path(self.widget_path)
        return ROOT / "widgets" / ("%s.html" % self.widget)

    @property
    def inverter_file(self) -> Path:
        return Path(self.inverter_path) if self.inverter_path else ROOT / "widgets" / "inverter.html"

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "svitlo.sqlite3"


def load_config(env_file: Optional[Path] = None, environ: Optional[Dict[str, str]] = None) -> Config:
    env: Dict[str, str] = {}
    env.update(read_env_file(env_file or (ROOT / ".env")))
    env.update(environ if environ is not None else os.environ)

    def get(key: str, default: str = "") -> str:
        return env.get(key, default).strip()

    defaults = Config()
    first = _id_list(get("ALLOWED_IDS"))
    owner = int(get("OWNER_ID") or 0) or (first[0] if first else 0)
    if get("BATTERY_NOMINAL_V"):
        import logging
        logging.getLogger(__name__).warning("BATTERY_NOMINAL_V больше не используется — задайте BATTERY_KWH")
    widget = get("WIDGET", defaults.widget).lower()
    if widget not in ("a", "b"):
        raise ValueError("WIDGET должен быть a или b, получено %r" % widget)
    return Config(
        bot_token=get("BOT_TOKEN"),
        api_base=get("API_BASE", defaults.api_base).rstrip("/"),
        api_test=get("API_TEST", "0") in ("1", "true", "yes"),
        logger_host=get("LOGGER_HOST", defaults.logger_host),
        logger_port=int(get("LOGGER_PORT", str(defaults.logger_port))),
        logger_serial=int(get("LOGGER_SERIAL") or defaults.logger_serial),
        logger_slave=int(get("LOGGER_SLAVE", str(defaults.logger_slave))),
        group=get("GROUP", defaults.group),
        yasno_region=int(get("YASNO_REGION", str(defaults.yasno_region))),
        yasno_dso=int(get("YASNO_DSO", str(defaults.yasno_dso))),
        widget=widget,
        widget_path=get("WIDGET_PATH") or None,
        widget_width=int(get("WIDGET_WIDTH", "0")),
        widget_height=int(get("WIDGET_HEIGHT", "0")),
        widget_queries=get("WIDGET_QUERIES", "0") in ("1", "true", "yes"),
        inverter_path=get("INVERTER_PATH") or None,
        inverter_minutes=float(get("INVERTER_MINUTES", str(defaults.inverter_minutes))),
        inverter_interval=float(get("INVERTER_INTERVAL", str(defaults.inverter_interval))),
        widget_probe_minutes=float(get("WIDGET_PROBE_MINUTES", str(defaults.widget_probe_minutes))),
        allowed_ids=_ids(get("ALLOWED_IDS")) | ({owner} if owner else set()),
        owner_id=owner,
        access_per_hour=int(get("ACCESS_PER_HOUR", str(defaults.access_per_hour))),
        battery_kwh=float(get("BATTERY_KWH", "0") or 0),
        data_dir=get("DATA_DIR", defaults.data_dir),
        tz=get("TZ_NAME", defaults.tz),
        poll_interval=float(get("POLL_INTERVAL", str(defaults.poll_interval))),
        schedule_interval=float(get("SCHEDULE_INTERVAL", str(defaults.schedule_interval))),
        live_interval=float(get("LIVE_INTERVAL", str(defaults.live_interval))),
        fresh_interval=float(get("FRESH_INTERVAL", str(defaults.fresh_interval))),
        renew_hours=float(get("DASHBOARD_RENEW_HOURS", str(defaults.renew_hours))),
        grid_on_v=float(get("GRID_ON_V", str(defaults.grid_on_v))),
        log_level=get("LOG_LEVEL", defaults.log_level).upper(),
    )
