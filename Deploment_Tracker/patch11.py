#!/usr/bin/env python3
"""
ALL-IN-ONE PATCH  (multi-booking + popup workflow + 30-minute running alert)

Usage (from your project root):
    python apply_all_patches.py [app.py] [templates/Release_Calender.html] \
                                [templates/governance_tracker.html] [mailer.py]

Includes the earlier multi-date / multi-slot booking patch, so apply ONLY this one
(if you already applied the earlier one, this detects it and just upgrades it).

- Writes <file>.bak backups first.  All-or-nothing: if any anchor is missing, nothing is written.
- Safe to re-run.   Revert: move each .bak file back over the original.
"""
import re, sys, shutil, pathlib

APP = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "app.py")
TPL = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "templates/Release_Calender.html")


def ws(s):
    """Regex that matches s but tolerates any whitespace differences."""
    return r"\s+".join(re.escape(t) for t in s.split())


def sub1(text, pattern, repl, label, flags=0):
    found = list(re.finditer(pattern, text, flags))
    if len(found) != 1:
        sys.exit(f"[FAILED] {label}: anchor matched {len(found)} times (expected 1). Nothing was written.")
    m = found[0]
    new = repl(m) if callable(repl) else repl
    print(f"  ok  {label}")
    return text[:m.start()] + new + text[m.end():]


# =====================================================================
# app.py
# =====================================================================
PARSE_AND_SAVE = r'''MAX_BOOKINGS_PER_SUBMIT = 50


def parse_bookings(data):
    """Parse the JSON list [{"date": "YYYY-MM-DD", "slot": "HH:MM"}, ...]."""
    try:
        items = json.loads(data.get("bookings") or "[]")
    except (TypeError, ValueError):
        items = []
    if not isinstance(items, list) or not items:
        raise ValueError("Select at least one deployment date and slot.")
    if len(items) > MAX_BOOKINGS_PER_SUBMIT:
        raise ValueError(f"You can book at most {MAX_BOOKINGS_PER_SUBMIT} slots at one time.")
    seen, result = set(), []
    for item in items:
        try:
            d = datetime.strptime(str(item.get("date")), "%Y-%m-%d").date()
            t = datetime.strptime(str(item.get("slot")), "%H:%M").time()
        except (ValueError, AttributeError) as exc:
            raise ValueError("Enter valid deployment dates and time slots.") from exc
        if (d, t) not in seen:
            seen.add((d, t))
            result.append((d, t))
    result.sort()
    return result


def save_deployment(data):
    """Save one deployment_tracker row per (date, slot). Returns (rfid, bookings)."""
    cr_number = re.sub(r"^CR-", "", (data.get("cr_number") or "").strip(), flags=re.IGNORECASE).strip()
    if not cr_number.isdigit():
        raise ValueError("CR Number must contain digits after CR-")
    cr_number = f"CR-{cr_number}"

    cr_details = validate_cr_number(cr_number)
    category = cr_details.get("category", "")
    app.logger.info("CR Validated | CR=%s | CAB Dates=%s", cr_number, cr_details.get("cab_dates"))

    business_unit = (data.get("business_unit") or "").strip()
    environment = (data.get("environment") or "").strip()

    git_links = [l.strip() for l in request.form.getlist("git_links[]") if l and l.strip()]
    if not git_links and not is_git_optional(category):
        raise ValueError("At least one Git link is required.")
    for link in git_links:
        if not validate_git_link(link):
            raise ValueError(f"Invalid Git link: {link}")

    application_name = (data.get("application_name") or "").strip()
    if not application_name:
        raise ValueError("Application Name is required.")
    change_requestor = (data.get("change_requestor") or "").strip()
    if not change_requestor:
        raise ValueError("Change Requestor is required.")
    if len(change_requestor) > 150:
        raise ValueError("Change Requestor cannot exceed 150 characters.")

    change_type = (data.get("change_type") or "").strip()
    if change_type not in CHANGE_TYPES:
        raise ValueError("Invalid change type.")
    if data.get("deployment_type") not in DEPLOYMENT_TYPES:
        raise ValueError("Invalid deployment type.")
    data["status"] = PENDING_STATUS

    roll_back = (data.get("roll_back") or "No").strip()
    if roll_back not in ("Yes", "No"):
        raise ValueError("Invalid rollback selection.")
    data["roll_back"] = roll_back

    try:
        deployment_count = int(data.get("deployment_count", "1"))
    except (TypeError, ValueError):
        raise ValueError("Deployment Count must be numeric.")
    if deployment_count < 1 or deployment_count > 5:
        raise ValueError("Deployment Count must be between 1 and 5.")

    # ---- validate every requested (date, slot) ----
    bookings = parse_bookings(data)
    allowed_dates = {parse_cab_date(d) for d in cr_details.get("cab_dates", [])}
    valid_starts = set(get_slot_starts(change_type))
    for d, t in bookings:
        if d not in allowed_dates:
            raise ValueError(f"{d.isoformat()} is not a deployment date in the CR.")
        validate_deployment_date(d, is_manager=is_manager_user(), change_type=change_type)
        if t.strftime("%H:%M") not in valid_starts:
            if change_type == "Emergency CR":
                raise ValueError("Emergency CR slots must start on a half-hour boundary.")
            raise ValueError("Choose a 30-minute slot between 6:30 PM and 11:00 PM.")

    conn = None
    try:
        conn = get_connection()
        with conn:  # one transaction: all bookings or none
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (cr_number.casefold(),))
                rfid = get_existing_rfid(cur, cr_number)
                if not rfid:
                    for _ in range(20):
                        candidate = generate_rfid()
                        cur.execute("SELECT 1 FROM public.deployment_tracker WHERE rfid = %s LIMIT 1", (candidate,))
                        if cur.fetchone() is None:
                            rfid = candidate
                            break
                    if not rfid:
                        raise RuntimeError("Could not generate an available RFID. Please retry.")

                max_cr_per_slot = get_release_settings(cur)

                # Lock slots in sorted order (prevents deadlocks between concurrent multi-bookings)
                slot_ranges = []
                for d, t in bookings:
                    s = datetime.combine(d, t)
                    slot_ranges.append((s, s + timedelta(minutes=30)))
                for s, _e in slot_ranges:
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                (f"release-slot:{s.isoformat()}",))

                blocked_cache = {}
                record_ids = []
                for (d, t), (slot_start, slot_end) in zip(bookings, slot_ranges):
                    if d not in blocked_cache:
                        blocked_cache[d] = get_blocked_date(cur, d)
                    if blocked_cache[d] is not None:
                        raise ValueError(
                            f"{d.isoformat()} is blocked by the Super Manager. "
                            f"Message: {blocked_cache[d] or 'No bookings are allowed on this date.'}")

                    cur.execute("""
                        SELECT COUNT(*) FROM public.deployment_tracker
                        WHERE slot_start < %s AND slot_end > %s
                          AND LOWER(TRIM(COALESCE(status, ''))) NOT IN ('cancelled', 'rejected')
                    """, (slot_end, slot_start))
                    if int(cur.fetchone()[0] or 0) >= max_cr_per_slot:
                        raise ValueError(
                            f"The slot {d.isoformat()} {slot_start:%H:%M} has reached its limit of "
                            f"{max_cr_per_slot} CR request(s). Nothing was saved; choose another slot.")

                    cur.execute("""
                        INSERT INTO public.deployment_tracker (
                            cr_number, rfid, deployment_date, slots, slot_start, slot_end,
                            application_name, category, deployment_count, deployment_type,
                            change_type, released_by, verified_and_deployed_by,
                            change_requestor, business_unit, environment, git_links,
                            incident_number, roll_back, status
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        RETURNING id
                    """, (
                        cr_number, rfid, d, f"{slot_start:%H:%M} - {slot_end:%H:%M}",
                        slot_start, slot_end, application_name,
                        (category or "").strip() or None, deployment_count,
                        data["deployment_type"], change_type, None, None,
                        change_requestor, business_unit, environment,
                        json.dumps(git_links), data.get("incident_number"),
                        data["roll_back"], data["status"],
                    ))
                    record_ids.append(cur.fetchone()[0])

        write_audit_log(
            "INFO",
            f"Request submitted successfully. {len(record_ids)} booking(s); Record IDs={record_ids}; "
            f"slots={[f'{d.isoformat()} {t:%H:%M}' for d, t in bookings]}",
            cr_number, rfid)
        return rfid, bookings
    except errors.ExclusionViolation as exc:
        raise ValueError("A slot was just booked by another request. Please refresh and choose again.") from exc
    except errors.UniqueViolation as exc:
        app.logger.exception("Unique constraint violation: %s", getattr(exc.diag, "constraint_name", ""))
        raise RuntimeError("A database uniqueness constraint rejected this request.") from exc
    except psycopg2.Error as exc:
        app.logger.exception("Database save failed; SQLSTATE=%s", exc.pgcode)
        raise RuntimeError("Could not save the request to the database. Check the server logs.") from exc
    finally:
        if conn is not None:
            conn.close()


'''

REQUIRED_FIELDS_NEW = '''required_fields = [
        "cr_number",
        "application_name",
        "deployment_type",
        "change_type",
        "change_requestor",
    ]  # deployment_date / slot_start removed; bookings are validated in save_deployment'''

SAVE_BLOCK_NEW = '''rfid, bookings = save_deployment(data)
        cr_number = (data.get("cr_number") or "").strip()

        # ONE email listing every booked slot (mailer.py reads data["bookings"])
        mail_data = dict(data, bookings=[{"date": d.isoformat(), "slot": t.strftime("%H:%M")} for d, t in bookings])
        sent_all, last_msg = send_deployment_notification(mail_data, rfid)

        success_message = (
            f"{len(bookings)} slot(s) booked successfully! CR Number: {cr_number} | RFID: {rfid}"
        )
        if sent_all:
            success_message += " | Email notification sent."
            flash(success_message, "success")
        else:
            flash(success_message, "success")
            flash(last_msg or "Email notification was not sent.", "warning")
        return redirect(url_for("Release_Calender"))'''


def patch_app(src):
    if "def parse_bookings(" in src:
        print("  --  multi-booking already applied; upgrading to a single combined email")
        return upgrade_email_loop(src)
    print("app.py")
    a = src.find("def save_deployment(data):")
    b = src.find("def get_released_by_users():")
    if a == -1 or b == -1 or b < a:
        sys.exit("[FAILED] app.py: could not locate save_deployment()/get_released_by_users().")
    src = src[:a] + PARSE_AND_SAVE + src[b:]
    print("  ok  replaced save_deployment() + added parse_bookings()")

    src = sub1(src,
               r"required_fields = \[\s*\"cr_number\",\s*\"deployment_date\",\s*\"slot_start\",\s*"
               r"\"application_name\",\s*\"deployment_type\",\s*\"change_type\",\s*\"change_requestor\",\s*\]",
               REQUIRED_FIELDS_NEW, "required_fields in Release_Calender")

    src = sub1(src,
               r"rfid = save_deployment\(data\)\s*"
               r"cr_number = \(data\.get\(\"cr_number\"\) or \"\"\)\.strip\(\)\s*"
               r"email_sent, email_message = send_deployment_notification\(data, rfid\)\s*"
               r"success_message = \(.*?\)\s*"
               r"if email_sent:.*?return redirect\(url_for\(\"Release_Calender\"\)\)",
               SAVE_BLOCK_NEW, "POST handler: save + per-booking email", re.S)
    return src


# =====================================================================
# Release_Calender.html
# =====================================================================
CSS_ADD = '''
/* ===== Multi-booking chips ===== */
.booking-summary{display:flex;flex-wrap:wrap;gap:6px;width:100%;margin-top:6px}
.booking-chip{display:inline-flex;align-items:center;gap:6px;padding:4px 6px 4px 10px;border-radius:999px;
  background:#e8f0ff;border:1px solid #b9ceff;color:#1e3472;font-size:11px;font-weight:700}
.booking-chip button{border:0;background:#2457d6;color:#fff;width:18px;height:18px;border-radius:50%;
  cursor:pointer;font-size:12px;line-height:1;padding:0;min-height:0}
.calendar-day.has-booking::after{content:"\\25CF";color:#16a34a;font-size:9px;line-height:1}
.calendar-day.selected.has-booking::after{color:#fff}

'''

JS_BOOKINGS = '''let allowedCabDates = [];

            // ---- multi-booking state: [{date:"YYYY-MM-DD", slot:"HH:MM"}, ...] ----
            let bookings = [];
            const bookingsInput = document.getElementById("bookings");
            const bookingSummary = document.getElementById("bookingSummary");
            try { bookings = JSON.parse(bookingsInput.value || "[]"); } catch (_) { bookings = []; }

            function hasBooking(d, s) { return bookings.some(b => b.date === d && b.slot === s); }

            function syncBookings() {
                bookings.sort((a, b) => (a.date + a.slot).localeCompare(b.date + b.slot));
                bookingsInput.value = JSON.stringify(bookings);
                renderBookingSummary();
            }

            function toggleBooking(d, s) {
                if (hasBooking(d, s)) bookings = bookings.filter(b => !(b.date === d && b.slot === s));
                else bookings.push({ date: d, slot: s });
                syncBookings();
            }

            function renderBookingSummary() {
                bookingSummary.innerHTML = "";
                bookings.forEach(function (b) {
                    const end = (function (t) {
                        const [h, m] = t.split(":").map(Number);
                        const total = h * 60 + m + 30;
                        return String(Math.floor(total / 60) % 24).padStart(2, "0") + ":" + String(total % 60).padStart(2, "0");
                    })(b.slot);
                    const chip = document.createElement("span");
                    chip.className = "booking-chip";
                    const label = new Date(b.date + "T12:00:00").toLocaleDateString(undefined,
                        { weekday: "short", day: "numeric", month: "short" });
                    chip.append(label + " \\u00B7 " + formatSlotTime(b.slot) + " \\u2013 " + formatSlotTime(end));
                    const x = document.createElement("button");
                    x.type = "button";
                    x.textContent = "\\u00D7";
                    x.setAttribute("aria-label", "Remove booking");
                    x.addEventListener("click", function () {
                        toggleBooking(b.date, b.slot);
                        renderMonthCalendar();
                        if (dateInput.value === b.date) loadSlots();
                    });
                    chip.appendChild(x);
                    bookingSummary.appendChild(chip);
                });
            }

            document.getElementById("slotDoneButton").addEventListener("click", closeSlotSelection);'''

JS_SELECTED_TEXT = '''const selectedText = bookings.length
                    ? '<div class="date-slot-selected">' + bookings.length + ' slot(s) selected in total</div>'
                    : "";'''

JS_TILE_CLICK = '''if (hasBooking(dateInput.value, slot.start)) tile.classList.add("selected");

                        if (canSelect) {
                            tile.addEventListener("click", function () {
                                toggleBooking(dateInput.value, slot.start);
                                tile.classList.toggle("selected", hasBooking(dateInput.value, slot.start));
                                slotSelection.textContent = bookings.length + " slot(s) selected in total.";
                                updateDateSlotSummary(result.slots || [], Boolean(result.blocked), "");
                                renderMonthCalendar();   // green dot on dates that have bookings
                            });
                        }
                        slotGrid.appendChild(tile);'''

JS_AFTER_LOOP = '''slotSelection.textContent = bookings.length
                        ? bookings.length + " slot(s) selected in total."
                        : "No slot selected.";
                    updateDateSlotSummary(result.slots || [], Boolean(result.blocked), "");

                } catch (error) {'''

JS_NO_BOOKINGS = '''if (!bookings.length) {
                    calendarHelp.textContent = "Select at least one deployment date and slot.";
                    calendarHelp.style.color = "#b91c1c";
                    openCalendar();
                    return;
                }'''

JS_PRESUBMIT = '''if (lookupFailed || isChecking || !allowedCabDates.length) {
                        showMessage("CR verification failed or the ITSM CAB date is missing. Please retry before saving.", "error");
                        return;
                    }
                    if (bookings.some(b => !allowedCabDates.includes(b.date))) {
                        showMessage("Only CAB dates returned by ITSM can be booked.", "error");
                        return;
                    }

                    // Re-verify every selected slot, one request per distinct date
                    const dates = [...new Set(bookings.map(b => b.date))];
                    for (const d of dates) {
                        const resp = await fetch(
                            slotStatusUrl + "?date=" + encodeURIComponent(d)
                            + "&change_type=" + encodeURIComponent(changeTypeInput.value || "")
                            + "&cr_number=" + encodeURIComponent(crInput.value || ""),
                            { headers: { "Accept": "application/json" }, cache: "no-store" }
                        );
                        if (!resp.ok) throw new Error("Unable to verify slot availability.");
                        const res = await resp.json();
                        const lost = bookings.filter(b => b.date === d).filter(function (b) {
                            const s = res.slots.find(x => x.start === b.slot);
                            return res.blocked || !s || s.state !== "available";
                        });
                        if (lost.length) {
                            bookings = bookings.filter(b => !lost.includes(b));
                            syncBookings();
                            renderMonthCalendar();
                            showMessage("Some slots on " + d + " are no longer available and were removed. Review and select again.", "error");
                            return;
                        }
                    }

                    saveButton.textContent = "Submitting...";
                    form.submit();'''


def patch_tpl(src):
    print("Release_Calender.html")
    if 'id="bookings"' in src:
        print("  --  multi-booking already applied; skipped")
        return src

    # ---- HTML ----
    src = sub1(src,
               ws('<input type="hidden" id="deployment_date" name="deployment_date" '
                  'value="{{ data.get(\'deployment_date\', \'\') }}">'),
               '<input type="hidden" id="deployment_date" value="{{ data.get(\'deployment_date\', \'\') }}">\n'
               '                    <input type="hidden" id="bookings" name="bookings" '
               'value="{{ data.get(\'bookings\', \'[]\') }}">',
               "HTML: hidden bookings field")

    src = sub1(src,
               ws('<input type="hidden" id="slot_start" name="slot_start" '
                  'value="{{ data.get(\'slot_start\', \'\') }}" required>'),
               '<input type="hidden" id="slot_start" value="{{ data.get(\'slot_start\', \'\') }}">',
               "HTML: slot_start no longer submitted")

    src = sub1(src,
               ws('<div class="date-slot-summary" id="dateSlotSummary" aria-live="polite"> '
                  '<span class="date-slot-placeholder">Slot details will appear after selecting a deployment date.</span> '
                  '</div>'),
               lambda m: m.group(0) + '\n            <div class="booking-summary" id="bookingSummary"></div>',
               "HTML: booking chips container")

    src = sub1(src,
               ws('<p class="slot-selection" id="slotSelection" aria-live="polite"> No slot selected. </p>'),
               lambda m: m.group(0) +
               '\n\n                    <button type="button" class="submit-button" id="slotDoneButton" '
               'style="margin-top:12px;width:100%!important;min-height:40px!important;">\n'
               '                        Done \u2013 add another date or continue\n                    </button>',
               "HTML: Done button in slot modal")

    src = sub1(src,
               ws('<p class="calendar-help" id="calendarHelp">Choose a Change Type, then select a future date. '
                  'Emergency CR: any day; other types: weekdays.</p>'),
               '<p class="calendar-help" id="calendarHelp">Pick a date, select one or more slots, '
               'then pick another CAB date if you need more.</p>',
               "HTML: calendar help text")

    # ---- CSS ----
    src = sub1(src, r"</style>", lambda m: CSS_ADD + "</style>", "CSS: booking chip styles")

    # ---- JS ----
    src = sub1(src, r"let allowedCabDates = \[\];", lambda m: JS_BOOKINGS, "JS: booking state + helpers")

    src = sub1(src,
               r"const selected = slotList\.find\(slot => slot\.start === selectedStart\);\s*"
               r"const selectedText = selected\s*\?.*?:\s*\"\";",
               lambda m: JS_SELECTED_TEXT, "JS: updateDateSlotSummary selected text", re.S)

    # remove the two slotSelection overwrites in loadSlots (Emergency / normal)
    src = sub1(src,
               r"\s*slotSelection\.textContent = \"Emergency CR: choose any available 30-minute time, day or night\.\";",
               "", "JS: drop Emergency slotSelection overwrite")
    src = sub1(src,
               r"(slotTimeHelp\.textContent = \"Each tile represents[^\"]*\";)\s*"
               r"slotSelection\.textContent = \"No slot selected\.\";",
               lambda m: m.group(1), "JS: drop normal slotSelection overwrite")

    src = sub1(src,
               r"if \(canSelect\) \{.*?slotGrid\.appendChild\(tile\);",
               lambda m: JS_TILE_CLICK, "JS: tile click toggles booking", re.S)

    src = sub1(src,
               r"const selected = result\.slots\.find\(function \(slot\) \{\s*return slot\.start === selectedSlot;.*?"
               r"updateDateSlotSummary\(result\.slots \|\| \[\], Boolean\(result\.blocked\), selectedSlot\);\s*"
               r"\} catch \(error\) \{",
               lambda m: JS_AFTER_LOOP, "JS: replace single-slot post-loop block", re.S)

    src = sub1(src,
               r"(if \(iso === dateToISO\(todayLocal\)\) button\.classList\.add\(\"today\"\);)",
               lambda m: m.group(1) +
               '\n                    if (bookings.some(b => b.date === iso)) button.classList.add("has-booking");',
               "JS: calendar green dot")

    src = sub1(src,
               r"(selectedSlot = \"\";)(\s*renderMonthCalendar\(\);\s*updateCalendarLauncher\(\);\s*"
               r"loadSlots\(\);\s*clearRFID\(\);)",
               lambda m: m.group(1) + "\nbookings = []; syncBookings();" + m.group(2),
               "JS: reset bookings when CR number is edited")

    src = sub1(src,
               r"if \(\s*currentDate &&\s*!allowedCabDates\.includes\(currentDate\)\s*\) \{",
               lambda m: "bookings = bookings.filter(b => allowedCabDates.includes(b.date));\n"
                         "        syncBookings();\n\n"
                         "        if (currentDate && !allowedCabDates.includes(currentDate)) {",
               "JS: keep only bookings on valid CAB dates")

    src = sub1(src,
               r"(lookupFailed = true;\s*gitOptional = false; applyGitRequirement\(\);\s*allowedCabDates = \[\];)",
               lambda m: m.group(1) + "\n        bookings = []; syncBookings();",
               "JS: clear bookings when CR validation fails")

    src = sub1(src,
               r"(changeTypeInput\.addEventListener\(\"change\", function \(\) \{\s*selectedSlot = \"\";)",
               lambda m: m.group(1) + "\n                bookings = []; syncBookings();",
               "JS: clear bookings when Change Type changes")

    src = sub1(src,
               r"if \(crInput\.value\) \{\s*checkCRNumber\(\);\s*\}",
               lambda m: m.group(0) + "\n\n            syncBookings();",
               "JS: initial render of chips")

    src = sub1(src,
               r"if \(!dateInput\.value\) \{\s*calendarHelp\.textContent = \"Please choose a deployment date from the calendar\.\";"
               r".*?if \(!slotInput\.value\) \{.*?return;\s*\}",
               lambda m: JS_NO_BOOKINGS, "JS: submit requires >= 1 booking", re.S)

    src = sub1(src,
               r"if \(lookupFailed \|\| isChecking \|\| !allowedCabDates\.length\) \{.*?form\.submit\(\);",
               lambda m: JS_PRESUBMIT, "JS: re-verify all slots then submit", re.S)
    return src



def upgrade_email_loop(src):
    pattern = (r"# One email per booking.*?sent_all, last_msg = True, None\s*for d, t in bookings:.*?"
               r"sent_all, last_msg = False, msg\s*")
    found = list(re.finditer(pattern, src, re.S))
    if len(found) != 1:
        print("  --  per-booking email loop not found; leaving the POST handler unchanged")
        return src
    m = found[0]
    print("  ok  POST handler now sends one combined email")
    return src[:m.start()] + NEW_EMAIL_CALL + src[m.end():]


NEW_EMAIL_CALL = '''# ONE email listing every booked slot (mailer.py reads data["bookings"])
        mail_data = dict(data, bookings=[{"date": d.isoformat(), "slot": t.strftime("%H:%M")} for d, t in bookings])
        sent_all, last_msg = send_deployment_notification(mail_data, rfid)
        '''



# =====================================================================
# mailer.py  -> ONE email that lists every booked slot
# =====================================================================
def patch_mailer(src):
    print("mailer.py")
    if "schedule_rows" in src:
        print("  --  already supports multiple slots; skipped")
        return src

    block_a = ws('deployment_date = datetime.strptime(data["deployment_date"], "%Y-%m-%d").date() '
                 'slot_start = datetime.combine(deployment_date, datetime.strptime(data["slot_start"], "%H:%M").time()) '
                 'slot_end = slot_start + timedelta(minutes=30)')
    new_a = '''raw_bookings = data.get("bookings")
        if isinstance(raw_bookings, str):
            try:
                raw_bookings = json.loads(raw_bookings)
            except ValueError:
                raw_bookings = None
        slots = []
        if isinstance(raw_bookings, (list, tuple)) and raw_bookings:
            for item in raw_bookings:
                _d = datetime.strptime(str(item["date"]), "%Y-%m-%d").date()
                _start = datetime.combine(_d, datetime.strptime(str(item["slot"]), "%H:%M").time())
                slots.append((_start, _start + timedelta(minutes=30)))
        else:  # legacy single-slot call
            _d = datetime.strptime(data["deployment_date"], "%Y-%m-%d").date()
            _start = datetime.combine(_d, datetime.strptime(data["slot_start"], "%H:%M").time())
            slots.append((_start, _start + timedelta(minutes=30)))
        slots.sort()
        if len(slots) == 1:
            schedule_rows = [
                ("row", "Deployment Date", slots[0][0].strftime("%d-%m-%Y (%A)")),
                ("row", "Slot Timing", f"{slots[0][0]:%I:%M %p} - {slots[0][1]:%I:%M %p}"),
            ]
        else:
            schedule_rows = [("row", "Total Slots", str(len(slots)))]
            for number, (_s, _e) in enumerate(slots, 1):
                schedule_rows.append(
                    ("row", f"Slot {number}", f"{_s:%d-%m-%Y (%A)}  |  {_s:%I:%M %p} - {_e:%I:%M %p}"))'''
    src = sub1(src, block_a, lambda m: new_a, "mailer: parse bookings list")

    src = sub1(src,
               ws('("row", "Deployment Date", deployment_date.strftime("%d-%m-%Y (%A)")), '
                  '("row", "Slot Timing", f"{slot_start:%I:%M %p} - {slot_end:%I:%M %p}"),'),
               lambda m: "*schedule_rows,", "mailer: schedule rows")

    src = sub1(src,
               ws('subject = f"{application_name}-{requestor} scheduled the deployment - {cr_number}"'),
               lambda m: 'subject = f"{application_name}-{requestor} scheduled the deployment - {cr_number}"'
                         '\n        if len(slots) > 1:\n            subject += f" ({len(slots)} slots)"',
               "mailer: subject shows slot count")
    return src


# =====================================================================
# governance_tracker.html  -> popup workflow + 30-minute running alert
# =====================================================================
GOV_CSS = r'''
/* ===== Popup workflow (modals) ===== */
body.modal-lock{overflow:hidden}
.modal-backdrop{position:fixed;inset:0;z-index:2150;background:rgba(15,23,42,.55);backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);animation:fade .25s var(--ease) both}
#detailsCard:not(.hidden){position:fixed;top:3vh;left:0;right:0;margin:0 auto;width:min(1040px,calc(100% - 24px));max-height:94vh;overflow:auto;z-index:2200;box-shadow:0 30px 80px rgba(15,23,42,.38)}
.details-header .badge{margin-left:auto}
.modal-close{margin-left:10px;width:38px;height:38px;padding:0;border-radius:50%;font-size:22px;line-height:1;display:inline-flex;align-items:center;justify-content:center}
#detailsMessage.message{margin:14px 0 0}

.gov-overlay{position:fixed;inset:0;z-index:2500;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(15,23,42,.58);backdrop-filter:blur(5px);-webkit-backdrop-filter:blur(5px);animation:fade .2s var(--ease) both}
.gov-dialog{width:min(560px,100%);max-height:90vh;overflow:auto;padding:24px;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-top:5px solid var(--primary);border-radius:18px;box-shadow:0 30px 80px rgba(15,23,42,.4);animation:rise .3s var(--ease) both}
.gov-dialog.warn{border-top-color:#f0a030}.gov-dialog.ok{border-top-color:var(--ok)}.gov-dialog.bad{border-top-color:var(--bad)}
.gov-dialog h3{margin:0 0 6px;font-size:19px}
.gov-dialog-sub{margin:0 0 14px;color:var(--muted);font-size:13.5px}
.gov-dialog-status{min-height:18px;margin-top:8px;font-size:12.5px;color:var(--bad)}
.gov-dialog-actions{display:flex;flex-wrap:wrap;gap:10px;justify-content:flex-end;margin-top:18px}
.slot-pick-list{display:grid;gap:10px;max-height:52vh;overflow:auto;padding:2px}
.slot-pick-item{display:grid;gap:3px;width:100%;text-align:left;padding:12px 14px;color:var(--ink);background:var(--surface-2);border:1.5px solid var(--line);border-radius:12px;box-shadow:none}
.slot-pick-item:hover{background:var(--primary-soft);border-color:var(--primary);box-shadow:none;transform:translateY(-2px)}
.slot-pick-date{font-weight:800}
.slot-pick-time{color:var(--primary);font-weight:800}
.slot-pick-meta{font-size:12px;color:var(--muted);font-weight:500}
.slot-pick-today{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:999px;background:var(--ok-soft);color:var(--ok);font-size:10.5px;font-weight:800}
.long-run-clock{margin:8px 0;font-size:36px;font-weight:800;text-align:center;color:var(--bad);font-variant-numeric:tabular-nums}

@keyframes popCenter{from{opacity:0;transform:translate(-50%,-46%)}to{opacity:1;transform:translate(-50%,-50%)}}
#addAuditUserWrap:not(.hidden),#newAuditPasscodePanel:not(.hidden){position:fixed;z-index:2400;top:50%;left:50%;transform:translate(-50%,-50%);width:min(440px,calc(100% - 28px))!important;margin:0!important;padding:22px!important;background:var(--surface)!important;border:1px solid var(--line)!important;border-radius:16px!important;box-shadow:0 0 0 100vmax rgba(15,23,42,.55),0 30px 80px rgba(15,23,42,.4);animation:popCenter .25s var(--ease) both!important}
#newAuditPasscodePanel:not(.hidden){background:var(--warn-soft)!important}
#addAuditUserWrap:not(.hidden)::before{content:"Register a new email";display:block;margin-bottom:12px;font-size:18px;font-weight:800}
'''

GOV_CANCEL_BTN = ('\n      <button type="button" class="secondary" id="cancelAddAuditUser" '
                  'style="margin:10px 0 0 8px">Cancel</button>')

GOV_SHOWMESSAGE = '''function showMessage(text, type) {
    message.textContent = text;
    message.className = "message " + type;
    // The Find-by-CR card sits behind the popup, so mirror the message inside it.
    const modalMessage = document.getElementById("detailsMessage");
    if (modalMessage) {
      const open = !detailsCard.classList.contains("hidden");
      modalMessage.textContent = open ? text : "";
      modalMessage.className = open ? "message " + type : "message";
    }
  }'''

GOV_JS = r'''// =====================================================================
  // POPUP WORKFLOW
  //  1) opening a CR asks which slot is being deployed
  //  2) deployment details, add-email/passcode and results are popups
  //  3) a deployment timer running > 30 minutes raises a popup
  // =====================================================================
  const detailsBackdrop = document.getElementById("detailsBackdrop");
  function syncDetailsModal() {
    const open = !detailsCard.classList.contains("hidden");
    detailsBackdrop.classList.toggle("hidden", !open);
    document.body.classList.toggle("modal-lock", open);
  }
  new MutationObserver(syncDetailsModal).observe(detailsCard, { attributes: true, attributeFilter: ["class"] });
  syncDetailsModal();
  document.getElementById("closeDetailsButton").addEventListener("click", function () {
    detailsCard.classList.add("hidden");
  });

  document.getElementById("cancelAddAuditUser").addEventListener("click", function () {
    auditUserSelect.value = "";
    newAuditEmailInput.value = "";
    toggleAuditIdentityFields();
  });

  function makeEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // Generic popup. actions[].onClick(dialog, button) may return true to keep it open.
  function showDialog(options) {
    const overlay = makeEl("div", "gov-overlay");
    const box = makeEl("div", "gov-dialog" + (options.tone ? " " + options.tone : ""));
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.appendChild(makeEl("h3", "", options.title));
    if (options.subtitle) box.appendChild(makeEl("p", "gov-dialog-sub", options.subtitle));
    (options.body || []).forEach(function (node) { box.appendChild(node); });

    function onKey(event) {
      const overlays = document.querySelectorAll(".gov-overlay");
      if (event.key === "Escape" && options.dismissible !== false &&
          overlays[overlays.length - 1] === overlay) api.close();
    }
    const api = {
      close: function () {
        if (!overlay.isConnected) return;
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        if (options.onClose) options.onClose();
      }
    };

    const foot = makeEl("div", "gov-dialog-actions");
    (options.actions || []).forEach(function (action) {
      const button = makeEl("button", action.cls || "", action.label);
      button.type = "button";
      button.addEventListener("click", async function () {
        let keepOpen = false;
        if (action.onClick) keepOpen = await action.onClick(api, button);
        if (keepOpen !== true) api.close();
      });
      foot.appendChild(button);
    });
    box.appendChild(foot);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    const first = foot.querySelector("button");
    if (first && !(options.body || []).length) first.focus();
    return api;
  }

  // ---- 1) Slot picker shown when a CR is opened ----
  function openSlotPicker(crNumber, deployments, rowsContainer) {
    if (deployments.length === 1) {            // only one slot: nothing to choose
      selectDeployment(deployments[0], rowsContainer.children[0]);
      return;
    }
    const todayText = localDateString(new Date());
    const items = deployments.map(function (record, index) { return { record: record, index: index }; });
    items.sort(function (a, b) {
      return String(a.record.deployment_date || "").localeCompare(String(b.record.deployment_date || "")) ||
             String(a.record.slot_start || "").localeCompare(String(b.record.slot_start || ""));
    });

    const list = makeEl("div", "slot-pick-list");
    items.forEach(function (item) {
      const record = item.record;
      const dateIso = record.deployment_date ? String(record.deployment_date).substring(0, 10) : "";
      const button = makeEl("button", "slot-pick-item");
      button.type = "button";

      const dateLine = makeEl("div", "slot-pick-date", dateIso ? readableScheduleDate(dateIso) : "No date");
      if (dateIso === todayText) dateLine.appendChild(makeEl("span", "slot-pick-today", "Today"));
      const timeText = record.slot_start
        ? displayTime(record.slot_start) + (record.slot_end ? " \u2013 " + displayTime(record.slot_end) : "")
        : (record.slots || "Time not set");
      button.append(
        dateLine,
        makeEl("div", "slot-pick-time", timeText),
        makeEl("div", "slot-pick-meta",
          (record.application_name || "Application") + "  \u00B7  " + (record.status || "Pending") +
          "  \u00B7  RFID " + (record.rfid || "-"))
      );
      button.addEventListener("click", function () {
        dialog.close();
        selectDeployment(record, rowsContainer.children[item.index]);
      });
      list.appendChild(button);
    });

    const dialog = showDialog({
      title: "Which slot are you going to deploy?",
      subtitle: crNumber + " has " + deployments.length + " booked slots. Select one to open its details.",
      body: [list],
      actions: [{ label: "Cancel", cls: "secondary" }]
    });
  }

  // ---- 3) Deployment running for more than 30 minutes ----
  const LONG_RUN_SECONDS = 30 * 60;
  const LONG_RUN_REPEAT_MS = 15 * 60 * 1000;   // "Keep running" asks again after 15 minutes
  const LONG_RUN_POLL_MS = 30 * 1000;
  let longRunDialogOpen = false;

  function readSnoozes() {
    try { return JSON.parse(sessionStorage.getItem("govLongRunSnooze") || "{}"); } catch (_) { return {}; }
  }
  function writeSnoozes(map) {
    try { sessionStorage.setItem("govLongRunSnooze", JSON.stringify(map)); } catch (_) { /* ignore */ }
  }
  function snoozeKey(timer) { return String(timer.deployment_id) + "|" + String(timer.started_at || ""); }

  function showLongRunDialog(timer) {
    longRunDialogOpen = true;
    const baseSeconds = Number(timer.elapsed_seconds || 0);
    const startedAtMs = Date.now();
    const clock = makeEl("div", "long-run-clock", formatDuration(baseSeconds));
    const info = makeEl("p", "gov-dialog-sub",
      (timer.cr_number || "This CR") + "  \u00B7  " + (timer.application_name || "Application") +
      (timer.slots ? "  \u00B7  Slot " + timer.slots : ""));
    const status = makeEl("div", "gov-dialog-status", "");
    const tick = setInterval(function () {
      clock.textContent = formatDuration(baseSeconds + (Date.now() - startedAtMs) / 1000);
    }, 1000);
    const snooze = function () {
      const map = readSnoozes();
      map[snoozeKey(timer)] = Date.now() + LONG_RUN_REPEAT_MS;
      writeSnoozes(map);
    };

    showDialog({
      title: "Deployment running for more than 30 minutes",
      subtitle: "This CR has been running longer than its 30-minute slot. Stop the timer if the work is finished.",
      tone: "warn",
      dismissible: false,
      body: [info, clock, status],
      onClose: function () { clearInterval(tick); longRunDialogOpen = false; },
      actions: [
        { label: "Stop Timer", cls: "reject", onClick: async function (dialog, button) {
            button.disabled = true;
            status.textContent = "Stopping timer...";
            try {
              const response = await fetch("/api/deployment-running-timer/" + encodeURIComponent(timer.deployment_id), {
                method: "POST",
                headers: { "Content-Type": "application/json", Accept: "application/json" },
                body: JSON.stringify({ action: "stop" })
              });
              const result = await parseApiResponse(response);
              if (!result.success) throw new Error(result.message || "Could not stop the timer.");
              if (String(currentDeploymentId) === String(timer.deployment_id)) {
                runningTimerState = result.timer || {};
                renderRunningTimer();
              }
              showMessage("Timer stopped for " + (timer.cr_number || "the deployment") + ".", "success");
              return false;
            } catch (error) {
              button.disabled = false;
              status.textContent = error.message || "Could not stop the timer.";
              return true;
            }
        } },
        { label: "Open Record", cls: "secondary", onClick: async function (dialog, button) {
            button.disabled = true;
            status.textContent = "Opening record...";
            try {
              const response = await fetch("/api/deployment-detail/" + encodeURIComponent(timer.deployment_id), {
                headers: { Accept: "application/json" }, cache: "no-store"
              });
              const result = await parseApiResponse(response);
              if (!result.success || !result.current) throw new Error(result.message || "Could not open the record.");
              snooze();
              populateDetails(result.current);
              activateTrackerTab("running");
              return false;
            } catch (error) {
              button.disabled = false;
              status.textContent = error.message || "Could not open the record.";
              return true;
            }
        } },
        { label: "Keep Running (remind in 15 min)", onClick: function () { snooze(); } }
      ]
    });
  }

  async function checkLongRunning() {
    if (longRunDialogOpen) return;
    try {
      const response = await fetch("/api/running-status?only_running=true", {
        headers: { Accept: "application/json" }, cache: "no-store"
      });
      const result = await parseApiResponse(response);
      if (!result.success) return;
      const snoozes = readSnoozes();
      const now = Date.now();
      const due = (result.timers || []).find(function (timer) {
        return timer.is_running && Number(timer.elapsed_seconds || 0) >= LONG_RUN_SECONDS &&
               (snoozes[snoozeKey(timer)] || 0) <= now;
      });
      if (due) showLongRunDialog(due);
    } catch (error) {
      console.error("Long-running timer check failed:", error);
    }
  }
  setInterval(checkLongRunning, LONG_RUN_POLL_MS);
  checkLongRunning();

  '''

GOV_RESULT_DIALOG = '''showDialog({
        title: "Submitted",
        subtitle: successText,
        tone: "ok",
        dismissible: false,
        actions: [{ label: "OK", cls: "approve", onClick: function () { window.location.reload(); return true; } }]
      });
      return;'''


def patch_governance(src):
    print("governance_tracker.html")
    if 'id="detailsBackdrop"' in src:
        print("  --  popup workflow already applied; skipped")
        return src

    src = sub1(src, r"</style>", lambda m: GOV_CSS + "</style>", "CSS: popup styles")

    src = sub1(src, ws('<section id="detailsCard" class="card hidden">'),
               lambda m: '<div id="detailsBackdrop" class="modal-backdrop hidden"></div>\n  ' + m.group(0),
               "HTML: details backdrop")

    src = sub1(src, ws('<span id="resultBadge" class="badge">Record found</span>'),
               lambda m: m.group(0) + '\n      <button type="button" class="secondary modal-close" '
                                      'id="closeDetailsButton" aria-label="Close details" title="Close">&times;</button>',
               "HTML: details close button")

    src = sub1(src, ws('<div class="button-row">'),
               lambda m: '<div id="detailsMessage" class="message" role="status" aria-live="polite"></div>\n\n    ' + m.group(0),
               "HTML: message area inside popup")

    src = sub1(src,
               ws('<button type="button" id="registerAuditUserButton" style="margin-top:10px">'
                  'Add Email &amp; Generate Passcode</button>'),
               lambda m: m.group(0) + GOV_CANCEL_BTN, "HTML: cancel button for Add Email popup")

    src = sub1(src,
               r"function showMessage\(text, type\) \{\s*message\.textContent = text;\s*"
               r"message\.className = \"message \" \+ type;\s*\}",
               lambda m: GOV_SHOWMESSAGE, "JS: showMessage also writes inside the popup")

    src = sub1(src,
               ws('showMessage(deployments.length + " deployment request(s) found for " + crNumber + '
                  '". Select the request you want to open.", "success");'),
               lambda m: m.group(0) + "\n      openSlotPicker(crNumber, deployments, resultsBody);",
               "JS: ask which slot after fetching a CR")

    src = sub1(src, r"alert\(successText\);\s*window\.location\.reload\(\);\s*return;",
               lambda m: GOV_RESULT_DIALOG, "JS: submit result as popup")

    src = sub1(src, r"message\.scrollIntoView\(\{ behavior: \"smooth\", block: \"center\" \}\);",
               lambda m: '(document.getElementById("detailsMessage") || message).scrollIntoView('
                         '{ behavior: "smooth", block: "center" });',
               "JS: scroll to the in-popup error")

    src = sub1(src, r"saveChangesButton\.textContent = \"Save Changes\";",
               lambda m: 'saveChangesButton.textContent = "Submit";', "JS: restore the Submit label")

    src = sub1(src, r"(loadPendingRequests\(\);\s*loadDailySchedule\(\);\s*\}\);)",
               lambda m: GOV_JS + m.group(1), "JS: popup workflow + 30-minute alert")
    return src


def main():
    names = ["app.py", "templates/Release_Calender.html", "templates/governance_tracker.html", "mailer.py"]
    paths = [pathlib.Path(sys.argv[i + 1]) if len(sys.argv) > i + 1 else pathlib.Path(n)
             for i, n in enumerate(names)]
    for p in paths:
        if not p.exists():
            sys.exit(f"File not found: {p}\nUsage: python apply_all_patches.py "
                     "[app.py] [templates/Release_Calender.html] [templates/governance_tracker.html] [mailer.py]")
    app_p, tpl_p, gov_p, mail_p = paths
    outputs = [
        (app_p, patch_app(app_p.read_text(encoding="utf-8"))),
        (tpl_p, patch_tpl(tpl_p.read_text(encoding="utf-8"))),
        (gov_p, patch_governance(gov_p.read_text(encoding="utf-8"))),
        (mail_p, patch_mailer(mail_p.read_text(encoding="utf-8"))),
    ]
    for p, _ in outputs:
        shutil.copy2(p, str(p) + ".bak")
    for p, text in outputs:
        p.write_text(text, encoding="utf-8")
    print("\nDone. A .bak backup was written next to each changed file.")


if __name__ == "__main__":
    main()


