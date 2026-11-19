"""Запуск: python -m svitlo [bot|probe|demo|accuracy]"""

import argparse
import asyncio
import logging
import sys
from zoneinfo import ZoneInfo

from .config import load_config


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    # httpx на INFO пишет URL запроса, а в URL Bot API — токен
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def check_inverter(cfg) -> None:
    missing = [k for k, v in (("LOGGER_HOST", cfg.logger_host), ("LOGGER_SERIAL", cfg.logger_serial),
                              ("GROUP", cfg.group)) if not v]
    if missing:
        sys.exit("Не задано в .env: %s" % ", ".join(missing))


async def run_bot(cfg) -> None:
    import httpx

    from .bot import DAY_EVERY, Bot
    from .schedule import ScheduleFetcher
    from .source import SolarmanSource
    from .state import Engine
    from .storage import Store
    from .telegram import Telegram
    from .widget import load_widget

    if not cfg.bot_token:
        sys.exit("BOT_TOKEN не задан (.env)")
    check_inverter(cfg)
    if not cfg.battery_kwh:
        logging.warning("BATTERY_KWH не задан — прогноза не будет (e=0)")
    if not cfg.allowed_ids:
        logging.warning("ALLOWED_IDS пуст — бот всем ответит отказом с их id")
    html = load_widget(cfg.widget_file)
    inverter_html = load_widget(cfg.inverter_file)
    tz = ZoneInfo(cfg.tz)
    store = Store(cfg.db_path)
    engine = Engine(store, tz, cfg.grid_on_v, cfg.battery_kwh, cfg.stale_after)
    source = SolarmanSource(cfg.logger_host, cfg.logger_serial, cfg.logger_port, cfg.logger_slave,
                            day_every=DAY_EVERY)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as http:
        tg = Telegram(cfg.bot_token, cfg.api_base, client=http, test_env=cfg.api_test)
        fetcher = ScheduleFetcher(http, cfg.group, tz, cfg.yasno_region, cfg.yasno_dso)
        bot = Bot(cfg, store, engine, source, tg, html, fetcher.fetch, tz, inverter_html=inverter_html)
        await bot.run()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m svitlo")
    ap.add_argument("mode", nargs="?", default="bot", choices=["bot", "probe", "demo", "accuracy"])
    ap.add_argument("--no-schedule", action="store_true", help="probe: не ходить за графиком")
    ap.add_argument("--current", action="store_true", help="accuracy: включить идущий сейчас период без сети")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.mode == "probe":
        setup_logging("WARNING")
        from .probe import probe
        check_inverter(cfg)
        sys.exit(probe(cfg, with_schedule=not args.no_schedule))
    if args.mode == "accuracy":
        from .accuracy import main as accuracy
        sys.exit(accuracy(cfg, include_current=args.current))
    if args.mode == "demo":
        setup_logging("WARNING")
        from .demo import demo
        asyncio.run(demo(cfg))
        return
    setup_logging(cfg.log_level)
    try:
        asyncio.run(run_bot(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
