#!/usr/bin/env python3
"""Adds the 'busy day = multicolor' feature. Safe to re-run, keeps .bak backups,
works with LF or CRLF files. Usage (from project root):
    python apply_heavy_days.py [app.py] [templates/Release_Calender.html]
"""
import re, shutil, sys

APP = sys.argv[1] if len(sys.argv) > 1 else "app.py"
HTML = sys.argv[2] if len(sys.argv) > 2 else "templates/Release_Calender.html"

ROUTE = '''HEAVY_DAY_THRESHOLD = 20


@app.route("/api/booking-load", methods=["GET"])
def booking_load():
    """Active booking count per date, used to colour busy days in the calendar."""
    raw = (request.args.get("dates") or "").split(",")
    days = []
    for item in raw:
        try:
            days.append(date.fromisoformat(item.strip()))
        except ValueError:
            continue
    if not days:
        return jsonify({"counts": {}, "threshold": HEAVY_DAY_THRESHOLD})
    conn = None
    try:
        conn = get_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT deployment_date, COUNT(*)
                FROM public.deployment_tracker
                WHERE deployment_date = ANY(%s)
                  AND LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cancelled', 'rejected')
                GROUP BY deployment_date
            """, (days,))
            counts = {d.isoformat(): int(n) for d, n in cur.fetchall()}
        return jsonify({"counts": counts, "threshold": HEAVY_DAY_THRESHOLD})
    except Exception:
        app.logger.exception("Could not load booking load")
        return jsonify({"counts": {}, "threshold": HEAVY_DAY_THRESHOLD}), 500
    finally:
        if conn is not None:
            conn.close()

'''

CSS = '''.calendar-day.heavy-day{
  background:linear-gradient(135deg,#ef4444,#f59e0b,#22c55e,#3b82f6,#8b5cf6)!important;
  border-color:transparent!important;color:#fff!important}
.calendar-day.heavy-day .day-caption{color:#fff!important;font-weight:700}
.calendar-day.heavy-day .day-number{text-shadow:0 1px 2px rgba(0,0,0,.45)}
.message.warning{color:#92400e;background:#fef3c7;border:1px solid #fcd34d}
'''

JS1 = '''let dateLoad = {};
const HEAVY_DAY_THRESHOLD = 20;

async function loadDateLoad() {
    if (!allowedCabDates.length) { dateLoad = {}; renderMonthCalendar(); return; }
    try {
        const r = await fetch("/api/booking-load?dates=" + encodeURIComponent(allowedCabDates.join(",")),
                              { headers: { "Accept": "application/json" }, cache: "no-store" });
        const j = await r.json();
        dateLoad = j.counts || {};
    } catch (_) { dateLoad = {}; }
    renderMonthCalendar();
}'''


def read(p):
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def write(p, s):
    shutil.copyfile(p, p + ".bak")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def nl_of(s):
    return "\r\n" if "\r\n" in s else "\n"


def fix_nl(text, nl):
    return text.replace("\r\n", "\n").replace("\n", nl)


def indent_block(text, indent):
    return "\n".join((indent + l if l.strip() else l) for l in text.split("\n"))


def main():
    # ---------------- app.py ----------------
    s = read(APP)
    nl = nl_of(s)
    if "def booking_load" in s:
        print("app.py: already patched, skipped")
    else:
        m = re.search(r'^@app\.route\(\s*"/lookup-cr"', s, re.M)
        if not m:
            sys.exit('app.py: could not find @app.route("/lookup-cr") - nothing changed')
        s = s[:m.start()] + fix_nl(ROUTE, nl) + s[m.start():]
        write(APP, s)
        print("app.py: route /api/booking-load added")

    # ---------------- template ----------------
    h = read(HTML)
    nl = nl_of(h)
    if "heavy-day" in h:
        print("Release_Calender.html: already patched, skipped")
        return
    steps = 0

    # 1) CSS
    i = h.rfind("</style>")
    if i < 0:
        sys.exit("html: </style> not found - nothing changed")
    h = h[:i] + fix_nl(CSS, nl) + h[i:]
    steps += 1

    # 2) state + loader after 'let allowedCabDates = [];'
    m = re.search(r'^([ \t]*)let allowedCabDates = \[\];[ \t]*\r?\n', h, re.M)
    if not m:
        sys.exit("html: 'let allowedCabDates = [];' not found - nothing changed")
    block = fix_nl(indent_block(JS1, m.group(1)), nl) + nl
    h = h[:m.end()] + block + h[m.end():]
    steps += 1

    # 3) call loadDateLoad() after CR validates
    pat = re.compile(r'^([ \t]*)updateCalendarLauncher\(\);[ \t]*\r?\n(?=\s*showMessage\(\s*"CR Approved)', re.M)
    m = pat.search(h)
    if not m:
        sys.exit("html: 'CR Approved' block not found - nothing changed")
    h = h[:m.end()] + m.group(1) + "loadDateLoad();" + nl + h[m.end():]
    steps += 1

    # 4) heavy-day class
    m = re.search(r'^([ \t]*)if \(bookings\.some\(b => b\.date === iso\)\) button\.classList\.add\("has-booking"\);[ \t]*\r?\n', h, re.M)
    if not m:
        sys.exit("html: has-booking line not found - nothing changed")
    ind = m.group(1)
    add = (ind + 'const load = dateLoad[iso] || 0;' + nl +
           ind + 'if (load > HEAVY_DAY_THRESHOLD) button.classList.add("heavy-day");' + nl)
    h = h[:m.end()] + add + h[m.end():]
    steps += 1

    # 5) caption
    pat = re.compile(r'^([ \t]*)caption\.textContent = iso === dateToISO\(todayLocal\) \? "Today" : "";[ \t]*\r?\n', re.M)
    m = pat.search(h)
    if not m:
        sys.exit("html: caption.textContent line not found - nothing changed")
    ind = m.group(1)
    new = (ind + 'caption.className = "day-caption";' + nl +
           ind + 'caption.textContent = load > HEAVY_DAY_THRESHOLD' + nl +
           ind + '    ? load + " booked"' + nl +
           ind + '    : (iso === dateToISO(todayLocal) ? "Today" : "");' + nl)
    h = h[:m.start()] + new + h[m.end():]
    steps += 1

    write(HTML, h)
    print(f"Release_Calender.html: {steps}/5 edits applied")


main()