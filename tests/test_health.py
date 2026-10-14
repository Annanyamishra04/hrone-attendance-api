"""/health: 200 only when Mongo answers AND all required indexes are verified; otherwise 503.
Uses fakes for the database; the real-server version is in test_mongo_integration.py."""
import logging

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import OperationFailure

from app import main
from app.main import REPAIR_RETRY_SECONDS, verify_indexes
from tests.fakes import FakeDB


@pytest.fixture
def make_client(monkeypatch):
    def make(fake_db):
        monkeypatch.setattr(main, "db", fake_db)
        return TestClient(main.app)   # used as a context manager so the lifespan (startup) runs
    return make


def test_ok_when_mongo_answers_and_indexes_are_verified(make_client):
    with make_client(FakeDB()) as c:
        r = c.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}   # no extra fields
    assert main.index_state.ready is True


def test_503_when_mongo_is_down_and_the_app_still_starts(make_client):
    with make_client(FakeDB(down=True)) as c:
        r = c.get("/health")
    assert r.status_code == 503
    assert r.json() == {"detail": "MongoDB is not reachable"}      # {"detail": <string>} per openapi Error
    assert main.index_state.ready is False


def test_503_when_indexes_cannot_be_created_then_200_once_they_can(make_client, clock):
    d = FakeDB()
    d["employees"].fail_create = OperationFailure("nope")
    with make_client(d) as c:
        r = c.get("/health")
        assert r.status_code == 503 and "indexes" in r.json()["detail"]
        assert main.index_state.ready is False

        d["employees"].fail_create = None            # operator fixes the problem; no restart needed
        clock.advance(REPAIR_RETRY_SECONDS)          # the failed repair is retried after the cooldown
        assert c.get("/health").status_code == 200
        assert main.index_state.ready is True


def test_never_ok_when_ping_works_but_an_index_is_silently_missing(make_client):
    d = FakeDB()
    d["attendance_logs"].silent_create = True
    with make_client(d) as c:
        assert c.get("/health").status_code == 503


def test_startup_logs_that_it_is_not_ready(make_client, caplog):
    d = FakeDB()
    d["employees"].fail_create = OperationFailure("nope")
    with caplog.at_level(logging.ERROR, logger="attendance"):
        with make_client(d):
            pass
    assert "Starting WITHOUT verified indexes" in caplog.text


# --------------------------------------------------------------------------- #
# Regression: readiness must be re-verified on every request, never remembered
# --------------------------------------------------------------------------- #
def test_REGRESSION_deleting_a_required_index_after_ready_cannot_leave_health_200(make_client):
    d = FakeDB()
    with make_client(d) as c:
        assert c.get("/health").status_code == 200          # ready is now remembered as True ...
        assert main.index_state.ready is True

        d["attendance_logs"].drop_index("uq_emp_code_date")  # ... then an operator deletes an index
        d["attendance_logs"].fail_create = OperationFailure("cannot rebuild")   # and it cannot be rebuilt
        r = c.get("/health")
        assert r.status_code == 503                          # the old `if ready: return True` gave 200 here
        assert "indexes" in r.json()["detail"]
        assert main.index_state.ready is False
        assert "uq_emp_code_date" in main.index_state.error


def test_REGRESSION_deleted_index_is_never_reported_ready_while_it_is_missing(make_client):
    d = FakeDB()
    with make_client(d) as c:
        assert c.get("/health").status_code == 200
        d["employees"].drop_index("uq_emp_code")
        d["employees"].silent_create = True                  # the rebuild "succeeds" but nothing appears
        assert c.get("/health").status_code == 503
        assert any("uq_emp_code" in p for p in verify_indexes(d))   # and it really is still missing


def test_a_deleted_index_that_can_be_rebuilt_gives_200_only_once_it_exists_again(make_client):
    d = FakeDB()
    with make_client(d) as c:
        assert c.get("/health").status_code == 200
        d["employees"].drop_index("uq_emp_code")
        assert c.get("/health").status_code == 200           # rebuilt by this very request ...
        assert verify_indexes(d) == []                       # ... and verified before answering


def test_health_stays_503_during_the_cooldown_even_if_the_cause_was_fixed(make_client, clock):
    d = FakeDB()
    with make_client(d) as c:
        assert c.get("/health").status_code == 200
        d["employees"].drop_index("uq_emp_code")
        d["employees"].fail_create = OperationFailure("cannot rebuild")
        assert c.get("/health").status_code == 503
        d["employees"].fail_create = None
        assert c.get("/health").status_code == 503           # index still missing, retry not yet due
        clock.advance(REPAIR_RETRY_SECONDS)
        assert c.get("/health").status_code == 200


def test_health_503_when_an_existing_index_has_the_right_keys_but_incompatible_options(make_client):
    d = FakeDB()
    partial = {"v": 2, "key": {"emp_code": 1}, "name": "emp_code_partial", "unique": True,
               "partialFilterExpression": {"emp_code": {"$exists": True}}}
    d["employees"].indexes.append(partial)
    with make_client(d) as c:
        r = c.get("/health")
        assert r.status_code == 503 and "indexes" in r.json()["detail"]
        assert "incompatible" in main.index_state.error and "emp_code_partial" in main.index_state.error
        assert c.get("/health").status_code == 503           # and it stays that way: never accepted
    assert partial in d["employees"].indexes                  # not dropped, not replaced
    assert not any(call[0] == [("emp_code", 1)] for call in d["employees"].create_calls)
