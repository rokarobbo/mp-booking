"""Booking helper: runs on YOUR PC in a visible browser.

It finds the best available option for your dates/routes (or uses the one you give),
fills circuit, route, date and entry time, then hands over to you for the CAPTCHA.
You stay in control of the CAPTCHA, the terms and the payment.

Usage:
  python book.py                                   # pick best available option from config.json
  python book.py --route "Circuito 2:2-B" --date 2027-01-03 --time 09:00
  python book.py --record --route "Circuito 1:1-A" --date 2026-12-10 --time 09:00
        # one-time: after you pass the CAPTCHA and click "Iniciar compra", saves the next
        # page's layout (no personal data) so the visitor-details step can be automated.
  python book.py --check --route "Circuito 1:1-A" --date 2026-12-10 --time 09:00
        # headless self-test: fill the form, screenshot, exit (no CAPTCHA, nothing submitted)
"""
import argparse, datetime as dt, json, pathlib, re, sys
from playwright.sync_api import sync_playwright
import watcher as w

HERE = pathlib.Path(__file__).parent
CAPTURES = HERE / "captures"


def minutes(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def choose(cfg, found, routes):
    """Route priority first, then closeness to the middle of the preferred window, then date."""
    win = cfg["preferred_window"]
    target = (minutes(win[0]) + minutes(win[1])) // 2
    order = {f"{r['circuit']}:{r['route']}": i for i, r in enumerate(routes)}
    cands = []
    for f in found:
        ridx = next((i for k, i in order.items() if k.split(":")[1] in f["route"]), 99)
        for s in f["slots"]:
            cands.append((ridx, abs(minutes(s["time"]) - target), f["date"], f, s))
    if not cands:
        return None
    cands.sort(key=lambda c: c[:3])
    _, _, _, f, s = cands[0]
    r = routes[cands[0][0]] if cands[0][0] < len(routes) else None
    return {"circuit": r["circuit"], "route": r["route"], "date": f["date"], "time": s["time"],
            "route_name": f["route"], "left": s["left"]}


def fill(pg, url, pick):
    """Fill circuit/route/date/time on an already-open page object."""
    pg.goto(url, wait_until="networkidle", timeout=90000)
    pg.wait_for_timeout(1500)
    pg.locator("mat-select").nth(0).click()
    pg.locator("mat-option", has_text=pick["circuit"]).first.click()
    pg.wait_for_timeout(1500)
    pg.locator("mat-select").nth(1).click()
    pg.locator("mat-option", has_text=pick["route"]).first.click()
    pg.wait_for_timeout(3000)
    d = dt.date.fromisoformat(pick["date"])
    w.ensure_calendar(pg)
    if not w.goto_month(pg, d.year, d.month) or d.day not in w.enabled_days(pg):
        raise RuntimeError(f"{pick['date']} is no longer available on {pick['route']}")
    pg.locator(".mat-calendar-body-cell:not(.mat-calendar-body-disabled)",
               has_text=re.compile(rf"^\s*{d.day}\s*$")).first.click()
    pg.wait_for_timeout(2500)
    pg.locator("mat-select").nth(2).click()
    pg.wait_for_selector("mat-option", timeout=20000)
    opt = pg.locator("mat-option", has_text=pick["time"])
    if not opt.count():
        raise RuntimeError(f"{pick['time']} slot is gone for {pick['date']}")
    opt.first.click()
    pg.wait_for_timeout(1000)
    pg.get_by_text("Confirma que eres humano").scroll_into_view_if_needed()


def describe_form(pg):
    """Field layout of the current page (labels/types/names only, never values)."""
    return pg.evaluate("""() => [...document.querySelectorAll('input,select,textarea,mat-select,mat-checkbox,button')]
      .filter(e => e.offsetParent !== null)
      .map(e => {
        const ff = e.closest('mat-form-field');
        const lab = ff ? (ff.querySelector('mat-label,label')||{}).innerText : (e.labels && e.labels[0] ? e.labels[0].innerText : '');
        return {tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '',
                id: e.id || '', formcontrol: e.getAttribute('formcontrolname') || '',
                placeholder: e.placeholder || '', label: (lab||'').trim(),
                text: e.tagName === 'BUTTON' || e.tagName === 'MAT-CHECKBOX' ? e.innerText.trim().slice(0,80) : ''};
      })""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--route", help='e.g. "Circuito 2:2-A"')
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--time", help="HH:MM")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))

    if args.route and args.date and args.time:
        c, r = [x.strip() for x in args.route.split(":")]
        pick = {"circuit": c, "route": r, "date": args.date, "time": args.time}
    else:
        print("Checking what's available for your dates...")
        routes = cfg["routes"]
        found, _ = w.scan(cfg, [dt.date.fromisoformat(s) for s in cfg["dates"]], routes)
        pick = choose(cfg, found, routes)
        if not pick:
            print("Nothing available right now for your dates/routes.")
            return 1
    print(f"Filling: {pick['circuit']} / {pick['route']} on {pick['date']} at {pick['time']}")

    with sync_playwright() as p:
        if args.check:
            browser = p.chromium.launch(headless=True)
        else:
            try:
                browser = p.chromium.launch(headless=False, channel="chrome")
            except Exception:
                browser = p.chromium.launch(headless=False)
        pg = browser.new_page(locale="es-PE", viewport={"width": 1366, "height": 900})
        fill(pg, cfg["url"], pick)

        if args.check:
            CAPTURES.mkdir(exist_ok=True)
            pg.screenshot(path=str(CAPTURES / "check_filled.png"), full_page=True)
            print("Self-test OK, screenshot:", CAPTURES / "check_filled.png")
            browser.close()
            return 0

        print("\n>>> Your turn: tick 'No soy un robot', then click 'Iniciar compra'.")
        if args.record:
            start_html = pg.content()
            pg.wait_for_function(
                "() => ![...document.querySelectorAll('button')].some(b => /Iniciar compra/i.test(b.innerText))"
                " || location.pathname.indexOf('llaqta_machupicchu') < 0", timeout=0)
            pg.wait_for_timeout(4000)
            CAPTURES.mkdir(exist_ok=True)
            pg.screenshot(path=str(CAPTURES / "step2.png"), full_page=True)
            (CAPTURES / "step2_fields.json").write_text(
                json.dumps({"url": pg.url, "fields": describe_form(pg)}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            (CAPTURES / "step2.html").write_text(pg.content(), encoding="utf-8")
            print("\nSaved the next page's layout to", CAPTURES)
            print("Don't enter anything or pay. Close the browser window when done.")
        else:
            print("Then fill in your details and pay. Close the browser window when finished.")
        pg.wait_for_event("close", timeout=0)
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
