"""R1-R5 at their exact boundaries. Expected values are written out by hand from the rule text in
openapi.yaml, not derived from the implementation."""
from datetime import date, datetime

import pytest

from app.main import (IST, UTC, attendance_date, compute_half_day, compute_late_minutes,
                      compute_overtime_minutes, compute_work_hours, derive_fields, is_overnight,
                      shift_end_at, shift_start_at)

D6, D7 = date(2026, 7, 6), date(2026, 7, 7)


def ist(h, m, s=0, us=0, day=6):
    return datetime(2026, 7, day, h, m, s, us, tzinfo=IST)


# ---- R2 late -------------------------------------------------------------------------------- #
@pytest.mark.parametrize("when,expected", [
    (ist(9, 40, 0), 0),                  # exactly 10:00 after start: inside grace
    (ist(9, 40, 0, 900000), 0),          # 09:40:00.900 counts as 09:40:00 (R1 truncation)
    (ist(9, 40, 0, 999999), 0),
    (ist(9, 40, 1), 10),                 # first late second; minutes counted from shift_start
    (ist(9, 40, 59), 10),
    (ist(9, 41, 0), 11),
    (ist(10, 15, 59), 45),               # example from the contract
    (ist(9, 30, 0), 0), (ist(9, 0, 0), 0),   # on time / early is never negative
])
def test_late_minutes_shift_0930(when, expected):
    assert compute_late_minutes(when, D6, "09:30") == expected


def test_late_minutes_does_not_depend_on_the_zone_the_instant_is_expressed_in():
    punch = ist(9, 40, 1)
    assert compute_late_minutes(punch.astimezone(UTC), D6, "09:30") == 10
    assert compute_late_minutes(punch.astimezone(UTC).replace(tzinfo=None), D6, "09:30") == 10  # naive = UTC


@pytest.mark.parametrize("when,att,expected", [
    (ist(22, 10, 0), D6, 0), (ist(22, 10, 1), D6, 10), (ist(21, 55), D6, 0),
    (ist(0, 5, day=7), D6, 125),         # after midnight, still the shift that started on the 6th
    (ist(3, 0, day=7), D6, 300),
])
def test_late_minutes_overnight_is_anchored_to_the_attendance_date(when, att, expected):
    assert compute_late_minutes(when, att, "22:00") == expected


# ---- R3 overtime ---------------------------------------------------------------------------- #
@pytest.mark.parametrize("when,expected", [
    (ist(18, 30), 0), (ist(17, 0), 0),
    (ist(18, 55), 0),                    # 25 min: under the 30-minute minimum
    (ist(18, 59, 59), 0),                # 29:59
    (ist(19, 0, 0), 30), (ist(19, 0, 59), 30),   # floor
    (ist(19, 10), 40),
    (ist(1, 0, day=7), 390),             # worked past midnight on a normal shift: still valid
])
def test_overtime_normal_shift(when, expected):
    assert compute_overtime_minutes(when, D6, "09:30", "18:30") == expected


@pytest.mark.parametrize("when,expected", [
    (ist(6, 40, day=7), 40), (ist(6, 29, 59, day=7), 0), (ist(6, 30, day=7), 30), (ist(5, 0, day=7), 0),
])
def test_overtime_overnight_shift_ends_on_the_next_day(when, expected):
    assert compute_overtime_minutes(when, D6, "22:00", "06:00") == expected


def test_shift_ending_at_midnight_is_overnight():
    assert is_overnight("16:00", "00:00")
    assert shift_end_at(D6, "16:00", "00:00") == ist(0, 0, day=7)
    assert compute_overtime_minutes(ist(0, 30, day=7), D6, "16:00", "00:00") == 30
    assert compute_overtime_minutes(ist(23, 59), D6, "16:00", "00:00") == 0


@pytest.mark.parametrize("start,end,expected", [
    ("09:30", "18:30", False), ("22:00", "06:00", True), ("16:00", "00:00", True), ("09:00", "09:00", True),
])
def test_is_overnight(start, end, expected):
    assert is_overnight(start, end) is expected


def test_shift_instants():
    assert shift_start_at(D6, "09:30") == ist(9, 30)
    assert shift_end_at(D6, "09:30", "18:30") == ist(18, 30)
    assert shift_end_at(D6, "22:00", "06:00") == ist(6, 0, day=7)


# ---- R1 attendance date --------------------------------------------------------------------- #
def test_attendance_date_is_the_ist_date_not_the_utc_date():
    just_before = datetime(2026, 7, 6, 18, 29, 59, tzinfo=UTC)   # 23:59:59 IST on the 6th
    just_after = datetime(2026, 7, 6, 18, 30, 0, tzinfo=UTC)     # 00:00:00 IST on the 7th
    assert attendance_date(just_before, "09:30", "18:30") == D6
    assert attendance_date(just_after, "09:30", "18:30") == D7


@pytest.mark.parametrize("when,expected", [
    (ist(21, 55), D6),
    (ist(12, 0), D6),
    (ist(0, 0, 0, day=7), D6),            # after midnight, earlier than 06:00 -> previous day's shift
    (ist(5, 59, 59, day=7), D6),
    (ist(5, 59, 59, 999999, day=7), D6),  # truncated to 05:59:59
    (ist(6, 0, 0, day=7), D7),            # NOT earlier than shift_end -> its own day
])
def test_attendance_date_overnight_rule(when, expected):
    assert attendance_date(when, "22:00", "06:00") == expected


def test_attendance_date_for_shift_ending_at_midnight_never_moves_back():
    assert attendance_date(ist(0, 30, day=7), "16:00", "00:00") == D7


# ---- R4 work hours / R5 half day ------------------------------------------------------------ #
T0 = datetime(2026, 7, 6, 4, 0, 0, tzinfo=UTC)


def secs(n):
    from datetime import timedelta
    return T0, T0 + timedelta(seconds=n)


@pytest.mark.parametrize("seconds,hours,half", [
    (450, 0.13, True),        # 0.125 h: half-up -> 0.13 (Python round(0.125, 2) gives 0.12)
    (18, 0.01, True),         # 0.005 h: an exact tie rounds up
    (1, 0.0, True),
    (12600, 3.5, True),
    (16181, 4.49, True),      # 4.4947 -> 4.49 < 4.50
    (16182, 4.5, False),      # 4.495 -> 4.50 (half-up) -> NOT a half day
    (16218, 4.51, False),     # 4.505 -> 4.51
    (32820, 9.12, False),     # 9 h 07 min
])
def test_work_hours_half_up_and_half_day(seconds, hours, half):
    a, b = secs(seconds)
    assert compute_work_hours(a, b) == hours
    assert compute_half_day(a, b) is half


def test_work_hours_truncates_both_instants_first():
    a = datetime(2026, 7, 6, 9, 0, 0, 900000, tzinfo=UTC)
    b = datetime(2026, 7, 6, 9, 0, 1, 100000, tzinfo=UTC)
    assert compute_work_hours(a, b) == 0.0                       # 09:00:00 -> 09:00:01 = 1 s


@pytest.mark.parametrize("a,b", [
    (datetime(2026, 7, 6, 9, 0, 0, 100000, tzinfo=UTC), datetime(2026, 7, 6, 9, 0, 0, 900000, tzinfo=UTC)),  # equal after truncation
    (datetime(2026, 7, 6, 9, 0, 1, tzinfo=UTC), datetime(2026, 7, 6, 9, 0, 0, tzinfo=UTC)),                  # backwards
])
def test_work_hours_requires_punch_out_after_punch_in(a, b):
    with pytest.raises(ValueError):
        compute_work_hours(a, b)


# ---- derive_fields: the table in DATA_MODEL.md ---------------------------------------------- #
@pytest.mark.parametrize("status", ["ABSENT", "LEAVE"])
def test_derive_non_presence_statuses(status):
    assert derive_fields(status, None, None, D6, "09:30", "18:30") == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_derive_open_presence_record():
    got = derive_fields("PRESENT", ist(10, 5), None, D6, "09:30", "18:30")
    assert got == {"work_hours": None, "late_minutes": 35, "overtime_minutes": 0, "half_day": False}


def test_derive_closed_record_late_with_overtime():
    got = derive_fields("WFH", ist(10, 5), ist(19, 10), D6, "09:30", "18:30")
    assert got == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}


def test_derive_overnight_record():
    got = derive_fields("ON_DUTY", ist(21, 55), ist(6, 40, day=7), D6, "22:00", "06:00")
    assert got == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


@pytest.mark.parametrize("args", [
    ("PRESENT", None, None),                       # presence status needs punch_in
    ("BOGUS", ist(9, 0), None),                    # unknown status
])
def test_derive_rejects_impossible_input(args):
    with pytest.raises(ValueError):
        derive_fields(*args, D6, "09:30", "18:30")
