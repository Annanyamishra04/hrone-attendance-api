"""The implemented endpoints compared with openapi.yaml, the authoritative contract.

Scope: every endpoint except /health (Phase 1, checked by test_health.py): employees, attendance, punch-out, PATCH and,
since Phase 4, the four analytics endpoints and /admin/explain/{endpoint}.
Runs against mongomock (and the test-only tests/mini_agg.py interpreter for the analytics pipelines) for the data, so this
proves the response SHAPES and the declared parameters, not anything about a real MongoDB."""
import copy
import pathlib

import jsonschema
import pytest
import yaml

from app import main
from tests.helpers import employee_payload, ist_ms

SPEC = yaml.safe_load((pathlib.Path(__file__).parent.parent / "openapi.yaml").read_text())
GENERATED = main.app.openapi()


def deref(node, document=SPEC):
    """Inline every $ref (and the {allOf: [$ref], description} wrapper the YAML uses) from `document`."""
    if isinstance(node, dict):
        if "$ref" in node:
            target = document
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            return deref(target, document)
        if "allOf" in node and len(node["allOf"]) == 1:
            merged = deref(node["allOf"][0], document)
            merged.update({k: deref(v, document) for k, v in node.items() if k != "allOf"})
            return merged
        return {k: deref(v, document) for k, v in node.items()}
    if isinstance(node, list):
        return [deref(x, document) for x in node]
    return node


def as_json_schema(node):
    """OpenAPI 3.0 -> JSON Schema: `nullable: true` becomes a type list."""
    if isinstance(node, dict):
        node = {k: as_json_schema(v) for k, v in node.items()}
        if node.pop("nullable", False) and "type" in node:
            node["type"] = [node["type"], "null"]
        return node
    if isinstance(node, list):
        return [as_json_schema(x) for x in node]
    return node


def no_extra_fields(schema):
    """The contract lists every response field, so a field it does not list is a contract violation."""
    schema = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node.setdefault("additionalProperties", False)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def schema_of(name):
    return no_extra_fields(as_json_schema(deref(SPEC["components"]["schemas"][name])))


ENDPOINTS = [("/employees", "post"), ("/employees", "get"), ("/attendance/punch-in", "post"),
             ("/attendance/punch-out", "post"), ("/attendance", "get"), ("/attendance/{emp_code}/{date}", "patch"),
             ("/analytics/employees/{emp_code}/monthly", "get"), ("/analytics/departments/summary", "get"),
             ("/analytics/leaderboard/late", "get"), ("/analytics/departments/{department}/trend", "get"),
             ("/admin/explain/{endpoint}", "get")]


def _parameters(operation, document):
    out = {}
    for param in operation.get("parameters", []):
        param = deref(param, document)
        schema = param.get("schema", {})
        if "anyOf" in schema:                                   # FastAPI writes Optional[X] as anyOf [X, null]
            schema = next(s for s in schema["anyOf"] if s.get("type") != "null")
        keep = ("type", "minimum", "maximum", "default", "enum")
        out[param["name"]] = {"required": param.get("required", False), **{k: schema[k] for k in keep if k in schema}}
    return out


# --------------------------------------------------------------------------- #
# Declared parameters and status codes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,method", ENDPOINTS)
def test_query_parameters_match_the_contract(path, method):
    expected = _parameters(SPEC["paths"][path][method], SPEC)
    actual = _parameters(GENERATED["paths"][path][method], GENERATED)
    assert actual == expected                                       # names, required, type, min, max, default, enum


@pytest.mark.parametrize("path,method", ENDPOINTS)
def test_every_status_code_the_contract_lists_is_declared(path, method):
    expected = set(SPEC["paths"][path][method]["responses"])
    assert expected <= set(GENERATED["paths"][path][method]["responses"])


def test_success_status_codes():
    assert set(SPEC["paths"]["/employees"]["post"]["responses"]) >= {"201"}
    assert set(SPEC["paths"]["/attendance/punch-in"]["post"]["responses"]) >= {"201"}
    # punch-out and PATCH update an existing record: 200, not 201, in the contract and in the implementation
    for path, method in (("/attendance/punch-out", "post"), ("/attendance/{emp_code}/{date}", "patch")):
        assert "200" in SPEC["paths"][path][method]["responses"] and "201" not in SPEC["paths"][path][method]["responses"]
        assert "200" in GENERATED["paths"][path][method]["responses"] and "201" not in GENERATED["paths"][path][method]["responses"]


# --------------------------------------------------------------------------- #
# Request bodies: required fields, field names, lengths, defaults, numeric limits
# --------------------------------------------------------------------------- #
def _body_schema(document, name):
    return document["components"]["schemas"][name]


def test_employee_create_fields_match_the_contract():
    expected = deref(_body_schema(SPEC, "EmployeeCreate"))
    actual = _body_schema(GENERATED, "EmployeeIn")
    assert set(actual["required"]) == set(expected["required"])
    assert set(actual["properties"]) == set(expected["properties"])
    for name, spec in expected["properties"].items():
        got = actual["properties"][name]
        for key in ("minLength", "maxLength", "default"):
            assert got.get(key) == spec.get(key), (name, key)


def test_punch_in_request_fields_match_the_contract():
    expected = deref(_body_schema(SPEC, "PunchInRequest"))
    actual = _body_schema(GENERATED, "PunchInIn")
    assert set(actual["required"]) == set(expected["required"])
    assert set(actual["properties"]) == set(expected["properties"])
    millis = actual["properties"]["punched_at"]
    assert (millis["minimum"], millis["maximum"], millis["type"]) == (
        expected["properties"]["punched_at"]["minimum"], expected["properties"]["punched_at"]["maximum"], "integer")
    status = actual["properties"]["status"]
    assert status["enum"] == expected["properties"]["status"]["enum"] and status["default"] == "PRESENT"


def test_punch_out_request_fields_match_the_contract():
    expected = deref(_body_schema(SPEC, "PunchOutRequest"))
    actual = _body_schema(GENERATED, "PunchOutIn")
    assert set(actual["required"]) == set(expected["required"])
    assert set(actual["properties"]) == set(expected["properties"])
    millis, want = actual["properties"]["punched_at"], expected["properties"]["punched_at"]
    assert (millis["minimum"], millis["maximum"], millis["type"]) == (want["minimum"], want["maximum"], "integer")


def test_regularize_request_fields_match_the_contract():
    expected = deref(_body_schema(SPEC, "RegularizeRequest"))
    actual = _body_schema(GENERATED, "RegularizeIn")
    assert set(actual["required"]) == set(expected["required"]) == {"reason", "regularized_by"}
    assert set(actual["properties"]) == set(expected["properties"])          # no derived field can be sent
    for name in ("reason", "regularized_by"):
        for key in ("minLength", "maxLength"):
            assert actual["properties"][name][key] == expected["properties"][name][key], (name, key)
    for name in ("punch_in", "punch_out"):
        got, want = actual["properties"][name], expected["properties"][name]
        assert (got["minimum"], got["maximum"], got["type"]) == (want["minimum"], want["maximum"], "integer")
    assert actual["properties"]["status"]["enum"] == expected["properties"]["status"]["enum"]


# --------------------------------------------------------------------------- #
# Real responses validate against the contract's own schemas (and carry no undocumented field)
# --------------------------------------------------------------------------- #
def test_employee_responses_conform(client, mdb):
    created = client.post("/employees", json=employee_payload())
    assert created.status_code == 201
    jsonschema.validate(created.json(), schema_of("Employee"))
    client.post("/employees", json=employee_payload(emp_code="EMP0002", shift_start="22:00", shift_end="06:00"))
    jsonschema.validate(client.get("/employees").json(), schema_of("EmployeePage"))


def test_punch_in_response_conforms(client, employees):
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": ist_ms(2026, 7, 20, 10, 5)})
    assert r.status_code == 201
    jsonschema.validate(r.json(), schema_of("AttendanceRecord"))


def test_attendance_list_conforms_for_every_sample_record_shape(client, seeded):
    body = client.get("/attendance", params={"page_size": 100}).json()
    assert body["total"] == 9
    jsonschema.validate(body, schema_of("AttendancePage"))          # includes absent, leave, open, history, legacy docs


def test_punch_out_response_conforms(client, seeded):
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": ist_ms(2026, 7, 7, 19, 10)})
    assert r.status_code == 200
    jsonschema.validate(r.json(), schema_of("AttendanceRecord"))


def test_regularize_response_conforms_including_history_with_nulls_and_instants(client, seeded):
    body = {"reason": "contract check", "regularized_by": "hr"}
    for emp, date, extra in [("EMP0003", "2026-07-06", {"status": "ABSENT"}),                       # nulls in changes
                             ("EMP0004", "2026-07-08", {"status": "PRESENT", "punch_in": ist_ms(2026, 7, 8, 9, 45)}),
                             ("EMP0006", "2026-07-07", {"punch_out": ist_ms(2026, 7, 7, 19, 10)}),
                             ("EMP0002", "2026-07-14", {"status": "WFH"})]:                         # legacy record
        r = client.patch(f"/attendance/{emp}/{date}", json={**body, **extra})
        assert r.status_code == 200, r.text
        jsonschema.validate(r.json(), schema_of("AttendanceRecord"))
    jsonschema.validate(client.get("/attendance", params={"page_size": 100}).json(), schema_of("AttendancePage"))


def _response_schema(name):
    body = SPEC["components"]["responses"][name]["content"]["application/json"]["schema"]
    return as_json_schema(deref(body))


def test_error_bodies_conform(client, employees):
    error, validation = _response_schema("Error"), _response_schema("ValidationError")
    jsonschema.validate(client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).json(), error)       # 404
    jsonschema.validate(client.post("/employees", json=employee_payload(emp_code="EMP0001")).json(), error)    # 409
    jsonschema.validate(client.post("/employees", json={}).json(), validation)                                 # 422
    jsonschema.validate(client.get("/attendance", params={"date_from": "2026-07-02", "date_to": "2026-07-01"}).json(), validation)


def test_error_bodies_conform_for_punch_out_and_regularize(client, seeded):
    error, validation = _response_schema("Error"), _response_schema("ValidationError")
    out = lambda **b: client.post("/attendance/punch-out", json=b)                                        # noqa: E731
    pat = lambda emp, date, **b: client.patch(f"/attendance/{emp}/{date}", json={
        "reason": "contract check", "regularized_by": "hr", **b})                                          # noqa: E731
    jsonschema.validate(out(emp_code="EMP9999").json(), error)                                            # 404 unknown employee
    jsonschema.validate(out(emp_code="EMP0002", punched_at=ist_ms(2026, 7, 1, 9, 0)).json(), error)        # 404 no punch-in
    jsonschema.validate(out(emp_code="EMP0001", punched_at=ist_ms(2026, 7, 6, 20, 0)).json(), error)       # 409 closed
    jsonschema.validate(out(emp_code="EMP0006", punched_at=ist_ms(2026, 7, 7, 9, 35)).json(), validation)  # 422 == punch_in
    jsonschema.validate(out().json(), validation)                                                         # 422 body
    jsonschema.validate(pat("EMP9999", "2026-07-06", status="WFH").json(), error)                         # 404
    jsonschema.validate(pat("EMP0003", "2026-07-09", status="WFH").json(), error)                         # 404
    jsonschema.validate(pat("EMP0003", "2026-07-06").json(), validation)                                  # 422 no-op
    jsonschema.validate(pat("EMP0003", "2026-07-06", status="LEAVE", punch_in=ist_ms(2026, 7, 6, 9, 0)).json(), validation)
    jsonschema.validate(client.patch("/attendance/EMP0003/not-a-date", json={}).json(), validation)       # 422 path



# --------------------------------------------------------------------------- #
# Phase 4: analytics and explain responses conform to the contract's schemas (no undocumented fields)
# --------------------------------------------------------------------------- #
def _analytics_world(db):
    from tests.datagen import make_dataset
    emps, logs = make_dataset(seed=11)
    db.employees.insert_many([dict(e) for e in emps])
    db.attendance_logs.insert_many([dict(l) for l in logs])
    return emps


def test_analytics_responses_conform(client, mini_engine):
    _analytics_world(mini_engine)
    r = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
    assert r.status_code == 200
    jsonschema.validate(r.json(), schema_of("EmployeeMonthly"))
    r = client.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert r.status_code == 200 and r.json()["items"]
    jsonschema.validate(r.json(), schema_of("DepartmentSummary"))
    r = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 5})
    assert r.status_code == 200 and r.json()["items"]
    jsonschema.validate(r.json(), schema_of("Leaderboard"))
    r = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2026-07-31"})
    assert r.status_code == 200 and len(r.json()["items"]) == 31
    jsonschema.validate(r.json(), schema_of("Trend"))


def test_empty_analytics_responses_conform(client, mini_engine):
    mini_engine.employees.insert_one({"emp_code": "EMP0001", "name": "A", "email": "a@b.co", "department": "Eng",
                                      "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-08-01"})
    for path, params, name in [("/analytics/employees/EMP0001/monthly", {"month": "2026-07"}, "EmployeeMonthly"),
                               ("/analytics/departments/summary", {"month": "2026-07"}, "DepartmentSummary"),
                               ("/analytics/leaderboard/late", {"month": "2026-07"}, "Leaderboard"),
                               ("/analytics/departments/Eng/trend", {"from": "2026-07-01", "to": "2026-07-10"}, "Trend")]:
        r = client.get(path, params=params)
        assert r.status_code == 200, (path, r.text)
        jsonschema.validate(r.json(), schema_of(name))


def test_explain_response_conforms(client, monkeypatch):
    class Rec:
        def command(self, cmd):
            return {"ok": 1.0, "queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}, "executionStats": {"nReturned": 0}}
    monkeypatch.setattr(main, "db", Rec())
    body = SPEC["paths"]["/admin/explain/{endpoint}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    for name in ("attendance_list",):
        r = client.get(f"/admin/explain/{name}")
        assert r.status_code == 200
        jsonschema.validate(r.json(), as_json_schema(deref(body)))


def test_phase4_error_bodies_conform(client, mini_engine):
    error, validation = _response_schema("Error"), _response_schema("ValidationError")
    jsonschema.validate(client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"}).json(), error)         # 404
    jsonschema.validate(client.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-02"}).json(), error)
    jsonschema.validate(client.get("/analytics/departments/summary", params={"month": "bad"}).json(), validation)               # 422
    jsonschema.validate(client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 99}).json(), validation)
    jsonschema.validate(client.get("/analytics/departments/Eng/trend", params={"from": "2026-07-02", "to": "2026-07-01"}).json(), validation)
    jsonschema.validate(client.get("/admin/explain/department_trend").json(), validation)
    jsonschema.validate(client.get("/admin/explain/nope").json(), validation)


def test_explain_endpoint_path_enum_matches_the_contract():
    expected = SPEC["paths"]["/admin/explain/{endpoint}"]["get"]["parameters"][0]["schema"]["enum"]
    assert set(expected) == set(main.EXPLAIN_REQUIRED)
