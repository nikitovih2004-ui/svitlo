#!/usr/bin/env python3
"""Витрина виджетов в макете чата Telegram → prototypes/showcase.html.

    python tools/build_showcase.py                 # ширина пузыря 360 px
    python tools/build_showcase.py --width 320     # другая ширина
    python tools/build_showcase.py --width 430 out.html

Дашборд (`widgets/a.html`, «Табло») — в состояниях по docs/contract.md: событие (обычное сообщение) и под ним
беззвучный дашборд. Виджет «Инвертор» (`widgets/inverter.html`) — отдельный ряд. Виджеты вставляются как есть
(те же файлы, что бот шлёт в Telegram) в iframe с заглушкой window.TelegramWidget и зафиксированным «сейчас»;
под каждым — высота в px (красным, если больше 600). Тексты событий и подписи — из svitlo/texts.py.
`tools/check_widget.py --render` строит эту же страницу и меряет высоты в headless Chromium.
"""
import json
import pathlib
import sys
import zlib
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svitlo import texts  # noqa: E402

KYIV = ZoneInfo("Europe/Kyiv")
D = 1794434400  # 12.11.2026 00:00 Europe/Kyiv, четверг
H = 3600
TODAY = [[0, 60], [540, 960], [1140, 1440]]                    # выдуманный график: 00:00–01:00, 09:00–16:00, 19:00–24:00
TOMORROW = [[1500, 1740], [1980, 2340], [2580, 2880]]          # +1440: 01:00–05:00, 09:00–15:00, 19:00–24:00
BUSY = [[0, 90], [240, 420], [600, 780], [960, 1140], [1260, 1440],
        [1500, 1620], [1740, 1920], [2040, 2220], [2400, 2580], [2700, 2880]]  # 5 + 5 отключений
LIMIT_H = 600


def at(h, m=0):
    return D + h * H + m * 60


def dash(label, now, event, g, s, p, c, b, l, u, e, h, o, w, m=0, ago=30):
    st = {"r": 1, "g": g, "s": s, "p": p, "c": c, "b": b, "l": l, "u": u, "e": e, "h": h,
          "o": o, "d": D, "w": w, "m": m, "t": now - ago}
    assert len(json.dumps(st, separators=(",", ":")).encode()) <= 1024
    return {"label": label, "now": now, "state": st, "event": event, "event_time": texts.hm(s, KYIV)}


def inv(label, now, ago=12, **kw):
    st = dict(r=1, t=now - ago, d=D, **kw)
    assert len(json.dumps(st, separators=(",", ":")).encode()) <= 1024
    return {"label": label, "now": now, "state": st}


OFF = texts.event_text(False, at(12, 46), 33471, KYIV)
ON = texts.event_text(True, at(16, 31), 13500, KYIV)
DASH = [
    dash("16:50 · свет есть, завтра известно", at(16, 50), ON, 1, at(16, 31), 13500, 74, -1850, 610, 228,
         at(18, 10), 74000, TODAY + TOMORROW, 1),
    dash("13:42 · света нет по графику", at(13, 42), OFF, 0, at(12, 46), 33471, 92, 430, 410, 0,
         at(13, 42) + 69000, 69000, TODAY, 0),
    dash("13:42 · свет есть во время планового отключения", at(13, 42), OFF, 1, at(3, 28), 4681, 98, 0, 520, 229,
         0, 95000, TODAY, 0),
    dash("14:10 · нет связи с инвертором (9 мин)", at(14, 10), OFF, 0, at(12, 46), 33471, 90, 402, 381, 0,
         at(14, 1) + 69000, 69000, TODAY, 0, ago=540),
    dash("11:20 · аварийные отключения, свет есть", at(11, 20), ON, 1, at(3, 28), 4681, 99, 0, 450, 231,
         0, 90000, [], 0, m=1),
    dash("11:20 · аварийные отключения, света нет", at(11, 20), OFF, 0, at(10, 52), 26640, 96, 520, 505, 0,
         at(11, 20) + 98000, 98000, [], 0, m=1),
    dash("03:10 · вне графика, батарея 14 %", at(3, 10), OFF, 0, at(0, 40), 9000, 14, 520, 505, 0,
         at(3, 10) + 2700, 2700, TODAY, 0),
    dash("21:00 · 5 отключений сегодня и 5 завтра", at(21), ON, 1, at(19), 5400, 60, -2500, 2900, 229,
         at(23, 10), 52000, BUSY, 1),
    dash("23:30 · график на завтра не опубликован", at(23, 30), ON, 1, at(22, 3), 25260, 96, -380, 450, 231,
         at(23, 55), 76000, TODAY, 0),
]
INV = [
    inv("16:50 · сеть есть, идёт заряд", at(16, 50), g=1, u=229.4, f=50.01, n=2470, l=610, c=78, b=-1840, a=-34.7,
        v=53.1, k=23.5, x=10, h=74000, e=at(18, 10), i=41.2, q=[6.2, 4.9, 14.3, 9.6], z=at(17, 5)),
    inv("13:42 · сети нет, разряд", at(13, 42), g=0, u=0, n=0, l=180, c=98, b=180, a=3.4, v=53.3, k=21.7, x=10,
        h=170000, e=at(13, 42) + 170000, i=40.6, q=[1.9, 1.3, 7.7, 7.1], z=at(13, 55)),
    inv("13:50 · данные устарели (9 мин), окно ещё идёт", at(13, 50), ago=540, g=0, u=0, n=0, l=181, c=98, b=182,
        a=3.4, v=53.3, k=21.7, x=10, h=170000, e=at(13, 41) + 170000, z=at(13, 57)),
    inv("14:20 · окно обновления прошло — снимок", at(14, 20), ago=1500, z=at(13, 57), g=0, u=0, n=0, l=181,
        c=97, b=182, a=3.4, v=53.3, k=21.7, x=10, h=168000, e=at(13, 55) + 168000, i=40.5, q=[1.9, 1.4, 7.7, 7.2]),
]


FRAME_JS = r"""
let id=0;
const frame=(html,state,now)=>{const i=id++;
 const doc='<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><script>Date.now=()=>'+now*1000+
 ';window.TelegramWidget={chatData:'+JSON.stringify(JSON.stringify(state))+',onEvent(){}}<\/script>'+html+
 '<script>const s=()=>parent.postMessage({i:'+i+',h:document.body.offsetHeight},"*");s();setTimeout(s,200)<\/script>';
 return '<iframe id="f'+i+'" srcdoc="'+doc.replace(/&/g,'&amp;').replace(/"/g,'&quot;')+'"></iframe>'};
addEventListener('message',e=>{const f=document.getElementById('f'+e.data.i);if(!f)return;f.style.height=e.data.h+'px';
 const h=document.getElementById('h'+e.data.i);h.textContent=e.data.h+' px';h.dataset.h=e.data.h;if(e.data.h>__LIMIT__)h.className='hh bad'});
"""

PAGE = """<!doctype html><meta charset="utf-8"><title>Виджеты «Свет»</title>
<style>
body{margin:0;padding:20px;background:#e9edf2;color:#1b1f24;font:14px/1.4 system-ui,sans-serif}
h2{font-size:17px;margin:0 0 4px}
.why{color:#4b5563;font-size:13px;margin:0 0 14px;max-width:900px}
.row{display:flex;flex-wrap:wrap;gap:18px;margin:0 0 28px}
.chat{width:__W__px}
.lab{font:12px ui-monospace,monospace;color:#4b5563;margin:0 0 6px}
.bub{border-radius:16px;overflow:hidden;margin:0 0 4px;background:#fff}
.bub iframe{display:block;width:100%;border:0;height:60px;background:#0d1015}
.cap,.txt{display:flex;justify-content:space-between;gap:10px;padding:7px 12px 8px;font-size:14px}
.cap span,.txt span{color:#8a93a0;font-size:11px;align-self:flex-end;white-space:nowrap}
.hh{font:11px ui-monospace,monospace;color:#8a93a0}
.hh.bad{color:#d00;font-weight:bold}
</style>
<div id="root"></div>
<script>
const DATA=__DATA__;__FRAME__
const W=DATA.w;
let h='<h2>Дашборд · '+W.a.name+' · '+W.a.size+' Б, zlib '+W.a.z+' / 4096 · ширина '+DATA.width+' px</h2>'+
 '<p class="why">Сверху — событие (обычное сообщение со звуком), под ним — беззвучный дашборд, который бот правит раз в 1–2 минуты.</p>'+
 '<div class="row" id="dash">'+DATA.dash.map(s=>'<div class="chat"><div class="lab">'+s.label+'</div>'+
 '<div class="bub"><div class="txt">'+s.event+'<span>'+s.event_time+'</span></div></div>'+
 '<div class="bub">'+frame(W.a.html,s.state,s.now)+'<div class="cap">'+DATA.caption+'<span>'+s.event_time+'</span></div></div>'+
 '<div class="hh" id="h'+(id-1)+'"></div></div>').join('')+'</div>';
h+='<h2>Инвертор · '+W.inv.name+' · '+W.inv.size+' Б, zlib '+W.inv.z+' / 4096</h2>'+
 '<p class="why">Отдельное сообщение по кнопке «Инвертор»: бот правит его 15 минут (ключ z), дальше это снимок.</p>'+
 '<div class="row" id="inv">'+DATA.inv.map(s=>'<div class="chat"><div class="lab">'+s.label+'</div>'+
 '<div class="bub">'+frame(W.inv.html,s.state,s.now)+'<div class="cap">'+DATA.inv_caption+'<span></span></div></div>'+
 '<div class="hh" id="h'+(id-1)+'"></div></div>').join('')+'</div>';
document.getElementById('root').innerHTML=h;
</script>
"""


def widget(name):
    raw = (ROOT / "widgets" / name).read_bytes()
    z = len(zlib.compress(raw, 9))
    assert len(raw) <= 32768 and z <= 4096, "%s не влезает в лимит Telegram" % name
    return {"name": name, "html": raw.decode(), "size": len(raw), "z": z}


def page(width: int = 360) -> str:
    data = json.dumps({"w": {"a": widget("a.html"), "inv": widget("inverter.html")}, "dash": DASH, "inv": INV,
                       "width": width, "caption": texts.DASHBOARD_CAPTION, "inv_caption": texts.INVERTER_CAPTION},
                      ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__W__", str(width)).replace("__FRAME__", FRAME_JS.replace("__LIMIT__", str(LIMIT_H)))
            .replace("__DATA__", data))


def labels():
    """Подписи сцен в порядке iframe на странице: (виджет, подпись)."""
    return [("a.html", s["label"]) for s in DASH] + [("inverter.html", s["label"]) for s in INV]


def main(argv) -> int:
    width = 360
    if argv[:1] == ["--width"]:
        width, argv = int(argv[1]), argv[2:]
    out = pathlib.Path(argv[0]) if argv else ROOT / "prototypes" / "showcase.html"
    out.parent.mkdir(exist_ok=True)
    html = page(width)
    out.write_text(html, encoding="utf-8")
    print("→", out, len(html.encode("utf-8")), "байт")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
