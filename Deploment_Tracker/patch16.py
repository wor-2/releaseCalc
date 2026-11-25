#!/usr/bin/env python3
"""Usage: python apply_bar_chart.py path/to/manager_dashboard.html

Replaces drawActivity() (line/area chart) with a rounded gradient bar chart.
A backup is saved next to the file as <name>.bak.
"""
import shutil
import sys

NEW_FUNC = r"""function drawActivity() {
    const canvas = document.getElementById('activityChart');
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const width = rect.width, height = rect.height;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const pad = { left: 32, right: 16, top: 22, bottom: 48 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    const baseY = pad.top + plotHeight;

    const days = [];
    for (let i = 0; i < 7; i++) {
        const date = addDays(selectedWeekStart, i);
        days.push({
            date,
            label: date.toLocaleDateString(undefined, { weekday: 'short' }),
            dateLabel: date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' }),
            count: 0
        });
    }
    recordRows.forEach(row => {
        const date = parseDate(row.dataset.date);
        if (!date) return;
        const day = days.find(item => dateKey(item.date) === dateKey(date));
        if (day) day.count++;
    });

    const maxCount = Math.max(4, ...days.map(day => day.count));

    ctx.font = '10px Segoe UI, Arial';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
        const value = Math.ceil(maxCount * i / 4);
        const y = baseY - (i / 4) * plotHeight;
        ctx.strokeStyle = '#edf1f8'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
        ctx.fillStyle = '#98a4b8'; ctx.fillText(String(value), pad.left - 9, y);
    }

    const slotWidth = plotWidth / days.length;
    const barWidth = Math.max(14, Math.min(46, slotWidth * 0.5));
    const todayKey = dateKey(new Date());

    const roundedTop = (x, y, w, h, r) => {
        r = Math.min(r, w / 2, h);
        ctx.beginPath();
        ctx.moveTo(x, y + h);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h);
        ctx.closePath();
    };

    days.forEach((day, i) => {
        const cx = pad.left + slotWidth * i + slotWidth / 2;
        const x = cx - barWidth / 2;
        const barHeight = day.count > 0
            ? Math.max(6, (day.count / maxCount) * (plotHeight - 8))
            : 0;
        const y = baseY - barHeight;
        const isToday = dateKey(day.date) === todayKey;

        if (barHeight > 0) {
            const grad = ctx.createLinearGradient(0, y, 0, baseY);
            grad.addColorStop(0, '#6f86ff');
            grad.addColorStop(1, '#4057dc');
            ctx.save();
            ctx.shadowColor = 'rgba(79,103,246,.28)';
            ctx.shadowBlur = 8;
            ctx.shadowOffsetY = 3;
            ctx.fillStyle = grad;
            roundedTop(x, y, barWidth, barHeight, 7);
            ctx.fill();
            ctx.restore();

            ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
            ctx.font = 'bold 11px Segoe UI, Arial'; ctx.fillStyle = '#33436a';
            ctx.fillText(String(day.count), cx, y - 5);
        } else {
            ctx.fillStyle = '#e3e8f5';
            roundedTop(x, baseY - 3, barWidth, 3, 2);
            ctx.fill();
        }

        ctx.textAlign = 'center'; ctx.textBaseline = 'top';
        ctx.font = (isToday ? 'bold ' : '') + '10px Segoe UI, Arial';
        ctx.fillStyle = isToday ? '#4057dc' : '#78859c';
        ctx.fillText(day.label, cx, baseY + 10);
        ctx.font = '9px Segoe UI, Arial'; ctx.fillStyle = '#a0aabd';
        ctx.fillText(day.dateLabel, cx, baseY + 24);
    });

    updateWeekLabel();
}

"""

START = "function drawActivity() {"
END = "function changeSelectedWeek("


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()

    s = text.find(START)
    e = text.find(END)
    if s == -1 or e == -1 or e < s:
        sys.exit("Could not find drawActivity() / changeSelectedWeek() in the file.")

    shutil.copyfile(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text[:s] + NEW_FUNC + text[e:])
    print("Done. Bar chart applied. Backup saved as " + path + ".bak")


if __name__ == "__main__":
    main()