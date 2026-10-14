"""Index initialisation control flow, against in-memory fakes (tests/fakes.py).
These prove create -> verify -> fail loudly. They do NOT prove MongoDB accepts the indexes:
that is tests/test_mongo_integration.py, which needs a real server."""
import logging

import pytest
from pymongo.errors import OperationFailure, PyMongoError

from app import main
from app.main import (
    REPAIR_RETRY_SECONDS,
    REQUIRED_INDEXES,
    IndexSetupError,
    ensure_indexes,
    initialize_indexes,
    verify_indexes,
)
from tests.fakes import FakeDB

EXPECTED = {   # name -> (collection, keys, unique): written out so a reviewer can read the design
    "uq_emp_code": ("employees", [("emp_code", 1)], True),
    "ix_department_joined_on": ("employees", [("department", 1), ("joined_on", 1)], False),
    "ix_joined_on": ("employees", [("joined_on", 1)], False),                      # added in Phase 4 (DECISIONS.md, answer 1)
    "uq_emp_code_date": ("attendance_logs", [("emp_code", 1), ("date", 1)], True),
    "ix_date_desc_emp_code": ("attendance_logs", [("date", -1), ("emp_code", 1)], False),
}


def test_required_index_definitions():
    assert {s.name: (s.collection, list(s.keys), s.unique) for s in REQUIRED_INDEXES} == EXPECTED


def test_creates_every_required_index_on_an_empty_database():
    d = FakeDB()
    ensure_indexes(d)
    for name, (coll, keys, unique) in EXPECTED.items():
        assert (keys, name, unique) in d[coll].create_calls


def test_is_idempotent_second_run_creates_nothing():
    d = FakeDB()
    ensure_indexes(d)
    before = {c: len(d[c].create_calls) for c in ("employees", "attendance_logs")}
    ensure_indexes(d)
    assert {c: len(d[c].create_calls) for c in before} == before


def test_accepts_an_equivalent_index_under_another_name():
    d = FakeDB()
    d["employees"].indexes.append({"v": 2, "key": {"emp_code": 1.0}, "name": "custom", "unique": True})
    ensure_indexes(d)
    assert all(call[1] != "uq_emp_code" for call in d["employees"].create_calls)


def test_same_keys_but_not_unique_is_an_error_and_nothing_is_dropped_or_created():
    d = FakeDB()
    d["employees"].indexes.append({"v": 2, "key": {"emp_code": 1}, "name": "emp_code_1"})
    with pytest.raises(IndexSetupError, match="unique"):
        ensure_indexes(d)
    assert d["employees"].create_calls == []


def test_create_failure_is_reported_with_the_index_name():
    d = FakeDB()
    d["employees"].fail_create = OperationFailure("boom")
    with pytest.raises(IndexSetupError, match="uq_emp_code"):
        ensure_indexes(d)


def test_index_missing_after_creation_is_detected():
    d = FakeDB()
    d["attendance_logs"].silent_create = True   # server "accepted" it but it is not listed
    with pytest.raises(IndexSetupError, match="verification failed after creation"):
        ensure_indexes(d)


def test_unreachable_database_is_an_index_error_not_a_crash():
    with pytest.raises(IndexSetupError):
        ensure_indexes(FakeDB(down=True))


def test_initialize_reports_failure_logs_it_and_recovers_on_retry(monkeypatch, caplog, clock):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    d["employees"].fail_create = OperationFailure("boom")
    with caplog.at_level(logging.ERROR, logger="attendance"):
        assert initialize_indexes() is False
    assert "NOT ready" in caplog.text
    assert main.index_state.ready is False and "uq_emp_code" in main.index_state.error

    d["employees"].fail_create = None
    clock.advance(REPAIR_RETRY_SECONDS)   # a failed repair is retried only after the cooldown
    assert initialize_indexes() is True
    assert main.index_state.ready is True and main.index_state.error is None


def test_initialize_creates_nothing_when_everything_is_already_there(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    assert initialize_indexes() is True
    calls = sum(len(d[c].create_calls) for c in ("employees", "attendance_logs"))
    assert initialize_indexes() is True
    assert sum(len(d[c].create_calls) for c in ("employees", "attendance_logs")) == calls


# --------------------------------------------------------------------------- #
# Readiness is re-verified every time (the original `if index_state.ready: return True` is gone)
# --------------------------------------------------------------------------- #
def _create_calls(d):
    return sum(len(d[c].create_calls) for c in ("employees", "attendance_logs"))


def test_verify_indexes_raises_when_the_server_cannot_be_read():
    with pytest.raises(PyMongoError):
        verify_indexes(FakeDB(down=True))


def test_verify_indexes_is_read_only_and_names_every_missing_index():
    d = FakeDB()
    problems = verify_indexes(d)
    assert len(problems) == len(REQUIRED_INDEXES) and all(p.startswith("missing index") for p in problems)
    assert _create_calls(d) == 0                       # looking never creates anything
    ensure_indexes(d)
    assert verify_indexes(d) == []


def test_ready_flag_is_not_trusted_a_deleted_index_is_noticed(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    assert initialize_indexes() is True and main.index_state.ready is True
    d["employees"].drop_index("uq_emp_code")           # deleted AFTER readiness was reported
    d["employees"].fail_create = OperationFailure("cannot rebuild")
    assert initialize_indexes() is False               # re-verified: not ready
    assert main.index_state.ready is False and "uq_emp_code" in main.index_state.error


def test_healthy_state_is_verified_each_time_but_never_recreated(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    initialize_indexes()
    calls = _create_calls(d)
    for _ in range(5):
        assert initialize_indexes() is True
    assert _create_calls(d) == calls                   # five more checks, zero more creates


def test_a_deleted_index_is_recreated_once_and_only_reported_ready_after_verification(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    initialize_indexes()
    d["attendance_logs"].drop_index("uq_emp_code_date")
    before = len(d["attendance_logs"].create_calls)
    assert initialize_indexes() is True
    assert len(d["attendance_logs"].create_calls) == before + 1   # exactly the missing one
    assert verify_indexes(d) == []                                # and it is really there


def test_a_create_that_silently_does_nothing_is_not_ready(monkeypatch):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    initialize_indexes()
    d["employees"].drop_index("uq_emp_code")
    d["employees"].silent_create = True                # create_index "succeeds"; index never appears
    assert initialize_indexes() is False


def test_failed_repair_is_not_retried_until_the_cooldown_has_passed(monkeypatch, clock):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    d["employees"].fail_create = OperationFailure("boom")
    assert initialize_indexes() is False
    attempts = len(d["employees"].create_calls)
    clock.advance(REPAIR_RETRY_SECONDS - 1)
    assert initialize_indexes() is False
    assert len(d["employees"].create_calls) == attempts           # still cooling down: no new attempt
    clock.advance(1)
    assert initialize_indexes() is False
    assert len(d["employees"].create_calls) == attempts + 1       # cooldown over: one more attempt


def test_cooldown_never_makes_an_unhealthy_database_look_ready(monkeypatch, clock):
    d = FakeDB()
    monkeypatch.setattr(main, "db", d)
    d["employees"].fail_create = OperationFailure("boom")
    initialize_indexes()
    d["employees"].fail_create = None
    assert initialize_indexes() is False               # inside the cooldown, index still missing
    clock.advance(REPAIR_RETRY_SECONDS)
    assert initialize_indexes() is True


# --------------------------------------------------------------------------- #
# Incompatible options: same key pattern is NOT enough
# --------------------------------------------------------------------------- #
def _raw(name, key, **options):
    return {"v": 2, "key": key, "name": name, **options}


@pytest.mark.parametrize(
    "options",
    [
        {"partialFilterExpression": {"emp_code": {"$exists": True}}},
        {"sparse": True},
        {"collation": {"locale": "en", "strength": 2}},
        {"expireAfterSeconds": 0},     # 0 is falsy but is a real TTL option: must still be rejected
        {"hidden": True},
    ],
    ids=["partial", "sparse", "collation", "ttl-zero", "hidden"],
)
def test_unique_index_with_incompatible_options_is_rejected_not_replaced(options):
    d = FakeDB()
    bad = _raw("emp_code_special", {"emp_code": 1}, unique=True, **options)
    d["employees"].indexes.append(bad)
    snapshot = [dict(i) for i in d["employees"].indexes]

    assert any("incompatible" in p for p in verify_indexes(d))
    with pytest.raises(IndexSetupError, match="not replacing"):
        ensure_indexes(d)
    assert d["employees"].create_calls == []           # nothing created for that key pattern
    assert d["employees"].indexes == snapshot          # nothing dropped, nothing modified


def test_explicit_false_options_are_plain_and_accepted():
    d = FakeDB()
    d["employees"].indexes.append(_raw("custom", {"emp_code": 1}, unique=True, sparse=False, hidden=False))
    ensure_indexes(d)
    assert all(call[1] != "uq_emp_code" for call in d["employees"].create_calls)


def test_non_unique_index_that_should_be_unique_is_rejected_by_verify():
    d = FakeDB()
    d["employees"].indexes.append(_raw("emp_code_1", {"emp_code": 1}))
    assert any("incompatible" in p for p in verify_indexes(d))


def test_unique_index_where_a_plain_one_is_required_is_rejected():
    d = FakeDB()   # (department, joined_on) must NOT be unique: two people can join the same day
    d["employees"].indexes.append(_raw("dept_joined_unique", {"department": 1, "joined_on": 1}, unique=True))
    with pytest.raises(IndexSetupError, match="not replacing"):
        ensure_indexes(d)


def test_key_order_and_direction_matter():
    d = FakeDB()
    d["attendance_logs"].indexes.append(_raw("wrong_dir", {"date": 1, "emp_code": 1}))      # date ASC, not DESC
    d["employees"].indexes.append(_raw("reversed", {"joined_on": 1, "department": 1}))      # fields reversed
    problems = verify_indexes(d)
    assert any("missing index: attendance_logs.ix_date_desc_emp_code" in p for p in problems)
    assert any("missing index: employees.ix_department_joined_on" in p for p in problems)


def test_a_compatible_index_is_enough_even_if_an_odd_one_sits_beside_it():
    d = FakeDB()
    d["employees"].indexes.append(_raw("odd", {"emp_code": 1}, unique=True, sparse=True))
    d["employees"].indexes.append(_raw("plain", {"emp_code": 1}, unique=True))
    d["attendance_logs"].indexes.append(_raw("a", {"emp_code": 1, "date": 1}, unique=True))
    d["attendance_logs"].indexes.append(_raw("b", {"date": -1, "emp_code": 1}))
    d["employees"].indexes.append(_raw("c", {"department": 1, "joined_on": 1}))
    d["employees"].indexes.append(_raw("d", {"joined_on": 1}))                     # Phase 4
    assert verify_indexes(d) == []


def test_unreachable_database_is_not_ready_and_no_repair_is_attempted(monkeypatch):
    d = FakeDB(down=True)
    monkeypatch.setattr(main, "db", d)
    assert initialize_indexes() is False
    assert "could not read indexes" in main.index_state.error
    assert _create_calls(d) == 0
    assert main.index_state.last_failed_repair is None   # an outage is not a failed repair: no cooldown
