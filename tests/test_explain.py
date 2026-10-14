"""GET /admin/explain/{endpoint}. mongomock cannot explain, so `db.command` is replaced by a recorder that returns a canned
explain document. These tests prove WHAT is sent to MongoDB (the same filter / sort / skip / limit / pipeline the real
endpoint sends, executionStats verbosity) and how the answer is turned into JSON. They prove nothing about the plans a
real server chooses: that is tests/test_mongo_integration_analytics.py (needs MongoDB 6.0+)."""
import datetime as dt
import json

import bson
import pytest
from bson.int64 import Int64
from bson.objectid import ObjectId
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure

from app import main
from tests.helpers import SpyDB


class Recorder:
    def __init__(self, reply=None, error=None):
        self.commands, self.reply, self.error = [], reply, error

    def command(self, cmd, *a, **kw):
        self.commands.append(cmd)
        if self.error:
            raise self.error
        return self.reply if self.reply is not None else {"ok": 1.0, "queryPlanner": {}, "executionStats": {"nReturned": 0}}


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(main, "db", r)
    return r


@pytest.fixture
def c():
    return TestClient(main.app)


PARAMS = {
    "attendance_list": {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31", "status": "PRESENT", "page": 3, "page_size": 25},
    "employee_monthly": {"emp_code": "EMP0001", "month": "2026-07"},
    "department_summary": {"month": "2026-07", "department": "Eng"},
    "late_leaderboard": {"month": "2026-07", "limit": 5, "department": "Eng"},
    "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
}


def test_the_command_is_explain_with_execution_stats_verbosity(rec, c):
    r = c.get("/admin/explain/employee_monthly", params=PARAMS["employee_monthly"])
    assert r.status_code == 200
    (cmd,) = rec.commands
    assert list(cmd)[0] == "explain" and cmd["verbosity"] == "executionStats"          # the command name must come first
    assert set(r.json()) == {"endpoint", "collection", "explain"}
    assert r.json()["endpoint"] == "employee_monthly" and r.json()["collection"] == "attendance_logs"


def test_attendance_list_explains_exactly_the_find_the_endpoint_runs(mdb, monkeypatch, c):
    spy = SpyDB(mdb)
    coll = spy.spy_on("attendance_logs")
    monkeypatch.setattr(main, "db", spy)
    mdb.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": f"2026-07-{d:02d}", "status": "PRESENT"} for d in range(1, 29)])
    params = {**PARAMS["attendance_list"], "page": 2, "page_size": 10}
    real = c.get("/attendance", params=params)
    assert real.status_code == 200 and len(real.json()["items"]) == 10
    sent = {name: arg for name, arg in coll.calls if arg is not None}             # ("find", filter) / ("sort", spec) / ...
    rec = Recorder()
    monkeypatch.setattr(main, "db", rec)
    r = c.get("/admin/explain/attendance_list", params=params)
    inner = rec.commands[0]["explain"]
    assert r.json()["collection"] == "attendance_logs"
    assert inner["find"] == "attendance_logs" and inner["filter"] == sent["find"]            # same filter as the real query
    assert list(inner["sort"].items()) == sent["sort"] == [("date", -1), ("emp_code", 1)]      # same sort, same key order
    assert (inner["skip"], inner["limit"]) == (sent["skip"], sent["limit"]) == (10, 10)       # same pagination
    assert inner["projection"] == {"_id": 0}


def test_attendance_list_defaults(rec, c):
    c.get("/admin/explain/attendance_list")
    inner = rec.commands[0]["explain"]
    assert inner["filter"] == {} and (inner["skip"], inner["limit"]) == (0, 20)


@pytest.mark.parametrize("name", ["employee_monthly", "department_summary", "late_leaderboard", "department_trend"])
def test_aggregate_endpoints_explain_the_pipeline_the_real_endpoint_runs(name, mdb, monkeypatch, c):
    """Capture the pipeline the real endpoint hands to aggregate, then check the explain command carries an identical one."""
    mdb.employees.insert_one({"emp_code": "EMP0001", "department": "Eng", "joined_on": "2026-01-01"})
    sent = []
    monkeypatch.setattr(main, "run_aggregate", lambda coll, pipeline: sent.append((coll, pipeline)) or [])
    path = {"employee_monthly": "/analytics/employees/EMP0001/monthly", "department_summary": "/analytics/departments/summary",
            "late_leaderboard": "/analytics/leaderboard/late", "department_trend": "/analytics/departments/Eng/trend"}[name]
    params = {k: v for k, v in PARAMS[name].items() if not (name in ("employee_monthly", "department_trend") and k in ("emp_code", "department"))}
    assert c.get(path, params=params).status_code == 200
    rec = Recorder()
    monkeypatch.setattr(main, "db", rec)
    r = c.get(f"/admin/explain/{name}", params=PARAMS[name])
    assert r.status_code == 200
    inner = rec.commands[0]["explain"]
    assert inner == {"aggregate": sent[0][0], "pipeline": sent[0][1], "cursor": {}}
    assert r.json()["collection"] == sent[0][0]


def test_unknown_employee_is_still_explained_not_404(rec, c):
    assert c.get("/admin/explain/employee_monthly", params={"emp_code": "EMP9999", "month": "2026-07"}).status_code == 200


def test_parameters_of_other_endpoints_are_accepted_and_ignored(rec, c):
    assert c.get("/admin/explain/department_summary", params={"month": "2026-07", "emp_code": "EMP0001", "limit": 3, "page": 2}).status_code == 200
    inner = rec.commands[0]["explain"]["pipeline"]
    assert inner == main.department_summary_pipeline("2026-07", None)


@pytest.mark.parametrize("endpoint,missing", [
    ("employee_monthly", {"emp_code": "EMP0001"}), ("employee_monthly", {"month": "2026-07"}), ("employee_monthly", {}),
    ("department_summary", {}), ("late_leaderboard", {}),
    ("department_trend", {"department": "Eng", "to": "2026-07-31"}), ("department_trend", {"department": "Eng", "from": "2026-07-01"}),
    ("department_trend", {"from": "2026-07-01", "to": "2026-07-31"}), ("department_trend", {}),
])
def test_missing_required_parameters_are_422_and_nothing_is_sent_to_mongodb(rec, c, endpoint, missing):
    r = c.get(f"/admin/explain/{endpoint}", params=missing)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list) and rec.commands == []
    assert all(e["type"] == "missing" and e["loc"][0] == "query" for e in r.json()["detail"])


@pytest.mark.parametrize("endpoint,params", [
    ("bogus", {}), ("", {}), ("Attendance_List", {}), ("attendance_list", {"limit": 0}), ("attendance_list", {"limit": 51}),
    ("attendance_list", {"page": 0}), ("attendance_list", {"page_size": 101}), ("attendance_list", {"page_size": 0}),
    ("attendance_list", {"status": "banana"}), ("attendance_list", {"date_from": "2026-02-30"}),
    ("attendance_list", {"date_from": "2026-07-02", "date_to": "2026-07-01"}), ("attendance_list", {"month": "2026-13"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-7"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-02", "to": "2026-07-01"}),
    ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-10-01"}),      # 93 days
    ("department_trend", {"department": "Eng", "from": "20260701", "to": "2026-07-31"}),
])
def test_invalid_values_are_422_exactly_like_the_endpoint_they_explain(rec, c, endpoint, params):
    r = c.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code in (404, 422) if endpoint in ("", ) else r.status_code == 422
    assert rec.commands == []


def test_bson_values_in_the_explain_output_become_plain_json(monkeypatch, c):
    reply = {"ok": 1.0, "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
             "executionStats": {"executionTimeMillis": Int64(7), "id": ObjectId("64b64b64b64b64b64b64b64b"),
                                "when": dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc), "nan": float("nan"),
                                "inf": float("inf"), "nested": [{"x": Int64(2**40)}]}}
    monkeypatch.setattr(main, "db", Recorder(reply))
    r = c.get("/admin/explain/department_summary", params={"month": "2026-07"})
    assert r.status_code == 200
    body = r.json()["explain"]
    json.dumps(body, allow_nan=False)                                                         # strictly valid JSON
    assert body["queryPlanner"]["winningPlan"]["stage"] == "IXSCAN" and body["executionStats"]["executionTimeMillis"] == 7
    assert body["executionStats"]["nan"] == {"$numberDouble": "NaN"}                          # extended JSON, still valid JSON
    assert body["executionStats"]["inf"] == {"$numberDouble": "Infinity"}
    assert body["executionStats"]["nested"] == [{"x": 2**40}]


def test_a_failing_explain_is_503_not_500(monkeypatch, c):
    monkeypatch.setattr(main, "db", Recorder(error=OperationFailure("explain failed")))
    r = c.get("/admin/explain/department_summary", params={"month": "2026-07"})
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}


@pytest.mark.parametrize("name", list(PARAMS))
def test_every_command_is_bson_encodable(name, rec, c):
    assert c.get(f"/admin/explain/{name}", params=PARAMS[name]).status_code == 200
    bson.encode(rec.commands[0])
