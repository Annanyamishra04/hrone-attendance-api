"""Malformed and hostile input to the Phase 4 endpoints: always a clean 4xx (never a 5xx), nothing reaches MongoDB."""
import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import SpyDB

BAD_MONTHS = ["2026-7", "202607", "2026-13", "2026-00", "0000-01", "2026-07-01", "2026-07\n", "2026-07 ", " 2026-07", "２０２６-０７",
              "2026/07", "july", "", "2026-07;", "2026-07%00", "99999-01", "-2026-07", "2026-+7", "2026-0x"]
BAD_DATES = ["2026-7-6", "20260706", "2026-02-30", "2026-13-01", "0000-01-01", "2026-07-06T00:00:00", "2026-07-06\n", "", "today",
             "２０２６-０７-０６", "2026-07-6", "2026-07-06 ", "1e3", "2026-02-29"]


@pytest.fixture
def guarded(mdb, monkeypatch):
    """A database that fails the test if anything is read or aggregated: a 422 must be decided before MongoDB is touched."""
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("MongoDB was used for a request that should have been rejected")
        __getitem__ = __getattr__
    monkeypatch.setattr(main, "db", Boom())
    monkeypatch.setattr(main, "run_aggregate", lambda *a: (_ for _ in ()).throw(AssertionError("aggregate called")))
    return TestClient(main.app)


@pytest.mark.parametrize("month", BAD_MONTHS)
def test_bad_months_are_422_everywhere(guarded, month):
    for path, extra in [("/analytics/employees/EMP0001/monthly", {}), ("/analytics/departments/summary", {}),
                        ("/analytics/leaderboard/late", {}), ("/admin/explain/department_summary", {})]:
        r = guarded.get(path, params={"month": month, **extra})
        assert r.status_code == 422, (path, month, r.text[:100])
        assert isinstance(r.json()["detail"], list)


@pytest.mark.parametrize("value", BAD_DATES)
def test_bad_dates_are_422_for_trend_and_explain(guarded, value):
    for key in ("from", "to"):
        good = {"from": "2026-07-01", "to": "2026-07-31"}
        good[key] = value
        assert guarded.get("/analytics/departments/Eng/trend", params=good).status_code == 422
        assert guarded.get("/admin/explain/department_trend", params={"department": "Eng", **good}).status_code == 422
    assert guarded.get("/admin/explain/attendance_list", params={"date_from": value}).status_code == 422


@pytest.mark.parametrize("limit", ["0", "-1", "51", "abc", "", "1.5", "1e1", "NaN", "inf", "99999999999999999999"])
def test_bad_limits_are_422(guarded, limit):
    assert guarded.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).status_code == 422
    assert guarded.get("/admin/explain/late_leaderboard", params={"month": "2026-07", "limit": limit}).status_code == 422


def test_missing_required_parameters_are_422(guarded):
    assert guarded.get("/analytics/employees/EMP0001/monthly").status_code == 422
    assert guarded.get("/analytics/departments/summary").status_code == 422
    assert guarded.get("/analytics/leaderboard/late").status_code == 422
    assert guarded.get("/analytics/departments/Eng/trend").status_code == 422
    assert guarded.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01"}).status_code == 422


def test_the_range_limit_and_order_are_422(guarded):
    for frm, to in [("2026-07-31", "2026-07-01"), ("2026-01-01", "2026-12-31"), ("2026-07-01", "2026-10-01"),
                    ("0001-01-01", "9999-12-31"), ("9999-12-01", "9999-12-31")]:
        assert guarded.get("/analytics/departments/Eng/trend", params={"from": frm, "to": to}).status_code == 422, (frm, to)


@pytest.mark.parametrize("raw", [b"/analytics/departments/%ED%A0%80/trend?from=2026-07-01&to=2026-07-02",
                                 b"/analytics/employees/%ED%A0%80/monthly?month=2026-07",
                                 b"/analytics/departments/summary?month=2026-07&department=%ED%A0%80",
                                 b"/analytics/leaderboard/late?month=2026-07&department=%ED%A0%80",
                                 b"/admin/explain/attendance_list?emp_code=%ED%A0%80",
                                 b"/analytics/employees/%FF%FE/monthly?month=2026-07",
                                 b"/analytics/departments/%00/trend?from=2026-07-01&to=2026-07-02"])
def test_undecodable_or_odd_bytes_in_text_parameters_never_cause_a_500(mdb, raw, monkeypatch):
    # mongomock cannot run these pipelines or `explain`; stubs stand in so only OUR request handling is exercised.
    monkeypatch.setattr(main, "run_aggregate", lambda collection, pipeline: [])
    monkeypatch.setattr(main, "run_explain", lambda command: {})
    c = TestClient(main.app, raise_server_exceptions=False)
    r = c.get(raw.decode("latin-1"))
    assert r.status_code < 500, (raw, r.status_code, r.text[:100])


def test_surrogate_text_is_rejected_by_the_storable_str_type():
    from pydantic import TypeAdapter, ValidationError
    with pytest.raises(ValidationError):
        TypeAdapter(main.StorableStr).validate_python(chr(0xD800))


@pytest.mark.parametrize("text", ["$where", "{\"$ne\": null}", pytest.param("a" * 5_000, id="long-5000"), "Eng\u0000",
                                  "'; DROP", "../..", "é\u202e", "%24gt", "$"])
def test_odd_but_valid_text_is_just_data(mini_engine, text):
    """Not rejected (any string can be a department): it matches nothing, and it is never interpreted."""
    mini_engine.employees.insert_one({"emp_code": "EMP0001", "department": "Eng", "joined_on": "2026-01-01", "name": "x"})
    c = TestClient(main.app, raise_server_exceptions=False)
    r1 = c.get("/analytics/departments/summary", params={"month": "2026-07", "department": text})
    r2 = c.get("/analytics/leaderboard/late", params={"month": "2026-07", "department": text})
    r3 = c.get("/analytics/employees/EMPX/monthly", params={"month": "2026-07"})
    assert r1.status_code == 200 and r1.json()["items"] == []
    assert r2.status_code == 200 and r2.json()["items"] == []
    assert r3.status_code == 404
