"""POST /attendance/punch-in, against mongomock (see conftest.py for what that does NOT prove).

Sample employees used (tests/conftest.py `employees`):
  EMP0001 Engineering 09:30-18:30     EMP0003 Sales 09:30-18:30     EMP0005 Support 22:00-06:00 (overnight)
Time zone: IST = UTC+05:30. ist_ms(...) builds an instant from an IST wall-clock time."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import ist_ms, run_together

RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
               "late_minutes", "overtime_minutes", "half_day", "history"}


def punch(client, emp="EMP0001", at=None, **extra):
    body = {"emp_code": emp, **extra}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-in", json=body)


# --------------------------------------------------------------------------- #
# Success: shape and stored values
# --------------------------------------------------------------------------- #
def test_late_punch_in_matches_the_documented_sample(client, employees):
    # DATA_MODEL.md sample 2: EMP0003 punched in 10:05 IST (04:35 UTC), 35 minutes after 09:30
    at = ist_ms(2026, 7, 6, 10, 5)
    r = punch(client, "EMP0003", at)
    assert r.status_code == 201
    assert r.json() == {
        "emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT",
        "punch_in": at, "punch_out": None, "work_hours": None,
        "late_minutes": 35, "overtime_minutes": 0, "half_day": False, "history": [],
    }


def test_response_has_exactly_the_attendance_record_fields_and_no_internal_id(client, employees):
    body = punch(client, "EMP0001", ist_ms(2026, 7, 20, 9, 0)).json()
    assert set(body) == RECORD_KEYS and "_id" not in body and "id" not in body


def test_what_is_stored_equals_what_is_returned(client, employees):
    at = ist_ms(2026, 7, 20, 10, 5, 7)
    body = punch(client, "EMP0001", at).json()
    stored = employees.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-20"})
    assert isinstance(stored["punch_in"], datetime) and main.datetime_to_ms(stored["punch_in"]) == at   # BSON date
    assert stored["punch_out"] is None and stored["work_hours"] is None
    assert (stored["late_minutes"], stored["overtime_minutes"], stored["half_day"], stored["history"]) == (
        body["late_minutes"], 0, False, [])                                  # derived values are STORED (DATA_MODEL.md)
    assert stored["status"] == "PRESENT"


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_the_three_presence_statuses_are_accepted(client, employees, status):
    r = punch(client, status=status)
    assert r.status_code == 201 and r.json()["status"] == status


def test_status_defaults_to_present(client, employees):
    assert punch(client).json()["status"] == "PRESENT"


def test_omitted_punched_at_means_now(client, employees, monkeypatch):
    fixed = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)     # 10:05 IST
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    r = punch(client, "EMP0003")
    assert r.status_code == 201
    assert (r.json()["punch_in"], r.json()["date"], r.json()["late_minutes"]) == (main.datetime_to_ms(fixed), "2026-07-06", 35)


def test_derived_fields_sent_by_the_client_are_ignored(client, employees):
    r = punch(client, "EMP0001", ist_ms(2026, 7, 20, 9, 0), late_minutes=999, overtime_minutes=999,
              work_hours=99.9, half_day=True, history=[{"x": 1}], punch_out=ist_ms(2026, 7, 20, 18, 0),
              date="1999-01-01", _id="abc", id="abc")
    assert r.status_code == 201
    b = r.json()
    assert (b["late_minutes"], b["overtime_minutes"], b["work_hours"], b["half_day"], b["history"], b["punch_out"],
            b["date"]) == (0, 0, None, False, [], None, "2026-07-20")


# --------------------------------------------------------------------------- #
# R2: late minutes (shift 09:30), boundary by boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("h,m,s,ms,late", [
    (9, 0, 0, 0, 0),            # early
    (9, 29, 59, 0, 0),
    (9, 30, 0, 0, 0),           # exactly on time
    (9, 39, 59, 0, 0),
    (9, 40, 0, 0, 0),           # exactly 10:00 after the start: still inside the grace period
    (9, 40, 0, 900, 0),         # 09:40:00.900 is truncated to 09:40:00 (R1) -> not late
    (9, 40, 1, 0, 10),          # one second past the grace period: late, counted from shift_start
    (9, 40, 59, 0, 10),         # floor of 10.98
    (9, 41, 0, 0, 11),
    (10, 15, 59, 0, 45),        # the example in the contract
    (10, 15, 59, 999, 45),      # .999 is truncated, not rounded up to 10:16:00
    (12, 0, 0, 0, 150),
    (23, 59, 59, 0, 869),       # 14:29:59 after 09:30 -> floor(869.98)
])
def test_r2_late_minutes_boundaries(client, employees, h, m, s, ms, late):
    r = punch(client, "EMP0001", ist_ms(2026, 7, 20, h, m, s, ms))
    assert r.status_code == 201 and r.json()["late_minutes"] == late


def test_instants_are_truncated_to_whole_seconds_and_the_truncated_value_is_stored_and_returned(client, employees):
    at = ist_ms(2026, 7, 20, 9, 40, 0, 900)
    body = punch(client, "EMP0001", at).json()
    assert body["punch_in"] == at - 900 and body["punch_in"] % 1000 == 0
    stored = employees.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-20"})["punch_in"]
    assert main.datetime_to_ms(stored) == at - 900


# --------------------------------------------------------------------------- #
# R1: the attendance date is the IST date of punch_in
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utc_instant,expected_date", [
    ("2026-07-06T18:29:59+00:00", "2026-07-06"),   # 23:59:59 IST
    ("2026-07-06T18:30:00+00:00", "2026-07-07"),   # 00:00:00 IST the next day
    ("2026-07-06T20:00:00+00:00", "2026-07-07"),   # still the 6th in UTC, already the 7th in IST
    ("2026-07-06T00:00:00+00:00", "2026-07-06"),   # 05:30 IST
    ("2026-07-05T18:30:00+00:00", "2026-07-06"),   # 00:00:00 IST exactly
    ("2026-07-05T18:29:59+00:00", "2026-07-05"),   # one second earlier
    ("2026-12-31T18:30:00+00:00", "2027-01-01"),   # year boundary
    ("2028-02-28T18:30:00+00:00", "2028-02-29"),   # leap day
])
def test_r1_date_is_the_ist_calendar_date_not_the_utc_date(client, employees, utc_instant, expected_date):
    at = main.datetime_to_ms(datetime.fromisoformat(utc_instant))
    r = punch(client, "EMP0001", at)
    assert r.status_code == 201 and r.json()["date"] == expected_date


def test_a_punch_in_just_after_midnight_ist_is_not_late_for_the_new_day(client, employees):
    body = punch(client, "EMP0001", ist_ms(2026, 7, 7, 0, 0, 0)).json()
    assert body["date"] == "2026-07-07" and body["late_minutes"] == 0


# --------------------------------------------------------------------------- #
# R1 + R2 for an overnight shift (EMP0005: 22:00-06:00)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("day,h,m,s,expected_date,late", [
    (6, 21, 55, 0, "2026-07-06", 0),     # DATA_MODEL.md sample 7: before the start -> not late
    (6, 22, 0, 0, "2026-07-06", 0),
    (6, 22, 10, 0, "2026-07-06", 0),     # exactly 10:00 -> grace
    (6, 22, 10, 1, "2026-07-06", 10),
    (6, 23, 59, 59, "2026-07-06", 119),
    (7, 0, 0, 0, "2026-07-06", 120),     # after midnight: still the shift that started on the 6th
    (7, 5, 59, 59, "2026-07-06", 479),   # earlier than shift_end 06:00 -> belongs to the previous day
    (7, 6, 0, 0, "2026-07-07", 0),       # NOT earlier than shift_end -> a new day's shift
    (7, 6, 0, 1, "2026-07-07", 0),
])
def test_overnight_shift_date_and_late_minutes(client, employees, day, h, m, s, expected_date, late):
    r = punch(client, "EMP0005", ist_ms(2026, 7, day, h, m, s))
    assert r.status_code == 201
    assert (r.json()["date"], r.json()["late_minutes"]) == (expected_date, late)


def test_overnight_second_punch_in_after_midnight_is_the_same_shift_so_409(client, employees):
    assert punch(client, "EMP0005", ist_ms(2026, 7, 10, 23, 0)).status_code == 201     # shift of the 10th
    assert punch(client, "EMP0005", ist_ms(2026, 7, 11, 2, 0)).status_code == 409      # 02:00 on the 11th = same shift
    assert punch(client, "EMP0005", ist_ms(2026, 7, 11, 22, 0)).status_code == 201     # the next shift


# --------------------------------------------------------------------------- #
# Timestamp validation (422)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [
    1783312500,                    # seconds, not milliseconds
    99_999_999_999,                # one below the minimum
    0, -1, -1783312500000,
    4_102_444_800_001,             # one above the maximum
    10 ** 18,
    1783312500000.0,               # float, even a whole one
    1783312500000.5,
    "1783312500000",               # string
    "",
    True, False,                   # bool is not an integer here
    None,                          # the schema is not nullable
    [1783312500000], {"ms": 1},
])
def test_invalid_punched_at_is_422(client, employees, bad):
    before = employees.attendance_logs.count_documents({})
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert employees.attendance_logs.count_documents({}) == before      # nothing was stored


@pytest.mark.parametrize("edge", [100_000_000_000, 4_102_444_800_000])
def test_the_inclusive_limits_of_punched_at_are_accepted(client, employees, edge):
    r = punch(client, "EMP0001", edge)
    assert r.status_code == 201 and r.json()["punch_in"] == edge


def test_an_invalid_timestamp_stores_nothing(client, mdb):
    mdb.employees.insert_one({"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"})
    for bad in (1783312500, 1783312500000.5, "x"):
        assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    assert mdb.attendance_logs.count_documents({}) == 0


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present", "Present", "", "UNKNOWN", 1, None, ["PRESENT"]])
def test_only_the_three_presence_statuses_are_accepted(client, employees, status):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status})
    assert r.status_code == 422


@pytest.mark.parametrize("body", [{}, {"punched_at": 1783312500000}, {"emp_code": None}, {"emp_code": 5}, {"emp_code": ["a"]}])
def test_missing_or_non_string_emp_code_is_422(client, employees, body):
    assert client.post("/attendance/punch-in", json=body).status_code == 422


@pytest.mark.parametrize("raw", [b"", b"nope", b"[]"])
def test_non_object_body_is_422(client, employees, raw):
    assert client.post("/attendance/punch-in", content=raw, headers={"content-type": "application/json"}).status_code == 422


# --------------------------------------------------------------------------- #
# 404 and the order of checks
# --------------------------------------------------------------------------- #
def test_unknown_employee_is_404_and_nothing_is_stored(client, employees):
    before = employees.attendance_logs.count_documents({})
    r = punch(client, "EMP9999", ist_ms(2026, 7, 20, 9, 0))
    assert r.status_code == 404 and r.json() == {"detail": "unknown employee"}
    assert employees.attendance_logs.count_documents({}) == before


@pytest.mark.parametrize("code", ["", "emp0001", "EMP0001 ", "EMP00010"])
def test_emp_code_must_match_an_employee_exactly(client, employees, code):
    assert punch(client, code, ist_ms(2026, 7, 20, 9, 0)).status_code == 404


def test_validation_comes_before_the_employee_lookup(client, employees):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP9999", "status": "ABSENT"})
    assert r.status_code == 422          # not 404: the body is checked first


# --------------------------------------------------------------------------- #
# 409: one record per (emp_code, date), decided by the unique index
# --------------------------------------------------------------------------- #
def test_second_punch_in_for_the_same_day_is_409_and_changes_nothing(client, employees):
    first = punch(client, "EMP0001", ist_ms(2026, 7, 20, 9, 0))
    second = punch(client, "EMP0001", ist_ms(2026, 7, 20, 11, 0), status="WFH")
    assert first.status_code == 201 and second.status_code == 409
    assert second.json() == {"detail": "already punched in for this date"}
    stored = list(employees.attendance_logs.find({"emp_code": "EMP0001", "date": "2026-07-20"}))
    assert len(stored) == 1 and stored[0]["status"] == "PRESENT"
    assert main.datetime_to_ms(stored[0]["punch_in"]) == ist_ms(2026, 7, 20, 9, 0)   # the first one wins untouched


def test_identical_requests_the_second_is_409(client, employees):
    at = ist_ms(2026, 7, 20, 9, 0)
    assert [punch(client, "EMP0001", at).status_code for _ in range(3)] == [201, 409, 409]


def test_other_employee_and_other_day_are_not_blocked(client, employees):
    assert punch(client, "EMP0001", ist_ms(2026, 7, 20, 9, 0)).status_code == 201
    assert punch(client, "EMP0003", ist_ms(2026, 7, 20, 9, 0)).status_code == 201     # same day, other employee
    assert punch(client, "EMP0001", ist_ms(2026, 7, 21, 9, 0)).status_code == 201     # same employee, next day


def test_a_seeded_absent_or_leave_record_blocks_punch_in_for_that_day(client, seeded):
    # EMP0004 has an ABSENT record on 2026-07-08 and EMP0003 a LEAVE record: a record already exists
    assert punch(client, "EMP0004", ist_ms(2026, 7, 8, 9, 30)).status_code == 409
    assert punch(client, "EMP0003", ist_ms(2026, 7, 8, 9, 30)).status_code == 409


def test_the_409_comes_from_the_unique_index_not_from_a_look_up_first(spy_db, employees):
    spy = spy_db.spy_on("attendance_logs", forbid=("find_one", "find", "count_documents"))
    employees.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-20", "status": "PRESENT"})
    r = TestClient(main.app).post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 9, 0)})
    assert r.status_code == 409
    assert spy.names() == ["list_indexes", "insert_one"]   # verify the index exists, then try to insert: nothing else


def test_simultaneous_punch_ins_for_the_same_employee_and_date_give_exactly_one_201(employees):
    def go(i):
        # different instants within the same attendance date, as real parallel requests would have
        return TestClient(main.app).post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 9, 0, i)}).status_code

    codes = run_together(go, 16)
    assert sorted(codes) == [201] + [409] * 15
    assert employees.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-20"}) == 1


def test_simultaneous_punch_ins_for_different_employees_all_succeed(employees):
    codes_for = ["EMP0001", "EMP0003", "EMP0004", "EMP0005", "EMP0006", "EMP0002"]

    def go(i):
        return TestClient(main.app).post("/attendance/punch-in", json={
            "emp_code": codes_for[i], "punched_at": ist_ms(2026, 7, 25, 22, 30)}).status_code

    assert run_together(go, len(codes_for)) == [201] * len(codes_for)


# --------------------------------------------------------------------------- #
# Database failure is a 503, never a 500
# --------------------------------------------------------------------------- #
def test_database_failure_is_503(monkeypatch):
    from pymongo.errors import ConnectionFailure

    class Broken:
        def __getattr__(self, name):          # db.employees
            raise ConnectionFailure("down")

        def __getitem__(self, name):          # db["employees"]
            raise ConnectionFailure("down")

    monkeypatch.setattr(main, "db", Broken())
    c = TestClient(main.app, raise_server_exceptions=False)
    for r in (c.post("/attendance/punch-in", json={"emp_code": "EMP0001"}),
              c.post("/employees", json={"emp_code": "EMP0001", "name": "a", "email": "a@b.co",
                                         "department": "d", "joined_on": "2026-01-05"}),
              c.get("/employees"), c.get("/attendance")):
        assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}


# --------------------------------------------------------------------------- #
# Review additions: truncation x boundaries, and seeded records
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utc_instant,expected_date", [
    ("2026-07-06T18:29:59.999+00:00", "2026-07-06"),   # 23:59:59.999 IST -> truncated to 23:59:59: still the 6th
    ("2026-07-06T18:30:00.000+00:00", "2026-07-07"),   # exactly IST midnight
    ("2026-07-06T18:30:00.999+00:00", "2026-07-07"),
    ("2026-07-05T18:29:59.999+00:00", "2026-07-05"),   # a millisecond before IST midnight is NOT rounded up to the 6th
])
def test_truncation_happens_before_the_ist_date_is_decided(client, employees, utc_instant, expected_date):
    at = main.datetime_to_ms(datetime.fromisoformat(utc_instant))
    assert punch(client, "EMP0001", at).json()["date"] == expected_date


@pytest.mark.parametrize("day,h,m,s,millis,expected_date,late", [
    (7, 5, 59, 59, 999, "2026-07-06", 479),    # 05:59:59.999 -> 05:59:59: earlier than shift_end 06:00 -> previous day
    (7, 6, 0, 0, 0, "2026-07-07", 0),          # exactly shift_end -> not earlier -> its own day
    (7, 6, 0, 0, 999, "2026-07-07", 0),
    (6, 22, 10, 0, 999, "2026-07-06", 0),      # 22:10:00.999 -> 22:10:00: still inside the grace period
    (6, 22, 10, 1, 0, "2026-07-06", 10),
])
def test_overnight_boundaries_with_milliseconds(client, employees, day, h, m, s, millis, expected_date, late):
    r = punch(client, "EMP0005", ist_ms(2026, 7, day, h, m, s, millis))
    assert (r.json()["date"], r.json()["late_minutes"]) == (expected_date, late)


def test_an_overnight_employee_punching_in_mid_day_is_a_normal_day_and_not_late(client, employees):
    body = punch(client, "EMP0005", ist_ms(2026, 7, 20, 12, 0)).json()      # between 06:00 and 22:00
    assert (body["date"], body["late_minutes"]) == ("2026-07-20", 0)


def test_a_seeded_present_record_blocks_punch_in_and_is_left_untouched(client, seeded):
    before = seeded.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    r = punch(client, "EMP0001", ist_ms(2026, 7, 6, 14, 0), status="WFH")
    assert r.status_code == 409
    assert seeded.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-06"}) == before
    assert seeded.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}) == 1


def test_the_duplicate_check_uses_the_ist_date_not_the_utc_date(client, seeded):
    # 2026-07-05T20:00Z is still the 5th in UTC but 01:30 on the 6th in IST, where EMP0001 already has a record
    at = main.datetime_to_ms(datetime(2026, 7, 5, 20, 0, tzinfo=timezone.utc))
    assert punch(client, "EMP0001", at).status_code == 409
    # and the reverse: the UTC date has a record but the IST date does not -> allowed
    at_2 = main.datetime_to_ms(datetime(2026, 7, 6, 18, 30, tzinfo=timezone.utc))       # 00:00 IST on the 7th ... EMP0001 has the 7th too
    assert punch(client, "EMP0001", at_2).status_code == 409
    assert punch(client, "EMP0003", at_2).status_code == 201                              # EMP0003 has no record on the 7th
