#!/usr/bin/env python3
"""Проверка виджетов Bot API 10.4.

    python tools/check_widget.py                 # все widgets/*.html
    python tools/check_widget.py path/to/x.html  # конкретные файлы
    python tools/check_widget.py --render        # + высота всех сцен tools/build_showcase.py на 320 и 430 px

Считает как сервер Telegram: размер UTF-8 HTML (≤ 32768) и длину zlib.compress(html, 9) (≤ 4096),
проверяет отсутствие NUL и запрещённых в виджетах возможностей (сеть, внешние шрифты, vh, CSS nesting,
:has, text-shadow, clip-path, mask, blend modes, SVG clipPath/mask/pattern/filter — svitlo/widget.py).
`--render` нужен Chromium: переменная CHROME или браузер Playwright (~/Library/Caches/ms-playwright,
~/.cache/ms-playwright). Высота больше 600 px — ошибка. Код выхода 1, если хоть что-то не проходит.
"""

import glob
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svitlo.widget import MAX_COMPRESSED, MAX_HTML, check_html  # noqa: E402

WIDTHS = (320, 430)
MAX_HEIGHT = 600


def find_chrome():
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for pattern in ("~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-mac/headless_shell",
                    "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        found = sorted(glob.glob(os.path.expanduser(pattern)))
        if found:
            return found[-1]
    return None


def render_heights(width: int, chrome: str):
    """[(виджет, сцена, высота px)] для всех сцен витрины при ширине пузыря `width`."""
    sys.path.insert(0, str(ROOT / "tools"))
    import build_showcase

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(build_showcase.page(width), encoding="utf-8")
        out = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                              "--window-size=1600,6000", "--virtual-time-budget=8000", "--dump-dom",
                              page.as_uri()], capture_output=True, text=True, timeout=120).stdout
    heights = {int(i): int(h) for i, h in re.findall(r'id="h(\d+)" data-h="(\d+)"', out)}
    return [(w, label, heights.get(i)) for i, (w, label) in enumerate(build_showcase.labels())]


def main(argv) -> int:
    render = "--render" in argv
    argv = [a for a in argv if a != "--render"]
    paths = [Path(p) for p in argv] or sorted((ROOT / "widgets").glob("*.html"))
    if not paths:
        print("нет файлов: widgets/*.html пуст")
        return 1
    bad = 0
    for p in paths:
        if not p.exists():
            print("%-28s нет файла" % p)
            bad += 1
            continue
        chk = check_html(p.read_bytes(), str(p))
        mark = "OK  " if chk.ok else "FAIL"
        print("%s %-28s html %5d / %d байт · zlib-9 %4d / %d байт (запас %d)"
              % (mark, p.name, chk.size, MAX_HTML, chk.compressed, MAX_COMPRESSED, MAX_COMPRESSED - chk.compressed))
        for problem in chk.problems:
            print("     - %s" % problem)
        bad += not chk.ok
    if render:
        chrome = find_chrome()
        if chrome is None:
            print("FAIL --render: Chromium не найден (задайте CHROME=путь)")
            return 1
        for width in WIDTHS:
            print("высота при ширине %d px:" % width)
            for w, label, h in render_heights(width, chrome):
                ok = h is not None and h <= MAX_HEIGHT
                bad += not ok
                print("  %s %-14s %4s px  %s" % ("OK  " if ok else "FAIL", w, h if h is not None else "?", label))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
