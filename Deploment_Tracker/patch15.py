#!/usr/bin/env python3
"""
patch.py - Implementation-window booking for Middleware / Cloud / Infra CRs.

For CRs whose ITSM category is middleware, cloud or infra, every day from the
implementation start date to the implementation end date becomes bookable
(past days are clipped). All other categories keep using the CAB dates.

Usage (run from the project folder):

    python patch.py \
        --impl-start-key udf_date_12345 \
        --impl-end-key   udf_date_12346

    # custom file locations
    python patch.py --app app.py --template templates/Release_Calender.html \
        --impl-start-key udf_date_12345 --impl-end-key udf_date_12346

    # preview only, nothing is written
    python patch.py --dry-run ...

The --impl-*-key options can be repeated; the first key that has a value wins.
Standard ITSM keys such as scheduled_start_time can be used as well.

A .bak copy of each file is created before it is changed. Running the script
twice is safe: already-patched files are skipped.
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

MARKER = "RANGE_CATEGORIES"

# ---------------------------------------------------------------------------
# 1. Helpers inserted before VERIFIED_BY_OPTIONS
# ---------------------------------------------------------------------------
HELPERS = '''# Categories whose CRs can be booked on ANY day between implementation start and end.
RANGE_CATEGORIES = {"middleware", "cloud", "infra"}

# ITSM field keys for the implementation window (first non-empty value wins).
IMPL_START_KEYS = __START_KEYS__
IMPL_END_KEYS = __END_KEYS__


def is_range_category(category):
    return (category or "").strip().casefold() in RANGE_CATEGORIES


def _itsm_date_value(change, udf, keys):
    """Return the first non-empty date among the given keys (UDF or standard field)."""
    for key in keys:
        raw = udf.get(key) if key.startswith("udf_") else change.get(key)
        if isinstance(raw, dict):
            raw = raw.get("display_value") or raw.get("value")
        if raw:
            return raw
    return None


'''

# ---------------------------------------------------------------------------
# 2. Replacement for the cab_dates empty-check block in validate_cr_number
# ---------------------------------------------------------------------------
CAB_BLOCK = '''cab_dates = list(dict.fromkeys(cab_dates))
        date_mode = "cab"
        range_start = range_end = None

        if is_range_category(category):
            raw_start = _itsm_date_value(change, udf, IMPL_START_KEYS)
            raw_end = _itsm_date_value(change, udf, IMPL_END_KEYS)
            if not raw_start or not raw_end:
                raise ValueError(
                    f"CR-{change_id} ({category}) does not have an implementation start/end date in ITSM."
                )
            impl_start = parse_cab_date(raw_start)
            impl_end = parse_cab_date(raw_end)
            if impl_end < impl_start:
                raise ValueError(
                    f"CR-{change_id} has an implementation end date before its start date."
                )

            today = date.today()
            if impl_end < today:
                raise ValueError(
                    f"CR-{change_id} implementation window has already ended ({impl_end.isoformat()})."
                )

            effective_start = max(impl_start, today)  # past days cannot be booked
            cab_dates = [
                (effective_start + timedelta(days=n)).isoformat()
                for n in range((impl_end - effective_start).days + 1)
            ]
            date_mode = "range"
            range_start, range_end = impl_start.isoformat(), impl_end.isoformat()

        if not cab_dates:
            raise ValueError(
                f"CR-{change_id} does not have any deployment dates configured in ITSM."
            )'''

# ---------------------------------------------------------------------------
# 4. Frontend success message
# ---------------------------------------------------------------------------
JS_MESSAGE = '''showMessage(
            result.date_mode === "range"
                ? "CR Approved. Implementation window: " + result.range_start +
                  " to " + result.range_end + ". Any date in this window can be booked."
                : "CR Approved. Available CAB dates: " + allowedCabDates.join(", "),
            "success"
        );'''


def py_tuple(keys):
    return "(" + ", ".join(repr(k) for k in keys) + ("," if len(keys) == 1 else "") + ")"


def patch_app(src, start_keys, end_keys):
    if MARKER in src:
        return src, ["app: already patched, skipped"]

    notes = []

    # 1. helpers
    anchor = 'VERIFIED_BY_OPTIONS = ["DBA TEAM", "MW TEAM", "DevOps Team"]'
    if src.count(anchor) != 1:
        raise SystemExit("ERROR: could not find the VERIFIED_BY_OPTIONS line in the app file.")
    helpers = (HELPERS
               .replace("__START_KEYS__", py_tuple(start_keys))
               .replace("__END_KEYS__", py_tuple(end_keys)))
    src = src.replace(anchor, helpers + anchor, 1)
    notes.append("app: added RANGE_CATEGORIES and helper functions")

    # 2. cab_dates block in validate_cr_number
    pattern = re.compile(
        r'cab_dates = list\(dict\.fromkeys\(cab_dates\)\)\s*'
        r'if not cab_dates:\s*'
        r'raise ValueError\(\s*'
        r'f"CR-\{change_id\} does not have any deployment dates configured in ITSM\."\s*'
        r'\)'
    )
    matches = pattern.findall(src)
    if len(matches) != 1:
        raise SystemExit(
            f"ERROR: expected 1 cab_dates block in validate_cr_number, found {len(matches)}."
        )
    src = pattern.sub(lambda m: CAB_BLOCK, src, count=1)
    notes.append("app: validate_cr_number now expands the implementation window")

    # 2b. returned dict
    ret_pattern = re.compile(r'("cab_dates": cab_dates)(\s*\n)(\s*)\}')
    if len(ret_pattern.findall(src)) != 1:
        raise SystemExit('ERROR: could not find the returned dict ("cab_dates": cab_dates).')

    def add_keys(m):
        indent = m.group(3) + "    "
        return (
            f'"cab_dates": cab_dates,\n'
            f'{indent}"date_mode": date_mode,\n'
            f'{indent}"range_start": range_start,\n'
            f'{indent}"range_end": range_end\n'
            f'{m.group(3)}}}'
        )

    src = ret_pattern.sub(add_keys, src, count=1)
    notes.append("app: validate_cr_number returns date_mode / range_start / range_end")

    # 3. validate_cr route
    route_anchor = '"cab_date_iso": allowed_dates[0] if allowed_dates else None,'
    if src.count(route_anchor) != 1:
        raise SystemExit("ERROR: could not find cab_date_iso in the /api/validate-cr route.")
    route_new = (
        route_anchor + "\n"
        '            "date_mode": cr_details.get("date_mode", "cab"),\n'
        '            "range_start": cr_details.get("range_start"),\n'
        '            "range_end": cr_details.get("range_end"),'
    )
    src = src.replace(route_anchor, route_new, 1)
    notes.append("app: /api/validate-cr passes date_mode / range_start / range_end")

    return src, notes


def patch_template(src):
    if "result.date_mode" in src:
        return src, ["template: already patched, skipped"]

    pattern = re.compile(
        r'showMessage\(\s*"CR Approved\. Available CAB dates: "\s*\+\s*'
        r'allowedCabDates\.join\(", "\),\s*"success"\s*\);'
    )
    if len(pattern.findall(src)) != 1:
        raise SystemExit("ERROR: could not find the 'CR Approved' showMessage call in the template.")
    src = pattern.sub(lambda m: JS_MESSAGE, src, count=1)
    return src, ["template: success message shows the implementation window"]


def write_with_backup(path, new_text):
    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(new_text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", default="app.py", help="Flask app file (default: app.py)")
    parser.add_argument("--template", default="templates/Release_Calender.html",
                        help="Release calendar template (default: templates/Release_Calender.html)")
    parser.add_argument("--impl-start-key", action="append", dest="start_keys",
                        help="ITSM key for implementation start date (repeatable)")
    parser.add_argument("--impl-end-key", action="append", dest="end_keys",
                        help="ITSM key for implementation end date (repeatable)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing")
    args = parser.parse_args()

    start_keys = args.start_keys or ["udf_date_XXXXX", "scheduled_start_time"]
    end_keys = args.end_keys or ["udf_date_YYYYY", "scheduled_end_time"]
    if any("XXXXX" in k or "YYYYY" in k for k in start_keys + end_keys):
        print("WARNING: placeholder ITSM keys are in use. Pass --impl-start-key / --impl-end-key "
              "with the real field names, or edit IMPL_START_KEYS / IMPL_END_KEYS afterwards.\n")

    app_path = Path(args.app)
    tpl_path = Path(args.template)
    for p in (app_path, tpl_path):
        if not p.is_file():
            sys.exit(f"ERROR: file not found: {p}")

    # Compute everything first so a failure leaves both files untouched.
    new_app, app_notes = patch_app(app_path.read_text(encoding="utf-8"), start_keys, end_keys)
    new_tpl, tpl_notes = patch_template(tpl_path.read_text(encoding="utf-8"))

    for note in app_notes + tpl_notes:
        print(" -", note)

    if args.dry_run:
        print("\nDry run: no files written.")
        return

    write_with_backup(app_path, new_app)
    write_with_backup(tpl_path, new_tpl)
    print("\nDone. Backups saved as *.bak. Restart the Flask app to apply the changes.")


if __name__ == "__main__":
    main()