"""Загрузка HTML виджета и проверка лимитов Bot API.

Лимиты (core.telegram.org/bots/widgets → Limits / Counting the size):
UTF-8 HTML ≤ 32768 байт, после zlib level 9 ≤ 4096 байт (Python zlib считает так же,
как сервер), без символа NUL.
"""

import re
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import List

MAX_HTML = 32768
MAX_COMPRESSED = 4096

# Что виджет Telegram не умеет или запрещает (core.telegram.org/bots/widgets): сеть, внешние шрифты, vh,
# CSS nesting, :has, text-shadow, clip-path, mask, blend modes, SVG clipPath/mask/pattern/filter.
CSS_FORBIDDEN = (
    (r"\d(?:\.\d+)?vh\b", "единицы vh"),
    (r":has\(", ":has()"),
    (r"text-shadow", "text-shadow"),
    (r"clip-path", "clip-path"),
    (r"(?<![\w-])mask(?:-[a-z-]+)?\s*:", "mask"),
    (r"blend-mode", "blend modes"),
    (r"(?<![\w-])filter\s*:", "CSS filter"),
    (r"@import|@font-face|url\(", "внешние ресурсы/шрифты в CSS"),
)
NESTING = r"&\s*[:.#\[>+~ a-z]|\{[^{}]*\{"  # только в <style>: инлайн-стили собираются в JS, там бывает &&
DOC_FORBIDDEN = (
    (r"<(?:clipPath|mask|pattern|filter)\b", "SVG clipPath/mask/pattern/filter"),
    (r"<link\b|<iframe\b|\bsrc\s*=\s*[\"']?(?:https?:)?//", "внешние ресурсы"),
    (r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource", "сетевые запросы"),
)


def lint_html(text: str):
    """Запрещённые в виджетах возможности (список строк; пусто — всё хорошо)."""
    problems = []
    sheet = " ".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S | re.I))
    css = sheet + " " + " ".join(re.findall(r"style=\"([^\"]*)\"", text))
    if re.search(NESTING, re.sub(r"@media[^{]*\{", "", sheet)):
        problems.append("в CSS: CSS nesting")
    for pattern, name in CSS_FORBIDDEN:
        if re.search(pattern, css, re.I):
            problems.append("в CSS: %s" % name)
    for pattern, name in DOC_FORBIDDEN:
        if re.search(pattern, text):
            problems.append(name)
    return problems


@dataclass
class WidgetCheck:
    path: str
    size: int
    compressed: int
    problems: List[str]

    @property
    def ok(self) -> bool:
        return not self.problems


def check_html(data: bytes, path: str = "<html>") -> WidgetCheck:
    problems = []
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        problems.append("не UTF-8: %s" % exc)
    if b"\x00" in data:
        problems.append("содержит NUL (U+0000) — сервер ответит INPUT_MEDIA_WIDGET_HTML_INVALID")
    if not data.strip():
        problems.append("пустой файл")
    problems.extend(lint_html(data.decode("utf-8", "replace")))
    size = len(data)
    compressed = len(zlib.compress(data, 9))
    if size > MAX_HTML:
        problems.append("HTML %d байт > %d" % (size, MAX_HTML))
    if compressed > MAX_COMPRESSED:
        problems.append("после zlib -9 %d байт > %d" % (compressed, MAX_COMPRESSED))
    return WidgetCheck(path, size, compressed, problems)


class WidgetError(Exception):
    pass


def load_widget(path: Path) -> str:
    if not path.exists():
        raise WidgetError("нет файла виджета %s" % path)
    data = path.read_bytes()
    chk = check_html(data, str(path))
    if not chk.ok:
        raise WidgetError("%s: %s" % (path, "; ".join(chk.problems)))
    return data.decode("utf-8")
