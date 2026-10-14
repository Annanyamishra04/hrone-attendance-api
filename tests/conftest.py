import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main
from tests.fixtures import ATTENDANCE_LOGS, EMPLOYEES
from tests.helpers import SpyDB


@pytest.fixture(scope="session")
def sample_employees():
    return {e["emp_code"]: e for e in EMPLOYEES}


@pytest.fixture(scope="session")
def sample_logs():
    return list(ATTENDANCE_LOGS)


def _reset_index_state():
    main.index_state.ready, main.index_state.error = False, None
    main.index_state.last_failed_repair = None


class FakeClock:
    """Stands in for main._clock so the repair-retry cooldown is tested without sleeping."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def reset_index_state(monkeypatch):
    """index_state is process-wide; every test starts from a clean slate with a fake clock."""
    _reset_index_state()
    monkeypatch.setattr(main, "_clock", FakeClock())
    yield
    _reset_index_state()


@pytest.fixture
def clock():
    return main._clock


# --------------------------------------------------------------------------- #
# Endpoint tests run against mongomock: an in-memory imitation of PyMongo. It lets us test OUR logic
# (validation, R1/R2, filters, sorting, pagination, duplicate-key handling) without a server.
# It is NOT MongoDB: query planning, index builds and true concurrency are only covered by
# tests/test_mongo_integration.py, which needs a real server.
# --------------------------------------------------------------------------- #
@pytest.fixture
def mdb(monkeypatch):
    """An empty mock database with the real required indexes (including the unique ones) created
    by the application's own ensure_indexes(), installed as main.db."""
    database = mongomock.MongoClient(tz_aware=True)["attendance_test"]
    main.ensure_indexes(database)
    monkeypatch.setattr(main, "db", database)
    return database


@pytest.fixture
def client(mdb):
    return TestClient(main.app)  # not used as a context manager: no lifespan, indexes come from mdb


@pytest.fixture
def employees(mdb, sample_employees):
    """The six sample employees and NO attendance records (free to punch in on any date)."""
    mdb.employees.insert_many([dict(e) for e in sample_employees.values()])
    return mdb


@pytest.fixture
def seeded(mdb, sample_employees, sample_logs):
    """The nine sample attendance documents and six sample employees."""
    mdb.employees.insert_many([dict(e) for e in sample_employees.values()])
    mdb.attendance_logs.insert_many([dict(l) for l in sample_logs])
    return mdb


@pytest.fixture
def spy_db(mdb, monkeypatch):
    spy = SpyDB(mdb)
    monkeypatch.setattr(main, "db", spy)
    return spy


# --------------------------------------------------------------------------- #
# Phase 4: mongomock cannot execute $lookup sub-pipelines, $setWindowFields or $densify. `mini_engine` routes the
# application's aggregations through tests/mini_agg.py (a test-only interpreter; NOT MongoDB) so the pipelines are
# executed and compared with tests/oracle.py. See that module's docstring for what this does and does not prove.
# --------------------------------------------------------------------------- #
@pytest.fixture
def mini_engine(mdb, monkeypatch):
    from tests import mini_agg
    monkeypatch.setattr(main, "run_aggregate", lambda collection, pipeline: mini_agg.aggregate(main.db, collection, pipeline))
    return mdb
