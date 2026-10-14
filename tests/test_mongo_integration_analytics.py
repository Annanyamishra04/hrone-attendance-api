"""Phase 4 tests that need a REAL MongoDB 6.0+ (MONGO_URI, default mongodb://localhost:27017). They skip themselves when
none is reachable. Each uses a throw-away database named attendance_test_<random>, dropped afterwards; MONGO_DB is never used.

STATUS: written, NOT EXECUTED against a real server by their author (none was reachable). Only the comparison helper was
dry-run against the test-only interpreter to catch typos. Until someone runs this file against MongoDB 6.0+, the following are
unverified: that the four pipelines execute on a real server at all ($lookup with localField + pipeline, $densify,
$setWindowFields), that they return what tests/oracle.py computes, that the semantic assumptions in tests/assumptions.py hold,
and that /admin/explain shows IXSCAN and no COLLSCAN on ~100,000 records."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main
from tests import oracle
from tests.assumptions import CASES
from tests.datagen import DEPARTMENTS, make_dataset
from tests.test_mongo_integration import MONGO_URI, server_reachable

pytestmark = pytest.mark.mongo


def _client_and_db():
    if not server_reachable():
        pytest.skip("no MongoDB reachable at MONGO_URI")
    client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    return client, name


@pytest.fixture
def real_db():
    client, name = _client_and_db()
    yield client[name]
    client.drop_database(name)
    client.close()


@pytest.fixture(scope="module")
def big_db():
    """~100,000 attendance records (1,000 employees x 100 days) with the app's own indexes, shared by the explain tests."""
    client, name = _client_and_db()
    database = client[name]
    main.ensure_indexes(database)
    start = date(2026, 4, 1)
    employees = [{"emp_code": f"EMP{i:04d}", "name": f"P{i}", "email": "p@example.com", "department": f"Dept{i % 10}",
                  "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01",
                  "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)} for i in range(1, 1001)]
    database.employees.insert_many(employees)
    batch = []
    for i in range(1, 1001):
        for d in range(100):
            day = start + timedelta(days=d)
            batch.append({"emp_code": f"EMP{i:04d}", "date": day.isoformat(), "status": "PRESENT",
                          "punch_in": datetime(day.year, day.month, day.day, 4, tzinfo=timezone.utc), "punch_out": None,
                          "work_hours": None, "late_minutes": (i * 7 + d) % 50, "overtime_minutes": 0, "half_day": False,
                          "history": []})
        if len(batch) >= 10_000:
            database.attendance_logs.insert_many(batch)
            batch = []
    if batch:
        database.attendance_logs.insert_many(batch)
    assert database.attendance_logs.estimated_document_count() == 100_000
    yield database
    client.drop_database(name)
    client.close()


# --------------------------------------------------------------------------- #
# the semantic assumptions, on the real server
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,docs,pipeline,expected", CASES, ids=[c[0][:60] for c in CASES])
def test_semantic_assumptions_hold_on_a_real_server(real_db, name, docs, pipeline, expected):
    real_db.cases.insert_many([dict(d) for d in docs])
    got = [{k: v for k, v in d.items()} for d in real_db.cases.aggregate(pipeline)]
    assert [dict(sorted(d.items())) for d in got] == [dict(sorted(d.items())) for d in expected], name


# --------------------------------------------------------------------------- #
# the endpoints against the oracle, on the real server
# --------------------------------------------------------------------------- #
def compare_with_oracle(client, emps, logs):
    """Every analytics endpoint on a grid of parameters, compared with tests/oracle.py. Returns nothing; asserts."""
    for month in ("2026-06", "2026-07", "2026-08", "2027-02"):
        for e in emps:
            got = client.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month})
            assert got.status_code == 200 and got.json() == oracle.monthly(e, logs, month), (e["emp_code"], month)
        for dep in [None] + DEPARTMENTS + ["Nope"]:
            params = {"month": month, **({} if dep is None else {"department": dep})}
            assert client.get("/analytics/departments/summary", params=params).json() == \
                oracle.department_summary(emps, logs, month, dep), (month, dep)
            for limit in (1, 3, 10, 50):
                got = client.get("/analytics/leaderboard/late", params={**params, "limit": limit})
                assert got.status_code == 200 and got.json() == oracle.leaderboard(emps, logs, month, limit, dep), (month, dep, limit)
    ranges = [("2026-07-01", "2026-07-31"), ("2026-07-06", "2026-07-06"), ("2026-07-04", "2026-07-05"),
              ("2026-06-01", "2026-08-31"), ("2026-01-01", "2026-01-20"), ("2026-12-01", "2026-12-10"),
              ("2026-07-27", "2026-08-09")]
    for dep in DEPARTMENTS:
        for frm, to in ranges:
            got = client.get(f"/analytics/departments/{dep}/trend", params={"from": frm, "to": to})
            assert got.status_code == 200 and got.json() == oracle.trend(emps, logs, dep, frm, to), (dep, frm, to)


@pytest.mark.parametrize("seed", [1, 2])
def test_endpoints_match_the_oracle_on_a_real_server(real_db, monkeypatch, seed):
    main.ensure_indexes(real_db)
    monkeypatch.setattr(main, "db", real_db)
    emps, logs = make_dataset(seed=seed)
    real_db.employees.insert_many([dict(e) for e in emps])
    real_db.attendance_logs.insert_many([dict(l) for l in logs])
    compare_with_oracle(TestClient(main.app), emps, logs)


def test_the_pipelines_return_correct_results_even_with_no_application_indexes(real_db, monkeypatch):
    """Correctness must not depend on the indexes (they are for speed); the unique ones only guard writes."""
    monkeypatch.setattr(main, "db", real_db)
    emps, logs = make_dataset(seed=3)
    real_db.employees.insert_many([dict(e) for e in emps])
    real_db.attendance_logs.insert_many([dict(l) for l in logs])
    client = TestClient(main.app)
    assert client.get("/analytics/departments/summary", params={"month": "2026-07"}).json() == \
        oracle.department_summary(emps, logs, "2026-07")


# --------------------------------------------------------------------------- #
# explain: IXSCAN and no COLLSCAN on ~100,000 records
# --------------------------------------------------------------------------- #
EXPLAIN_CASES = [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0007", "date_from": "2026-05-01", "date_to": "2026-05-31", "page": 2, "page_size": 10}),
    ("attendance_list", {"status": "PRESENT", "date_from": "2026-06-01"}),
    ("employee_monthly", {"emp_code": "EMP0007", "month": "2026-06"}),
    ("department_summary", {"month": "2026-06"}),                                     # needs ix_joined_on
    ("department_summary", {"month": "2026-06", "department": "Dept3"}),
    ("late_leaderboard", {"month": "2026-06"}),
    ("late_leaderboard", {"month": "2026-06", "limit": 5, "department": "Dept3"}),
    ("department_trend", {"department": "Dept3", "from": "2026-05-01", "to": "2026-06-30"}),
]


@pytest.mark.parametrize("endpoint,params", EXPLAIN_CASES, ids=[f"{e}-{sorted(p)}" for e, p in EXPLAIN_CASES])
def test_explain_shows_an_index_scan_and_no_collection_scan(big_db, monkeypatch, endpoint, params):
    monkeypatch.setattr(main, "db", big_db)
    r = TestClient(main.app).get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    text = json.dumps(body)
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    assert "executionStats" in text
    assert "IXSCAN" in text, "no index scan in the plan"
    assert "COLLSCAN" not in text, "the plan contains a collection scan"


def test_without_ix_joined_on_the_unfiltered_summary_would_scan_the_employees_collection(big_db, monkeypatch):
    """Documents WHY the fifth index exists (DECISIONS.md, answer 1). Drops it, shows the plan changes, restores it."""
    monkeypatch.setattr(main, "db", big_db)
    big_db.employees.drop_index("ix_joined_on")
    try:
        text = json.dumps(_Explain.get("department_summary", {"month": "2026-06"}))
    finally:
        main.ensure_indexes(big_db)
    assert "COLLSCAN" in text, "expected a collection scan without ix_joined_on (if this fails the index may be unnecessary)"


class _Explain:
    @staticmethod
    def get(endpoint, params):
        return TestClient(main.app).get(f"/admin/explain/{endpoint}", params=params).json()


def test_explain_does_not_change_the_data(big_db, monkeypatch):
    monkeypatch.setattr(main, "db", big_db)
    before = big_db.attendance_logs.estimated_document_count()
    TestClient(main.app).get("/admin/explain/department_trend", params={"department": "Dept3", "from": "2026-05-01", "to": "2026-05-10"})
    assert big_db.attendance_logs.estimated_document_count() == before
