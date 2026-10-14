"""The four analytics endpoints, run through the pipelines (tests/mini_agg.py executes them) on seeded random data and
compared field by field with the independent reference implementation in tests/oracle.py.

Reading this file honestly: both sides here are Python. The pipelines are executed by a test-only interpreter, so this
shows the pipelines compute what the contract says under the author's reading of MongoDB's semantics. It does not show
that a real MongoDB agrees; tests/test_mongo_integration_analytics.py repeats these comparisons on a real server."""
import pytest
from fastapi.testclient import TestClient

from app import main
from tests import oracle
from tests.datagen import DEPARTMENTS, make_dataset

SEEDS = [1, 2, 3]
MONTHS = ["2026-06", "2026-07", "2026-08", "2027-02"]   # before any data, two busy months, after all data


@pytest.fixture(params=SEEDS)
def world(request, mini_engine):
    emps, logs = make_dataset(seed=request.param)
    mini_engine.employees.insert_many([dict(e) for e in emps])
    mini_engine.attendance_logs.insert_many([dict(l) for l in logs])
    return TestClient(main.app), emps, logs


def test_employee_monthly_matches_the_oracle(world):
    client, emps, logs = world
    for month in MONTHS:
        for e in emps:
            got = client.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": month})
            assert got.status_code == 200
            assert got.json() == oracle.monthly(e, logs, month), (e["emp_code"], month)


def test_department_summary_matches_the_oracle(world):
    client, emps, logs = world
    for month in MONTHS:
        got = client.get("/analytics/departments/summary", params={"month": month}).json()
        assert got == oracle.department_summary(emps, logs, month), month
        for dep in DEPARTMENTS + ["Nope"]:
            got = client.get("/analytics/departments/summary", params={"month": month, "department": dep}).json()
            assert got == oracle.department_summary(emps, logs, month, dep), (month, dep)


@pytest.mark.parametrize("limit", [1, 3, 10, 50])
def test_late_leaderboard_matches_the_oracle(world, limit):
    client, emps, logs = world
    for month in MONTHS[1:3]:
        for dep in [None, "Engineering", "Sales", "Empty Dept", "Nope"]:
            params = {"month": month, "limit": limit, **({} if dep is None else {"department": dep})}
            got = client.get("/analytics/leaderboard/late", params=params)
            assert got.status_code == 200
            assert got.json() == oracle.leaderboard(emps, logs, month, limit, dep), (month, dep, limit)


RANGES = [("2026-07-01", "2026-07-31"), ("2026-07-06", "2026-07-06"), ("2026-07-04", "2026-07-05"),   # a weekend
          ("2026-06-01", "2026-08-31"),                                                               # 92 days
          ("2026-01-01", "2026-01-20"), ("2026-12-01", "2026-12-10"), ("2026-07-27", "2026-08-09")]


def test_department_trend_matches_the_oracle(world):
    client, emps, logs = world
    for dep in DEPARTMENTS:
        for frm, to in RANGES:
            got = client.get(f"/analytics/departments/{dep}/trend", params={"from": frm, "to": to})
            assert got.status_code == 200, got.text
            assert got.json() == oracle.trend(emps, logs, dep, frm, to), (dep, frm, to)


def test_the_aggregation_path_of_employee_monthly_also_runs_on_mongomock_itself(mdb):
    """employee_monthly only uses operators mongomock implements, so its REAL pipeline also runs on mongomock
    (no interpreter involved) and must give the same answers as the oracle."""
    emps, logs = make_dataset(seed=7)
    mdb.employees.insert_many([dict(e) for e in emps])
    mdb.attendance_logs.insert_many([dict(l) for l in logs])
    client = TestClient(main.app)
    for e in emps:
        got = client.get(f"/analytics/employees/{e['emp_code']}/monthly", params={"month": "2026-07"})
        assert got.json() == oracle.monthly(e, logs, "2026-07")
