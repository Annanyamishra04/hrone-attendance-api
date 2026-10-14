"""Regression tests found during the Phase 2 review.

1. Hostile request bodies must be a 422, never a 500 (NaN / Infinity / deep nesting / lone surrogates used to
   make FastAPI's default 422 response impossible to serialize).
2. A write endpoint must refuse (503) when the unique index behind its 409 is not really there, instead of
   silently creating duplicates.
Both run against mongomock, so they prove OUR logic, not MongoDB's behaviour (see conftest.py)."""
import mongomock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from tests.fakes import FakeDB
from tests.helpers import employee_payload, ist_ms

JSON = {"content-type": "application/json"}
EMPLOYEE_JSON = b'{"emp_code":"EMP0001","name":%s,"email":"a@b.co","department":"d","joined_on":"2026-01-05"}'


@pytest.fixture
def lenient(mdb):
    """A client that returns a real 500 response instead of re-raising, so a 500 is an assertion failure.
    The database is empty (apart from its indexes); a test that needs employees asks for `employees` too."""
    return TestClient(main.app, raise_server_exceptions=False)


# --------------------------------------------------------------------------- #
# 1. Validation failures are always a clean 422
# --------------------------------------------------------------------------- #
HOSTILE_BODIES = {
    "NaN": b'{"emp_code":"EMP0001","punched_at":NaN}',
    "Infinity": b'{"emp_code":"EMP0001","punched_at":Infinity}',
    "-Infinity": b'{"emp_code":"EMP0001","punched_at":-Infinity}',
    "NaN in a string field": b'{"emp_code":NaN}',
    "deeply nested array": b"[" * 5000 + b"]" * 5000,
    "deeply nested value": b'{"emp_code":' + b"[" * 5000 + b"]" * 5000 + b"}",
    "lone surrogate emp_code": b'{"emp_code":"\\ud800"}',
    "lone surrogate in a later field": b'{"emp_code":"EMP0001","status":"\\udfff"}',
    "operator object as emp_code": b'{"emp_code":{"$ne":""}}',
    "operator object as status": b'{"emp_code":"EMP0001","status":{"$ne":"x"}}',
    "invalid UTF-8": b'{"emp_code":"\xff"}',
    "duplicate key, last wins and is invalid": b'{"emp_code":"EMP0001","punched_at":1783312500000,"punched_at":1}',
    "huge integer": b'{"emp_code":"EMP0001","punched_at":' + b"9" * 400 + b"}",
    "exponent float": b'{"emp_code":"EMP0001","punched_at":1.783312500000e12}',
    "bare scalar": b"123",
    "null": b"null",
    "empty": b"",
}


@pytest.mark.parametrize("url", ["/attendance/punch-in", "/employees"])
@pytest.mark.parametrize("body", HOSTILE_BODIES.values(), ids=HOSTILE_BODIES.keys())
def test_hostile_body_is_never_a_5xx(lenient, url, body):
    r = lenient.post(url, content=body, headers=JSON)
    assert r.status_code == 422, (r.status_code, r.text[:200])   # every body above is invalid: 422, never 5xx


@pytest.mark.parametrize("body", [b'{"emp_code":"EMP0001","punched_at":NaN}',
                                  b'{"emp_code":"EMP0001","punched_at":Infinity}',
                                  b'{"emp_code":"EMP0001","punched_at":-Infinity}'], ids=["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_422_and_store_nothing(lenient, employees, body):
    r = lenient.post("/attendance/punch-in", content=body, headers=JSON)
    assert r.status_code == 422
    assert employees.attendance_logs.count_documents({}) == 0


@pytest.mark.parametrize("field", ["name", "department"])
def test_lone_surrogate_in_an_employee_string_is_422(lenient, employees, field):
    body = employee_payload(emp_code="EMP0999")
    raw = ('{' + ",".join(f'"{k}":"{v}"' for k, v in body.items() if k != field) + f',"{field}":"\\ud800"' + '}').encode()
    r = lenient.post("/employees", content=raw, headers=JSON)
    assert r.status_code == 422
    assert employees.employees.count_documents({"emp_code": "EMP0999"}) == 0


def test_lone_surrogate_emp_code_never_reaches_the_database(lenient, spy_db, employees):
    # a lone surrogate cannot be BSON-encoded: on a real server the lookup would raise inside PyMongo
    spy = spy_db.spy_on("employees", forbid=("find_one", "find", "insert_one"))
    r = lenient.post("/attendance/punch-in", content=b'{"emp_code":"\\ud800"}', headers=JSON)
    assert r.status_code == 422 and spy.calls == []


def test_a_valid_non_ascii_emp_code_is_still_an_ordinary_404(lenient):
    r = lenient.post("/attendance/punch-in", json={"emp_code": "EMP\u00e9\U0001F600"})
    assert r.status_code == 404


def test_the_422_body_has_type_loc_msg_and_does_not_echo_the_input(lenient):
    r = lenient.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": "secret-looking-value"})
    detail = r.json()["detail"]
    assert r.status_code == 422 and isinstance(detail, list) and detail
    assert all(set(item) == {"type", "loc", "msg"} for item in detail)
    assert detail[0]["loc"] == ["body", "punched_at"]
    assert "secret-looking-value" not in r.text


def test_every_422_in_the_api_has_the_same_shape(lenient, employees):
    responses = [
        lenient.get("/attendance", params={"date_from": "2026-07-02", "date_to": "2026-07-01"}),   # raised by our code
        lenient.get("/attendance", params={"date_from": "2026-02-30"}),                            # validator
        lenient.get("/attendance", params={"page": 0}),                                            # constraint
        lenient.get("/employees", params={"page_size": 101}),
        lenient.post("/employees", json={}),
    ]
    for r in responses:
        assert r.status_code == 422
        assert all(set(i) == {"type", "loc", "msg"} for i in r.json()["detail"])


@pytest.mark.parametrize("raw", [b'{"emp_code":"\xff"}', b"\xc3", b'\xff\xfe{"a":1}', b'{"emp_code":"EMP0001"}\x80'],
                         ids=["invalid-utf8", "lone-lead-byte", "utf16-bom", "trailing-garbage-byte"])
@pytest.mark.parametrize("url", ["/attendance/punch-in", "/employees"])
def test_a_body_that_is_not_valid_utf8_is_a_422_like_any_other_invalid_body(lenient, url, raw):
    r = lenient.post(url, content=raw, headers=JSON)          # FastAPI alone answers 400 here
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "json_invalid"


def test_other_http_errors_keep_their_status_and_body(lenient, employees):
    assert lenient.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).json() == {"detail": "unknown employee"}
    assert lenient.get("/no-such-path").status_code == 404
    assert lenient.put("/employees").status_code == 405


@pytest.mark.parametrize("params", [
    {"emp_code": "$ne"}, {"emp_code": ".*"}, {"emp_code": "EMP0001\x00"}, {"emp_code": "%ED%A0%80"},
    {"date_from": "2026-07-01\x00"}, {"date_from": "\u0662\u0660\u0662\u0666-\u0660\u0667-\u0660\u0661"},
    {"date_from": " 2026-07-01"}, {"date_to": "9999-12-31"}, {"date_from": "0001-01-01"},
    {"page": "9" * 400}, {"page": "-"}, {"page_size": "1e2"}, {"department": "$ne"}, {"department": "\u00e9" * 1000},
    {"emp_code[$ne]": "x"},
])
@pytest.mark.parametrize("url", ["/attendance", "/employees"])
def test_odd_query_strings_are_never_a_5xx(lenient, url, params):
    assert lenient.get(url, params=params).status_code < 500


# --------------------------------------------------------------------------- #
# 2. The unique-index guard on the two write endpoints
# --------------------------------------------------------------------------- #
def test_punch_in_is_refused_with_503_when_the_unique_index_is_gone(lenient, employees):
    employees.attendance_logs.drop_index("uq_emp_code_date")
    r = lenient.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 9, 0)})
    assert r.status_code == 503 and "unique index" in r.json()["detail"]
    assert employees.attendance_logs.count_documents({}) == 0       # nothing written


def test_create_employee_is_refused_with_503_when_the_unique_index_is_gone(lenient, mdb):
    mdb.employees.drop_index("uq_emp_code")
    assert lenient.post("/employees", json=employee_payload()).status_code == 503
    assert lenient.post("/employees", json=employee_payload()).status_code == 503
    assert mdb.employees.count_documents({}) == 0                   # not even one copy, let alone two


def test_reads_still_work_without_the_unique_indexes(lenient, seeded):
    seeded.attendance_logs.drop_index("uq_emp_code_date")
    seeded.employees.drop_index("uq_emp_code")
    assert lenient.get("/attendance").status_code == 200 and lenient.get("/employees").status_code == 200


def test_the_guard_never_creates_or_drops_an_index(spy_db, employees):
    spy = spy_db.spy_on("attendance_logs")
    before = list(employees.attendance_logs.list_indexes())
    TestClient(main.app).post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(2026, 7, 20, 9, 0)})
    assert not {"create_index", "create_indexes", "drop_index", "drop_indexes"} & set(spy.names())
    assert list(employees.attendance_logs.list_indexes()) == before


def test_seeded_duplicates_that_block_the_unique_index_make_writes_fail_closed(monkeypatch):
    """The data model promises at most one record per (emp_code, date). If seeded data breaks that promise the
    unique index cannot be built. Before the guard: /health said 503 but punch-in still returned 201 twice."""
    database = mongomock.MongoClient(tz_aware=True)["dupes"]
    database.employees.insert_one({"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"})
    for _ in range(2):   # two records for one (emp_code, date): violates the data model
        database.attendance_logs.insert_one({"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"})
    monkeypatch.setattr(main, "db", database)
    assert main.initialize_indexes() is False                       # the index cannot be built, and we say so
    c = TestClient(main.app, raise_server_exceptions=False)
    at = ist_ms(2026, 7, 6, 9, 0)
    assert [c.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": at}).status_code for _ in range(3)] == [503] * 3
    assert database.attendance_logs.count_documents({"emp_code": "EMP0001", "date": "2026-07-06"}) == 2   # unchanged


# the guard itself, on the index shapes that must NOT count as "the unique index"
UNIQUE_KEYS = {"emp_code": 1, "date": 1}


@pytest.mark.parametrize("options", [
    {}, {"unique": False},
    {"unique": True, "partialFilterExpression": {"status": "PRESENT"}},
    {"unique": True, "sparse": True},
    {"unique": True, "collation": {"locale": "en", "strength": 2}},
    {"unique": True, "expireAfterSeconds": 0},
    {"unique": True, "hidden": True},
], ids=["not-unique", "unique-false", "partial", "sparse", "collation", "ttl-zero", "hidden"])
def test_guard_rejects_an_index_with_the_right_keys_but_incompatible_options(monkeypatch, options):
    d = FakeDB()
    d["attendance_logs"].indexes.append({"v": 2, "key": UNIQUE_KEYS, "name": "lookalike", **options})
    monkeypatch.setattr(main, "db", d)
    with pytest.raises(HTTPException) as exc:
        main.require_unique_index("attendance_logs")
    assert exc.value.status_code == 503
    assert d["attendance_logs"].create_calls == []                   # never "fixes" it by creating or replacing


def test_guard_accepts_an_equivalent_unique_index_under_any_name(monkeypatch):
    d = FakeDB()
    d["attendance_logs"].indexes.append({"v": 2, "key": UNIQUE_KEYS, "name": "my_own_name", "unique": True})
    d["employees"].indexes.append({"v": 2, "key": {"emp_code": 1}, "name": "x", "unique": True})
    monkeypatch.setattr(main, "db", d)
    main.require_unique_index("attendance_logs")                     # no exception
    main.require_unique_index("employees")


def test_guard_for_an_empty_database_is_a_503(monkeypatch):
    monkeypatch.setattr(main, "db", FakeDB())
    for name in ("employees", "attendance_logs"):
        with pytest.raises(HTTPException):
            main.require_unique_index(name)
