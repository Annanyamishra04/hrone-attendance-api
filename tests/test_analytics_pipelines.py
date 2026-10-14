"""Properties of the pipelines themselves: they use the indexes the design relies on, rank before limiting, are
BSON-encodable, never put user input where MongoDB would evaluate it, and the endpoints never pull a collection into Python."""
import bson
import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import SpyDB

HOSTILE = ['$where', '{"$gt": ""}', "x'; sleep(1000); '", '$$ROOT', '$not_a_field', "a\x00b", '"; db.dropDatabase(); "', "}{", "é\u202e"]


def all_pipelines(department="Eng", emp_code="EMP0001"):
    from datetime import date
    return {
        "employee_monthly": ("attendance_logs", main.employee_monthly_pipeline(emp_code, "2026-07")),
        "department_summary": ("employees", main.department_summary_pipeline("2026-07", department)),
        "department_summary_all": ("employees", main.department_summary_pipeline("2026-07", None)),
        "late_leaderboard": ("attendance_logs", main.late_leaderboard_pipeline("2026-07", 10, department)),
        "late_leaderboard_all": ("attendance_logs", main.late_leaderboard_pipeline("2026-07", 10, None)),
        "department_trend": ("employees", main.department_trend_pipeline(department, date(2026, 7, 1), date(2026, 7, 31))),
    }


def stage_names(pipeline):
    return [next(iter(stage)) for stage in pipeline]


@pytest.mark.parametrize("name", list(all_pipelines()))
def test_every_pipeline_is_bson_encodable(name):
    bson.encode({"pipeline": all_pipelines()[name][1]})        # raises on anything PyMongo could not send


def test_first_stages_are_indexed_matches():
    p = all_pipelines()
    assert p["employee_monthly"][1][0] == {"$match": {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}}}
    assert p["late_leaderboard"][1][0]["$match"]["date"] == {"$gte": "2026-07-01", "$lte": "2026-07-31"}
    assert p["department_summary"][1][0] == {"$match": {"joined_on": {"$lte": "2026-07-31"}, "department": "Eng"}}
    assert p["department_summary_all"][1][0] == {"$match": {"joined_on": {"$lte": "2026-07-31"}}}
    assert p["department_trend"][1][0] == {"$match": {"department": "Eng"}}


def test_the_summary_and_trend_start_from_employees_so_people_without_logs_still_count():
    p = all_pipelines()
    assert p["department_summary"][0] == "employees" and p["department_trend"][0] == "employees"
    assert stage_names(p["department_summary"][1])[:2] == ["$match", "$lookup"]


def test_lookups_use_the_indexed_equality_and_bound_the_dates_inside_the_lookup():
    for name in ("department_summary", "department_trend"):
        lookup = next(s["$lookup"] for s in all_pipelines()[name][1] if "$lookup" in s)
        assert (lookup["from"], lookup["localField"], lookup["foreignField"]) == ("attendance_logs", "emp_code", "emp_code")
        assert lookup["pipeline"][0]["$match"]["date"]["$gte"] and lookup["pipeline"][0]["$match"]["date"]["$lte"]


def test_leaderboard_ranks_before_it_limits_and_has_no_limit_or_skip_stage():
    names = stage_names(all_pipelines()["late_leaderboard"][1])
    assert "$limit" not in names and "$skip" not in names                      # sorting + limiting + array index is the wrong approach
    window = names.index("$setWindowFields")
    rank_filter = next(i for i, s in enumerate(all_pipelines()["late_leaderboard"][1]) if s.get("$match", {}).get("rank"))
    assert window < rank_filter
    stage = all_pipelines()["late_leaderboard"][1][window]["$setWindowFields"]
    assert stage["sortBy"] == {"total_late_minutes": -1} and stage["output"]["rank"] == {"$rank": {}}
    assert all_pipelines()["late_leaderboard"][1][rank_filter]["$match"] == {"rank": {"$lte": 10}}


def test_leaderboard_department_filter_comes_before_ranking():
    pipeline = all_pipelines()["late_leaderboard"][1]
    names = stage_names(pipeline)
    dep = next(i for i, s in enumerate(pipeline) if s.get("$match") == {"emp.department": "Eng"})
    assert dep < names.index("$setWindowFields")
    assert not any(s.get("$match") == {"emp.department": "Eng"} for s in all_pipelines()["late_leaderboard_all"][1])


def test_leaderboard_drops_logs_of_unknown_employees():
    names = stage_names(all_pipelines()["late_leaderboard"][1])
    assert names.index("$lookup") < names.index("$unwind") < names.index("$setWindowFields")


def test_trend_gap_fills_in_the_database_and_bounds_are_from_and_the_day_after_to():
    from datetime import datetime, timezone
    pipeline = all_pipelines()["department_trend"][1]
    dens = next(s["$densify"] for s in pipeline if "$densify" in s)
    assert dens["range"] == {"step": 1, "unit": "day", "bounds": [datetime(2026, 7, 1, tzinfo=timezone.utc),
                                                                  datetime(2026, 8, 1, tzinfo=timezone.utc)]}
    names = stage_names(pipeline)
    assert names.index("$densify") < names.index("$setWindowFields")
    windows = [s["$setWindowFields"]["output"] for s in pipeline if "$setWindowFields" in s]
    assert windows[0]["headcount"]["window"] == {"documents": ["unbounded", "current"]}
    assert windows[1]["win_sum"]["window"] == windows[1]["win_n"]["window"] == {"documents": [-6, 0]}


@pytest.mark.parametrize("hostile", HOSTILE)
def test_user_text_only_ever_appears_as_a_plain_value_of_a_match_equality(hostile):
    """A department / emp_code is never placed inside an expression (where '$...' would be read as a field path or an
    operator). Walk every pipeline and check each occurrence of the hostile string."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, path + [i])
        elif node == hostile:
            found.append(path)

    for name, (_, pipeline) in all_pipelines(department=hostile, emp_code=hostile).items():
        found.clear()
        walk(pipeline, [])
        if name.endswith("_all"):
            assert not found, name                                              # no parameter, so no user text at all
            continue
        assert found, name                                                      # the value really is used (not silently dropped)
        for path in found:
            stage = pipeline[path[0]]
            assert "$match" in stage and path[1] == "$match", (name, path)     # always a $match value ...
            assert path[-1] in ("department", "emp.department", "emp_code"), (name, path)   # ... of a plain field equality


def test_analytics_endpoints_never_read_a_collection_into_python(mdb, monkeypatch):
    """The endpoints may call aggregate() and find_one() (existence checks), nothing else on the collections."""
    seen = []
    monkeypatch.setattr(main, "run_aggregate", lambda collection, pipeline: seen.append(collection) or [])
    spy = SpyDB(mdb)
    for name in ("employees", "attendance_logs"):
        spy.spy_on(name, forbid=("find", "count_documents", "distinct", "estimated_document_count"))
    monkeypatch.setattr(main, "db", spy)
    mdb.employees.insert_one({"emp_code": "EMP0001", "department": "Eng", "joined_on": "2026-01-01"})
    c = TestClient(main.app)
    assert c.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).status_code == 200
    assert c.get("/analytics/departments/summary", params={"month": "2026-07"}).status_code == 200
    assert c.get("/analytics/leaderboard/late", params={"month": "2026-07"}).status_code == 200
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-01", "to": "2026-07-31"}).status_code == 200
    assert seen == ["attendance_logs", "employees", "attendance_logs", "employees"]
