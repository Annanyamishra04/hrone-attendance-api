"""Tests that need a REAL MongoDB (6.0+). They skip themselves when none is reachable at MONGO_URI
(default mongodb://localhost:27017), so a plain `pytest` run still works without a server.

Each test gets a throw-away database named attendance_test_<random> that is dropped afterwards.
MONGO_DB is deliberately NOT used, so these tests can never touch your real data."""
import functools
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

from app import main
from app.main import IndexSetupError, ensure_indexes, verify_indexes
from tests.helpers import add_record, employee_payload, ist_ms, run_together

pytestmark = pytest.mark.mongo


MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")


@functools.lru_cache(maxsize=1)
def server_reachable() -> bool:
    """Probed once per run, so the skip path costs one timeout instead of one per test."""
    probe = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        probe.admin.command("ping")
        return True
    except PyMongoError:
        return False
    finally:
        probe.close()


@pytest.fixture
def real_db():
    if not server_reachable():
        pytest.skip("no MongoDB reachable at MONGO_URI")
    client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
    name = f"attendance_test_{uuid.uuid4().hex[:8]}"
    yield client[name]
    client.drop_database(name)
    client.close()


def index_map(coll):
    return {i["name"]: bool(i.get("unique", False)) for i in coll.list_indexes()}


def test_server_is_mongodb_6_or_newer(real_db):
    version = real_db.client.server_info()["versionArray"]
    assert tuple(version[:2]) >= (6, 0), f"found {version}"


def test_indexes_are_created_and_creation_is_idempotent(real_db):
    ensure_indexes(real_db)
    first = {c: index_map(real_db[c]) for c in ("employees", "attendance_logs")}
    assert first["employees"] == {"_id_": False, "uq_emp_code": True, "ix_department_joined_on": False, "ix_joined_on": False}
    assert first["attendance_logs"] == {"_id_": False, "uq_emp_code_date": True, "ix_date_desc_emp_code": False}
    ensure_indexes(real_db)   # second run: no error, nothing changes
    assert {c: index_map(real_db[c]) for c in first} == first


def test_unique_indexes_are_enforced_by_the_server(real_db):
    ensure_indexes(real_db)
    real_db.employees.insert_one({"emp_code": "EMP0001"})
    with pytest.raises(DuplicateKeyError):
        real_db.employees.insert_one({"emp_code": "EMP0001"})
    real_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    real_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-07"})   # other day: fine
    with pytest.raises(DuplicateKeyError):
        real_db.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06"})


def test_duplicate_rows_make_index_creation_fail_loudly(real_db):
    real_db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06"},
                                         {"emp_code": "EMP0001", "date": "2026-07-06"}])
    with pytest.raises(IndexSetupError, match="uq_emp_code_date"):
        ensure_indexes(real_db)
    assert "uq_emp_code_date" not in index_map(real_db.attendance_logs)

    real_db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-07-06"})
    ensure_indexes(real_db)   # data repaired -> succeeds
    assert index_map(real_db.attendance_logs)["uq_emp_code_date"] is True


def test_health_against_a_real_server_including_recovery(real_db, monkeypatch):
    monkeypatch.setattr(main, "db", real_db)
    with TestClient(main.app) as c:
        assert c.get("/health").json() == {"status": "ok"}

        # a required index disappears and the data now blocks re-creating it -> must report 503
        real_db.attendance_logs.drop_index("uq_emp_code_date")
        real_db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06"},
                                             {"emp_code": "EMP0001", "date": "2026-07-06"}])
        r = c.get("/health")
        assert r.status_code == 503 and "indexes" in r.json()["detail"]

        # data repaired; the failed rebuild is retried once the cooldown has passed
        real_db.attendance_logs.delete_one({"emp_code": "EMP0001", "date": "2026-07-06"})
        main._clock.advance(main.REPAIR_RETRY_SECONDS)
        assert c.get("/health").status_code == 200
        assert index_map(real_db.attendance_logs)["uq_emp_code_date"] is True


def test_REGRESSION_dropping_a_required_index_after_ready_is_never_a_stale_200(real_db, monkeypatch):
    monkeypatch.setattr(main, "db", real_db)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200
        assert main.index_state.ready is True

        real_db.attendance_logs.drop_index("uq_emp_code_date")   # deleted after readiness
        real_db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06"},
                                             {"emp_code": "EMP0001", "date": "2026-07-06"}])   # blocks a rebuild
        assert c.get("/health").status_code == 503
        assert "uq_emp_code_date" not in index_map(real_db.attendance_logs)   # still really missing


def test_a_dropped_index_is_rebuilt_by_health_and_verified_before_200(real_db, monkeypatch):
    monkeypatch.setattr(main, "db", real_db)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200
        real_db.employees.drop_index("uq_emp_code")
        assert c.get("/health").status_code == 200
        assert verify_indexes(real_db) == []
        assert index_map(real_db.employees)["uq_emp_code"] is True


def test_an_equivalent_index_under_another_name_is_accepted_and_not_duplicated(real_db):
    real_db.employees.create_index([("emp_code", 1)], name="my_own_name", unique=True)
    ensure_indexes(real_db)
    assert "uq_emp_code" not in index_map(real_db.employees)
    assert verify_indexes(real_db) == []


# Same key pattern as a required index, but not equivalent to it. Each must be REJECTED and left untouched.
INCOMPATIBLE = {
    "partial": {"unique": True, "partialFilterExpression": {"emp_code": {"$exists": True}}},
    "sparse": {"unique": True, "sparse": True},
    "collation": {"unique": True, "collation": {"locale": "en", "strength": 2}},
    "not-unique": {"unique": False},
}


@pytest.mark.parametrize("options", INCOMPATIBLE.values(), ids=INCOMPATIBLE.keys())
def test_same_keys_with_incompatible_options_is_rejected_and_left_untouched(real_db, options):
    real_db.employees.create_index([("emp_code", 1)], name="legacy_emp_code", **options)
    before = list(real_db.employees.list_indexes())

    assert any("incompatible" in p for p in verify_indexes(real_db))
    with pytest.raises(IndexSetupError, match="not replacing"):
        ensure_indexes(real_db)
    assert list(real_db.employees.list_indexes()) == before      # nothing dropped, nothing replaced


def test_health_is_503_for_an_incompatible_existing_index(real_db, monkeypatch):
    real_db.employees.create_index([("emp_code", 1)], name="legacy_emp_code", unique=True,
                                   partialFilterExpression={"emp_code": {"$exists": True}})
    monkeypatch.setattr(main, "db", real_db)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/health").status_code == 503
    assert "uq_emp_code" not in index_map(real_db.employees)


# =========================================================================== #
# Phase 2 (employees, punch-in, attendance list) against a REAL server.
# These are the only tests that can prove true concurrency and real query plans.
# =========================================================================== #
@pytest.fixture
def api(real_db, monkeypatch):
    ensure_indexes(real_db)                     # the real, unique indexes
    monkeypatch.setattr(main, "db", real_db)
    return real_db


def test_phase2_simultaneous_employee_creates_give_exactly_one_201(api):
    codes = run_together(lambda i: TestClient(main.app).post("/employees", json=employee_payload()).status_code, 20)
    assert sorted(codes) == [201] + [409] * 19
    assert api.employees.count_documents({"emp_code": "EMP0001"}) == 1


def test_phase2_simultaneous_punch_ins_give_exactly_one_201(api):
    api.employees.insert_one({"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"})

    def go(i):
        return TestClient(main.app).post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 9, 0, i)}).status_code

    codes = run_together(go, 20)
    assert sorted(codes) == [201] + [409] * 19
    assert api.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-20"}) == 1


def test_phase2_create_punch_in_and_list_end_to_end(api):
    c = TestClient(main.app)
    for code, dept in (("EMP0002", "Sales"), ("EMP0001", "Engineering")):
        assert c.post("/employees", json=employee_payload(emp_code=code, department=dept)).status_code == 201
    assert [e["emp_code"] for e in c.get("/employees").json()["items"]] == ["EMP0001", "EMP0002"]
    assert c.get("/employees", params={"department": "Sales"}).json()["total"] == 1
    at = ist_ms(2026, 7, 20, 10, 5)
    r = c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": at})
    assert r.status_code == 201 and r.json()["late_minutes"] == 35 and r.json()["punch_in"] == at
    assert c.get("/attendance").json()["items"] == [r.json()]       # what was returned is what is listed


def test_phase2_attendance_pagination_on_a_real_server(api):
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 1, 1)
    api.attendance_logs.insert_many([
        {"emp_code": f"EMP{e:04d}", "date": (start + timedelta(days=d)).date().isoformat(), "status": "PRESENT",
         "punch_in": datetime(2026, 1, 1, 4, tzinfo=timezone.utc), "punch_out": None, "work_hours": None,
         "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
        for e in range(1, 6) for d in range(50)])                       # 250 records
    c = TestClient(main.app)
    seen = []
    for page, expected_len in ((1, 100), (2, 100), (3, 50), (4, 0)):
        body = c.get("/attendance", params={"page": page, "page_size": 100}).json()
        assert body["total"] == 250 and len(body["items"]) == expected_len
        seen += [(r["date"], r["emp_code"]) for r in body["items"]]
    assert seen == sorted(seen, key=lambda t: (-int(t[0].replace("-", "")), t[1])) and len(set(seen)) == 250


def _stage_names(node):
    """Every execution-plan stage name anywhere in an explain output (the nesting differs per server version)."""
    names = set()
    if isinstance(node, dict):
        if isinstance(node.get("stage"), str):
            names.add(node["stage"])
        for value in node.values():
            names |= _stage_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _stage_names(value)
    return names


@pytest.mark.parametrize("query", [
    {},
    {"emp_code": "EMP0003"},
    {"date": {"$gte": "2026-01-10", "$lte": "2026-01-20"}},
    {"emp_code": "EMP0003", "date": {"$gte": "2026-01-10", "$lte": "2026-01-20"}},
    {"status": "PRESENT"},
], ids=["unfiltered", "emp_code", "date-range", "emp_code+range", "status"])
def test_phase2_list_queries_use_an_index_and_need_no_in_memory_sort(api, query):
    """HYPOTHESIS to be confirmed on a real server (not verified when this was written): the list query
    shape (filter + sort date desc, emp_code asc) is served by an index with no blocking SORT. The
    assignment itself only requires IXSCAN and no COLLSCAN; the SORT assertion is stricter. If the
    emp_code cases fail on SORT, an (emp_code, date -1) index is the fix."""
    from datetime import datetime, timedelta, timezone
    api.attendance_logs.insert_many([
        {"emp_code": f"EMP{e:04d}", "date": (datetime(2026, 1, 1) + timedelta(days=d)).date().isoformat(),
         "status": "PRESENT", "punch_in": datetime(2026, 1, 1, 4, tzinfo=timezone.utc)}
        for e in range(1, 21) for d in range(60)])                      # 1,200 records
    plan = api.attendance_logs.find(query, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(20).limit(20).explain()
    stages = _stage_names(plan["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in stages and "COLLSCAN" not in stages, stages
    assert "SORT" not in stages, f"blocking in-memory sort in plan: {stages}"


# =========================================================================== #
# Review additions: things a mock cannot prove. NOT RUN unless a real MongoDB is reachable.
# =========================================================================== #
def test_review_lone_surrogate_emp_code_is_a_422_not_a_bson_encoding_error(api):
    api.employees.insert_one({"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"})
    c = TestClient(main.app, raise_server_exceptions=False)
    r = c.post("/attendance/punch-in", content=b'{"emp_code":"\\ud800"}', headers={"content-type": "application/json"})
    assert r.status_code == 422                       # on a real server an unvalidated value would raise in PyMongo (500)


def test_review_dropping_the_unique_index_makes_punch_in_fail_closed_on_a_real_server(api):
    api.employees.insert_one({"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"})
    api.attendance_logs.drop_index("uq_emp_code_date")
    codes = run_together(lambda i: TestClient(main.app).post("/attendance/punch-in", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 9, 0, i)}).status_code, 10)
    assert codes == [503] * 10
    assert api.attendance_logs.count_documents({}) == 0          # no duplicates, no records at all


def test_review_seeded_duplicates_block_the_index_and_writes_are_refused(real_db, monkeypatch):
    monkeypatch.setattr(main, "db", real_db)
    real_db.employees.insert_one({"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"})
    real_db.attendance_logs.insert_many([{"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"},
                                         {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"}])
    assert main.initialize_indexes() is False
    r = TestClient(main.app).post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 6, 9, 0)})
    assert r.status_code == 503
    assert real_db.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}) == 2


# =========================================================================== #
# Phase 3: punch-out and PATCH. NOT RUN unless a real MongoDB is reachable.
#
# These are the only tests that can show that (1) MongoDB really applies one update_one atomically when requests are
# truly parallel, (2) the compare-and-set filters used by the two endpoints behave on real BSON the way they do on
# mongomock, and (3) no correction's history entry is lost. The deterministic interleaving tests in
# test_punch_out.py / test_regularize.py cover only this application's own logic.
# =========================================================================== #
P3_EMP = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
P3_DATE = "2026-07-20"


def _patch(i, body):
    return TestClient(main.app).patch(f"/attendance/EMP0001/{P3_DATE}", json=body)


def test_phase3_simultaneous_punch_outs_give_exactly_one_200(api):
    api.employees.insert_one(P3_EMP)
    add_record(api, "EMP0001", P3_DATE, ist_ms(2026, 7, 20, 9, 30))
    n = 20
    results = run_together(lambda i: (TestClient(main.app).post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 19, 0, i)}), i), n)
    codes = sorted(r.status_code for r, _ in results)
    assert codes == [200] + [409] * (n - 1)
    winner = next(i for r, i in results if r.status_code == 200)
    s = api.attendance_logs.find_one({"emp_code": "EMP0001", "date": P3_DATE})
    assert s["punch_out"] == main.ms_to_datetime(ist_ms(2026, 7, 20, 19, 0, winner))     # the winner's value, intact
    assert s["history"] == []                                                          # punch-out never writes history
    assert api.attendance_logs.count_documents({"emp_code": "EMP0001"}) == 1


def test_phase3_simultaneous_identical_punch_outs_give_exactly_one_200(api):
    api.employees.insert_one(P3_EMP)
    add_record(api, "EMP0001", P3_DATE, ist_ms(2026, 7, 20, 9, 30))
    codes = run_together(lambda i: TestClient(main.app).post("/attendance/punch-out", json={
        "emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 19, 0)}).status_code, 20)
    assert sorted(codes) == [200] + [409] * 19


def test_phase3_concurrent_corrections_never_lose_a_history_entry(api):
    """Every 200 must have exactly one history entry, and the entries must form an unbroken chain: each entry's
    `from` is the previous entry's `to`. A lost update or a lost entry breaks the chain or the count."""
    api.employees.insert_one(P3_EMP)
    original_out = ist_ms(2026, 7, 20, 18, 30)
    add_record(api, "EMP0001", P3_DATE, ist_ms(2026, 7, 20, 9, 30), original_out)
    n = 12
    results = run_together(lambda i: (_patch(i, {
        "reason": f"fix number {i}", "regularized_by": f"hr{i:02d}",
        "punch_out": ist_ms(2026, 7, 20, 19, i)}).status_code, i), n)             # 12 DIFFERENT punch_out values
    assert {code for code, _ in results} <= {200, 409}, results                    # 409 = the loser of a long streak
    winners = sorted(f"hr{i:02d}" for code, i in results if code == 200)
    assert winners, "at least one correction must succeed"
    s = api.attendance_logs.find_one({"emp_code": "EMP0001", "date": P3_DATE})
    assert sorted(e["by"] for e in s["history"]) == winners                         # one entry per success, none lost
    previous = main.ms_to_datetime(original_out)
    for entry in s["history"]:
        assert entry["changes"]["punch_out"]["from"] == previous
        previous = entry["changes"]["punch_out"]["to"]
    assert s["punch_out"] == previous                                              # the last entry describes the record
    final = main.derive_fields("PRESENT", s["punch_in"], s["punch_out"], main.parse_date(P3_DATE), "09:30", "18:30")
    assert (s["work_hours"], s["overtime_minutes"], s["half_day"]) == (
        final["work_hours"], final["overtime_minutes"], final["half_day"])


def test_phase3_a_correction_racing_a_punch_out_never_erases_the_punch_out(api):
    api.employees.insert_one(P3_EMP)
    for round_no in range(15):                                                     # many rounds: races are probabilistic
        day = f"2026-08-{round_no + 1:02d}"
        add_record(api, "EMP0001", day, ist_ms(2026, 8, round_no + 1, 9, 30))

        def go(i, day=day, d=round_no + 1):
            if i == 0:
                r = TestClient(main.app).post("/attendance/punch-out", json={
                    "emp_code": "EMP0001", "punched_at": ist_ms(2026, 8, d, 19, 10)})
            else:
                r = TestClient(main.app).patch(f"/attendance/EMP0001/{day}", json={
                    "reason": "racing a punch-out", "regularized_by": "hr", "status": "WFH"})
            return i, r.status_code

        codes = dict(run_together(go, 2))
        assert codes[0] == 200 and codes[1] in (200, 409), (round_no, codes)       # a punch-out on an open record wins
        s = api.attendance_logs.find_one({"emp_code": "EMP0001", "date": day})
        assert s["punch_out"] == main.ms_to_datetime(ist_ms(2026, 8, round_no + 1, 19, 10)), (round_no, codes)
        assert (s["status"] == "WFH") == (codes[1] == 200)
        assert len(s["history"]) == (1 if codes[1] == 200 else 0)
        final = main.derive_fields(s["status"], s["punch_in"], s["punch_out"], main.parse_date(day), "09:30", "18:30")
        assert (s["work_hours"], s["overtime_minutes"]) == (final["work_hours"], final["overtime_minutes"])


def test_phase3_the_end_points_work_on_real_bson_for_the_documented_samples(api):
    api.employees.insert_one(P3_EMP)
    add_record(api, "EMP0001", P3_DATE, ist_ms(2026, 7, 20, 9, 35))
    c = TestClient(main.app)
    r = c.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 19, 10)})
    assert r.status_code == 200 and (r.json()["work_hours"], r.json()["overtime_minutes"]) == (9.58, 40)
    r = c.patch(f"/attendance/EMP0001/{P3_DATE}", json={"reason": "biometric glitch", "regularized_by": "hr.admin",
                                                        "punch_in": ist_ms(2026, 7, 20, 9, 28)})
    assert r.status_code == 200 and (r.json()["work_hours"], r.json()["late_minutes"]) == (9.7, 0)
    assert main.record_to_api(api.attendance_logs.find_one({"emp_code": "EMP0001"})) == r.json()
    assert c.patch(f"/attendance/EMP0001/{P3_DATE}", json={"reason": "same again", "regularized_by": "hr",
                                                           "punch_in": ist_ms(2026, 7, 20, 9, 28)}).status_code == 422


@pytest.mark.parametrize("legacy", [{}, {"history": None}], ids=["history-missing", "history-null"])
def test_phase3_a_legacy_record_without_a_usable_history_array_is_corrected(api, legacy):
    api.employees.insert_one(P3_EMP)
    api.attendance_logs.insert_one({"emp_code": "EMP0001", "date": P3_DATE, "status": "PRESENT",
                                    "punch_in": main.ms_to_datetime(ist_ms(2026, 7, 20, 9, 30)), **legacy})
    r = TestClient(main.app).patch(f"/attendance/EMP0001/{P3_DATE}", json={
        "reason": "legacy record", "regularized_by": "hr", "status": "WFH"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1
    assert len(api.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]) == 1


def test_phase3_server_semantics_the_compare_and_set_filters_rely_on(api):
    """The assumptions behind the update filters, checked on a real server."""
    coll = api.attendance_logs
    at = main.ms_to_datetime(ist_ms(2026, 7, 20, 9, 30))
    coll.insert_one({"emp_code": "EMP0001", "date": P3_DATE, "status": "PRESENT", "punch_in": at})   # no punch_out, no history
    key = {"emp_code": "EMP0001", "date": P3_DATE}
    read = coll.find_one(key)
    assert coll.update_one({**key, "punch_in": read["punch_in"], "punch_out": None, "history": None},
                           {"$set": {"x": 1}}).matched_count == 1       # None matches a MISSING field; datetime round-trips
    assert coll.update_one({**key, "history": {"$size": 0}}, {"$set": {"x": 2}}).matched_count == 0   # $size needs an array
    coll.update_one(key, {"$set": {"history": []}})
    assert coll.update_one({**key, "history": {"$size": 0}}, {"$push": {"history": {"n": 1}}}).matched_count == 1
    assert coll.update_one({**key, "history": {"$size": 0}}, {"$push": {"history": {"n": 2}}}).matched_count == 0   # stale
    coll.update_one(key, {"$set": {"history": None}})
    from pymongo.errors import WriteError
    with pytest.raises(WriteError):                                      # why a null history is started with $set, not $push
        coll.update_one(key, {"$push": {"history": {"n": 3}}})
    assert coll.update_one({**key, "history": None}, {"$set": {"history": [{"n": 4}]}}).matched_count == 1


def test_phase3_punch_out_lookup_uses_an_index_and_no_collection_scan(api):
    """HYPOTHESIS to be confirmed on a real server (not verified when this was written): the punch-out lookup
    (emp_code equality, punch_in <= x, newest first) is served by the (emp_code, date) unique index with a small
    per-employee SORT stage. The assignment's IXSCAN / no-COLLSCAN requirement is only stated for the five explained
    endpoints; this just guards against a full scan on every punch-out."""
    api.attendance_logs.insert_many([
        {"emp_code": f"EMP{e:04d}", "date": f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}", "status": "PRESENT",
         "punch_in": main.ms_to_datetime(ist_ms(2026, 1 + d // 28, 1 + d % 28, 9, 30))}
        for e in range(1, 21) for d in range(60)])
    plan = api.attendance_logs.find(
        {"emp_code": "EMP0003", "status": {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
         "punch_in": {"$lt": main.ms_to_datetime(ist_ms(2026, 2, 10, 19, 0))}}, {"_id": 0}
    ).sort([("punch_in", -1), ("date", -1)]).limit(1).explain()
    stages = _stage_names(plan["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in stages and "COLLSCAN" not in stages, stages

