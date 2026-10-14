"""Seeded random employees and attendance records (stored shape, written straight into a database), with the awkward
cases the rules describe: employees with no logs, mid-month and later joiners, weekend records, half days, legacy records
without the derived fields, ABSENT / LEAVE / ON_DUTY rows, late-minute ties, logs of employees that do not exist."""
import random
from datetime import date, datetime, timedelta, timezone

DEPARTMENTS = ["Engineering", "Sales", "Ops", "HR", "Empty Dept"]


def make_dataset(seed=1, month_start=date(2026, 7, 1), months=2, employees=24):
    rng = random.Random(seed)
    emps = []
    for i in range(1, employees + 1):
        dept = DEPARTMENTS[i % len(DEPARTMENTS)] if i % 7 else "Engineering"
        joined = rng.choice([date(2026, 1, 5), date(2026, 6, 20), date(2026, 7, 1), date(2026, 7, 13),
                             date(2026, 7, 29), date(2026, 8, 10), date(2026, 9, 15), date(2027, 1, 4)])
        emps.append({"emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com",
                     "department": dept, "shift_start": "09:30", "shift_end": "18:30",
                     "joined_on": joined.isoformat(), "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    logs = []
    end = month_start + timedelta(days=31 * months)
    for e in emps:
        if rng.random() < 0.15:
            continue                                             # an employee with no logs at all
        day = max(month_start, date.fromisoformat(e["joined_on"]))   # nobody works before joining
        while day < end:
            if rng.random() < 0.8 and (day.weekday() < 5 or rng.random() < 0.25):
                status = rng.choices(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"], [60, 12, 6, 8, 8])[0]
                rec = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": status, "history": []}
                if status in ("PRESENT", "WFH", "ON_DUTY"):
                    pin = datetime(day.year, day.month, day.day, 4, 0, tzinfo=timezone.utc)
                    rec["punch_in"] = pin
                    rec["late_minutes"] = rng.choice([0, 0, 0, 11, 15, 15, 30, 45, 45, 60])
                    if rng.random() < 0.85:
                        rec["punch_out"] = pin + timedelta(hours=9)
                        rec["work_hours"] = rng.choice([1.15, 3.25, 4.35, 4.49, 4.5, 4.51, 6.45, 7.99, 8.0, 8.2, 8.5, 9.01, 9.76])
                        rec["half_day"] = rec["work_hours"] < 4.5
                        rec["overtime_minutes"] = rng.choice([0, 0, 30, 45, 90])
                    else:
                        rec["punch_out"], rec["work_hours"], rec["overtime_minutes"], rec["half_day"] = None, None, 0, False
                    if rng.random() < 0.05:                      # legacy document: derived fields omitted
                        for k in ("late_minutes", "overtime_minutes", "half_day", "history"):
                            rec.pop(k, None)
                else:
                    rec.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0,
                               half_day=False)
                logs.append(rec)
            day += timedelta(days=1)
    for k in range(3):                                           # logs whose employee does not exist
        logs.append({"emp_code": f"EMP9{k:03d}", "date": month_start.isoformat(), "status": "PRESENT",
                     "punch_in": datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc), "punch_out": None,
                     "work_hours": None, "late_minutes": 90, "overtime_minutes": 0, "half_day": False, "history": []})
    return emps, logs
