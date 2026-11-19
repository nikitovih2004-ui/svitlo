#!/usr/bin/env python3
"""README screenshots → docs/screenshots/*.png.

    python tools/screenshots.py            # needs headless Chromium (CHROME=path or a Playwright browser)

Every scene is drawn from the real code: widget HTML from widgets/, states from tools/build_showcase.py,
message texts from svitlo/texts.py. The chat frame around them is a mock-up of Telegram's dark theme.
Data is demo data (12.11.2026), not anyone's real inverter.
"""
import datetime
import json
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build_showcase as sc  # noqa: E402
from check_widget import find_chrome  # noqa: E402
from svitlo import texts  # noqa: E402

OUT = ROOT / "docs" / "screenshots"
KYIV = sc.KYIV
DAY = datetime.date(2026, 11, 12)
TOMORROW = datetime.date(2026, 11, 13)
WIDGET = (ROOT / "widgets" / "a.html").read_text(encoding="utf-8")
INVERTER = (ROOT / "widgets" / "inverter.html").read_text(encoding="utf-8")

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:28px;background:#0b0f14;font:15px/1.38 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 color:#f1f4f7;display:inline-block}
.row{display:flex;gap:28px;align-items:flex-start}
.col{width:400px}
.cap{font-size:15px;font-weight:600;color:#c9d3dd;margin:0 0 10px 4px}
.phone{background:#0e1621;border-radius:22px;overflow:hidden;border:1px solid #1d2733}
.top{display:flex;align-items:center;gap:11px;padding:11px 14px;background:#17212b;border-bottom:1px solid #0b1118}
.ava{width:38px;height:38px;border-radius:50%;background:linear-gradient(135deg,#f6c445,#ef8b2c);display:flex;
 align-items:center;justify-content:center;font-size:20px}
.nm{font-weight:600;font-size:15px}.sub{font-size:13px;color:#6d8296}
.chat{padding:12px 10px 10px;display:flex;flex-direction:column;gap:6px;min-height:120px}
.m{max-width:94%;background:#182533;border-radius:14px 14px 14px 4px;padding:7px 11px 6px;white-space:pre-wrap;
 word-wrap:break-word;align-self:flex-start}
.m.me{align-self:flex-end;background:#2b5278;border-radius:14px 14px 4px 14px}
.m .t{float:right;font-size:11.5px;color:#6d8296;margin:6px 0 -4px 10px}
.m.me .t{color:#7da8d3}
.m s{color:#8a99a8}
.w{align-self:flex-start;width:360px;background:#182533;border-radius:14px 14px 14px 4px;overflow:hidden}
.w iframe{display:block;width:360px;border:0;height:80px;background:#0d1015}
.w .c{display:flex;justify-content:space-between;padding:6px 11px 7px}
.w .c span{font-size:11.5px;color:#6d8296;align-self:flex-end}
.btns{display:flex;gap:5px;margin-top:-1px;align-self:stretch;max-width:94%}
.btns div{flex:1;text-align:center;background:#1e2c3a;border-radius:9px;padding:8px 6px;font-size:14px;color:#e6edf3}
.kb{display:flex;gap:6px;padding:8px;background:#17212b;border-top:1px solid #0b1118}
.kb div{flex:1;text-align:center;background:#232e3c;border-radius:9px;padding:10px 4px;font-size:14px}
.day{align-self:center;font-size:12.5px;color:#c3ccd6;background:rgba(30,44,58,.85);border-radius:10px;padding:2px 10px;margin:2px 0}
.note{font-size:13px;color:#7d8b99;margin:10px 4px 0;max-width:400px}
"""

FRAME = r"""
let fid=0;
function frame(html,state,now){const i=fid++;
 const doc='<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width"><script>Date.now=()=>'+now*1000+
 ';window.TelegramWidget={chatData:'+JSON.stringify(JSON.stringify(state))+',onEvent(){}}<\/script>'+html+
 '<script>const s=()=>parent.postMessage({i:'+i+',h:document.body.offsetHeight},"*");s();setTimeout(s,200)<\/script>';
 return '<iframe id="f'+i+'" srcdoc="'+doc.replace(/&/g,'&amp;').replace(/"/g,'&quot;')+'"></iframe>'}
addEventListener('message',e=>{const f=document.getElementById('f'+e.data.i);if(f)f.style.height=e.data.h+'px'});
for(const el of document.querySelectorAll('[data-frame]')){const d=JSON.parse(el.dataset.frame);
 el.insertAdjacentHTML('afterbegin',frame(WIDGETS[d.w],d.state,d.now))}
setTimeout(()=>{document.title='H'+document.body.getBoundingClientRect().height+'W'+document.body.getBoundingClientRect().width},1500);
"""


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def msg(html, time, me=False):
    return '<div class="m%s">%s<span class="t">%s</span></div>' % (" me" if me else "", html, time)


def widget(kind, state, now, caption, time):
    data = esc(json.dumps({"w": kind, "state": state, "now": now}))
    return '<div class="w" data-frame="%s"><div class="c">%s<span>%s</span></div></div>' % (data, caption, time)


def buttons(*labels):
    return '<div class="btns">%s</div>' % "".join("<div>%s</div>" % b for b in labels)


def day(label):
    return '<div class="day">%s</div>' % label


def phone(caption, items, keyboard=True, note=""):
    kb = ""
    if keyboard:
        kb = '<div class="kb">%s</div>' % "".join(
            "<div>%s</div>" % b for b in (texts.BTN_DASHBOARD, texts.BTN_SCHEDULE, texts.BTN_INVERTER))
    return ('<div class="col"><div class="cap">%s</div><div class="phone"><div class="top"><div class="ava">💡</div>'
            '<div><div class="nm">Свет</div><div class="sub">бот</div></div></div><div class="chat">%s</div>%s</div>%s</div>'
            % (caption, "".join(items), kb, '<div class="note">%s</div>' % note if note else ""))


def page(cols):
    widgets = json.dumps({"a": WIDGET, "inv": INVERTER}).replace("</", "<\\/")
    return ('<!doctype html><meta charset="utf-8"><style>%s</style><div class="row">%s</div>'
            '<script>const WIDGETS=%s;%s</script>' % (CSS, "".join(cols), widgets, FRAME))


def hm(ts):
    return texts.hm(ts, KYIV)


def scenes():
    on, off, nolink, emerg, low = sc.DASH[0], sc.DASH[1], sc.DASH[3], sc.DASH[4], sc.DASH[6]
    inv = sc.INV[0]
    out = {}

    out["widgets"] = page([
        phone("Power is back", [
            day("12 ноября"), msg(on["event"], on["event_time"]),
            widget("a", on["state"], on["now"], texts.DASHBOARD_CAPTION, on["event_time"])]),
        phone("Outage by schedule", [
            day("12 ноября"), msg(off["event"], off["event_time"]),
            widget("a", off["state"], off["now"], texts.DASHBOARD_CAPTION, off["event_time"])]),
        phone("Inverter button", [
            day("12 ноября"), msg(texts.BTN_INVERTER, hm(inv["now"]), me=True),
            widget("inv", inv["state"], inv["now"], texts.INVERTER_CAPTION, hm(inv["now"]))]),
    ])

    out["widget-states"] = page([
        phone("Inverter unreachable", [widget("a", nolink["state"], nolink["now"], texts.DASHBOARD_CAPTION,
                                              hm(nolink["now"]))], keyboard=False),
        phone("Emergency outages", [widget("a", emerg["state"], emerg["now"], texts.DASHBOARD_CAPTION,
                                           hm(emerg["now"]))], keyboard=False),
        phone("Off-schedule outage, battery 14%", [widget("a", low["state"], low["now"], texts.DASHBOARD_CAPTION,
                                                          hm(low["now"]))], keyboard=False),
    ])

    s = off["state"]
    today = [(a, b) for a, b in sc.TODAY]
    tomorrow = [(a - 1440, b - 1440) for a, b in sc.TOMORROW]
    changed = [(0, 60), (540, 900), (1140, 1440)]
    out["text-mode"] = page([
        phone("Event + live dashboard", [
            day("12 ноября"), msg(off["event"], off["event_time"]),
            msg(texts.dashboard_text(s, off["now"], KYIV), off["event_time"])]),
        phone("Schedule button and changes", [
            day("12 ноября"), msg(texts.BTN_SCHEDULE, "13:40", me=True),
            msg(texts.current_schedule([(DAY, False, "applies", today), (TOMORROW, True, "applies", tomorrow)]),
                "13:40"),
            msg(texts.schedule_message(DAY, changed, False, old=today), "14:05")]),
        phone("Inverter button", [
            day("12 ноября"), msg(texts.BTN_INVERTER, hm(inv["now"]), me=True),
            msg(texts.inverter_text(inv["state"], inv["now"], KYIV), hm(inv["now"]))]),
    ])

    out["access"] = page([
        phone("Someone opens the bot link", [
            msg("/start", "18:02", me=True), msg(texts.ACCESS_REQUESTED, "18:02"),
            msg(texts.ACCESS_GRANTED, "18:04")]),
        phone("The owner gets a request", [
            msg(texts.access_request("Мама", "", 0), "18:02"), buttons(texts.BTN_ALLOW, texts.BTN_REJECT),
            msg("/users", "18:05", me=True),
            msg(texts.users_list([("Олег", "", 0, True), ("Мама", "", 0, False)], []), "18:05"),
            buttons(texts.BTN_REMOVE + " · Мама")],
            note="After a tap on «Разрешить» the request turns into «%s»." % texts.access_granted("Мама")),
    ])
    return out


def render(chrome, html, png):
    with tempfile.TemporaryDirectory() as tmp:
        f = pathlib.Path(tmp) / "page.html"
        f.write_text(html, encoding="utf-8")
        base = [chrome, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                "--virtual-time-budget=5000"]
        dom = subprocess.run(base + ["--window-size=1600,4000", "--dump-dom", f.as_uri()],
                             capture_output=True, text=True, timeout=120).stdout
        m = re.search(r"<title>H([\d.]+)W([\d.]+)</title>", dom)
        if not m:
            raise SystemExit("could not measure %s" % png.name)
        h, w = (int(float(x)) + 1 for x in m.groups())
        subprocess.run(base + ["--force-device-scale-factor=2", "--window-size=%d,%d" % (w, h),
                               "--screenshot=%s" % png, f.as_uri()], capture_output=True, timeout=120, check=True)
    print("→", png.relative_to(ROOT), "%dx%d css px" % (w, h))


def main() -> int:
    chrome = find_chrome()
    if chrome is None:
        print("headless Chromium not found (set CHROME=path)")
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    for name, html in scenes().items():
        render(chrome, html, OUT / ("%s.png" % name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
