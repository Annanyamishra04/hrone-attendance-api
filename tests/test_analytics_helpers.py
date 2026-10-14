"""Pure helpers behind the analytics endpoints: integer half-up division, working-day arithmetic, the trend range."""
import calendar
from datetime import date, timedelta
from fractions import Fraction
from math import floor

import pytest
from fastapi.exceptions import RequestValidationError

from app import main


def test_half_up_div_matches_exact_fractions_for_a_large_grid():
    for n in range(0, 400):
        for d in range(1, 130):
            assert main.half_up_div(n, d) == floor(Fraction(n, d) + Fraction(1, 2)), (n, d)


@pytest.mark.parametrize("n,d,expected", [(1, 2, 1), (3, 2, 2), (5, 2, 3), (1, 3, 0), (2, 3, 1), (125, 100, 1), (7, 14, 1)])
def test_half_up_div_examples(n, d, expected):
    assert main.half_up_div(n, d) == expected               # ties round UP: 0.5 -> 1, 1.5 -> 2, 2.5 -> 3 (Python's round() gives 0, 2, 2)


def test_the_pipeline_rate_formula_equals_half_up_of_the_exact_fraction():
    """The trend pipeline computes floor((units * 10000 + h) / (2 h)) with a DOUBLE division. For every realistic
    (units, headcount) the double result must equal the exact integer answer."""
    for h in range(1, 400):
        for units in range(0, 2 * h + 3):
            exact = main.half_up_div(units * 10000, 2 * h)
            assert floor((units * 10000 + h) / (2 * h)) == exact, (units, h)


def test_the_pipeline_moving_average_formula_equals_half_up_of_the_exact_mean():
    for n in range(1, 8):
        for total in range(0, n * 10001, 37):
            assert floor((2 * total + n) / (2 * n)) == main.half_up_div(total, n), (total, n)


@pytest.mark.parametrize("year", [2024, 2026, 2028, 2100])
def test_count_working_days_matches_a_brute_force_count(year):
    for month in range(1, 13):
        last = calendar.monthrange(year, month)[1]
        for joined_day in (1, 2, 8, 15, last):
            joined = date(year, month, joined_day)
            expected = sum(1 for k in range(joined_day, last + 1) if date(year, month, k).weekday() < 5)
            assert main.count_working_days(year, month, joined.isoformat()) == expected
        assert main.count_working_days(year, month, (date(year, month, 1) - timedelta(days=1)).isoformat()) == \
            sum(1 for k in range(1, last + 1) if date(year, month, k).weekday() < 5)
        assert main.count_working_days(year, month, (date(year, month, last) + timedelta(days=1)).isoformat()) == 0


def test_weekend_dates():
    assert main.weekend_dates(2026, 7) == ["2026-07-04", "2026-07-05", "2026-07-11", "2026-07-12", "2026-07-18",
                                           "2026-07-19", "2026-07-25", "2026-07-26"]
    assert len(main.weekend_dates(2028, 2)) == 8                    # leap February


@pytest.mark.parametrize("frm,to,ok", [
    ("2026-07-01", "2026-07-01", True), ("2026-07-01", "2026-09-30", True),       # 1 day ; 92 days
    ("2026-07-01", "2026-10-01", False),                                          # 93 days
    ("2026-07-02", "2026-07-01", False), ("2026-12-31", "2026-01-01", False),
    ("2026-02-27", "2026-03-01", True), ("0001-01-01", "0001-02-01", True),
    ("9999-12-01", "9999-12-31", False),                                          # the day after `to` is not representable
])
def test_check_trend_range(frm, to, ok):
    if ok:
        assert main.check_trend_range(frm, to) == (date.fromisoformat(frm), date.fromisoformat(to))
    else:
        with pytest.raises(RequestValidationError):
            main.check_trend_range(frm, to)


def test_month_param_rejects_year_zero_and_bad_months():
    from pydantic import TypeAdapter, ValidationError
    adapter = TypeAdapter(main.MonthStr)
    assert adapter.validate_python("2026-07") == "2026-07"
    for bad in ("0000-01", "2026-00", "2026-13", "2026-7", "202607", "2026-07-01", "2026-07\n", " 2026-07", "２０２６-０７", ""):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)
