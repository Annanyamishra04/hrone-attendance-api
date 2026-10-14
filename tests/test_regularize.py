"""PATCH /attendance/{emp_code}/{date}, against mongomock (see conftest.py for what that does NOT prove).

Sample records used (tests/conftest.py `seeded`, DATA_MODEL.md section 5). Shifts: everybody 09:30-18:30 except
EMP0005 (22:00-06:00). All times below are IST.
  EMP0003 07-06  in 10:05 out 19:10  work 9.08  late 35  overtime 40        EMP0006 07-07  in 09:35, OPEN
  EMP0004 07-07  WFH 09:30-13:00     work 3.5   half day                    EMP0004 07-08  ABSENT      EMP0003 07-08  LEAVE
  EMP0005 07-06  in 21:55, out 06:40 (07-07)  work 8.75  overtime 40         EMP0001 07-07  already corrected once
  EMP0002 07-14  legacy: no history, no half_day

As in test_punch_out.py, the concurrency tests are DETERMINISTIC interleavings (a rival write lands between the
endpoint's read and its write). They test this application's compare-and-set / retry logic, not MongoDB's atomicity;
that needs tests/test_mongo_integration.py and a real server (NOT RUN when none is reachable)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import add_record, ist_ms, racing_db, stored

RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
               "late_minutes", "overtime_minutes", "half_day", "history"}
FIXED_NOW = datetime(2026, 7, 8, 5, 0, 0, tzinfo=timezone.utc)      # the "at" of DATA_MODEL sample 8


def patch(client, emp, date, reason="because reasons", by="hr.admin", **fields):
    return client.patch(f"/attendance/{emp}/{date}", json={"reason": reason, "regularized_by": by, **fields})


def api(db, emp, date):
    return main.record_to_api(stored(db, emp, date))


# --------------------------------------------------------------------------- #
# Success: recomputation and history
# --------------------------------------------------------------------------- #
def test_reproduces_data_model_sample_8(client, employees, sample_logs, monkeypatch):
    monkeypatch.setattr(main, "now_utc", lambda: FIXED_NOW)
    # The record as it was BEFORE HR's correction: punch-in 10:05, out 18:30 -> late 35, work 8 h 25 min = 8.42.
    add_record(employees, "EMP0001", "2026-07-07", ist_ms(2026, 7, 7, 10, 5), ist_ms(2026, 7, 7, 18, 30))
    r = patch(client, "EMP0001", "2026-07-07", reason="biometric glitch", by="hr.admin",
              punch_in=ist_ms(2026, 7, 7, 9, 28))
    assert r.status_code == 200
    sample = next(main.record_to_api(d) for d in sample_logs if (d["emp_code"], d["date"]) == ("EMP0001", "2026-07-07"))
    assert r.json() == sample          # the whole record, including the one history entry, equals the documented sample
    assert r.json()["history"][0]["changes"] == {
        "punch_in": {"from": ist_ms(2026, 7, 7, 10, 5), "to": ist_ms(2026, 7, 7, 9, 28)},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 8.42, "to": 9.03},
    }


def test_late_punch_in_corrected_to_on_time(client, seeded):
    # EMP0003 07-06: 10:05 -> 09:28. late 35 -> 0; work 09:28 -> 19:10 = 9 h 42 min = 9.70; overtime stays 40.
    r = patch(client, "EMP0003", "2026-07-06", punch_in=ist_ms(2026, 7, 6, 9, 28))
    assert r.status_code == 200
    b = r.json()
    assert (b["punch_in"], b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"]) == (
        ist_ms(2026, 7, 6, 9, 28), 9.7, 0, 40, False)
    assert set(b["history"][0]["changes"]) == {"punch_in", "work_hours", "late_minutes"}   # overtime did not change


def test_status_only_change_keeps_everything_else_and_records_only_status(client, seeded):
    before = api(seeded, "EMP0003", "2026-07-06")
    r = patch(client, "EMP0003", "2026-07-06", status="WFH")
    assert r.status_code == 200
    b = r.json()
    assert {k: v for k, v in b.items() if k not in ("status", "history")} == {
        k: v for k, v in before.items() if k not in ("status", "history")}
    assert b["status"] == "WFH" and b["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_setting_punch_out_on_an_open_record(client, seeded):
    # EMP0006 07-07, in 09:35, out 19:10 -> 9 h 35 min = 9.58, overtime 40, not a half day
    at = ist_ms(2026, 7, 7, 19, 10)
    r = patch(client, "EMP0006", "2026-07-07", punch_out=at)
    assert r.status_code == 200
    assert (r.json()["punch_out"], r.json()["work_hours"], r.json()["overtime_minutes"], r.json()["half_day"]) == (
        at, 9.58, 40, False)
    assert r.json()["history"][0]["changes"] == {
        "punch_out": {"from": None, "to": at},
        "work_hours": {"from": None, "to": 9.58},
        "overtime_minutes": {"from": 0, "to": 40},
    }


def test_correcting_a_half_day_into_a_full_day(client, seeded):
    # EMP0004 07-07 (WFH 09:30-13:00, 3.5 h, half day). Punch-out corrected to 18:00 -> 8.5 h, no longer a half day.
    r = patch(client, "EMP0004", "2026-07-07", punch_out=ist_ms(2026, 7, 7, 18, 0))
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["half_day"], r.json()["overtime_minutes"]) == (8.5, False, 0)
    assert r.json()["history"][0]["changes"] == {
        "punch_out": {"from": ist_ms(2026, 7, 7, 13, 0), "to": ist_ms(2026, 7, 7, 18, 0)},
        "work_hours": {"from": 3.5, "to": 8.5},
        "half_day": {"from": True, "to": False},
    }


def test_correcting_a_full_day_into_a_half_day(client, seeded):
    # EMP0003 07-06 punch-out 19:10 -> 13:30: 10:05 -> 13:30 = 3 h 25 min = 3.4167 -> 3.42, half day, overtime 0
    r = patch(client, "EMP0003", "2026-07-06", punch_out=ist_ms(2026, 7, 6, 13, 30))
    assert (r.json()["work_hours"], r.json()["half_day"], r.json()["overtime_minutes"]) == (3.42, True, 0)


def test_present_to_absent_clears_both_punch_times_and_every_derived_value(client, seeded):
    r = patch(client, "EMP0003", "2026-07-06", status="ABSENT")
    assert r.status_code == 200
    b = r.json()
    assert (b["status"], b["punch_in"], b["punch_out"], b["work_hours"], b["late_minutes"], b["overtime_minutes"],
            b["half_day"]) == ("ABSENT", None, None, None, 0, 0, False)
    assert b["history"][0]["changes"] == {
        "status": {"from": "PRESENT", "to": "ABSENT"},
        "punch_in": {"from": ist_ms(2026, 7, 6, 10, 5), "to": None},
        "punch_out": {"from": ist_ms(2026, 7, 6, 19, 10), "to": None},
        "work_hours": {"from": 9.08, "to": None},
        "late_minutes": {"from": 35, "to": 0},
        "overtime_minutes": {"from": 40, "to": 0},
    }
    s = stored(seeded, "EMP0003", "2026-07-06")
    assert s["punch_in"] is None and s["punch_out"] is None and s["work_hours"] is None


def test_half_day_record_set_to_leave_resets_half_day(client, seeded):
    r = patch(client, "EMP0004", "2026-07-07", status="LEAVE")
    assert (r.json()["half_day"], r.json()["work_hours"]) == (False, None)
    assert r.json()["history"][0]["changes"]["half_day"] == {"from": True, "to": False}


def test_absent_to_present_needs_and_uses_a_punch_in(client, seeded):
    # EMP0004 07-08 was ABSENT. Punch-in 09:45 is 15 minutes after the 09:30 start (> the 10 minute grace): late 15.
    at = ist_ms(2026, 7, 8, 9, 45)
    r = patch(client, "EMP0004", "2026-07-08", status="PRESENT", punch_in=at)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["punch_in"], r.json()["late_minutes"], r.json()["work_hours"]) == (
        "PRESENT", at, 15, None)
    assert r.json()["history"][0]["changes"] == {
        "status": {"from": "ABSENT", "to": "PRESENT"},
        "punch_in": {"from": None, "to": at},
        "late_minutes": {"from": 0, "to": 15},
    }


def test_leave_to_wfh_with_both_punch_times(client, seeded):
    # EMP0003 07-08 was LEAVE. 09:30 -> 19:00 = 9.5 h; 19:00 is exactly 30 minutes after the shift end: overtime 30.
    r = patch(client, "EMP0003", "2026-07-08", status="WFH", punch_in=ist_ms(2026, 7, 8, 9, 30),
              punch_out=ist_ms(2026, 7, 8, 19, 0))
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["late_minutes"], r.json()["overtime_minutes"], r.json()["half_day"]) == (
        9.5, 0, 30, False)
    assert set(r.json()["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "overtime_minutes"}


@pytest.mark.parametrize("emp,date,old,new", [("EMP0004", "2026-07-08", "ABSENT", "LEAVE"),
                                              ("EMP0003", "2026-07-08", "LEAVE", "ABSENT")])
def test_absent_and_leave_can_be_swapped(client, seeded, emp, date, old, new):
    r = patch(client, emp, date, status=new)
    assert r.status_code == 200 and r.json()["history"][0]["changes"] == {"status": {"from": old, "to": new}}


def test_overnight_shift_punch_out_correction_uses_the_shift_end_on_the_next_day(client, seeded):
    # EMP0005 07-06 (22:00-06:00), in 21:55. Punch-out corrected to 07:00 on the 7th: 21:55 -> 07:00 = 9 h 5 min = 9.08;
    # the shift ended at 06:00 on the 7th, so overtime 60.
    r = patch(client, "EMP0005", "2026-07-06", punch_out=ist_ms(2026, 7, 7, 7, 0))
    assert r.status_code == 200
    assert (r.json()["work_hours"], r.json()["overtime_minutes"]) == (9.08, 60)
    assert r.json()["history"][0]["changes"]["overtime_minutes"] == {"from": 40, "to": 60}


def test_overnight_punch_in_after_midnight_still_belongs_to_the_shift_that_started_the_day_before(client, seeded):
    # 03:00 on the 7th is earlier than the 06:00 shift end, so it is still the shift of 2026-07-06 (R1).
    # Late: 5 h after the 22:00 start = 300 min. Work: 03:00 -> 06:40 = 3 h 40 min = 3.67 -> half day.
    r = patch(client, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, 7, 7, 3, 0))
    assert r.status_code == 200
    assert (r.json()["late_minutes"], r.json()["work_hours"], r.json()["half_day"]) == (300, 3.67, True)


@pytest.mark.parametrize("wall", [(7, 6, 6, 0, 0), (7, 5, 23, 59, 59)])      # 06:00 on the 7th is the NEXT shift's day
def test_overnight_punch_in_on_another_attendance_date_is_422(client, seeded, wall):
    before = stored(seeded, "EMP0005", "2026-07-06")
    mo, d, h, m, s = wall
    r = patch(client, "EMP0005", "2026-07-06", punch_in=ist_ms(2026, mo, d, h, m, s))
    assert r.status_code == 422 and stored(seeded, "EMP0005", "2026-07-06") == before


# --------------------------------------------------------------------------- #
# History: shape, what is stored, append-only
# --------------------------------------------------------------------------- #
def test_history_entry_has_when_who_why_and_what_both_in_the_response_and_in_the_database(client, seeded, monkeypatch):
    monkeypatch.setattr(main, "now_utc", lambda: FIXED_NOW)
    at = ist_ms(2026, 7, 6, 9, 28)
    r = patch(client, "EMP0003", "2026-07-06", reason="  forgot to tap in  ", by="priya.hr", punch_in=at)
    entry = r.json()["history"][0]
    assert set(entry) == {"at", "by", "reason", "changes"}
    assert (entry["at"], entry["by"], entry["reason"]) == (main.datetime_to_ms(FIXED_NOW), "priya.hr", "  forgot to tap in  ")
    s = stored(seeded, "EMP0003", "2026-07-06")["history"][0]
    assert s["at"] == FIXED_NOW and isinstance(s["at"], datetime)                      # BSON date, not an integer
    assert isinstance(s["changes"]["punch_in"]["from"], datetime) and isinstance(s["changes"]["punch_in"]["to"], datetime)
    assert s["changes"]["late_minutes"] == {"from": 35, "to": 0}                       # numbers stay numbers
    assert main.record_to_api(stored(seeded, "EMP0003", "2026-07-06")) == r.json()      # response == what is stored


def test_each_successful_change_appends_exactly_one_entry_and_earlier_entries_are_never_touched(client, seeded):
    first = patch(client, "EMP0003", "2026-07-06", reason="first fix", by="a.hr", punch_in=ist_ms(2026, 7, 6, 9, 28))
    assert first.status_code == 200 and len(first.json()["history"]) == 1
    second = patch(client, "EMP0003", "2026-07-06", reason="second fix", by="b.hr", punch_out=ist_ms(2026, 7, 6, 18, 30))
    assert second.status_code == 200
    h = second.json()["history"]
    assert len(h) == 2 and h[0] == first.json()["history"][0]                           # the first entry is unchanged
    assert (h[1]["by"], h[1]["reason"]) == ("b.hr", "second fix")
    # 09:28 -> 18:30 = 9 h 2 min = 9.03; the entry records the values as they were just BEFORE this correction
    assert h[1]["changes"] == {
        "punch_out": {"from": ist_ms(2026, 7, 6, 19, 10), "to": ist_ms(2026, 7, 6, 18, 30)},
        "work_hours": {"from": 9.7, "to": 9.03},
        "overtime_minutes": {"from": 40, "to": 0},
    }
    assert len(stored(seeded, "EMP0003", "2026-07-06")["history"]) == 2


def test_an_existing_history_is_extended_not_replaced(client, seeded):
    before = api(seeded, "EMP0001", "2026-07-07")["history"]
    assert len(before) == 1
    r = patch(client, "EMP0001", "2026-07-07", status="ON_DUTY")
    assert r.status_code == 200 and r.json()["history"][0] == before[0] and len(r.json()["history"]) == 2


def test_failed_corrections_write_no_history(client, seeded):
    before = stored(seeded, "EMP0003", "2026-07-06")
    assert patch(client, "EMP0003", "2026-07-06", punch_in=ist_ms(2026, 7, 6, 10, 5)).status_code == 422   # no-op
    assert patch(client, "EMP0003", "2026-07-06", punch_out=ist_ms(2026, 7, 6, 9, 0)).status_code == 422   # before in
    assert stored(seeded, "EMP0003", "2026-07-06") == before


def test_legacy_record_without_history_or_half_day_can_be_corrected(client, seeded):
    assert "history" not in stored(seeded, "EMP0002", "2026-07-14") and "half_day" not in stored(seeded, "EMP0002", "2026-07-14")
    r = patch(client, "EMP0002", "2026-07-14", status="ON_DUTY")
    assert r.status_code == 200
    assert r.json()["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "ON_DUTY"}}   # half_day missing == False
    s = stored(seeded, "EMP0002", "2026-07-14")
    assert s["half_day"] is False and len(s["history"]) == 1


def test_a_history_field_that_is_null_is_started_not_pushed_to(client, employees):
    D = "2026-07-20"
    add_record(employees, "EMP0001", D, ist_ms(2026, 7, 20, 9, 30), history=None)
    r = patch(client, "EMP0001", D, status="WFH")
    assert r.status_code == 200 and len(r.json()["history"]) == 1
    assert len(stored(employees, "EMP0001", D)["history"]) == 1


# --------------------------------------------------------------------------- #
# Which fields count, and how
# --------------------------------------------------------------------------- #
def test_omitted_fields_are_unchanged(client, seeded):
    before = stored(seeded, "EMP0003", "2026-07-06")
    patch(client, "EMP0003", "2026-07-06", punch_out=ist_ms(2026, 7, 6, 18, 30))
    after = stored(seeded, "EMP0003", "2026-07-06")
    assert (after["status"], after["punch_in"]) == (before["status"], before["punch_in"])
    assert after["punch_out"] != before["punch_out"]


def test_derived_fields_sent_by_the_client_are_ignored(client, seeded):
    r = client.patch("/attendance/EMP0003/2026-07-06", json={      # raw body: `date` / `emp_code` are body keys here
        "reason": "because reasons", "regularized_by": "hr.admin", "punch_in": ist_ms(2026, 7, 6, 9, 28),
        "work_hours": 99.9, "late_minutes": 999, "overtime_minutes": 999, "half_day": True, "history": [{"x": 1}],
        "date": "1999-01-01", "emp_code": "EMP0001", "_id": "a", "id": "a"})
    assert r.status_code == 200
    b = r.json()
    assert (b["work_hours"], b["late_minutes"], b["overtime_minutes"], b["half_day"], b["date"], b["emp_code"]) == (
        9.7, 0, 40, False, "2026-07-06", "EMP0003")
    assert len(b["history"]) == 1 and b["history"][0]["by"] == "hr.admin"


def test_instants_are_truncated_to_whole_seconds_everywhere(client, seeded):
    at = ist_ms(2026, 7, 6, 9, 28, 30, 900)
    r = patch(client, "EMP0003", "2026-07-06", punch_in=at)
    assert r.json()["punch_in"] == at - 900 and r.json()["history"][0]["changes"]["punch_in"]["to"] == at - 900
    assert main.datetime_to_ms(stored(seeded, "EMP0003", "2026-07-06")["punch_in"]) == at - 900


def test_a_correction_that_moves_punch_in_by_less_than_a_second_is_a_no_op(client, seeded):
    r = patch(client, "EMP0003", "2026-07-06", punch_in=ist_ms(2026, 7, 6, 10, 5, 0, 999))   # same second as stored
    assert r.status_code == 422


def test_a_24_hour_window_includes_exactly_24_hours(client, seeded):
    r = patch(client, "EMP0006", "2026-07-07", punch_out=ist_ms(2026, 7, 7, 9, 35) + 86_400_000)
    assert r.status_code == 200 and r.json()["work_hours"] == 24.0


def test_punch_in_boundaries_of_the_attendance_date_on_a_day_shift(client, seeded):
    # EMP0006 07-07 is open. The whole IST day 2026-07-07 is allowed, the neighbouring seconds are not.
    for wall, ok in [((7, 7, 0, 0, 0), True), ((7, 7, 23, 59, 59), True), ((7, 6, 23, 59, 59), False),
                     ((7, 8, 0, 0, 0), False)]:
        r = patch(client, "EMP0006", "2026-07-07", punch_in=ist_ms(2026, *wall))
        assert (r.status_code == 200) == ok, wall


def test_sample_records_sent_their_own_values_are_no_ops(client, seeded, sample_logs):
    # Also proves the nine documented samples are internally consistent with the rules the PATCH recomputes with.
    for doc in sample_logs:
        body = {"status": doc["status"]}
        if doc.get("punch_in"):
            body["punch_in"] = main.datetime_to_ms(doc["punch_in"])
        if doc.get("punch_out"):
            body["punch_out"] = main.datetime_to_ms(doc["punch_out"])
        r = patch(client, doc["emp_code"], doc["date"], **body)
        assert r.status_code == 422, (doc["emp_code"], doc["date"], r.json())


def test_a_seeded_record_whose_stored_derived_values_drift_is_not_healed_by_a_request_that_changes_nothing(client, employees):
    D = "2026-07-20"
    add_record(employees, "EMP0001", D, ist_ms(2026, 7, 20, 9, 30), ist_ms(2026, 7, 20, 18, 30),
               late_minutes=99, work_hours=1.0)                                       # stored values disagree with R2/R4
    before = stored(employees, "EMP0001", D)
    r = patch(client, "EMP0001", D, punch_in=ist_ms(2026, 7, 20, 9, 30))               # same punch_in: nothing to correct
    assert r.status_code == 422 and stored(employees, "EMP0001", D) == before
    # A real correction recomputes from the final times, and the history shows the stored values as they were.
    r = patch(client, "EMP0001", D, punch_out=ist_ms(2026, 7, 20, 18, 0))
    assert r.status_code == 200
    assert r.json()["history"][0]["changes"]["late_minutes"] == {"from": 99, "to": 0}
    assert r.json()["history"][0]["changes"]["work_hours"] == {"from": 1.0, "to": 8.5}


# --------------------------------------------------------------------------- #
# 422: invalid corrections. Each one must leave the stored record exactly as it was.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fields", [
    {"status": "ABSENT", "punch_in": ist_ms(2026, 7, 6, 9, 0)},
    {"status": "ABSENT", "punch_out": ist_ms(2026, 7, 6, 18, 0)},
    {"status": "LEAVE", "punch_in": ist_ms(2026, 7, 6, 9, 0), "punch_out": ist_ms(2026, 7, 6, 18, 0)},
    {"status": "LEAVE", "punch_in": ist_ms(2026, 7, 6, 9, 0)},
])
def test_absent_or_leave_with_punch_times_is_422(client, seeded, fields):
    before = stored(seeded, "EMP0003", "2026-07-06")
    r = patch(client, "EMP0003", "2026-07-06", **fields)
    assert r.status_code == 422 and stored(seeded, "EMP0003", "2026-07-06") == before


def test_absent_with_punch_times_is_422_even_for_an_unknown_employee_because_it_needs_no_database(client, employees):
    r = patch(client, "EMP9999", "2026-07-06", status="ABSENT", punch_in=ist_ms(2026, 7, 6, 9, 0))
    assert r.status_code == 422                       # documented order: the body is checked before any 404


def test_giving_an_absent_record_a_punch_time_without_a_presence_status_is_422(client, seeded):
    before = stored(seeded, "EMP0004", "2026-07-08")
    for field in ("punch_in", "punch_out"):
        r = patch(client, "EMP0004", "2026-07-08", **{field: ist_ms(2026, 7, 8, 10, 0)})
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", field]
    assert stored(seeded, "EMP0004", "2026-07-08") == before


@pytest.mark.parametrize("emp,date,status", [("EMP0004", "2026-07-08", s) for s in ("PRESENT", "WFH", "ON_DUTY")]
                         + [("EMP0003", "2026-07-08", "PRESENT")])
def test_a_presence_status_without_any_punch_in_is_422(client, seeded, emp, date, status):
    before = stored(seeded, emp, date)
    r = patch(client, emp, date, status=status)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "punch_in"]
    assert stored(seeded, emp, date) == before


@pytest.mark.parametrize("fields,loc", [
    ({"punch_out": ist_ms(2026, 7, 7, 9, 35)}, "punch_out"),                       # equal to the punch_in
    ({"punch_out": ist_ms(2026, 7, 7, 9, 0)}, "punch_out"),                        # before it
    ({"punch_in": ist_ms(2026, 7, 7, 10, 0), "punch_out": ist_ms(2026, 7, 7, 9, 0)}, "punch_out"),
    ({"punch_out": ist_ms(2026, 7, 7, 9, 35) + 86_401_000}, "punch_out"),          # 24 h and 1 s
])
def test_punch_out_must_be_after_punch_in_and_within_24_hours(client, seeded, fields, loc):
    before = stored(seeded, "EMP0006", "2026-07-07")
    r = patch(client, "EMP0006", "2026-07-07", **fields)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", loc]
    assert stored(seeded, "EMP0006", "2026-07-07") == before


@pytest.mark.parametrize("new_in", [ist_ms(2026, 7, 6, 18, 35), ist_ms(2026, 7, 6, 19, 0)])
def test_moving_punch_in_to_or_after_the_stored_punch_out_is_422(client, seeded, new_in):
    before = stored(seeded, "EMP0001", "2026-07-06")                               # in 09:28, out 18:35
    assert patch(client, "EMP0001", "2026-07-06", punch_in=new_in).status_code == 422
    assert stored(seeded, "EMP0001", "2026-07-06") == before


def test_the_24_hour_limit_is_checked_on_the_final_pair_of_times(client, seeded):
    assert patch(client, "EMP0001", "2026-07-06", punch_out=ist_ms(2026, 7, 7, 9, 0)).status_code == 200    # 23 h 32 min
    r = patch(client, "EMP0001", "2026-07-06", punch_in=ist_ms(2026, 7, 6, 8, 0))                         # now 25 h
    assert r.status_code == 422 and len(stored(seeded, "EMP0001", "2026-07-06")["history"]) == 1


@pytest.mark.parametrize("fields", [
    {},                                                           # reason and regularized_by only
    {"status": "PRESENT"},                                        # same status
    {"punch_in": ist_ms(2026, 7, 6, 10, 5)},                      # same punch_in
    {"punch_out": ist_ms(2026, 7, 6, 19, 10)},
    {"status": "PRESENT", "punch_in": ist_ms(2026, 7, 6, 10, 5), "punch_out": ist_ms(2026, 7, 6, 19, 10)},
    {"work_hours": 1.0, "late_minutes": 5, "overtime_minutes": 5, "half_day": True, "history": []},   # derived: ignored
])
def test_a_request_that_changes_nothing_is_422(client, seeded, fields):
    before = stored(seeded, "EMP0003", "2026-07-06")
    r = patch(client, "EMP0003", "2026-07-06", **fields)
    assert r.status_code == 422 and r.json()["detail"][0]["msg"] == "the request changes nothing"
    assert stored(seeded, "EMP0003", "2026-07-06") == before


def test_the_same_correction_twice_is_200_then_422_with_one_history_entry(client, seeded):
    fields = {"punch_in": ist_ms(2026, 7, 6, 9, 28)}
    assert patch(client, "EMP0003", "2026-07-06", **fields).status_code == 200
    assert patch(client, "EMP0003", "2026-07-06", **fields).status_code == 422
    assert len(stored(seeded, "EMP0003", "2026-07-06")["history"]) == 1


@pytest.mark.parametrize("body", [
    {"reason": "abcd", "regularized_by": "hr"},                                   # 4 characters
    {"reason": "a" * 201, "regularized_by": "hr"},
    {"reason": "", "regularized_by": "hr"},
    {"regularized_by": "hr"},                                                     # reason missing
    {"reason": "valid reason"},                                                   # regularized_by missing
    {"reason": "valid reason", "regularized_by": ""},
    {"reason": "valid reason", "regularized_by": "x" * 51},
    {"reason": 12345, "regularized_by": "hr"}, {"reason": None, "regularized_by": "hr"},
    {"reason": "valid reason", "regularized_by": 7}, {"reason": "valid reason", "regularized_by": None},
    {"reason": "valid reason", "regularized_by": "hr", "status": "banana"},
    {"reason": "valid reason", "regularized_by": "hr", "status": "present"},     # case matters
    {"reason": "valid reason", "regularized_by": "hr", "status": None},
    {"reason": "valid reason", "regularized_by": "hr", "status": 1},
    *({"reason": "valid reason", "regularized_by": "hr", field: bad}
      for field in ("punch_in", "punch_out")
      for bad in (1783312500, 1783312500000.0, 1783312500000.5, "1783312500000", True, None, 0, -5,
                  99_999_999_999, 4_102_444_800_001, [1783312500000], {"a": 1})),
    {"reason": "valid reason", "regularized_by": "hr", "emp_code": "EMP0003", "status": "banana"},
])
def test_invalid_bodies_are_422_and_change_nothing(client, seeded, body):
    before = stored(seeded, "EMP0003", "2026-07-06")
    r = client.patch("/attendance/EMP0003/2026-07-06", json=body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert stored(seeded, "EMP0003", "2026-07-06") == before


@pytest.mark.parametrize("reason,by", [("abcde", "h"), ("a" * 200, "x" * 50)])    # the exact limits are allowed
def test_reason_and_regularized_by_limits_are_inclusive(client, seeded, reason, by):
    r = patch(client, "EMP0003", "2026-07-06", reason=reason, by=by, status="WFH")
    assert r.status_code == 200 and (r.json()["history"][0]["reason"], r.json()["history"][0]["by"]) == (reason, by)


@pytest.mark.parametrize("bad_date", ["2026-13-01", "2026-02-30", "2026-00-10", "2026-7-6", "20260706", "yesterday",
                                      "2026-07-06x", "٢٠٢٦-٠٧-٠٦", "0000-01-01"])
def test_invalid_path_dates_are_422(client, seeded, bad_date):
    r = patch(client, "EMP0003", bad_date, status="WFH")
    assert r.status_code == 422


@pytest.mark.parametrize("raw", [
    b'{"reason": "valid reason", "regularized_by": "hr", "punch_in": NaN}',
    b'{"reason": "valid reason", "regularized_by": "hr", "punch_in": Infinity}',
    b'{"reason": "\\ud800 lone surrogate", "regularized_by": "hr", "status": "WFH"}',   # not storable as BSON
    b'{"reason": "valid reason", "regularized_by": "\\udfff", "status": "WFH"}',
    b'{"reason": "valid reason", "regularized_by": "hr", "punch_in": ' + b"[" * 5000 + b"]" * 5000 + b"}",
    b"not json", b"", b"[]", b"null", b"\xff\xfe\xfd",
])
def test_hostile_bodies_are_422_never_500(seeded, raw):
    r = TestClient(main.app, raise_server_exceptions=False).patch(
        "/attendance/EMP0003/2026-07-06", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_a_422_does_not_echo_the_input(client, seeded):
    r = client.patch("/attendance/EMP0003/2026-07-06", json={"reason": "secret-value", "regularized_by": "hr",
                                                             "punch_in": "secret-value"})
    assert r.status_code == 422 and "secret-value" not in r.text
    assert all(set(e) == {"type", "loc", "msg"} for e in r.json()["detail"])


# --------------------------------------------------------------------------- #
# 404: missing records
# --------------------------------------------------------------------------- #
def test_unknown_employee_is_404(client, seeded):
    r = patch(client, "EMP9999", "2026-07-06", status="WFH")
    assert r.status_code == 404 and r.json() == {"detail": "unknown employee"}


def test_known_employee_without_a_record_for_that_date_is_404(client, seeded):
    r = patch(client, "EMP0003", "2026-07-09", status="WFH")
    assert r.status_code == 404 and "no attendance record" in r.json()["detail"]
    assert seeded.attendance_logs.count_documents({"emp_code": "EMP0003"}) == 2       # nothing was created


def test_a_record_for_another_day_or_another_employee_does_not_count(client, seeded):
    assert patch(client, "EMP0006", "2026-07-06", status="WFH").status_code == 404    # EMP0006 only has the 7th
    assert patch(client, "EMP0002", "2026-07-06", status="WFH").status_code == 404    # EMP0002 only has the 14th


def test_emp_code_is_case_sensitive(client, seeded):
    assert patch(client, "emp0003", "2026-07-06", status="WFH").status_code == 404


def test_a_log_whose_employee_does_not_exist_is_404_unknown_employee(client, mdb):
    mdb.attendance_logs.insert_one({"emp_code": "EMP7777", "date": "2026-07-06", "status": "PRESENT",
                                    "punch_in": main.ms_to_datetime(ist_ms(2026, 7, 6, 9, 30))})
    r = patch(client, "EMP7777", "2026-07-06", status="WFH")
    assert r.status_code == 404 and r.json() == {"detail": "unknown employee"}


def test_404_is_decided_before_the_state_dependent_422s(client, seeded):
    # no record for the 9th: a no-op body or a "presence needs a punch_in" body is a 404, not a 422
    assert patch(client, "EMP0004", "2026-07-09").status_code == 404
    assert patch(client, "EMP0004", "2026-07-09", status="PRESENT").status_code == 404
    assert patch(client, "EMP9999", "2026-07-06").status_code == 404


def test_database_failure_is_503_not_500(monkeypatch):
    from pymongo.errors import ConnectionFailure

    class Broken:
        def __getattr__(self, name):
            raise ConnectionFailure("down")

        def __getitem__(self, name):
            raise ConnectionFailure("down")

    monkeypatch.setattr(main, "db", Broken())
    r = TestClient(main.app, raise_server_exceptions=False).patch(
        "/attendance/EMP0003/2026-07-06", json={"reason": "valid reason", "regularized_by": "hr", "status": "WFH"})
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}


# --------------------------------------------------------------------------- #
# Response shape
# --------------------------------------------------------------------------- #
def test_response_has_exactly_the_attendance_record_fields_and_no_internal_id(client, seeded):
    body = patch(client, "EMP0003", "2026-07-06", status="WFH").json()
    assert set(body) == RECORD_KEYS and "_id" not in body and "id" not in body


# --------------------------------------------------------------------------- #
# Concurrency, DETERMINISTIC interleavings (see the module docstring for what this does and does not prove)
# --------------------------------------------------------------------------- #
def rival_patch(emp, date, **fields):
    def go(_collection):
        r = patch(TestClient(main.app), emp, date, reason="rival fix", by="rival.hr", **fields)
        assert r.status_code == 200, r.text
    return go


def test_two_corrections_that_overlap_both_keep_their_history_entry_and_both_effects(client, seeded, monkeypatch):
    # Our request A reads EMP0003 07-06 (in 10:05, out 19:10). Before A writes, a rival B corrects punch_out to 18:30.
    # Without a compare-and-set A would write its stale snapshot over B's change (and B's entry would describe a
    # change that was undone). Now A notices, re-reads, and applies its change on top of B's.
    racing_db(seeded, monkeypatch, rival_patch("EMP0003", "2026-07-06", punch_out=ist_ms(2026, 7, 6, 18, 30)))
    r = patch(client, "EMP0003", "2026-07-06", reason="A: late tap-in", by="a.hr", punch_in=ist_ms(2026, 7, 6, 9, 28))
    assert r.status_code == 200
    s = api(seeded, "EMP0003", "2026-07-06")
    assert r.json() == s
    assert (s["punch_in"], s["punch_out"]) == (ist_ms(2026, 7, 6, 9, 28), ist_ms(2026, 7, 6, 18, 30))   # both effects
    assert (s["work_hours"], s["late_minutes"], s["overtime_minutes"]) == (9.03, 0, 0)                  # from final times
    assert [(e["by"], e["reason"]) for e in s["history"]] == [("rival.hr", "rival fix"), ("a.hr", "A: late tap-in")]
    # B's entry: 10:05 -> 18:30 = 8.42, overtime 40 -> 0. A's entry starts from what B left, not from the old snapshot.
    assert s["history"][0]["changes"]["work_hours"] == {"from": 9.08, "to": 8.42}
    assert s["history"][1]["changes"] == {
        "punch_in": {"from": ist_ms(2026, 7, 6, 10, 5), "to": ist_ms(2026, 7, 6, 9, 28)},
        "work_hours": {"from": 8.42, "to": 9.03},
        "late_minutes": {"from": 35, "to": 0},
    }


def test_if_the_rival_already_made_our_change_we_become_a_422_and_history_has_one_entry(client, seeded, monkeypatch):
    same = {"punch_in": ist_ms(2026, 7, 6, 9, 28)}
    racing_db(seeded, monkeypatch, rival_patch("EMP0003", "2026-07-06", **same))
    r = patch(client, "EMP0003", "2026-07-06", **same)
    assert r.status_code == 422 and r.json()["detail"][0]["msg"] == "the request changes nothing"
    assert len(stored(seeded, "EMP0003", "2026-07-06")["history"]) == 1


def test_a_punch_out_that_lands_between_our_read_and_write_is_not_erased_by_our_correction(client, seeded, monkeypatch):
    # A: status -> WFH on the open record EMP0006 07-07. Before A writes, the employee punches out at 19:10.
    def rival_punch_out(_collection):
        r = TestClient(main.app).post("/attendance/punch-out",
                                      json={"emp_code": "EMP0006", "punched_at": ist_ms(2026, 7, 7, 19, 10)})
        assert r.status_code == 200, r.text

    racing_db(seeded, monkeypatch, rival_punch_out)
    r = patch(client, "EMP0006", "2026-07-07", status="WFH")
    assert r.status_code == 200
    s = api(seeded, "EMP0006", "2026-07-07")
    assert r.json() == s
    assert (s["status"], s["punch_out"], s["work_hours"], s["overtime_minutes"]) == ("WFH", ist_ms(2026, 7, 7, 19, 10),
                                                                                     9.58, 40)
    assert len(s["history"]) == 1 and s["history"][0]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_a_legacy_record_that_gets_its_first_history_entry_in_between_is_handled(client, seeded, monkeypatch):
    # EMP0002 07-14 has no `history` field. The rival creates it; our retry must append, not start a new array.
    racing_db(seeded, monkeypatch, rival_patch("EMP0002", "2026-07-14", status="WFH"))
    r = patch(client, "EMP0002", "2026-07-14", reason="A: duty", by="a.hr", status="ON_DUTY")
    assert r.status_code == 200
    h = api(seeded, "EMP0002", "2026-07-14")["history"]
    assert [e["by"] for e in h] == ["rival.hr", "a.hr"]
    assert h[1]["changes"] == {"status": {"from": "WFH", "to": "ON_DUTY"}}


def test_a_record_that_keeps_changing_ends_in_409_without_writing_or_losing_anything(client, seeded, monkeypatch):
    statuses = iter(["WFH", "ON_DUTY", "PRESENT", "WFH", "ON_DUTY", "PRESENT", "WFH", "ON_DUTY"])

    def keep_changing(collection):
        collection.update_one({"emp_code": "EMP0003", "date": "2026-07-06"}, {"$set": {"status": next(statuses)}})

    racing = racing_db(seeded, monkeypatch, keep_changing, every_time=True)
    r = patch(client, "EMP0003", "2026-07-06", punch_in=ist_ms(2026, 7, 6, 9, 28))
    assert r.status_code == 409 and racing.reads == main.MAX_WRITE_ATTEMPTS
    s = stored(seeded, "EMP0003", "2026-07-06")
    assert s["history"] == [] and s["punch_in"] == main.ms_to_datetime(ist_ms(2026, 7, 6, 10, 5))   # none of ours applied


# --------------------------------------------------------------------------- #
# plan_regularization: the pure rules, without HTTP
# --------------------------------------------------------------------------- #
def _doc(**over):
    base = {"emp_code": "EMP0001", "date": "2026-07-20", "status": "PRESENT",
            "punch_in": main.ms_to_datetime(ist_ms(2026, 7, 20, 9, 30)), "punch_out": None,
            "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
    base.update(over)
    return base


def test_plan_returns_the_final_state_and_only_the_changed_fields():
    new, changes = main.plan_regularization(_doc(), "09:30", "18:30",
                                            punch_out=main.ms_to_datetime(ist_ms(2026, 7, 20, 19, 0)))
    assert (new["status"], new["work_hours"], new["overtime_minutes"], new["half_day"], new["late_minutes"]) == (
        "PRESENT", 9.5, 30, False, 0)
    assert list(changes) == ["punch_out", "work_hours", "overtime_minutes"]


def test_plan_treats_missing_legacy_fields_as_their_documented_defaults():
    legacy = {"emp_code": "EMP0001", "date": "2026-07-20", "status": "PRESENT",
              "punch_in": main.ms_to_datetime(ist_ms(2026, 7, 20, 9, 30))}              # no derived fields at all
    new, changes = main.plan_regularization(legacy, "09:30", "18:30", status="WFH")
    assert list(changes) == ["status"] and new["half_day"] is False


def test_plan_reads_naive_datetimes_as_utc():
    naive = _doc(punch_in=datetime(2026, 7, 20, 4, 0, 0))                                # 09:30 IST, no tzinfo
    with pytest.raises(main.RegularizationRejected, match="changes nothing"):
        main.plan_regularization(naive, "09:30", "18:30", punch_in=main.ms_to_datetime(ist_ms(2026, 7, 20, 9, 30)))


@pytest.mark.parametrize("guard_input,expected", [
    ({"history": []}, {"history": {"$size": 0}}),
    ({"history": [{}, {}]}, {"history": {"$size": 2}}),
    ({}, {"history": None}),                       # missing
    ({"history": None}, {"history": None}),        # null
])
def test_history_guard(guard_input, expected):
    assert main._history_guard(guard_input) == expected
