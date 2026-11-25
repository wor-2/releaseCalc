#!/usr/bin/env python3
"""1) Removes the moving diagonal stripes from RUNNING cards (glow, spinner, timer stay).
2) Shows "Impacted: <application>" on every running/status card, row and category cell.
   Also stores impacted_application in the database when a release is saved.
Run from the project root:  python apply_running_fix.py [app.py] [templates/manager.html]
Safe to re-run; keeps .bak backups; works with LF or CRLF files."""
import re, shutil, sys

APP = sys.argv[1] if len(sys.argv) > 1 else "app.py"
HTML = sys.argv[2] if len(sys.argv) > 2 else "templates/manager.html"


def load(p):
    with open(p, encoding="utf-8", newline="") as f:
        raw = f.read()
    return raw.replace("\r\n", "\n"), ("\r\n" in raw)


def save(p, s, crlf):
    shutil.copyfile(p, p + ".bak")
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s.replace("\n", "\r\n") if crlf else s)


def need(label, n, expected):
    if n < expected:
        sys.exit(f"{label}: expected {expected} match(es), found {n} - nothing changed in this file")


# ------------------------------------------------------------------ manager.html
def patch_html():
    s, crlf = load(HTML)
    if "rs-imp" in s:
        print("manager.html: already patched, skipped"); return

    s, n = re.subn(r'repeating-linear-gradient\(135deg, #ffffff22 0 10px, transparent 10px 20px\),\s*', '', s)
    need("stripes", n, 1)
    s, n = re.subn(r'\n[ \t]*background-size: 40px 40px, 100% 100%;', '', s)
    need("background-size", n, 1)
    old = 'animation: jobStripes 1s linear infinite, jobGlow 1.8s ease-in-out infinite;'
    need("animation", s.count(old), 1)
    s = s.replace(old, 'animation: jobGlow 1.8s ease-in-out infinite;')

    def add_after(cls, imp_cls, expected):
        nonlocal s
        old = ("'<div class=\"%s\" title=\"' + esc(t.application_name) + '\">' + "
               "esc(t.application_name || 'Application not set') + '</div>' +") % cls
        new = old + ("\n            '<div class=\"%s\" title=\"' + esc(t.impacted_application) + "
                     "'\">Impacted: ' + esc(t.impacted_application || '\u2014') + '</div>' +") % imp_cls
        need(cls, s.count(old), expected)
        s = s.replace(old, new)

    add_after("rs-app", "rs-imp", 2)      # full view card + rollback card
    add_after("rsp-app", "rsp-imp", 1)    # dashboard panel row
    add_after("cc-app", "cc-imp", 1)      # category table cell

    css = (".rs-imp, .rsp-imp, .cc-imp { position:relative; z-index:1; margin-top:2px; font-size:10.5px; "
           "opacity:.92; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }\n"
           ".rsp-imp { max-width:100%; }\n")
    i = s.rfind("</style>")
    need("</style>", 1 if i >= 0 else 0, 1)
    s = s[:i] + css + s[i:]
    save(HTML, s, crlf)
    print("manager.html: stripes removed + Impacted line added")


# ------------------------------------------------------------------ app.py
HELPER = '''_IMPACTED_COL_READY = False


def ensure_impacted_column():
    """Adds deployment_tracker.impacted_application once (no-op afterwards)."""
    global _IMPACTED_COL_READY
    if _IMPACTED_COL_READY:
        return
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute("ALTER TABLE public.deployment_tracker "
                            "ADD COLUMN IF NOT EXISTS impacted_application TEXT")
        _IMPACTED_COL_READY = True
    except Exception:
        app.logger.exception("Could not ensure impacted_application column")
    finally:
        if conn is not None:
            conn.close()


@app.before_request
def _impacted_column_hook():
    if _IMPACTED_COL_READY:
        return
    if (request.path in ("/api/running-daily", "/api/rollback-daily")
            or (request.path == "/" and request.method == "POST")):
        ensure_impacted_column()


'''


def patch_app():
    s, crlf = load(APP)
    if "impacted_application" in s:
        print("app.py: already patched, skipped"); return

    # helper before running_category()
    m = re.search(r'^def running_category\(', s, re.M)
    need("running_category", 1 if m else 0, 1)
    s = s[:m.start()] + HELPER + s[m.start():]

    # running_daily: select + unpack + output
    s, n = re.subn(r'(d\.category,)(\s*\n\s*rt\.started_at, rt\.stopped_at,)',
                   r'\1\n                           d.impacted_application,\2', s, count=1)
    need("running_daily select", n, 1)
    s, n = re.subn(r'(is_running, cr_category,)(\s*\n\s*rb_started, rb_stopped, rb_acc, rb_running\) in rows:)',
                   r'\1 cr_impacted,\2', s, count=1)
    need("running_daily unpack", n, 1)
    old = '"cr_category": (cr_category or "Uncategorized").strip() or "Uncategorized",'
    need("running_daily output", s.count(old), 1)
    s = s.replace(old, old + '\n                "impacted_application": (cr_impacted or "").strip(),')

    # rollback_daily
    s, n = re.subn(r'(COALESCE\(t\.is_running, FALSE\))(\s*\n\s*FROM public\.deployment_tracker d\s*\n\s*LEFT JOIN public\.deployment_rollback_timers t)',
                   r'\1,\n                           d.impacted_application\2', s, count=1)
    need("rollback_daily select", n, 1)
    s, n = re.subn(r'(ctype, rb, slots,\s*\n\s*started_at, stopped_at, acc, is_running)\) in rows:',
                   r'\1, impacted) in rows:', s, count=1)
    need("rollback_daily unpack", n, 1)
    old = '"rb_state": state, "rb_label": label,'
    need("rollback_daily output", s.count(old), 1)
    s = s.replace(old, old + '\n                "impacted_application": (impacted or "").strip(),')

    # save_deployment INSERT
    old = 'application_name, category, deployment_count, deployment_type,'
    need("insert columns", s.count(old), 1)
    s = s.replace(old, 'application_name, category, impacted_application, deployment_count, deployment_type,')
    old = 'VALUES (' + ','.join(['%s'] * 20) + ')'
    need("insert placeholders", s.count(old), 1)
    s = s.replace(old, 'VALUES (' + ','.join(['%s'] * 21) + ')')
    old = '(category or "").strip() or None, deployment_count,'
    need("insert params", s.count(old), 1)
    s = s.replace(old, '(category or "").strip() or None,\n                        '
                       '(cr_details.get("impacted_application") or "").strip() or None, deployment_count,')
    save(APP, s, crlf)
    print("app.py: impacted_application column, save and API output added")


patch_html()
patch_app()