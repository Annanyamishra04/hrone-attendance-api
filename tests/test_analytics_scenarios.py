"""Hand-computed expectations for the analytics endpoints (not derived from the oracle or from the app's own helpers).
Pipelines are executed by tests/mini_agg.py; employee_monthly would also run on mongomock itself (see
test_analytics_oracle.py). Every expected number below is worked out in a comment."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def api(mini_engine):
    return mini_engine, TestClient(main.app)


def emp(db, code, dept="Eng", joined="2026-01-05", name=None):
    db.employees.insert_one({"emp_code": code, "name": name or f"N {code}", "email": "a@b.co", "department": dept,
                             "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
                             "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)})


def log(db, code, date, status="PRESENT", late=0, ot=0, hours=None, half=False):
    db.attendance_logs.insert_one({
        "emp_code": code, "date": date, "status": status, "history": [],
        "punch_in": None if status in ("ABSENT", "LEAVE") else datetime(2026, 7, 1, 4, tzinfo=timezone.utc),
        "punch_out": None, "work_hours": hours, "late_minutes": late, "overtime_minutes": ot, "half_day": half})


def monthly(client, code, month="2026-07"):
    r = client.get(f"/analytics/employees/{code}/monthly", params={"month": month})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# employee monthly
# --------------------------------------------------------------------------- #
def test_monthly_counts_present_half_leave_late_and_overtime(api):
    db, c = api
    emp(db, "EMP0001")                                                  # joined long ago: July 2026 has 23 weekdays
    log(db, "EMP0001", "2026-07-01", late=15, ot=40, hours=9.5)         # Wed  present 1
    log(db, "EMP0001", "2026-07-02", "WFH", hours=8.0)                  # Thu  present 1
    log(db, "EMP0001", "2026-07-03", "ON_DUTY")                         # Fri  present 1 (open record, no hours)
    log(db, "EMP0001", "2026-07-06", late=11, hours=3.0, half=True)     # Mon  half day 0.5
    log(db, "EMP0001", "2026-07-07", "ABSENT")                          # Tue  0
    log(db, "EMP0001", "2026-07-08", "LEAVE")                           # Wed  leave 1
    log(db, "EMP0001", "2026-07-11", late=30, ot=60, hours=8.0)         # Sat  NOT present, but late and overtime count
    log(db, "EMP0001", "2026-07-12", "LEAVE")                           # Sun  a LEAVE record is a LEAVE record
    # present = 1 + 1 + 1 + 0.5 = 3.5 ; late = 15 + 11 + 30 = 56 over 3 records ; overtime = 40 + 60
    assert monthly(c, "EMP0001") == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 3.5, "leave_days": 2,
        "late_count": 3, "total_late_minutes": 56, "total_overtime_minutes": 100,
        "attendance_pct": 15.22}                                        # 3.5 / 23 * 100 = 15.2173... -> 15.22


def test_monthly_for_an_employee_with_no_records_is_zero_not_missing(api):
    db, c = api
    emp(db, "EMP0001")
    assert monthly(c, "EMP0001") == {
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 0.0, "leave_days": 0,
        "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0}


@pytest.mark.parametrize("joined,month,working_days", [
    ("2026-07-13", "2026-07", 15),     # Mon 13th: 13-17, 20-24, 27-31
    ("2026-07-11", "2026-07", 15),     # joined on a Saturday: counting starts Monday
    ("2026-07-31", "2026-07", 1),      # joined on the last day (a Friday)
    ("2026-07-01", "2026-07", 23),     # first day of the month
    ("2026-08-01", "2026-07", 0),      # joins after the month: no working days
    ("2026-06-30", "2026-07", 23),     # joined the day before
    ("2028-02-10", "2028-02", 14),     # leap-year February: Thu 10th: 10,11 + 14-18 + 21-25 + 28,29 = 2+5+5+2
    ("2027-03-01", "2026-12", 0),
])
def test_working_days_are_counted_from_joined_on(api, joined, month, working_days):
    db, c = api
    emp(db, "EMP0001", joined=joined)
    body = monthly(c, "EMP0001", month)
    assert body["working_days"] == working_days
    if working_days == 0:
        assert body["attendance_pct"] is None                           # null, never a division by zero
    else:
        assert body["attendance_pct"] == 0.0


def test_attendance_pct_rounds_half_up_not_to_even(api):
    db, c = api
    emp(db, "EMP0001", joined="2026-07-10")                             # Fri 10th: 10, 13-17, 20-24, 27-31 = 16 working days
    log(db, "EMP0001", "2026-07-10", late=0, hours=3.0, half=True)      # 0.5 day present
    assert monthly(c, "EMP0001")["working_days"] == 16
    assert monthly(c, "EMP0001")["attendance_pct"] == 3.13              # 0.5 / 16 * 100 = 3.125 exactly: half-up -> 3.13 (even would give 3.12)


def test_monthly_only_counts_that_employee_and_that_month(api):
    db, c = api
    emp(db, "EMP0001"); emp(db, "EMP0002")
    log(db, "EMP0001", "2026-06-30", late=99)                           # previous month
    log(db, "EMP0001", "2026-08-01", late=99)                           # next month
    log(db, "EMP0002", "2026-07-01", late=99)                           # somebody else
    log(db, "EMP0001", "2026-07-01", late=20)
    body = monthly(c, "EMP0001")
    assert (body["late_count"], body["total_late_minutes"], body["present_days"]) == (1, 20, 1.0)


def test_monthly_treats_missing_derived_fields_as_zero(api):
    db, c = api
    emp(db, "EMP0001")
    db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT"})   # legacy document
    body = monthly(c, "EMP0001")
    assert (body["present_days"], body["late_count"], body["total_late_minutes"], body["total_overtime_minutes"]) == (1.0, 0, 0, 0)


def test_monthly_unknown_employee_is_404_even_if_logs_exist(api):
    db, c = api
    log(db, "EMP0009", "2026-07-01", late=5)
    r = c.get("/analytics/employees/EMP0009/monthly", params={"month": "2026-07"})
    assert r.status_code == 404 and r.json() == {"detail": "unknown employee"}


# --------------------------------------------------------------------------- #
# department summary
# --------------------------------------------------------------------------- #
def summary(client, **params):
    r = client.get("/analytics/departments/summary", params={"month": "2026-07", **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_headcount_includes_people_without_logs_and_excludes_later_joiners(api):
    db, c = api
    emp(db, "EMP0001")                                    # has logs
    emp(db, "EMP0002")                                    # no logs at all: still counts
    emp(db, "EMP0003", joined="2026-07-31")               # joins on the last day: counts (R9: on or before)
    emp(db, "EMP0004", joined="2026-08-01")               # joins the day after: does not count
    log(db, "EMP0001", "2026-07-01", hours=8.0)
    body = summary(c)
    assert [(i["department"], i["headcount"]) for i in body["items"]] == [("Eng", 3)]


def test_a_department_with_headcount_zero_is_omitted(api):
    db, c = api
    emp(db, "EMP0001", dept="Late Starters", joined="2026-09-01")
    emp(db, "EMP0002", dept="Eng")
    assert [i["department"] for i in summary(c)["items"]] == ["Eng"]


def test_average_work_hours_is_over_records_not_an_average_of_averages(api):
    db, c = api
    emp(db, "EMP0001"); emp(db, "EMP0002")
    log(db, "EMP0001", "2026-07-01", hours=8.0); log(db, "EMP0001", "2026-07-02", hours=8.0)
    log(db, "EMP0002", "2026-07-01", hours=2.0)
    item = summary(c)["items"][0]
    assert item["avg_work_hours"] == 6.0                  # (8 + 8 + 2) / 3 ; an average of averages would be 5.0


def test_average_work_hours_rounds_half_up_and_skips_open_and_non_present_records(api):
    db, c = api
    emp(db, "EMP0001")
    log(db, "EMP0001", "2026-07-01", hours=4.0)
    log(db, "EMP0001", "2026-07-02", hours=4.01)          # mean 4.005 exactly -> 4.01 (half-even would give 4.0)
    log(db, "EMP0001", "2026-07-03")                      # present but no punch-out: no work_hours, not in the mean
    log(db, "EMP0001", "2026-07-06", "ABSENT", hours=9.0) # not a presence status: excluded even if it carries hours
    assert summary(c)["items"][0]["avg_work_hours"] == 4.01


def test_average_work_hours_is_null_without_any_hours(api):
    db, c = api
    emp(db, "EMP0001"); log(db, "EMP0001", "2026-07-01")
    assert summary(c)["items"][0]["avg_work_hours"] is None


def test_summary_counts_by_status_and_weekday_rule(api):
    db, c = api
    emp(db, "EMP0001"); emp(db, "EMP0002")
    log(db, "EMP0001", "2026-07-01", late=20, hours=8.0)            # Wed present
    log(db, "EMP0001", "2026-07-02", "ON_DUTY")                     # Thu present, on duty
    log(db, "EMP0002", "2026-07-03", late=5, hours=3.0, half=True)  # Fri half day
    log(db, "EMP0002", "2026-07-04", "ON_DUTY", late=40)            # Sat: on duty counted, present_days NOT, late counted
    log(db, "EMP0002", "2026-07-06", "LEAVE")
    log(db, "EMP0002", "2026-07-07", "ABSENT")
    assert summary(c) == {"month": "2026-07", "items": [{
        "department": "Eng", "headcount": 2, "present_days": 2.5, "avg_work_hours": 5.5,   # (8 + 3) / 2
        "late_count": 3, "total_late_minutes": 65, "leave_count": 1, "on_duty_count": 2}]}


def test_summary_is_sorted_by_department_and_can_be_restricted(api):
    db, c = api
    for i, dept in enumerate(["Sales", "Eng", "HR", "eng"], start=1):    # department is case-sensitive
        emp(db, f"EMP000{i}", dept=dept)
    assert [i["department"] for i in summary(c)["items"]] == ["Eng", "HR", "Sales", "eng"]
    assert [(i["department"], i["headcount"]) for i in summary(c, department="Eng")["items"]] == [("Eng", 1)]
    assert summary(c, department="Nope")["items"] == []


def test_summary_ignores_logs_of_unknown_employees_and_other_months(api):
    db, c = api
    emp(db, "EMP0001")
    log(db, "EMP0099", "2026-07-01", late=500, hours=9.0)
    log(db, "EMP0001", "2026-08-03", late=500, hours=9.0)
    item = summary(c)["items"][0]
    assert (item["late_count"], item["present_days"], item["avg_work_hours"]) == (0, 0.0, None)


def test_summary_of_an_empty_database_is_an_empty_list(api):
    assert summary(api[1]) == {"month": "2026-07", "items": []}


# --------------------------------------------------------------------------- #
# late leaderboard
# --------------------------------------------------------------------------- #
def board(client, **params):
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def ties_world(db):
    """Totals: EMP0001 100, EMP0002 60, EMP0003 60, EMP0004 60, EMP0005 20  -> ranks 1, 2, 2, 2, 5  (standard competition)"""
    for i in range(1, 7):
        emp(db, f"EMP000{i}", dept="Eng" if i % 2 else "Sales")
    log(db, "EMP0001", "2026-07-01", late=60); log(db, "EMP0001", "2026-07-04", late=40)   # a Saturday counts
    for code in ("EMP0004", "EMP0003", "EMP0002"):
        log(db, code, "2026-07-02", late=60)
    log(db, "EMP0005", "2026-07-02", late=20)
    log(db, "EMP0006", "2026-07-02", late=0)                                              # total 0: never listed


def test_ties_share_a_rank_and_the_next_rank_is_skipped(api):
    db, c = api
    ties_world(db)
    assert [(r["rank"], r["emp_code"], r["total_late_minutes"]) for r in board(c, limit=50)] == [
        (1, "EMP0001", 100), (2, "EMP0002", 60), (2, "EMP0003", 60), (2, "EMP0004", 60), (5, "EMP0005", 20)]


@pytest.mark.parametrize("limit,expected", [
    (1, ["EMP0001"]),
    (2, ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]),     # limit applies AFTER ranking: all three rank-2 rows stay
    (3, ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]),     # nobody has rank 3 or 4
    (4, ["EMP0001", "EMP0002", "EMP0003", "EMP0004"]),
    (5, ["EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005"]),
])
def test_limit_is_applied_after_ranking(api, limit, expected):
    db, c = api
    ties_world(db)
    assert [r["emp_code"] for r in board(c, limit=limit)] == expected


def test_default_limit_is_10(api):
    db, c = api
    for i in range(1, 16):
        emp(db, f"EMP{i:04d}"); log(db, f"EMP{i:04d}", "2026-07-01", late=100 + i)    # 15 distinct totals
    rows = board(c)
    assert len(rows) == 10 and [r["rank"] for r in rows] == list(range(1, 11))
    assert rows[0]["emp_code"] == "EMP0015"                                           # highest total first


def test_the_ranking_example_from_the_contract_1_2_2_4(api):
    db, c = api
    for i, total in enumerate([50, 30, 30, 10], start=1):
        emp(db, f"EMP000{i}"); log(db, f"EMP000{i}", "2026-07-01", late=total)
    assert [r["rank"] for r in board(c, limit=10)] == [1, 2, 2, 4]


def test_department_filter_ranks_inside_the_department_only(api):
    db, c = api
    ties_world(db)                                     # Eng: EMP0001 100, EMP0003 60, EMP0005 20 ; Sales: EMP0002 60, EMP0004 60
    assert [(r["rank"], r["emp_code"]) for r in board(c, limit=50, department="Eng")] == [(1, "EMP0001"), (2, "EMP0003"), (3, "EMP0005")]
    assert [(r["rank"], r["emp_code"]) for r in board(c, limit=50, department="Sales")] == [(1, "EMP0002"), (1, "EMP0004")]
    assert board(c, department="Nope") == []


def test_rows_carry_name_department_and_late_count(api):
    db, c = api
    emp(db, "EMP0001", dept="Sales", name="Asha Rao")
    log(db, "EMP0001", "2026-07-01", late=30); log(db, "EMP0001", "2026-07-02", late=15); log(db, "EMP0001", "2026-07-03", late=0)
    assert board(c) == [{"rank": 1, "emp_code": "EMP0001", "name": "Asha Rao", "department": "Sales",
                         "total_late_minutes": 45, "late_count": 2}]


def test_logs_of_unknown_employees_and_other_months_are_ignored(api):
    db, c = api
    emp(db, "EMP0001")
    log(db, "EMP0099", "2026-07-01", late=500)         # no such employee: ignored, and it must not take rank 1
    log(db, "EMP0001", "2026-06-30", late=500)
    log(db, "EMP0001", "2026-07-01", late=11)
    assert [(r["rank"], r["emp_code"]) for r in board(c)] == [(1, "EMP0001")]


def test_empty_leaderboard(api):
    assert board(api[1]) == []


# --------------------------------------------------------------------------- #
# department trend
# --------------------------------------------------------------------------- #
def trend(client, dept="Eng", frm="2026-07-06", to="2026-07-12"):
    r = client.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to})
    assert r.status_code == 200, r.text
    return r.json()


def week_world(db):
    emp(db, "EMP0001", joined="2026-06-01")
    emp(db, "EMP0002", joined="2026-07-08")            # joins on Wednesday of the week
    emp(db, "EMP0003", joined="2026-07-20")            # joins after the week: never counted in it
    emp(db, "EMP0009", dept="Sales", joined="2026-01-01")
    log(db, "EMP0001", "2026-07-03")                                   # BEFORE the range: must not enter the moving average
    log(db, "EMP0001", "2026-07-06")                                   # Mon present
    log(db, "EMP0001", "2026-07-08", "WFH"); log(db, "EMP0002", "2026-07-08", late=40, hours=3.0, half=True)    # Wed 1 + 0.5
    log(db, "EMP0001", "2026-07-09", "ABSENT"); log(db, "EMP0002", "2026-07-09", "LEAVE")                       # Thu nobody present
    log(db, "EMP0001", "2026-07-10")                                   # Fri
    log(db, "EMP0001", "2026-07-11", late=20)                          # Sat: counts as present_count and late_count, no rate
    log(db, "EMP0009", "2026-07-06"); log(db, "EMP0009", "2026-07-07", late=99)   # another department: ignored


def test_trend_has_one_row_per_day_with_gap_filling_and_a_moving_average(api):
    db, c = api
    week_world(db)
    rows = trend(c)["items"]
    assert [r["date"] for r in rows] == [f"2026-07-{d:02d}" for d in range(6, 13)]
    # Per day (Mon 6 .. Sun 12): rates 1.0, 0.0, 0.75, 0.0, 0.5, null, null; moving averages 1.0, 0.5, 1.75/3, 1.75/4, 2.25/5, same, same.
    # Fri: EMP0001 present (1.0) of headcount 2 -> present_count 1.0, rate 0.5 ; moving avg (1 + 0 + .75 + 0 + .5) / 5 = 0.45
    assert rows[4] == {"date": "2026-07-10", "is_working_day": True, "headcount": 2, "present_count": 1.0,
                       "late_count": 0, "attendance_rate": 0.5, "moving_avg_7d": 0.45}
    # Sat: weekend record counts for present_count / late_count, rate and is_working_day follow the weekday rule;
    # the moving average still sees the last non-null rates (window = Mon..Sat -> same five rates -> 0.45)
    assert rows[5] == {"date": "2026-07-11", "is_working_day": False, "headcount": 2, "present_count": 1.0,
                       "late_count": 1, "attendance_rate": None, "moving_avg_7d": 0.45}
    assert rows[6] == {"date": "2026-07-12", "is_working_day": False, "headcount": 2, "present_count": 0.0,
                       "late_count": 0, "attendance_rate": None, "moving_avg_7d": 0.45}
    assert rows[0] == {"date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 1.0,
                       "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0}   # 07-03 is before `from`: not used
    assert rows[1]["attendance_rate"] == 0.0 and rows[1]["moving_avg_7d"] == 0.5
    assert rows[2] == {"date": "2026-07-08", "is_working_day": True, "headcount": 2, "present_count": 1.5,
                       "late_count": 1, "attendance_rate": 0.75, "moving_avg_7d": 0.5833}
    assert rows[3]["moving_avg_7d"] == 0.4375 and rows[3]["present_count"] == 0.0


def test_moving_average_uses_at_most_the_current_and_six_previous_rows(api):
    db, c = api
    emp(db, "EMP0001", joined="2026-06-01")
    log(db, "EMP0001", "2026-07-06")                               # Mon present -> 1.0 ; every other weekday 0.0
    rows = trend(c, frm="2026-07-06", to="2026-07-17")["items"]    # Mon 6 .. Fri 17
    rates = [r["attendance_rate"] for r in rows]
    assert rates == [1.0, 0.0, 0.0, 0.0, 0.0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0]
    # Row 7 (Mon 13) sees rows 1..7: Monday the 6th has left the window -> mean of six zeros
    assert [r["moving_avg_7d"] for r in rows][:7] == [1.0, 0.5, 0.3333, 0.25, 0.2, 0.2, 0.2]
    assert [r["moving_avg_7d"] for r in rows][7:] == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_the_moving_average_is_null_while_the_window_has_no_rate(api):
    db, c = api
    emp(db, "EMP0001", joined="2026-06-01")
    rows = trend(c, frm="2026-07-04", to="2026-07-06")["items"]     # Sat, Sun, Mon
    assert [(r["attendance_rate"], r["moving_avg_7d"]) for r in rows] == [(None, None), (None, None), (0.0, 0.0)]


def test_a_working_day_with_no_employee_yet_has_a_null_rate(api):
    db, c = api
    emp(db, "EMP0001", joined="2026-07-08")
    rows = trend(c)["items"]
    assert [(r["headcount"], r["attendance_rate"]) for r in rows] == [
        (0, None), (0, None), (1, 0.0), (1, 0.0), (1, 0.0), (1, None), (1, None)]
    assert [r["moving_avg_7d"] for r in rows][:2] == [None, None]


def test_department_whose_employees_all_join_after_the_range_still_returns_every_day(api):
    db, c = api
    emp(db, "EMP0001", joined="2026-09-01")
    rows = trend(c)["items"]
    assert len(rows) == 7 and all(r["headcount"] == 0 and r["attendance_rate"] is None and r["moving_avg_7d"] is None
                                  and r["present_count"] == 0.0 for r in rows)


def test_rate_is_rounded_half_up_to_four_decimals(api):
    db, c = api
    for i in range(1, 33):
        emp(db, f"EMP{i:04d}", joined="2026-01-01")
    log(db, "EMP0001", "2026-07-06")                                 # 1 / 32 = 0.03125 exactly -> 0.0313 (half-even would give 0.0312)
    row = trend(c, frm="2026-07-06", to="2026-07-06")["items"][0]
    assert (row["headcount"], row["attendance_rate"], row["moving_avg_7d"]) == (32, 0.0313, 0.0313)


def test_range_limits(api):
    db, c = api
    emp(db, "EMP0001")
    assert len(trend(c, frm="2026-07-01", to="2026-07-01")["items"]) == 1          # a single day
    assert len(trend(c, frm="2026-07-01", to="2026-09-30")["items"]) == 92         # exactly 92 days: allowed
    for frm, to in [("2026-07-01", "2026-10-01"), ("2026-07-02", "2026-07-01")]:    # 93 days ; to < from
        r = c.get("/analytics/departments/Eng/trend", params={"from": frm, "to": to})
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_unknown_department_is_404_even_for_an_invalid_looking_name(api):
    db, c = api
    emp(db, "EMP0001")
    for name in ("Nope", "eng", "Eng ", "%24where"):
        r = c.get(f"/analytics/departments/{name}/trend", params={"from": "2026-07-01", "to": "2026-07-02"})
        assert r.status_code == 404 and r.json() == {"detail": "unknown department"}, name


def test_trend_across_a_month_boundary_and_a_leap_day(api):
    db, c = api
    emp(db, "EMP0001", joined="2028-01-01")
    log(db, "EMP0001", "2028-02-29")
    rows = trend(c, frm="2028-02-27", to="2028-03-02")["items"]
    assert [r["date"] for r in rows] == ["2028-02-27", "2028-02-28", "2028-02-29", "2028-03-01", "2028-03-02"]
    assert [r["present_count"] for r in rows] == [0.0, 0.0, 1.0, 0.0, 0.0]
