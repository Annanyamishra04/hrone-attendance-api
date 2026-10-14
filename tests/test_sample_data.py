"""The nine documents in tests/fixtures.py were written by 'the API' in DATA_MODEL.md, so our rules must
reproduce every stored derived value and every stored `date` exactly."""
from app.main import PRESENCE_STATUSES, attendance_date, derive_fields

DEFAULTS = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False, "work_hours": None}


def test_fixture_shape(sample_logs, sample_employees):
    assert len(sample_logs) == 9 and len(sample_employees) == 6
    assert all(l["punch_in"] is None or l["punch_in"].tzinfo is not None for l in sample_logs)  # tz-aware


def test_every_sample_record_is_reproduced(sample_logs, sample_employees):
    checked = 0
    for log in sample_logs:
        emp = sample_employees[log["emp_code"]]
        ss, se = emp["shift_start"], emp["shift_end"]
        if log["status"] in PRESENCE_STATUSES:
            att = attendance_date(log["punch_in"], ss, se)
            assert att.isoformat() == log["date"], log
        else:
            assert log["punch_in"] is None and log["punch_out"] is None
            att = None
        got = derive_fields(log["status"], log["punch_in"], log["punch_out"], att, ss, se)
        for field, default in DEFAULTS.items():
            assert got[field] == log.get(field, default), (field, log)   # missing field == default
        checked += 1
    assert checked == 9


def test_legacy_record_really_lacks_fields_and_defaults_apply(sample_logs):
    legacy = next(l for l in sample_logs if l["emp_code"] == "EMP0002")
    assert "history" not in legacy and "half_day" not in legacy
    assert legacy.get("half_day", False) is False


def test_overnight_sample_is_dated_by_shift_start(sample_logs, sample_employees):
    log = next(l for l in sample_logs if l["emp_code"] == "EMP0005")
    emp = sample_employees["EMP0005"]
    assert log["punch_out"].date() > log["punch_in"].date() or log["punch_out"].day != log["punch_in"].day
    assert attendance_date(log["punch_in"], emp["shift_start"], emp["shift_end"]).isoformat() == log["date"] == "2026-07-06"
