"""Small helpers shared by the Phase 2 endpoint tests."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app import main


def ist_ms(year, month, day, hour=0, minute=0, second=0, millis=0) -> int:
    """Epoch milliseconds of a wall-clock time in IST (the time zone the business rules use)."""
    return main.datetime_to_ms(datetime(year, month, day, hour, minute, second, millis * 1000, tzinfo=main.IST))


def employee_payload(**overrides) -> dict:
    """A valid POST /employees body (shift defaults to 09:30-18:30 by omission)."""
    body = {"emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
            "department": "Engineering", "joined_on": "2026-01-05"}
    body.update(overrides)
    return body


def run_together(task, n):
    """Run task(i) in n threads that are released at the same instant; returns the n results.
    Each task should use its own TestClient. This is how 'simultaneous requests' are simulated."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return task(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


# --------------------------------------------------------------------------- #
# A spy around a collection: records HOW the app queries (sort / skip / limit), and can forbid calls
# --------------------------------------------------------------------------- #
class RecordingCursor:
    def __init__(self, inner, calls):
        self._inner, self._calls = inner, calls

    def _record(self, name, *args):
        self._calls.append((name, *args))
        self._inner = getattr(self._inner, name)(*args)
        return self

    def sort(self, *args):
        return self._record("sort", *args)

    def skip(self, *args):
        return self._record("skip", *args)

    def limit(self, *args):
        return self._record("limit", *args)

    def __iter__(self):
        return iter(self._inner)


class RecordingCollection:
    """Passes everything through to the real (mock) collection. `calls` lists what was called, in
    order, e.g. ("count_documents", filter), ("find", filter), ("sort", spec), ("skip", n), ("limit", n).
    Methods named in `forbid` raise AssertionError if the application calls them."""

    def __init__(self, inner, forbid=()):
        self._inner, self._forbid, self.calls = inner, set(forbid), []

    def __getattr__(self, name):
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            if name in self._forbid:
                raise AssertionError(f"{name}() must not be called here")
            self.calls.append((name, *args[:1]))  # the filter / document is enough for the assertions
            result = target(*args, **kwargs)
            return RecordingCursor(result, self.calls) if name == "find" else result

        return call

    def names(self):
        return [c[0] for c in self.calls]


class SpyDB:
    """Stands in for main.db: the named collections are wrapped in spies, the rest pass through."""

    def __init__(self, real):
        self._real, self._spies = real, {}

    def spy_on(self, collection_name, forbid=()):
        spy = RecordingCollection(self._real[collection_name], forbid)
        self._spies[collection_name] = spy
        return spy

    def __getitem__(self, name):          # db["employees"]
        return self._spies[name] if name in self._spies else self._real[name]

    def __getattr__(self, name):          # db.employees
        if name in self._spies:
            return self._spies[name]
        return getattr(self._real, name)


# --------------------------------------------------------------------------- #
# Phase 3 helpers
# --------------------------------------------------------------------------- #
def add_record(db, emp_code, date, punch_in_ms, punch_out_ms=None, status="PRESENT", **extra):
    """Insert an attendance record the way the API would have stored it (derived fields via main.derive_fields,
    using the employee's real shift from `db`). `extra` overrides any stored field, e.g. history=[...]."""
    emp = db.employees.find_one({"emp_code": emp_code})
    p_in = None if punch_in_ms is None else main.ms_to_datetime(punch_in_ms)
    p_out = None if punch_out_ms is None else main.ms_to_datetime(punch_out_ms)
    doc = {"emp_code": emp_code, "date": date, "status": status, "punch_in": p_in, "punch_out": p_out,
           **main.derive_fields(status, p_in, p_out, main.parse_date(date), emp["shift_start"], emp["shift_end"]),
           "history": []}
    doc.update(extra)
    db.attendance_logs.insert_one(doc)
    return doc


def stored(db, emp_code, date):
    """The stored document without its _id (for before / after comparisons)."""
    return db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})


class RacingCollection:
    """DETERMINISTIC stand-in for a concurrent writer. Passes everything through to the real (mock) collection;
    right AFTER the application's find_one returns, it calls rival(collection) once (or after every read when
    every_time=True). The application then holds a snapshot that is already stale, exactly like a request that
    lost a race between its read and its write.

    This tests OUR compare-and-set and retry logic. It says nothing about whether MongoDB applies a single
    update atomically; that is only covered by tests/test_mongo_integration.py (needs a real server)."""

    def __init__(self, inner, rival, every_time=False):
        self._inner, self._rival, self._every_time = inner, rival, every_time
        self.reads = 0

    def find_one(self, *args, **kwargs):
        result = self._inner.find_one(*args, **kwargs)
        self.reads += 1
        if self._every_time or self.reads == 1:
            self._rival(self._inner)
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def racing_db(real_db, monkeypatch, rival, every_time=False):
    """Install a db whose attendance_logs is a RacingCollection; returns the collection wrapper."""
    spy = SpyDB(real_db)
    racing = RacingCollection(real_db.attendance_logs, rival, every_time)
    spy._spies["attendance_logs"] = racing
    monkeypatch.setattr(main, "db", spy)
    return racing
