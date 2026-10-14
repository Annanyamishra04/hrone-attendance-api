"""Epoch-millisecond conversion, whole-second truncation and strict calendar-string parsing."""
import calendar
import random
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.main import (EPOCH_MS_MAX, EPOCH_MS_MIN, IST, UTC, EpochMillis, datetime_to_ms, hhmm_to_seconds,
                      month_bounds, ms_to_datetime, now_utc, parse_date, parse_month, to_whole_seconds)

epoch_adapter = TypeAdapter(EpochMillis)
MS = 1783312500000  # 2026-07-06 04:35:00 UTC = 10:05 IST (the openapi.yaml example value)


# ---- request validation: EpochMillis -------------------------------------------------------- #
@pytest.mark.parametrize("value", [MS, EPOCH_MS_MIN, EPOCH_MS_MAX])
def test_epoch_millis_accepts_valid_integers(value):
    assert epoch_adapter.validate_python(value) == value


@pytest.mark.parametrize("value", [
    MS + 0.0, MS + 0.5, str(MS), True, False, None, [], {},   # wrong types (float, string, bool, ...)
    1783312500,                                               # seconds, not milliseconds
    EPOCH_MS_MIN - 1, EPOCH_MS_MAX + 1, 0, -MS,               # out of range
])
def test_epoch_millis_rejects_everything_else(value):
    with pytest.raises(ValidationError):
        epoch_adapter.validate_python(value)


@pytest.mark.parametrize("raw", [b"1783312500000.0", b'"1783312500000"', b"true", b"null", b"1783312500"])
def test_epoch_millis_rejects_bad_json_tokens(raw):
    """Same checks on raw JSON text, which is what a client actually sends."""
    with pytest.raises(ValidationError):
        epoch_adapter.validate_json(raw)


# ---- conversions ---------------------------------------------------------------------------- #
def test_ms_to_datetime_is_utc_and_matches_known_instant():
    assert ms_to_datetime(MS) == datetime(2026, 7, 6, 4, 35, 0, tzinfo=UTC)
    assert ms_to_datetime(MS).utcoffset() == timedelta(0)


def test_ms_to_datetime_truncates_to_whole_seconds_not_rounds():
    assert ms_to_datetime(MS + 999) == ms_to_datetime(MS)      # .999 s is dropped, not rounded up
    assert ms_to_datetime(MS + 1000) == ms_to_datetime(MS) + timedelta(seconds=1)


@pytest.mark.parametrize("bad", [True, 1.5, "1783312500000", None, EPOCH_MS_MIN - 1, EPOCH_MS_MAX + 1])
def test_ms_to_datetime_rejects_invalid_input(bad):
    with pytest.raises(ValueError):
        ms_to_datetime(bad)


def test_datetime_to_ms_is_exact_with_milliseconds():
    dt = datetime(2026, 7, 6, 4, 35, 0, 123000, tzinfo=UTC)
    assert datetime_to_ms(dt) == calendar.timegm(dt.utctimetuple()) * 1000 + 123


def test_datetime_to_ms_never_off_by_one_millisecond():
    # int(dt.timestamp() * 1000) is wrong by 1 ms for ~0.6% of values (float arithmetic); these
    # three are known failures of that approach, followed by a seeded sweep over the valid range.
    known_float_failures = [543817582558, 543043827836, 1095278878735]
    rng = random.Random(1)
    sweep = [rng.randrange(EPOCH_MS_MIN, EPOCH_MS_MAX) for _ in range(5000)]
    for ms in known_float_failures + sweep:
        assert datetime_to_ms(datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)) == ms


def test_datetime_to_ms_same_instant_in_any_zone_and_naive_means_utc():
    aware_utc = datetime(2026, 7, 6, 4, 35, tzinfo=UTC)
    assert datetime_to_ms(aware_utc) == MS
    assert datetime_to_ms(aware_utc.astimezone(IST)) == MS
    assert datetime_to_ms(datetime(2026, 7, 6, 4, 35)) == MS   # naive = what MongoDB stored


def test_round_trip_keeps_whole_seconds():
    assert datetime_to_ms(ms_to_datetime(MS + 999)) == MS


def test_to_whole_seconds_truncates_and_normalises():
    assert to_whole_seconds(datetime(2026, 7, 6, 9, 40, 0, 900000, tzinfo=IST)) == datetime(2026, 7, 6, 4, 10, tzinfo=UTC)
    assert to_whole_seconds(datetime(2026, 7, 6, 4, 10, 0, 999999)).tzinfo == UTC


def test_now_utc_is_aware_whole_seconds():
    n = now_utc()
    assert n.tzinfo is not None and n.utcoffset() == timedelta(0) and n.microsecond == 0


# ---- strict calendar strings ---------------------------------------------------------------- #
@pytest.mark.parametrize("ok", ["2026-07-06", "2028-02-29", "1999-12-31"])
def test_parse_date_accepts_real_dates(ok):
    assert parse_date(ok).isoformat() == ok


@pytest.mark.parametrize("bad", [
    "2026-02-30", "2027-02-29", "2026-13-01", "2026-00-10", "0000-01-01",
    "2026-7-6", "20260706", "2026-07-06\n", " 2026-07-06", "2026-07-06T00:00:00",
    "\uff12\uff10\uff12\uff16-07-06",   # full-width digits
    "", "abc", 20260706, None,
])
def test_parse_date_rejects_everything_else(bad):
    with pytest.raises(ValueError):
        parse_date(bad)


def test_parse_month_and_bounds():
    assert parse_month("2026-07") == (2026, 7)
    assert month_bounds("2026-07") == ("2026-07-01", "2026-07-31")
    assert month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    assert month_bounds("2028-02") == ("2028-02-01", "2028-02-29")   # leap year
    assert month_bounds("2026-12") == ("2026-12-01", "2026-12-31")


@pytest.mark.parametrize("bad", ["2026-13", "2026-00", "2026-7", "202607", "0000-05", "2026-07-01", "2026-07\n", None])
def test_parse_month_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_month(bad)


@pytest.mark.parametrize("text,secs", [("00:00", 0), ("09:30", 34200), ("18:30", 66600), ("23:59", 86340)])
def test_hhmm_to_seconds(text, secs):
    assert hhmm_to_seconds(text) == secs


@pytest.mark.parametrize("bad", ["24:00", "9:30", "09:60", "0930", "09:30\n", "09:30:00", "", None])
def test_hhmm_rejects_invalid(bad):
    with pytest.raises(ValueError):
        hhmm_to_seconds(bad)
