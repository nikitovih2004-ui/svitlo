import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from svitlo.widget import WidgetError, check_html, load_widget
from tests.conftest import STUB_WIDGET

ROOT = Path(__file__).resolve().parent.parent


def test_stub_ok():
    chk = check_html(STUB_WIDGET.read_bytes())
    assert chk.ok and chk.compressed == len(zlib.compress(STUB_WIDGET.read_bytes(), 9))
    assert "TelegramWidget" in load_widget(STUB_WIDGET)


def test_limits():
    big = os.urandom(5000).hex().encode()          # плохо сжимается
    chk = check_html(big)
    assert not chk.ok and any("zlib" in p for p in chk.problems)
    huge = b"a" * 40000                              # сжимается, но длинный
    assert any("32768" in p for p in check_html(huge).problems)
    assert any("NUL" in p for p in check_html(b"<p>\x00</p>").problems)


def test_load_missing(tmp_path):
    with pytest.raises(WidgetError):
        load_widget(tmp_path / "nope.html")


def test_check_tool(tmp_path):
    bad = tmp_path / "bad.html"
    bad.write_bytes(os.urandom(6000).hex().encode())
    tool = ROOT / "tools" / "check_widget.py"
    ok = subprocess.run([sys.executable, str(tool), str(STUB_WIDGET)], capture_output=True, text=True)
    assert ok.returncode == 0 and "OK" in ok.stdout
    fail = subprocess.run([sys.executable, str(tool), str(STUB_WIDGET), str(bad)], capture_output=True, text=True)
    assert fail.returncode == 1 and "FAIL" in fail.stdout


def test_real_widgets_pass_limits_and_lint():
    from svitlo.widget import lint_html

    for name in ("a.html", "inverter.html"):
        data = (ROOT / "widgets" / name).read_bytes()
        chk = check_html(data, name)
        assert chk.ok, chk.problems
    a = (ROOT / "widgets" / "a.html").read_text()
    assert "аварийные отключения, график не действует" in a and "d.m>>" in a   # ключ `m`
    assert "хватит на" in a                                                   # строка «хватит на …» (h/e)


def test_lint_catches_forbidden():
    from svitlo.widget import lint_html

    bad = ('<style>a{height:100vh} b{text-shadow:0 0 1px} .x{&:hover{}} c{filter:blur(1px)} d{mask:none}'
           '@import "x.css";</style><svg><clipPath id=c/></svg><script>fetch("/x")</script>')
    found = " ".join(lint_html(bad))
    for name in ("vh", "text-shadow", "nesting", "filter", "mask", "внешние ресурсы", "clipPath", "сетевые"):
        assert name in found, name
    ok = '<style>a{text-transform:uppercase}@media (min-width:400px){b{c:d}}</style><script>[1].filter(x=>x&&1)</script>'
    assert lint_html(ok) == []
