"""POST /employees and GET /employees, against mongomock (see conftest.py for what that does NOT prove)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from tests.helpers import employee_payload, run_together

EMPLOYEE_KEYS = {"emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at"}


# --------------------------------------------------------------------------- #
# POST /employees: success
# --------------------------------------------------------------------------- #
def test_create_returns_201_with_exactly_the_employee_fields(client, mdb, monkeypatch):
    fixed = datetime(2026, 7, 6, 4, 35, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "now_utc", lambda: fixed)
    r = client.post("/employees", json=employee_payload())
    assert r.status_code == 201
    assert r.json() == {
        "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering",
        "shift_start": "09:30", "shift_end": "18:30",           # defaults when omitted
        "joined_on": "2026-01-05",
        "created_at": main.datetime_to_ms(fixed),                # epoch ms, set by the server
    }
    assert set(r.json()) == EMPLOYEE_KEYS and "_id" not in r.json()


def test_create_stores_created_at_as_a_bson_datetime_and_get_returns_the_same_ms(client, mdb):
    created = client.post("/employees", json=employee_payload()).json()
    stored = mdb.employees.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["created_at"], datetime)
    assert main.datetime_to_ms(stored["created_at"]) == created["created_at"]
    assert client.get("/employees").json()["items"][0] == created


def test_a_client_supplied_created_at_or_unknown_field_is_ignored(client, mdb):
    r = client.post("/employees", json=employee_payload(created_at=1, _id="x", late_minutes=5, extra="y"))
    assert r.status_code == 201 and r.json()["created_at"] > 1_000_000_000_000
    stored = mdb.employees.find_one({"emp_code": "EMP0001"})
    assert "extra" not in stored and "late_minutes" not in stored and stored["_id"] != "x"


def test_overnight_shift_is_accepted(client):
    r = client.post("/employees", json=employee_payload(shift_start="22:00", shift_end="06:00"))
    assert r.status_code == 201 and (r.json()["shift_start"], r.json()["shift_end"]) == ("22:00", "06:00")


def _email_of_length(n):
    local_and_domain = "a@" + "b" * (n - len("a@") - len(".co")) + ".co"
    assert len(local_and_domain) == n
    return local_and_domain


@pytest.mark.parametrize("field,value", [
    ("emp_code", "EMP0001"), ("emp_code", "EMP000001"),          # 4 and 6 digits: both allowed
    ("name", "N" * 100), ("department", "D" * 50),                # exactly the maximum length
    ("email", _email_of_length(120)),
])
def test_boundary_values_that_are_valid(client, field, value):
    assert client.post("/employees", json=employee_payload(**{field: value})).status_code == 201


# --------------------------------------------------------------------------- #
# POST /employees: 422
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("body", [
    employee_payload(emp_code="emp0001"),                 # lower case
    employee_payload(emp_code="EMP123"),                  # 3 digits: too short
    employee_payload(emp_code="EMP1234567"),              # 7 digits: too long
    employee_payload(emp_code="EMP0001\n"),               # trailing newline must not slip through
    employee_payload(emp_code="EMP\u0660\u0660\u0660\u0661"),  # Arabic-Indic digits are not [0-9]
    employee_payload(emp_code="XYZ0001"),
    employee_payload(emp_code=""),
    employee_payload(emp_code=1234),                      # not a string
    employee_payload(name=""),
    employee_payload(name="N" * 101),
    employee_payload(department=""),
    employee_payload(department="D" * 51),
    employee_payload(email="no-at-sign"),
    employee_payload(email="a@b"),                        # no dot after the @
    employee_payload(email="a b@c.com"),
    employee_payload(email=_email_of_length(121)),
    employee_payload(shift_start="24:00"),
    employee_payload(shift_start="9:30"),
    employee_payload(shift_start="09:60"),
    employee_payload(shift_start="0930"),
    employee_payload(shift_end="18:30:00"),
    employee_payload(shift_start="10:00", shift_end="10:00"),   # must differ
    employee_payload(shift_start="09:30", shift_end="09:30"),   # equal because of the defaults
    employee_payload(joined_on="2026-02-30"),             # not a real date
    employee_payload(joined_on="2026-7-6"),
    employee_payload(joined_on="20260706"),
    employee_payload(joined_on="2026-07-06T00:00:00"),
    employee_payload(joined_on=""),
    employee_payload(joined_on=20260706),
], ids=lambda b: repr({k: v for k, v in b.items() if v != employee_payload().get(k)})[:60])
def test_invalid_input_is_422_and_nothing_is_stored(client, mdb, body):
    r = client.post("/employees", json=body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert mdb.employees.count_documents({}) == 0


@pytest.mark.parametrize("missing", ["emp_code", "name", "email", "department", "joined_on"])
def test_missing_required_field_is_422(client, missing):
    body = employee_payload()
    del body[missing]
    assert client.post("/employees", json=body).status_code == 422


def test_default_shift_equal_to_an_explicit_shift_is_422(client):
    # shift_end defaults to 18:30; asking for shift_start 18:30 would make them equal
    assert client.post("/employees", json=employee_payload(shift_start="18:30")).status_code == 422


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"x"'])
def test_non_object_body_is_422(client, raw):
    r = client.post("/employees", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# POST /employees: duplicates (409) are decided by the unique index, not by a pre-check
# --------------------------------------------------------------------------- #
def test_duplicate_emp_code_is_409_and_the_original_is_untouched(client, mdb):
    first = client.post("/employees", json=employee_payload(name="Original"))
    second = client.post("/employees", json=employee_payload(name="Impostor", department="Sales"))
    assert first.status_code == 201 and second.status_code == 409
    assert second.json() == {"detail": "emp_code already exists"}
    assert mdb.employees.count_documents({}) == 1
    assert mdb.employees.find_one({"emp_code": "EMP0001"})["name"] == "Original"


def test_the_409_comes_from_the_unique_index_not_from_a_look_up_first(spy_db, mdb):
    spy = spy_db.spy_on("employees", forbid=("find_one", "find", "count_documents"))
    mdb.employees.insert_one(employee_payload())   # the row a concurrent request would have just inserted
    r = TestClient(main.app).post("/employees", json=employee_payload())
    assert r.status_code == 409
    assert spy.names() == ["list_indexes", "insert_one"]   # verify the index exists, then try to insert: nothing else


def test_emp_code_is_case_sensitive_distinct_codes_do_not_collide(client):
    assert client.post("/employees", json=employee_payload(emp_code="EMP0001")).status_code == 201
    assert client.post("/employees", json=employee_payload(emp_code="EMP00010")).status_code == 201
    assert client.post("/employees", json=employee_payload(emp_code="EMP0002")).status_code == 201


def test_simultaneous_creates_of_one_emp_code_give_exactly_one_201(mdb):
    def create(_i):
        return TestClient(main.app).post("/employees", json=employee_payload()).status_code

    codes = run_together(create, 16)
    assert sorted(codes) == [201] + [409] * 15
    assert mdb.employees.count_documents({"emp_code": "EMP0001"}) == 1


# --------------------------------------------------------------------------- #
# GET /employees
# --------------------------------------------------------------------------- #
def _insert_employees(mdb, n, department=lambda i: "Engineering"):
    mdb.employees.insert_many([
        {"emp_code": f"EMP{i:04d}", "name": f"Person {i}", "email": f"p{i}@example.com", "department": department(i),
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
         "created_at": datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc)}
        for i in range(1, n + 1)
    ])


def test_empty_collection(client):
    assert client.get("/employees").json() == {"items": [], "total": 0, "page": 1, "page_size": 20}


def test_sample_employees_have_the_employee_shape(client, seeded, sample_employees):
    body = client.get("/employees").json()
    assert body["total"] == 6 and [e["emp_code"] for e in body["items"]] == [f"EMP000{i}" for i in range(1, 7)]
    for item in body["items"]:
        assert set(item) == EMPLOYEE_KEYS
        assert item["created_at"] == main.datetime_to_ms(sample_employees[item["emp_code"]]["created_at"])


def test_sorted_by_emp_code_whatever_the_insertion_order(client, mdb):
    for code in ("EMP0003", "EMP0005", "EMP0001", "EMP0004", "EMP0002"):   # scrambled on purpose
        mdb.employees.insert_one({"emp_code": code, "name": "x", "email": "x@x.co", "department": "D",
                                  "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05",
                                  "created_at": datetime(2026, 1, 5, tzinfo=timezone.utc)})
    codes = [e["emp_code"] for e in client.get("/employees").json()["items"]]
    assert codes == ["EMP0001", "EMP0002", "EMP0003", "EMP0004", "EMP0005"]


def test_department_filter_is_exact_and_case_sensitive_and_total_counts_the_filtered_set(client, mdb):
    _insert_employees(mdb, 9, department=lambda i: "Engineering" if i <= 6 else "Sales")
    r = client.get("/employees", params={"department": "Engineering"}).json()
    assert r["total"] == 6 and len(r["items"]) == 6 and {e["department"] for e in r["items"]} == {"Engineering"}
    assert client.get("/employees", params={"department": "Sales"}).json()["total"] == 3   # not 9
    assert client.get("/employees", params={"department": "engineering"}).json()["total"] == 0
    assert client.get("/employees", params={"department": "Engineer"}).json()["total"] == 0
    assert client.get("/employees", params={"department": "Nope"}).json() == {
        "items": [], "total": 0, "page": 1, "page_size": 20}


def test_pagination_pages_are_contiguous_and_complete(client, mdb):
    _insert_employees(mdb, 25)
    seen = []
    for page, expected_len in ((1, 20), (2, 5), (3, 0)):
        r = client.get("/employees", params={"page": page}).json()
        assert (r["page"], r["page_size"], r["total"], len(r["items"])) == (page, 20, 25, expected_len)
        seen += [e["emp_code"] for e in r["items"]]
    assert seen == [f"EMP{i:04d}" for i in range(1, 26)]       # no overlap, no gap, in order


def test_page_one_starts_at_the_first_row(client, mdb):
    # the starter used skip = page * page_size, which made page 1 start at row 21
    _insert_employees(mdb, 25)
    assert client.get("/employees").json()["items"][0]["emp_code"] == "EMP0001"


@pytest.mark.parametrize("page_size", [1, 7, 20, 25, 26, 100])
def test_every_page_size_reassembles_to_the_same_list(client, mdb, page_size):
    _insert_employees(mdb, 25)
    codes, page = [], 1
    while True:
        r = client.get("/employees", params={"page": page, "page_size": page_size}).json()
        assert r["total"] == 25 and r["page_size"] == page_size
        if not r["items"]:
            break
        assert len(r["items"]) <= page_size
        codes += [e["emp_code"] for e in r["items"]]
        page += 1
    assert codes == [f"EMP{i:04d}" for i in range(1, 26)]


def test_filtered_pagination_uses_the_filtered_total(client, mdb):
    _insert_employees(mdb, 30, department=lambda i: "Sales" if i % 3 == 0 else "Engineering")   # 10 Sales
    r = client.get("/employees", params={"department": "Sales", "page": 2, "page_size": 4}).json()
    assert r["total"] == 10 and [e["emp_code"] for e in r["items"]] == ["EMP0015", "EMP0018", "EMP0021", "EMP0024"]


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page": -1}, {"page": "abc"}, {"page": "1.5"}, {"page": ""},
    {"page_size": 0}, {"page_size": -5}, {"page_size": 101}, {"page_size": "x"},
])
def test_invalid_paging_is_422(client, params):
    assert client.get("/employees", params=params).status_code == 422


def test_page_size_100_is_the_allowed_maximum(client, mdb):
    _insert_employees(mdb, 3)
    r = client.get("/employees", params={"page_size": 100})
    assert r.status_code == 200 and r.json()["page_size"] == 100


def test_an_absurdly_large_page_is_an_empty_page_not_an_error(client, mdb):
    _insert_employees(mdb, 3)
    r = client.get("/employees", params={"page": 10 ** 30})
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["total"] == 3


def test_paging_is_done_by_the_database(spy_db, mdb):
    _insert_employees(mdb, 25)
    spy = spy_db.spy_on("employees")
    TestClient(main.app).get("/employees", params={"department": "Engineering", "page": 3, "page_size": 5})
    assert ("count_documents", {"department": "Engineering"}) in spy.calls
    assert ("sort", [("emp_code", 1)]) in spy.calls and ("skip", 10) in spy.calls and ("limit", 5) in spy.calls


def test_an_employee_without_created_at_is_listed_with_null_not_a_500(client, mdb):
    mdb.employees.insert_one({"emp_code": "EMP0001", "name": "x", "email": "x@x.co", "department": "D",
                              "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-05"})
    r = client.get("/employees")
    assert r.status_code == 200 and r.json()["items"][0]["created_at"] is None
