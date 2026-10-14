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

Last verified run: GitHub Actions, commit `84d4c42`, Python 3.12.15, MongoDB 7.0.45 service container:
**1038 mock-based tests passed and 62 real-MongoDB integration tests passed, 0 failed, 0 skipped.**
MongoDB 6.0 job (same commit): succeeded; `pytest -m mongo` gave 62 passed, 0 skipped.
`python -m pytest -m "not mongo"` runs only the 1038 mock-based tests; `python -m pytest -m mongo` selects only the
62 real-MongoDB tests.

* The 1038 mock-based tests use **`mongomock`** (an in-memory imitation, not MongoDB). `mongomock` cannot run `$lookup`
  with a sub-pipeline, `$setWindowFields`, `$densify`, `$round` or `explain`, so those pipelines are executed in the
  mock tests by `tests/mini_agg.py`, a small interpreter I wrote, and compared with `tests/oracle.py`. That only shows
  the pipelines match my reading of MongoDB's documentation.
* The **62 real-MongoDB integration tests** (`tests/test_mongo_integration.py`: 34; `tests/test_mongo_integration_analytics.py`: 28)
  run against a real server and use a throw-away database, never `MONGO_DB`. Locally they skip unless `MONGO_URI` points at
  MongoDB 6.0+. They cover unique-index enforcement, concurrent writes, the four analytics pipelines compared with the
  oracle, the semantic assumptions (`$rank` ties, `$densify` bounds, window frames) and explain plans (index scans, no collection scans).

### Continuous integration

`.github/workflows/tests.yml` runs the full suite on every push against `mongo:6.0` and `mongo:7.0` service containers.
The real-MongoDB step fails if any of the 62 integration tests is skipped. Both the MongoDB 7.0 job and the MongoDB 6.0
job passed.

## Notes

* Errors are `{"detail": ...}`; validation failures are 422 and unhandled database errors are 503.
* Instants are epoch milliseconds, truncated to whole seconds. Dates are `YYYY-MM-DD`, months `YYYY-MM`.
* Duplicates are decided by unique indexes (409), not by a read first. Punch-out and PATCH are compare-and-set
  updates with up to 5 retries, then 409.
* Analytics rounding is half-up in integer arithmetic, not `$round`. `openapi.yaml` is authoritative for behaviour.

## Known limitations

* The local development machine had no MongoDB, so real-server behaviour was first verified through GitHub Actions
  (see above). An earlier partial run against FerretDB, which is not MongoDB, is kept in `REVIEW.md` section G for history only.
* `total` in list responses comes from a separate `count_documents` (an unfiltered count scans the collection).
* The punch-out lookup sorts one employee's records in memory; no extra index was added for it.
* Each write adds one `listIndexes` call to confirm the unique index exists.

## How this was built

I used AI assistance (Claude) while building and testing this project. I reviewed the code and can explain it:
the business rules, the unique-index approach to simultaneous requests, the correction history, and the aggregation pipelines.

## What is complete / missing

Complete: all endpoints in `openapi.yaml`. Missing: nothing known, other than the limitations listed above.
