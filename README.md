# Employee Attendance & Analytics API

FastAPI + MongoDB. Application code is in `app/main.py`; the contract is `openapi.yaml`; stored documents are described in
`DATA_MODEL.md`; starter defects and fixes are in `REVIEW.md`; design answers are in `DECISIONS.md`.

All endpoints in the contract are implemented: health, employees, punch-in, punch-out, attendance list, attendance
correction (PATCH), the four analytics endpoints and `GET /admin/explain/{endpoint}`.

## Install and run

Requires Python 3.12 (the version used here) and **MongoDB 6.0 or newer** (the analytics use `$setWindowFields`,
`$densify` and `$lookup` with a sub-pipeline).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit if needed
uvicorn app.main:app --port 8000
```

Environment variables (a real environment variable overrides `.env`):

| Variable | Meaning | Default in `.env.example` |
|---|---|---|
| `MONGO_URI` | MongoDB connection string | `mongodb://localhost:27017` |
| `MONGO_DB` | Database name | `attendance_db` |

On startup the app creates its five required indexes if missing; it never drops or replaces one. No seed script or
sample data is shipped.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v -rs
```

Last recorded full run, 2026-10-12, on a clean checkout (Python 3.12.3, FastAPI 0.143.0, Pydantic 2.14.1, PyMongo 4.18.3,
mongomock 4.3.0): **1038 passed, 62 skipped, 0 failed.** `python -m pytest -m "not mongo"` runs only the 1038 mock-based tests;
`python -m pytest -m mongo` selects only the 62 real-MongoDB tests.

* The 1038 passing tests use **`mongomock`** (an in-memory imitation, not MongoDB). `mongomock` cannot run `$lookup` with a
  sub-pipeline, `$setWindowFields`, `$densify`, `$round` or `explain`, so the summary, leaderboard and trend pipelines are
  executed by `tests/mini_agg.py`, a small interpreter written by the same author, and compared with `tests/oracle.py`.
  They show the pipelines match that reading of MongoDB's documented behaviour, not that MongoDB agrees.
* **The 62 skipped tests are the real-MongoDB integration tests** (`tests/test_mongo_integration.py`: 34;
  `tests/test_mongo_integration_analytics.py`: 28). They skip themselves unless `MONGO_URI` points at a reachable
  MongoDB 6.0+, and use a throw-away database, never `MONGO_DB`. **They have not been run, so these are unverified:**
  unique-index enforcement and concurrent writes on a real server, that the four analytics pipelines execute and
  return what the oracle computes, the semantic assumptions (`$rank` ties, `$densify` bounds, window frames), and the
  explain plans (index scans, no collection scans).

### Running the real-MongoDB tests without a local MongoDB (GitHub Actions)

`.github/workflows/tests.yml` runs the full suite on every push against `mongo:6.0` and `mongo:7.0` service containers. Its
real-MongoDB step **fails if any of the 62 integration tests is skipped**, so a green run means they really ran. **This
workflow has been written and its YAML parsed, but it has not been run yet** (it needs a GitHub repository). Until it has
been run and is green, the MongoDB-dependent behaviour listed above is unverified.

## Notes

* Errors are `{"detail": ...}`; validation failures are 422 and unhandled database errors are 503.
* Instants are epoch milliseconds, truncated to whole seconds. Dates are `YYYY-MM-DD`, months `YYYY-MM`.
* Duplicates are decided by unique indexes (409), not by a read first. Punch-out and PATCH are compare-and-set
  updates with up to 5 retries, then 409.
* Analytics rounding is half-up in integer arithmetic, not `$round`. `openapi.yaml` is authoritative for behaviour.

## Known limitations

* **No real MongoDB was available where this was built** (the MongoDB download hosts are not reachable from that
  environment and it cannot build or run Docker images). A partial run of the 62 integration tests against *FerretDB 1.24*
  (a MongoDB-compatible server that is not MongoDB; it lacks `$setWindowFields`, `$densify`, `explain` execution stats and
  atomic `update_one`) is described in `REVIEW.md` section G. It is **not** evidence that MongoDB behaves as expected.

* Everything under "skipped" above is unverified on a real MongoDB; index plans and aggregation semantics are designed
  and mock-tested only.
* `total` in list responses comes from a separate `count_documents` (an unfiltered count scans the collection).
* The punch-out lookup sorts one employee's records in memory; no extra index was added for it.
* Each write adds one `listIndexes` call to confirm the unique index exists.
