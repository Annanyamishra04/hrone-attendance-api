"""GET /attendance, against mongomock (see conftest.py for what that does NOT prove)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import ist_ms

RECORD_KEYS = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
               "late_minutes", "overtime_minutes", "half_day", "history"}


def keys(body):
    """[(date, emp_code), ...] of a response page."""
    return [(r["date"], r["emp_code"]) for r in body["items"]]


def get(client, **params):
    return client.get("/attendance", params=params)


def bulk(mdb, rows):
    """rows: (emp_code, date, status). Minimal valid documents; punch_in only for presence statuses."""
    docs = []
    for emp, date, status in rows:
        present = status in main.PRESENCE_STATUSES
        docs.append({"emp_code": emp, "date": date, "status": status,
                     "punch_in": datetime.fromisoformat(date + "T04:00:00+00:00") if present else None,
                     "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0,
                     "half_day": False, "history": []})
    mdb.attendance_logs.insert_many(docs)


def grid(mdb, employees=("EMP0001", "EMP0002", "EMP0003"), days=15, start=datetime(2026, 6, 20)):
    """employees x days records (one per employee per day), dates 2026-06-20 onwards."""
    rows = [(e, (start + timedelta(days=d)).date().isoformat(), "PRESENT") for d in range(days) for e in employees]
    bulk(mdb, rows)
    return rows


# --------------------------------------------------------------------------- #
# Shape, and the stored -> API conversion on the sample documents
# --------------------------------------------------------------------------- #
def test_sample_records_come_back_sorted_date_desc_then_emp_code_asc(client, seeded):
    body = get(client).json()
    assert body["total"] == 9 and (body["page"], body["page_size"]) == (1, 20)
    assert keys(body) == [
        ("2026-07-14", "EMP0002"),
        ("2026-07-08", "EMP0003"), ("2026-07-08", "EMP0004"),                       # same date: emp_code ascending
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0004"), ("2026-07-07", "EMP0006"),
        ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0003"), ("2026-07-06", "EMP0005"),
    ]


def test_every_item_has_exactly_the_record_fields_and_no_internal_id(client, seeded):
    for item in get(client).json()["items"]:
        assert set(item) == RECORD_KEYS and "_id" not in item and "id" not in item


def _by_key(client, emp, date):
    return get(client, emp_code=emp, date_from=date, date_to=date).json()["items"][0]


def test_full_day_record_converts_datetimes_to_epoch_ms(client, seeded):
    r = _by_key(client, "EMP0001", "2026-07-06")                 # DATA_MODEL.md sample 1
    assert r == {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                 "punch_in": ist_ms(2026, 7, 6, 9, 28), "punch_out": ist_ms(2026, 7, 6, 18, 35),
                 "work_hours": 9.12, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


def test_open_absent_and_half_day_records(client, seeded):
    open_rec = _by_key(client, "EMP0006", "2026-07-07")            # sample 3: not punched out
    assert (open_rec["punch_out"], open_rec["work_hours"]) == (None, None)
    absent = _by_key(client, "EMP0004", "2026-07-08")              # sample 5
    assert (absent["status"], absent["punch_in"], absent["punch_out"], absent["late_minutes"]) == ("ABSENT", None, None, 0)
    half = _by_key(client, "EMP0004", "2026-07-07")                # sample 4
    assert (half["status"], half["work_hours"], half["half_day"]) == ("WFH", 3.5, True)


def test_history_is_returned_with_every_instant_in_epoch_ms(client, seeded):
    entry = _by_key(client, "EMP0001", "2026-07-07")["history"]    # sample 8
    assert len(entry) == 1 and entry[0]["by"] == "hr.admin" and entry[0]["reason"] == "biometric glitch"
    assert entry[0]["at"] == main.datetime_to_ms(datetime(2026, 7, 8, 5, 0, tzinfo=timezone.utc))
    assert entry[0]["changes"] == {
        "punch_in": {"from": ist_ms(2026, 7, 7, 10, 5), "to": ist_ms(2026, 7, 7, 9, 28)},   # BSON dates -> ms
        "late_minutes": {"from": 35, "to": 0},                                              # others unchanged
        "work_hours": {"from": 8.42, "to": 9.03},
    }


def test_legacy_record_without_history_or_half_day_gets_the_defaults(client, seeded):
    r = _by_key(client, "EMP0002", "2026-07-14")                   # sample 9
    assert (r["history"], r["half_day"]) == ([], False)


def test_missing_derived_fields_default_to_zero_false_empty(client, mdb):
    mdb.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
                                    "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=timezone.utc)})
    r = get(client).json()["items"][0]
    assert (r["late_minutes"], r["overtime_minutes"], r["half_day"], r["history"], r["punch_out"], r["work_hours"]) == (
        0, 0, False, [], None, None)


def test_naive_datetimes_are_read_as_utc():
    # a client that is not tz-aware returns naive datetimes; they are UTC by definition (R1 / DATA_MODEL.md)
    doc = {"emp_code": "E", "date": "2026-07-06", "status": "PRESENT", "punch_in": datetime(2026, 7, 6, 4, 35)}
    assert main.record_to_api(doc)["punch_in"] == main.datetime_to_ms(datetime(2026, 7, 6, 4, 35, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def test_filter_by_emp_code_is_exact(client, seeded):
    assert keys(get(client, emp_code="EMP0001").json()) == [("2026-07-07", "EMP0001"), ("2026-07-06", "EMP0001")]
    assert get(client, emp_code="EMP000").json()["total"] == 0           # not a prefix match
    assert get(client, emp_code="emp0001").json()["total"] == 0          # case-sensitive
    assert get(client, emp_code="EMP9999").json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_emp_code_filter_does_not_match_a_longer_code(client, mdb):
    bulk(mdb, [("EMP0001", "2026-07-06", "PRESENT"), ("EMP00010", "2026-07-06", "PRESENT")])
    assert keys(get(client, emp_code="EMP0001").json()) == [("2026-07-06", "EMP0001")]


@pytest.mark.parametrize("status,expected_total", [("PRESENT", 6), ("WFH", 1), ("ABSENT", 1), ("LEAVE", 1), ("ON_DUTY", 0)])   # 6+1+1+1 = the 9 samples
def test_filter_by_status(client, seeded, status, expected_total):
    body = get(client, status=status).json()
    assert body["total"] == expected_total and {r["status"] for r in body["items"]} <= {status}


def test_date_range_is_inclusive_at_both_ends(client, seeded):
    assert {d for d, _ in keys(get(client, date_from="2026-07-07", date_to="2026-07-08").json())} == {"2026-07-07", "2026-07-08"}
    assert get(client, date_from="2026-07-07", date_to="2026-07-07").json()["total"] == 3       # a single day
    assert get(client, date_from="2026-07-14").json()["total"] == 1                             # open-ended: from
    assert get(client, date_to="2026-07-06").json()["total"] == 3                               # open-ended: to
    assert get(client, date_from="2026-07-09", date_to="2026-07-13").json()["total"] == 0       # a gap


def test_filters_combine_with_and(client, seeded):
    body = get(client, emp_code="EMP0004", status="WFH", date_from="2026-07-01", date_to="2026-07-31").json()
    assert keys(body) == [("2026-07-07", "EMP0004")] and body["total"] == 1
    assert get(client, emp_code="EMP0004", status="LEAVE").json()["total"] == 0


def test_dates_sort_correctly_across_month_and_year_boundaries(client, mdb):
    bulk(mdb, [("EMP0001", d, "PRESENT") for d in ("2025-12-31", "2026-01-01", "2026-02-01", "2025-02-01", "2026-01-31")])
    assert [d for d, _ in keys(get(client).json())] == ["2026-02-01", "2026-01-31", "2026-01-01", "2025-12-31", "2025-02-01"]
    assert get(client, date_from="2025-12-31", date_to="2026-01-01").json()["total"] == 2


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-02", "date_to": "2026-07-01"},        # from after to
    {"date_from": "2026-12-01", "date_to": "2026-01-01"},
    {"date_from": "2026-02-30"}, {"date_to": "2026-13-01"},      # not real dates
    {"date_from": "2026-7-6"}, {"date_from": "20260706"}, {"date_from": "06-07-2026"},
    {"date_from": "2026-07-06T00:00:00"}, {"date_to": ""}, {"date_from": "yesterday"},
    {"status": "present"}, {"status": "UNKNOWN"}, {"status": ""},
])
def test_invalid_filters_are_422(client, seeded, params):
    r = get(client, **params)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)


def test_from_equal_to_is_valid(client, seeded):
    assert get(client, date_from="2026-07-07", date_to="2026-07-07").status_code == 200


# --------------------------------------------------------------------------- #
# Pagination (R10) and deterministic order
# --------------------------------------------------------------------------- #
def test_pagination_reassembles_the_full_sorted_list(client, mdb):
    rows = grid(mdb)                                               # 45 records, 3 per date
    expected = sorted(((d, e) for e, d, _ in rows), key=lambda t: (-int(t[0].replace("-", "")), t[1]))
    seen = []
    for page in (1, 2, 3):
        body = get(client, page=page).json()
        assert (body["total"], body["page"], body["page_size"]) == (45, page, 20)
        assert len(body["items"]) == (20 if page < 3 else 5)
        seen += keys(body)
    assert seen == expected and len(set(seen)) == 45               # in order, no overlap, no gap


@pytest.mark.parametrize("page_size", [1, 2, 4, 7, 20, 44, 45, 46, 100])
def test_any_page_size_gives_the_same_total_order(client, mdb, page_size):
    rows = grid(mdb)
    expected = sorted(((d, e) for e, d, _ in rows), key=lambda t: (-int(t[0].replace("-", "")), t[1]))
    seen, page = [], 1
    while True:
        body = get(client, page=page, page_size=page_size).json()
        assert body["total"] == 45
        if not body["items"]:
            break
        assert len(body["items"]) <= page_size
        seen += keys(body)
        page += 1
    assert seen == expected


def test_a_date_group_that_straddles_a_page_boundary_is_split_by_emp_code(client, mdb):
    grid(mdb)                                                      # 3 employees per date; page_size 2 splits every date
    page1, page2 = keys(get(client, page=1, page_size=2).json()), keys(get(client, page=2, page_size=2).json())
    assert page1 == [("2026-07-04", "EMP0001"), ("2026-07-04", "EMP0002")]
    assert page2 == [("2026-07-04", "EMP0003"), ("2026-07-03", "EMP0001")]


def test_insertion_order_does_not_change_the_result(client, mdb):
    rows = [("EMP0003", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-06", "PRESENT"), ("EMP0002", "2026-07-07", "PRESENT"),
            ("EMP0002", "2026-07-06", "PRESENT"), ("EMP0001", "2026-07-07", "PRESENT")]
    bulk(mdb, rows)
    assert keys(get(client).json()) == [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"),
                                        ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002"), ("2026-07-06", "EMP0003")]


def test_total_reflects_the_filters_on_every_page(client, mdb):
    grid(mdb)
    for page in (1, 2, 3):
        assert get(client, emp_code="EMP0002", page=page, page_size=6).json()["total"] == 15
    body = get(client, emp_code="EMP0002", page=3, page_size=6).json()
    assert len(body["items"]) == 3 and {r["emp_code"] for r in body["items"]} == {"EMP0002"}
    ranged = get(client, date_from="2026-06-25", date_to="2026-06-29", page_size=100).json()
    assert ranged["total"] == 15 == len(ranged["items"])          # 5 days x 3 employees


def test_page_beyond_the_end_is_empty_but_keeps_the_total(client, mdb):
    grid(mdb)
    assert get(client, page=4).json() == {"items": [], "total": 45, "page": 4, "page_size": 20}
    assert get(client, page=10 ** 30).json()["items"] == []        # no overflow / 500


def test_default_paging_and_the_maximum_page_size(client, mdb):
    grid(mdb)
    assert (get(client).json()["page"], get(client).json()["page_size"]) == (1, 20)
    body = get(client, page_size=100).json()
    assert len(body["items"]) == 45 and body["page_size"] == 100
    assert get(client, page_size=1).json()["items"] == get(client).json()["items"][:1]


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page": -3}, {"page": "x"}, {"page": "1.5"}, {"page": ""},
    {"page_size": 0}, {"page_size": 101}, {"page_size": -1}, {"page_size": "x"}, {"page_size": "10.5"},
])
def test_invalid_paging_is_422(client, seeded, params):
    assert get(client, **params).status_code == 422


def test_empty_collection(client, mdb):
    assert get(client).json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


# --------------------------------------------------------------------------- #
# The database does the work (no load-everything-then-slice)
# --------------------------------------------------------------------------- #
def test_filter_sort_skip_and_limit_are_all_sent_to_the_database(spy_db, mdb):
    grid(mdb)
    spy = spy_db.spy_on("attendance_logs")
    TestClient(main.app).get("/attendance", params={
        "emp_code": "EMP0002", "status": "PRESENT", "date_from": "2026-06-22", "date_to": "2026-07-02", "page": 3, "page_size": 4})
    the_filter = {"emp_code": "EMP0002", "status": "PRESENT", "date": {"$gte": "2026-06-22", "$lte": "2026-07-02"}}
    assert ("count_documents", the_filter) in spy.calls
    assert ("find", the_filter) in spy.calls
    assert ("sort", [("date", -1), ("emp_code", 1)]) in spy.calls
    assert ("skip", 8) in spy.calls and ("limit", 4) in spy.calls      # (page 3 - 1) * 4
    assert spy.names().count("find") == 1


def test_a_page_past_the_end_does_not_even_query_for_documents(spy_db, mdb):
    grid(mdb)
    spy = spy_db.spy_on("attendance_logs")
    assert TestClient(main.app).get("/attendance", params={"page": 99}).json()["items"] == []
    assert "find" not in spy.names() and "count_documents" in spy.names()


def test_the_cursor_never_yields_more_than_one_page(spy_db, mdb):
    grid(mdb, days=40)                                              # 120 records
    spy = spy_db.spy_on("attendance_logs")
    body = TestClient(main.app).get("/attendance", params={"page_size": 7}).json()
    assert body["total"] == 120 and len(body["items"]) == 7
    assert ("limit", 7) in spy.calls


def test_query_has_only_the_filters_that_were_asked_for(spy_db, mdb):
    spy = spy_db.spy_on("attendance_logs")
    TestClient(main.app).get("/attendance")
    assert ("count_documents", {}) in spy.calls                     # no stray empty "date": {} etc.
    TestClient(main.app).get("/attendance", params={"date_from": "2026-07-01"})
    assert ("count_documents", {"date": {"$gte": "2026-07-01"}}) in spy.calls


def test_status_literals_match_the_status_constants():
    from typing import get_args
    assert set(get_args(main.Status)) == main.ALL_STATUSES
    assert set(get_args(main.PresenceStatus)) == main.PRESENCE_STATUSES
