"""POST /attendance/punch-out, against mongomock (see conftest.py for what that does NOT prove).

Sample employees (tests/conftest.py `employees`):
  EMP0001 Engineering 09:30-18:30   EMP0003 Sales 09:30-18:30   EMP0005 Support 22:00-06:00 (overnight)
Time zone: IST = UTC+05:30; ist_ms(...) builds an instant from an IST wall-clock time.

The concurrency tests here are DETERMINISTIC interleavings (tests/helpers.py RacingCollection): a rival write is made to
land between the endpoint's read and its write. They test this application's compare-and-set and retry logic. They do
NOT show that MongoDB applies the update atomically under truly parallel requests: that is only covered by
tests/test_mongo_integration.py, which needs a real server (NOT RUN when none is reachable).
mongomock itself is not atomic, so no threaded test of an update is attempted against it."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import add_record, ist_ms, racing_db, stored

RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
               "late_minutes", "overtime_minutes", "half_day", "history"}
D = "2026-07-20"  # a Monday


def out(client, emp="EMP0001", at=None, **extra):
    body = {"emp_code": emp, **extra}
    if at is not None:
        body["punched_at"] = at
    return client.post("/attendance/punch-out", json=body)


def open_day(db, emp="EMP0001", date=D, h=9, m=30, s=0, **extra):
    """An open record punched in at h:m:s IST on `date` (the date's own calendar day)."""
    y, mo, d = (int(x) for x in date.split("-"))
    return add_record(db, emp, date, ist_ms(y, mo, d, h, m, s), **extra)


# --------------------------------------------------------------------------- #
# Success: values, shape, what is stored
# --------------------------------------------------------------------------- #
def test_closes_the_open_sample_record_with_hand_calculated_values(client, seeded):
    # DATA_MODEL sample 3: EMP0006 punched in 09:35 IST on 2026-07-07 and has not punched out.
    # 09:35 -> 19:10 = 9 h 35 min = 9.5833 h -> 9.58; shift ends 18:30, so 40 min overtime.
    at = ist_ms(2026, 7, 7, 19, 10)
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": at})
    assert r.status_code == 200
    assert r.json() == {
        "emp_code": "EMP0006", "date": "2026-07-07", "status": "PRESENT",
        "punch_in": ist_ms(2026, 7, 7, 9, 35), "punch_out": at, "work_hours": 9.58,
        "late_minutes": 0, "overtime_minutes": 40, "half_day": False, "history": [],
    }


def test_response_has_exactly_the_attendance_record_fields_and_no_internal_id(client, employees):
    open_day(employees)
    body = out(client, at=ist_ms(2026, 7, 20, 18, 0)).json()
    assert set(body) == RECORD_KEYS and "_id" not in body and "id" not in body


def test_what_is_stored_equals_what_is_returned(client, employees):
    open_day(employees)
    at = ist_ms(2026, 7, 20, 19, 10, 7)
    body = out(client, at=at).json()
    s = stored(employees, "EMP0001", D)
    assert isinstance(s["punch_out"], datetime) and main.datetime_to_ms(s["punch_out"]) == at   # a BSON date
    assert (s["work_hours"], s["overtime_minutes"], s["half_day"]) == (body["work_hours"], body["overtime_minutes"],
                                                                      body["half_day"])
    assert main.record_to_api(s) == body                                                       # one source of truth


def test_punch_out_never_writes_history(client, employees):
    open_day(employees)
    assert out(client, at=ist_ms(2026, 7, 20, 19, 10)).json()["history"] == []
    assert stored(employees, "EMP0001", D)["history"] == []


def test_end_to_end_with_the_punch_in_endpoint(client, employees):
    at_in, at_out = ist_ms(2026, 7, 20, 10, 5), ist_ms(2026, 7, 20, 19, 10)   # DATA_MODEL sample 2 (EMP0003)
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": at_in}).status_code == 201
    r = out(client, "EMP0003", at_out)
    assert r.status_code == 200
    assert (r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["work_hours"]) == (35, 40, 9.08)


def test_late_minutes_belong_to_punch_in_and_are_not_touched_by_punch_out(client, employees):
    open_day(employees, late_minutes=99)       # a stored value punch-out has no business recomputing
    assert out(client, at=ist_ms(2026, 7, 20, 19, 10)).json()["late_minutes"] == 99
    assert stored(employees, "EMP0001", D)["late_minutes"] == 99


def test_status_is_kept(client, employees):
    open_day(employees, status="WFH")
    assert out(client, at=ist_ms(2026, 7, 20, 18, 0)).json()["status"] == "WFH"


def test_omitted_punched_at_means_now(client, employees, monkeypatch):
    open_day(employees)
    fixed = datetime(2026, 7, 20, 13, 40, 0, tzinfo=timezone.utc)                # 19:10 IST
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    r = out(client)
    assert r.status_code == 200
    assert (r.json()["punch_out"], r.json()["work_hours"], r.json()["overtime_minutes"]) == (
        main.datetime_to_ms(fixed), 9.67, 40)                                     # 09:30 -> 19:10 = 9 h 40 min


def test_derived_fields_and_unknown_keys_sent_by_the_client_are_ignored(client, employees):
    open_day(employees)
    r = out(client, at=ist_ms(2026, 7, 20, 18, 0), work_hours=99.9, overtime_minutes=999, half_day=True,
            late_minutes=5, history=[{"x": 1}], punch_in=1, date="1999-01-01", status="LEAVE", _id="a", id="a")
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["overtime_minutes"], b["half_day"], b["late_minutes"], b["history"], b["date"],
            b["status"], b["punch_in"]) == (8.5, 0, False, 0, [], D, "PRESENT", ist_ms(2026, 7, 20, 9, 30))


def test_instants_are_truncated_to_whole_seconds_and_the_truncated_value_is_stored_and_returned(client, employees):
    open_day(employees)
    at = ist_ms(2026, 7, 20, 18, 59, 59, 900)          # 18:59:59.900 counts as 18:59:59 (R1)
    body = out(client, at=at).json()
    assert body["punch_out"] == at - 900 and body["punch_out"] % 1000 == 0
    assert main.datetime_to_ms(stored(employees, "EMP0001", D)["punch_out"]) == at - 900
    assert body["overtime_minutes"] == 0               # 29:59 after shift end: under the 30 minute minimum (R3)


# --------------------------------------------------------------------------- #
# R3: overtime, boundary by boundary (shift 09:30-18:30)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("h,m,s,overtime", [
    (17, 0, 0, 0),         # before the shift end
    (18, 29, 59, 0),
    (18, 30, 0, 0),        # exactly at the shift end
    (18, 59, 59, 0),       # 29:59 over: under the minimum
    (19, 0, 0, 30),        # exactly 30:00 over: counted
    (19, 0, 59, 30),       # floor of 30.98
    (19, 1, 0, 31),
    (20, 0, 0, 90),
    (23, 59, 59, 329),     # 5:29:59 over -> floor(329.98)
])
def test_r3_overtime_boundaries(client, employees, h, m, s, overtime):
    open_day(employees)
    r = out(client, at=ist_ms(2026, 7, 20, h, m, s))
    assert r.status_code == 200 and r.json()["overtime_minutes"] == overtime


@pytest.mark.parametrize("out_wall,overtime", [
    ((7, 21, 6, 29, 59), 0),     # 29:59 after the 06:00 shift end (which is on the NEXT day)
    ((7, 21, 6, 30, 0), 30),
    ((7, 21, 6, 40, 0), 40),
    ((7, 21, 5, 0, 0), 0),       # before the shift end
])
def test_r3_overnight_shift_end_is_on_the_next_day(client, employees, out_wall, overtime):
    add_record(employees, "EMP0005", D, ist_ms(2026, 7, 20, 22, 0))        # 22:00-06:00, punched in at 22:00
    mo, d, h, mi, s = out_wall
    r = out(client, "EMP0005", ist_ms(2026, mo, d, h, mi, s))
    assert r.status_code == 200 and r.json()["overtime_minutes"] == overtime


# --------------------------------------------------------------------------- #
# R4 / R5: work hours (half-up) and half day, decided on the ROUNDED value. Punch-in 09:30:00.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("seconds,hours,half_day", [
    (1, 0.0, True),              # the shortest allowed shift
    (16181, 4.49, True),         # 4.4947 h
    (16182, 4.5, False),         # 4.4950 h: rounds half-UP to 4.50, which is not < 4.50
    (16218, 4.51, False),        # 4.5050 h: float round() gives 4.5, half-up gives 4.51
    (12600, 3.5, True),
    (28800, 8.0, False),
    (86400, 24.0, False),        # exactly 24 h is allowed
])
def test_r4_r5_work_hours_and_half_day(client, employees, seconds, hours, half_day):
    open_day(employees)
    at = ist_ms(2026, 7, 20, 9, 30) + seconds * 1000
    r = out(client, at=at)
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["half_day"]) == (hours, half_day)


def test_a_24_hour_shift_has_overtime_from_the_shift_end_of_the_attendance_date(client, employees):
    open_day(employees)
    r = out(client, at=ist_ms(2026, 7, 21, 9, 30))        # 24:00:00 after punch-in; 15 h after the 18:30 end
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 900


def test_overnight_sample_matches_data_model_sample_7(client, employees):
    # EMP0005 22:00-06:00. Punch-in 21:55 IST on the 6th, out 06:40 IST on the 7th: the record's date is the 6th.
    assert client.post("/attendance/punch-in", json={
        "emp_code": "EMP0005", "punched_at": ist_ms(2026, 7, 6, 21, 55)}).json()["date"] == "2026-07-06"
    r = out(client, "EMP0005", ist_ms(2026, 7, 7, 6, 40))
    assert r.status_code == 200
    assert (r.json()["date"], r.json()["work_hours"], r.json()["overtime_minutes"], r.json()["half_day"]) == (
        "2026-07-06", 8.75, 40, False)                    # 21:55 -> 06:40 = 8 h 45 min


# --------------------------------------------------------------------------- #
# Eligibility and ordering: the most recent record whose punch_in is at or before punched_at
# --------------------------------------------------------------------------- #
def test_the_most_recent_eligible_record_is_closed_and_older_ones_are_left_alone(client, employees):
    add_record(employees, "EMP0001", "2026-07-20", ist_ms(2026, 7, 20, 9, 30), ist_ms(2026, 7, 20, 18, 30))
    open_day(employees, date="2026-07-21")
    before = stored(employees, "EMP0001", "2026-07-20")
    r = out(client, at=ist_ms(2026, 7, 21, 18, 30))
    assert r.status_code == 200 and r.json()["date"] == "2026-07-21"
    assert stored(employees, "EMP0001", "2026-07-20") == before


def test_a_record_that_punched_in_after_punched_at_is_not_eligible(client, employees):
    open_day(employees, date="2026-07-20")                                  # 09:30
    open_day(employees, date="2026-07-21", h=9, m=0)                        # later punch-in, must be skipped
    r = out(client, at=ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 200 and r.json()["date"] == "2026-07-20"
    assert stored(employees, "EMP0001", "2026-07-21")["punch_out"] is None


def test_it_works_across_midnight_for_an_overnight_shift(client, employees):
    add_record(employees, "EMP0005", "2026-07-06", ist_ms(2026, 7, 6, 21, 55))      # shift of the 6th
    add_record(employees, "EMP0005", "2026-07-07", ist_ms(2026, 7, 7, 21, 50))      # next night's punch-in (later)
    r = out(client, "EMP0005", ist_ms(2026, 7, 7, 6, 40))                           # morning of the 7th
    assert r.status_code == 200 and r.json()["date"] == "2026-07-06"                # not the 7th (its punch-in is later)
    assert stored(employees, "EMP0005", "2026-07-07")["punch_out"] is None


def test_if_the_most_recent_record_is_already_closed_the_answer_is_409_not_an_older_open_record(client, employees):
    open_day(employees, date="2026-07-20")                                           # open and older
    add_record(employees, "EMP0001", "2026-07-21", ist_ms(2026, 7, 21, 9, 30), ist_ms(2026, 7, 21, 18, 30))
    r = out(client, at=ist_ms(2026, 7, 21, 20, 0))
    assert r.status_code == 409
    assert stored(employees, "EMP0001", "2026-07-20")["punch_out"] is None         # no fallback to the older record


def test_other_employees_records_are_never_considered(client, employees):
    open_day(employees, "EMP0001")
    r = out(client, "EMP0003", ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 404
    assert stored(employees, "EMP0001", D)["punch_out"] is None


def test_a_stored_punch_in_with_milliseconds_is_compared_as_whole_seconds(client, employees):
    # seeded data may carry milliseconds. 09:00:00.500 counts as 09:00:00 (R1), so punching out at 09:00:00 finds the
    # record (it is "at or before") and is then refused as not AFTER punch_in (422), not reported as "no punch-in" (404).
    add_record(employees, "EMP0001", D, ist_ms(2026, 7, 20, 9, 0, 0, 500))
    assert out(client, at=ist_ms(2026, 7, 20, 9, 0, 0)).status_code == 422
    r = out(client, at=ist_ms(2026, 7, 20, 9, 0, 1))
    assert r.status_code == 200 and r.json()["work_hours"] == 0.0


# --------------------------------------------------------------------------- #
# Missing records: 404
# --------------------------------------------------------------------------- #
def test_unknown_employee_is_404(client, employees):
    r = out(client, "EMP9999", ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 404 and r.json() == {"detail": "unknown employee"}


def test_employee_without_any_record_is_404(client, employees):
    r = out(client, "EMP0001", ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 404 and "no punch-in" in r.json()["detail"]


def test_punched_at_before_the_only_punch_in_is_404(client, employees):
    open_day(employees)
    assert out(client, at=ist_ms(2026, 7, 20, 9, 29, 59)).status_code == 404
    assert stored(employees, "EMP0001", D)["punch_out"] is None


@pytest.mark.parametrize("status", ["LEAVE", "ABSENT"])
def test_leave_and_absent_records_cannot_be_punched_out(client, employees, status):
    add_record(employees, "EMP0001", D, None, status=status)
    assert out(client, at=ist_ms(2026, 7, 20, 18, 0)).status_code == 404
    assert stored(employees, "EMP0001", D)["punch_out"] is None


def test_an_absent_record_that_somehow_carries_a_punch_in_is_still_not_a_punch_in(client, employees):
    add_record(employees, "EMP0001", D, ist_ms(2026, 7, 20, 9, 30), status="ABSENT")
    assert out(client, at=ist_ms(2026, 7, 20, 18, 0)).status_code == 404


# --------------------------------------------------------------------------- #
# 409 and 422
# --------------------------------------------------------------------------- #
def test_second_punch_out_is_409_and_changes_nothing(client, employees):
    open_day(employees)
    first = out(client, at=ist_ms(2026, 7, 20, 18, 0))
    assert first.status_code == 200
    before = stored(employees, "EMP0001", D)
    for at in (ist_ms(2026, 7, 20, 19, 0), ist_ms(2026, 7, 20, 18, 0)):
        r = out(client, at=at)
        assert r.status_code == 409 and r.json() == {"detail": "already punched out"}
    assert stored(employees, "EMP0001", D) == before


def test_a_seeded_closed_record_is_409(client, seeded):
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 20, 0)})
    assert r.status_code == 409


def test_punched_at_equal_to_punch_in_is_422_and_nothing_is_stored(client, employees):
    # "at or before" finds the record, and then punched_at <= punch_in is refused. (A punched_at EARLIER than the
    # punch-in finds no record at all: the 404 test above.)
    open_day(employees)
    r = out(client, at=ist_ms(2026, 7, 20, 9, 30))
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "punched_at"]
    assert stored(employees, "EMP0001", D)["punch_out"] is None


def test_more_than_24_hours_after_punch_in_is_422_exactly_24_is_fine(client, employees):
    open_day(employees)
    at_24h = ist_ms(2026, 7, 20, 9, 30) + 86_400_000
    over = out(client, at=at_24h + 1000)
    assert over.status_code == 422 and over.json()["detail"][0]["loc"] == ["body", "punched_at"]
    assert stored(employees, "EMP0001", D)["punch_out"] is None
    assert out(client, at=at_24h).status_code == 200


def test_409_wins_over_422_when_the_record_is_already_closed(client, employees):
    # Design choice: the state of the record is decided before the arithmetic on punched_at.
    open_day(employees)
    assert out(client, at=ist_ms(2026, 7, 20, 18, 0)).status_code == 200
    assert out(client, at=ist_ms(2026, 7, 25, 18, 0)).status_code == 409


# --------------------------------------------------------------------------- #
# Request validation (422), nothing written
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    {},
    {"punched_at": 1783312500000},                                   # emp_code missing
    {"emp_code": None}, {"emp_code": 7}, {"emp_code": ["EMP0001"]}, {"emp_code": {"a": 1}},
    {"emp_code": "EMP0001", "punched_at": 1783312500},               # looks like seconds
    {"emp_code": "EMP0001", "punched_at": 1783312500000.0},          # float
    {"emp_code": "EMP0001", "punched_at": 1783312500000.5},
    {"emp_code": "EMP0001", "punched_at": "1783312500000"},          # string
    {"emp_code": "EMP0001", "punched_at": True},
    {"emp_code": "EMP0001", "punched_at": None},
    {"emp_code": "EMP0001", "punched_at": 0},
    {"emp_code": "EMP0001", "punched_at": -5},
    {"emp_code": "EMP0001", "punched_at": 99_999_999_999},           # just below the range
    {"emp_code": "EMP0001", "punched_at": 4_102_444_800_001},        # just above the range
    {"emp_code": "EMP0001", "punched_at": [1783312500000]},
])
def test_invalid_bodies_are_422(client, employees, body):
    open_day(employees)
    r = client.post("/attendance/punch-out", json=body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert stored(employees, "EMP0001", D)["punch_out"] is None


@pytest.mark.parametrize("raw", [
    b'{"emp_code": "EMP0001", "punched_at": NaN}',
    b'{"emp_code": "EMP0001", "punched_at": Infinity}',
    b'{"emp_code": "\\ud800"}',                                      # lone surrogate: not storable as BSON
    b'{"emp_code": "EMP0001", "punched_at": ' + b"[" * 5000 + b"]" * 5000 + b"}",
    b"not json", b"", b"[]", b"null", b"\xff\xfe\xfd",
])
def test_hostile_bodies_are_422_never_500(employees, raw):
    r = TestClient(main.app, raise_server_exceptions=False).post(
        "/attendance/punch-out", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_a_422_body_has_the_documented_shape_and_does_not_echo_the_input(client, employees):
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": "secret-value"})
    assert r.status_code == 422
    assert all(set(e) == {"type", "loc", "msg"} for e in r.json()["detail"]) and "secret-value" not in r.text


def test_database_failure_is_503_not_500(monkeypatch):
    from pymongo.errors import ConnectionFailure

    class Broken:
        def __getattr__(self, name):
            raise ConnectionFailure("down")

        def __getitem__(self, name):
            raise ConnectionFailure("down")

    monkeypatch.setattr(main, "db", Broken())
    r = TestClient(main.app, raise_server_exceptions=False).post(
        "/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 18, 0)})
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}


# --------------------------------------------------------------------------- #
# Concurrency, DETERMINISTIC interleavings (see the module docstring for what this does and does not prove)
# --------------------------------------------------------------------------- #
def _set(emp, date, **fields):
    return lambda coll: coll.update_one({"emp_code": emp, "date": date}, {"$set": fields})


def test_a_rival_punch_out_between_our_read_and_write_makes_us_the_409_and_is_not_overwritten(
        client, employees, monkeypatch):
    open_day(employees)
    rival_out = main.ms_to_datetime(ist_ms(2026, 7, 20, 17, 0))
    racing_db(employees, monkeypatch, _set("EMP0001", D, punch_out=rival_out, work_hours=7.5,
                                           overtime_minutes=0, half_day=False))
    r = out(client, at=ist_ms(2026, 7, 20, 19, 10))
    assert r.status_code == 409
    s = stored(employees, "EMP0001", D)
    assert (s["punch_out"], s["work_hours"]) == (rival_out, 7.5)            # the winner's values are intact


def test_a_correction_that_moves_punch_in_between_read_and_write_is_not_overwritten_with_stale_numbers(
        client, employees, monkeypatch):
    open_day(employees)                                                        # we read: punch-in 09:30
    racing_db(employees, monkeypatch, _set("EMP0001", D, punch_in=main.ms_to_datetime(ist_ms(2026, 7, 20, 10, 0)),
                                           late_minutes=30))                  # rival: punch-in is now 10:00
    r = out(client, at=ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 200
    # recalculated from the NEW punch-in: 10:00 -> 18:00 = 8.0 h (a stale 09:30 would have given 8.5)
    assert (r.json()["punch_in"], r.json()["work_hours"], r.json()["late_minutes"]) == (
        ist_ms(2026, 7, 20, 10, 0), 8.0, 30)
    assert stored(employees, "EMP0001", D)["work_hours"] == 8.0


def test_a_correction_that_changes_status_between_read_and_write_keeps_the_new_status(client, employees, monkeypatch):
    open_day(employees)
    racing_db(employees, monkeypatch, _set("EMP0001", D, status="WFH"))
    r = out(client, at=ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 200 and r.json()["status"] == "WFH"
    assert stored(employees, "EMP0001", D)["status"] == "WFH"


def test_a_record_that_keeps_changing_ends_in_409_after_a_bounded_number_of_reads_and_writes_nothing(
        client, employees, monkeypatch):
    open_day(employees)
    flip = [0]

    def keep_changing(coll):                       # a different punch_in after every single read
        flip[0] += 1
        coll.update_one({"emp_code": "EMP0001", "date": D},
                        {"$set": {"punch_in": main.ms_to_datetime(ist_ms(2026, 7, 20, 9, 30) + flip[0] * 60_000)}})

    racing = racing_db(employees, monkeypatch, keep_changing, every_time=True)
    r = out(client, at=ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 409 and racing.reads == main.MAX_WRITE_ATTEMPTS
    assert stored(employees, "EMP0001", D)["punch_out"] is None             # we never wrote anything
