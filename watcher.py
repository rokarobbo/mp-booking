"""Machu Picchu ticket watcher for tuboleto.cultura.pe.

Loads the public booking page in a headless browser (exactly like a visitor would),
checks the configured circuit/routes for the configured dates, reads the entry-time
slots and remaining tickets, and sends a phone notification via ntfy. It never passes the CAPTCHA, never
reserves and never pays: booking is done by you.

Usage:
  python watcher.py                 # normal run (pushes via ntfy if NTFY_TOPIC is set)
  python watcher.py --dry-run       # print the notification instead of sending
  python watcher.py --dry-run --dates 2026-12-10,2026-12-11 --routes "Circuito 1:1-A"
                                    # test against dates that are on sale today
"""
import argparse, datetime as dt, json, os, pathlib, re, sys, time, traceback, urllib.request
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
STATE_FILE = HERE / "state.json"
LIMA = dt.timezone(dt.timedelta(hours=-5))
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- page helpers
def open_form(browser, url, circuit, route):
    pg = browser.new_page(locale="es-PE", viewport={"width": 1366, "height": 900})
    pg.goto(url, wait_until="networkidle", timeout=90000)
    pg.wait_for_timeout(1500)
    pg.locator("mat-select").nth(0).click()
    pg.locator("mat-option", has_text=circuit).first.click()
    pg.wait_for_timeout(1500)
    pg.locator("mat-select").nth(1).click()
    pg.wait_for_selector("mat-option", timeout=20000)
    opt = pg.locator("mat-option", has_text=route)
    if not opt.count():
        names = [o.inner_text().strip() for o in pg.query_selector_all("mat-option")]
        raise RuntimeError(f"Route '{route}' not found for {circuit}. Site shows: {names}")
    route_name = opt.first.inner_text().strip()
    opt.first.click()
    pg.wait_for_timeout(3000)
    return pg, route_name


def ensure_calendar(pg):
    if not pg.locator(".mat-calendar").count():
        pg.locator("mat-datepicker-toggle button").first.click()
    pg.wait_for_selector(".mat-calendar", timeout=20000)
    pg.wait_for_timeout(1000)


def goto_month(pg, year, month):
    """Navigate the open calendar to month/year. Returns False if it can't get there."""
    for _ in range(30):
        period = pg.locator(".mat-calendar-period-button").inner_text().strip()
        m = re.search(r"(\d{1,2})/(\d{4})", period)
        if not m:
            raise RuntimeError(f"Unexpected calendar header: {period!r}")
        cur = (int(m.group(2)), int(m.group(1)))
        if cur == (year, month):
            return True
        btn = pg.locator(".mat-calendar-next-button" if cur < (year, month)
                         else ".mat-calendar-previous-button")
        if btn.is_disabled():
            return False
        btn.click()
        pg.wait_for_timeout(1500)
    return False


def enabled_days(pg):
    return {int(c.inner_text().strip()) for c in pg.query_selector_all(
        ".mat-calendar-body-cell:not(.mat-calendar-body-disabled)")}


def read_slots(pg, day):
    pg.locator(".mat-calendar-body-cell:not(.mat-calendar-body-disabled)",
               has_text=re.compile(rf"^\s*{day}\s*$")).first.click()
    pg.wait_for_timeout(2500)
    pg.locator("mat-select").nth(2).click()
    pg.wait_for_selector("mat-option", timeout=20000)
    pg.wait_for_timeout(800)
    slots = []
    for o in pg.query_selector_all("mat-option"):
        txt = o.inner_text().strip()
        tm = re.search(r"(\d{1,2}:\d{2})", txt)
        cnt = re.search(r"(\d+)\s*boleto", txt)
        if tm and o.get_attribute("aria-disabled") != "true":
            left = int(cnt.group(1)) if cnt else None
            if left is None or left > 0:
                slots.append({"time": tm.group(1).zfill(5), "left": left})
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(500)
    return slots


# ---------------------------------------------------------------- scanning
def scan(cfg, dates, routes):
    by_month = {}
    for d in dates:
        by_month.setdefault((d.year, d.month), []).append(d)
    found, release_seen = [], False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for r in routes:
                pg, route_name = open_form(browser, cfg["url"], r["circuit"], r["route"])
                for (y, m), ds in sorted(by_month.items()):
                    ensure_calendar(pg)
                    if not goto_month(pg, y, m):
                        continue
                    open_days = enabled_days(pg)
                    if open_days:
                        release_seen = True
                    for d in ds:
                        if d.day not in open_days:
                            continue
                        slots = read_slots(pg, d.day)
                        if slots:
                            found.append({"route": route_name, "date": d.isoformat(), "slots": slots})
                        ensure_calendar(pg)
                        goto_month(pg, y, m)
                pg.close()
            # canary: has the target month been released at all (any circuit)?
            c = cfg.get("release_canary")
            if c and not release_seen:
                pg, _ = open_form(browser, cfg["url"], c["circuit"], c["route"])
                for (y, m) in by_month:
                    ensure_calendar(pg)
                    if goto_month(pg, y, m) and enabled_days(pg):
                        release_seen = True
                pg.close()
        finally:
            browser.close()
    return found, release_seen


# ---------------------------------------------------------------- messaging
def in_window(t, win):
    return win[0] <= t <= win[1]


def format_found(found, cfg):
    win = cfg["preferred_window"]
    lines = []
    for f in found:
        d = dt.date.fromisoformat(f["date"])
        slots = ", ".join(
            s["time"] + (" ⭐" if in_window(s["time"], win) else "")
            + (f" ({s['left']} left)" if s["left"] is not None else "")
            for s in f["slots"])
        lines.append(f"• {f['route']} — {WEEKDAYS[d.weekday()]} {d.day} {d.strftime('%b')}: {slots}")
    lines.append(f"⭐ = {win[0]}–{win[1]}")
    lines.append("Tap this → Comprar → Circuito 2 → route → date → time → No soy un robot → "
                 "Iniciar compra. You then have 15 min to pay.")
    return "\n".join(lines)


def send(title, body, dry_run, priority="default", tags="", click=None):
    """Push a notification via ntfy (topic from the NTFY_TOPIC env var / GitHub secret)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if dry_run or not topic:
        print(("[dry-run] " if dry_run else "[no NTFY_TOPIC] ") + f"would send ({priority}): {title}\n{body}")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers)
    urllib.request.urlopen(req, timeout=20).read()
    print(f"sent ({priority}): {title}\n{body}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dates", help="comma-separated YYYY-MM-DD (overrides config)")
    ap.add_argument("--routes", help='e.g. "Circuito 2:2-A,Circuito 2:2-B" (overrides config)')
    args = ap.parse_args()

    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    dates = [dt.date.fromisoformat(s.strip()) for s in (args.dates.split(",") if args.dates else cfg["dates"])]
    routes = ([{"circuit": c.split(":")[0].strip(), "route": c.split(":")[1].strip()}
               for c in args.routes.split(",")] if args.routes else cfg["routes"])

    state = load_state()
    now = time.time()
    lima_now = dt.datetime.now(LIMA)
    target_desc = f"{', '.join(r['circuit'] + ' ' + r['route'] for r in routes)} on {dates[0]:%d %b}–{dates[-1]:%d %b %Y}"

    try:
        found, release_seen = scan(cfg, dates, routes)
        state["fail_count"] = 0
    except Exception as e:
        state["fail_count"] = state.get("fail_count", 0) + 1
        traceback.print_exc()
        if state["fail_count"] == cfg["fail_alert_after"]:
            send("Machu Picchu watcher is failing",
                 f"{state['fail_count']} runs in a row failed (the site may have changed). "
                 f"Last error: {str(e)[:300]}", args.dry_run, priority="high", tags="warning")
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        return 0

    print(f"[{lima_now:%Y-%m-%d %H:%M} Lima] release_seen={release_seen} found={json.dumps(found, ensure_ascii=False)}")

    if found:
        sig = json.dumps([(f["route"], f["date"], [s["time"] for s in f["slots"]]) for f in found])
        stale = now - state.get("last_alert_ts", 0) > cfg["repeat_alert_minutes"] * 60
        if sig != state.get("last_sig") or stale:
            send("Machu Picchu tickets AVAILABLE", format_found(found, cfg), args.dry_run,
                 priority="urgent", tags="rotating_light,ticket", click=cfg["url"])
            state.update(last_sig=sig, last_alert_ts=now)
    else:
        if state.get("last_sig"):
            send("Machu Picchu tickets gone", f"The tickets reported earlier for {target_desc} are gone. "
                 "Still watching.", args.dry_run, tags="x")
        state["last_sig"] = None

    if release_seen and not state.get("release_announced"):
        if not found:
            send("Tu Boleto opened sales for your month",
                 f"Nothing free yet for {target_desc}. Still watching.", args.dry_run,
                 priority="high", tags="mega", click=cfg["url"])
        state["release_announced"] = True

    today = lima_now.date().isoformat()
    if lima_now.hour >= cfg["heartbeat_hour_lima"] and state.get("last_heartbeat") != today:
        if not found:
            status = "sales for your dates are open, no free slots" if release_seen else "your dates are not on sale yet"
            send("Watcher daily check-in", f"Running. Status: {status} ({target_desc}).",
                 args.dry_run, priority="low", tags="eyes")
        state["last_heartbeat"] = today

    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
