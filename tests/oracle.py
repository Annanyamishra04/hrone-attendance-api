"""Reference implementation of the Phase 4 analytics, written straight from the contract text (openapi.yaml, R5-R10)
with plain Python loops over raw documents and EXACT arithmetic (fractions). It shares no code with app/main.py (it
does not use half_up_div, the pipeline builders or the response shapers), so comparing the two catches mistakes in either.

It is the expected-value source for tests/test_analytics_oracle.py (the pipelines run through tests/mini_agg.py) and for
tests/test_mongo_integration_analytics.py (the pipelines run on a real MongoDB, when one is reachable)."""
import calendar
from datetime import date, timedelta
from fractions import Fraction
from math import floor

PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def half_up(x: Fraction, places: int) -> float:
    scale = 10 ** places
    return floor(x * scale + Fraction(1, 2)) / scale


def d(s: str) -> date:
    return date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def month_days(month: str):
    y, m = int(month[:4]), int(month[5:7])
    return [date(y, m, i) for i in range(1, calendar.monthrange(y, m)[1] + 1)]


def in_month(rec, month):
    return rec["date"][:7] == month


def present_value(rec) -> Fraction:
    """1 for a present record, 1/2 for a present half day, 0 otherwise (R5, R6)."""
    if rec["status"] not in PRESENT:
        return Fraction(0)
    return Fraction(1, 2) if rec.get("half_day") else Fraction(1)


def is_weekday(rec) -> bool:
    return d(rec["date"]).weekday() < 5


def late(rec) -> int:
    return rec.get("late_minutes") or 0


def monthly(emp, logs, month):
    days = month_days(month)
    start = max(days[0], d(emp["joined_on"]))
    working = sum(1 for day in days if day >= start and day.weekday() < 5)
    recs = [r for r in logs if r["emp_code"] == emp["emp_code"] and in_month(r, month)]
    present = sum((present_value(r) for r in recs if is_weekday(r)), Fraction(0))
    return {
        "emp_code": emp["emp_code"], "month": month, "working_days": working, "present_days": float(present),
        "leave_days": sum(1 for r in recs if r["status"] == "LEAVE"),
        "late_count": sum(1 for r in recs if late(r) > 0),
        "total_late_minutes": sum(late(r) for r in recs if late(r) > 0),
        "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in recs),
        "attendance_pct": None if working == 0 else half_up(present / working * 100, 2),
    }


def department_summary(employees, logs, month, department=None):
    last = month_days(month)[-1].isoformat()
    by_dept = {}
    for e in employees:
        if e["joined_on"] <= last and (department is None or e["department"] == department):
            by_dept.setdefault(e["department"], []).append(e)
    items = []
    for dep in sorted(by_dept):
        codes = {e["emp_code"] for e in by_dept[dep]}
        recs = [r for r in logs if r["emp_code"] in codes and in_month(r, month)]
        hours = [Fraction(str(r["work_hours"])) for r in recs if r["status"] in PRESENT and r.get("work_hours") is not None]
        items.append({
            "department": dep, "headcount": len(by_dept[dep]),
            "present_days": float(sum((present_value(r) for r in recs if is_weekday(r)), Fraction(0))),
            "avg_work_hours": half_up(sum(hours, Fraction(0)) / len(hours), 2) if hours else None,
            "late_count": sum(1 for r in recs if late(r) > 0),
            "total_late_minutes": sum(late(r) for r in recs if late(r) > 0),
            "leave_count": sum(1 for r in recs if r["status"] == "LEAVE"),
            "on_duty_count": sum(1 for r in recs if r["status"] == "ON_DUTY"),
        })
    return {"month": month, "items": items}


def leaderboard(employees, logs, month, limit=10, department=None):
    known = {e["emp_code"]: e for e in employees}
    totals = {}
    for r in logs:
        if in_month(r, month) and late(r) > 0 and r["emp_code"] in known:
            e = known[r["emp_code"]]
            if department is None or e["department"] == department:
                t = totals.setdefault(r["emp_code"], [0, 0])
                t[0] += late(r)
                t[1] += 1
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
    rows = []
    for code, (minutes, count) in ordered:
        rank = 1 + sum(1 for _, (m2, _c) in ordered if m2 > minutes)        # standard competition ranking
        if rank <= limit:
            e = known[code]
            rows.append({"rank": rank, "emp_code": code, "name": e["name"], "department": e["department"],
                         "total_late_minutes": minutes, "late_count": count})
    return {"month": month, "items": rows}


def trend(employees, logs, department, frm, to):
    members = [e for e in employees if e["department"] == department]
    codes = {e["emp_code"] for e in members}
    start, end = d(frm), d(to)
    rows, rates = [], []
    day = start
    while day <= end:
        iso = day.isoformat()
        head = sum(1 for e in members if e["joined_on"] <= iso)
        recs = [r for r in logs if r["emp_code"] in codes and r["date"] == iso]
        present = sum((present_value(r) for r in recs), Fraction(0))
        working = day.weekday() < 5
        rate = half_up(present / head, 4) if working and head > 0 else None
        rates.append(rate)
        window = [Fraction(str(x)) for x in rates[-7:] if x is not None]
        rows.append({
            "date": iso, "is_working_day": working, "headcount": head, "present_count": float(present),
            "late_count": sum(1 for r in recs if late(r) > 0), "attendance_rate": rate,
            "moving_avg_7d": half_up(sum(window, Fraction(0)) / len(window), 4) if window else None})
        day += timedelta(days=1)
    return {"department": department, "items": rows}
