"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

Contract: openapi.yaml (authoritative). Stored data: DATA_MODEL.md.
"""
import calendar
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from time import monotonic
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, StringConstraints, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.exceptions import HTTPException as StarletteHTTPException

load_dotenv()  # override=False (default): real environment variables take priority

logger = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# Configuration and connection
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset, no DST

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
SERVER_SELECTION_TIMEOUT_MS = 5000  # so /health answers 503 quickly instead of hanging

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones, so
# datetimes read from MongoDB can be compared with the aware datetimes this code creates.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS)
db = client[MONGO_DB]


# --------------------------------------------------------------------------- #
# Instants (epoch milliseconds <-> aware UTC datetimes) and calendar strings
# --------------------------------------------------------------------------- #
EPOCH_MS_MIN = 100_000_000_000  # below this the value "looks like seconds" -> 422
EPOCH_MS_MAX = 4_102_444_800_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# For request models: a JSON integer only. StrictInt rejects floats, strings and booleans
# (a lax `int` would accept "1783312500000", 1783312500000.0 and true).
EpochMillis = Annotated[StrictInt, Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


def as_utc(dt: datetime) -> datetime:
    """Aware UTC datetime. A naive datetime is taken to be UTC (that is how MongoDB stores it)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_whole_seconds(dt: datetime) -> datetime:
    """R1: instants are truncated (not rounded) to whole seconds before any calculation."""
    return as_utc(dt).replace(microsecond=0)


def ms_to_datetime(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    if isinstance(ms, bool) or not isinstance(ms, int) or not EPOCH_MS_MIN <= ms <= EPOCH_MS_MAX:
        raise ValueError(f"not a valid epoch-milliseconds value: {ms!r}")
    return _EPOCH + timedelta(seconds=ms // 1000)


def datetime_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds using integer arithmetic (dt.timestamp() * 1000 can be off by one)."""
    delta = as_utc(dt) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def optional_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else datetime_to_ms(dt)


def now_utc() -> datetime:
    return to_whole_seconds(datetime.now(UTC))


_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MONTH_RE = re.compile(r"[0-9]{4}-[0-9]{2}")
_HHMM_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
# [0-9] and fullmatch on purpose: \d also matches non-ASCII digits, and date.fromisoformat()
# accepts compact forms such as "20260706" on Python 3.11+.


def parse_date(value: str) -> date:
    """Strict YYYY-MM-DD calendar date. Raises ValueError (e.g. for 2026-02-30 or 2026-7-6)."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"expected YYYY-MM-DD, got {value!r}")
    return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))


def parse_month(value: str) -> tuple[int, int]:
    """Strict YYYY-MM -> (year, month). Raises ValueError."""
    if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
        raise ValueError(f"expected YYYY-MM, got {value!r}")
    year, month = int(value[0:4]), int(value[5:7])
    date(year, month, 1)  # rejects month 00 / 13 and year 0000
    return year, month


def month_bounds(value: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31'), the inclusive string range used to match `date`."""
    year, month = parse_month(value)
    last = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"


def hhmm_to_seconds(value: str) -> int:
    """Strict 'HH:MM' (24-hour) -> seconds since midnight."""
    if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
        raise ValueError(f"expected HH:MM, got {value!r}")
    return int(value[0:2]) * 3600 + int(value[3:5]) * 60


# --------------------------------------------------------------------------- #
# Business rules R1 - R5 (pure functions, no database access)
# --------------------------------------------------------------------------- #
PRESENCE_STATUSES = frozenset({"PRESENT", "WFH", "ON_DUTY"})  # R6
NON_PRESENCE_STATUSES = frozenset({"ABSENT", "LEAVE"})
ALL_STATUSES = PRESENCE_STATUSES | NON_PRESENCE_STATUSES

GRACE_SECONDS = 10 * 60          # R2
OVERTIME_MIN_SECONDS = 30 * 60   # R3
HALF_DAY_BELOW_HUNDREDTHS = 450  # R5: rounded work_hours < 4.50
MAX_SHIFT_SECONDS = 24 * 3600    # punch-out / correction window: punch_out may be at most 24 h after punch_in


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_to_seconds(shift_end) <= hhmm_to_seconds(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date of punch_in; for an overnight shift, a punch-in whose IST time is
    earlier than shift_end belongs to the shift that started the previous day."""
    local = to_whole_seconds(punch_in).astimezone(IST)
    day = local.date()
    seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
    if is_overnight(shift_start, shift_end) and seconds_of_day < hhmm_to_seconds(shift_end):
        day -= timedelta(days=1)
    return day


def shift_start_at(att_date: date, shift_start: str) -> datetime:
    secs = hhmm_to_seconds(shift_start)
    return datetime.combine(att_date, time(secs // 3600, (secs % 3600) // 60), tzinfo=IST)


def shift_end_at(att_date: date, shift_start: str, shift_end: str) -> datetime:
    """The shift end as an instant; for an overnight shift it falls on the next calendar day."""
    secs = hhmm_to_seconds(shift_end)
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    return datetime.combine(end_day, time(secs // 3600, (secs % 3600) // 60), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift_start; then whole minutes (floor)
    since shift_start (not since the end of the grace period)."""
    elapsed = int((to_whole_seconds(punch_in) - shift_start_at(att_date, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def _work_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    """(punch_out - punch_in) in hours, rounded half-up to 2 decimals, as an exact integer count of
    hundredths. Integer arithmetic avoids float rounding (round(0.125, 2) == 0.12 in Python)."""
    seconds = int((to_whole_seconds(punch_out) - to_whole_seconds(punch_in)).total_seconds())
    if seconds <= 0:
        raise ValueError("punch_out must be after punch_in")
    return (seconds * 100 + 1800) // 3600  # floor(x + 1/2), x = seconds * 100 / 3600


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4."""
    return _work_hundredths(punch_in, punch_out) / 100


def compute_half_day(punch_in: datetime, punch_out: datetime) -> bool:
    """R5: decided on the ROUNDED work hours, so 4.495 h (rounds to 4.50) is not a half day."""
    return _work_hundredths(punch_in, punch_out) < HALF_DAY_BELOW_HUNDREDTHS


def compute_overtime_minutes(punch_out: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes (floor) from shift_end to punch_out, counted only when that is >= 30."""
    end = shift_end_at(att_date, shift_start, shift_end)
    elapsed = int((to_whole_seconds(punch_out) - end).total_seconds())
    return elapsed // 60 if elapsed >= OVERTIME_MIN_SECONDS else 0


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    att_date: date,
    shift_start: str,
    shift_end: str,
) -> dict:
    """The four stored derived fields, per the table in DATA_MODEL.md. This is the single place
    punch-in, punch-out and corrections will use, so the rules cannot drift apart."""
    if status in NON_PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    if punch_in is None:
        raise ValueError("a presence status requires punch_in")
    late = compute_late_minutes(punch_in, att_date, shift_start)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    return {
        "work_hours": compute_work_hours(punch_in, punch_out),
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, att_date, shift_start, shift_end),
        "half_day": compute_half_day(punch_in, punch_out),
    }


# --------------------------------------------------------------------------- #
# Corrections (PATCH): the rules as a pure function, so they are testable without a database
# --------------------------------------------------------------------------- #
class RegularizationRejected(Exception):
    """A correction that breaks a rule. The endpoint turns it into a 422 that points at `field`."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field, self.message = field, message


# The seven fields a correction can change and that `history[].changes` can name, in a stable order.
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
CLIENT_EDITABLE_FIELDS = ("status", "punch_in", "punch_out")  # the rest are derived


def stored_state(doc: dict) -> dict:
    """The seven CHANGE_FIELDS as stored, with the DATA_MODEL.md defaults for fields a legacy record omits."""
    return {
        "status": doc["status"],
        "punch_in": None if doc.get("punch_in") is None else as_utc(doc["punch_in"]),
        "punch_out": None if doc.get("punch_out") is None else as_utc(doc["punch_out"]),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
    }


def plan_regularization(
    record: dict,
    shift_start: str,
    shift_end: str,
    status: Optional[str] = None,
    punch_in: Optional[datetime] = None,
    punch_out: Optional[datetime] = None,
) -> tuple:
    """Apply a correction to the stored `record` IN MEMORY and return (new_state, changes).

    `status`, `punch_in`, `punch_out` are what the client supplied (None = omitted = unchanged); instants must
    already be whole-second aware datetimes. new_state holds all seven CHANGE_FIELDS after the correction, with the
    four derived ones recomputed by derive_fields (R2-R5) from the FINAL times. `changes` is
    {field: {"from": old, "to": new}} for the fields whose value actually differs, in CHANGE_FIELDS order.
    Raises RegularizationRejected (-> 422) for:
      * punch times supplied while the final status is ABSENT / LEAVE (those statuses clear both times);
      * a presence status without a punch_in (supplied or stored);
      * a supplied punch_in that is not on the record's attendance date (R1);
      * a final punch_out that is not after punch_in, or is more than 24 h after it;
      * a request that changes none of status / punch_in / punch_out (a no-op).
    The no-op test looks at the three client-editable fields only. If they all stay the same the request changes
    nothing, even if a seeded record's stored derived values would differ from a recomputation: a correction
    does not quietly "heal" a record the client did not actually change."""
    old = stored_state(record)
    att_date = parse_date(record["date"])
    new_status = old["status"] if status is None else status

    if new_status in NON_PRESENCE_STATUSES:
        for field, supplied in (("punch_in", punch_in), ("punch_out", punch_out)):
            if supplied is not None:
                raise RegularizationRejected(
                    field, f"a {new_status} record has no punch times; to give it one, also send status "
                           f"PRESENT, WFH or ON_DUTY")
        new_in = new_out = None
    else:
        new_in = old["punch_in"] if punch_in is None else punch_in
        new_out = old["punch_out"] if punch_out is None else punch_out
        if new_in is None:
            raise RegularizationRejected("punch_in", f"status {new_status} requires a punch_in")
        # "punch_in must stay on the record's attendance date": checked when the client supplies one.
        if punch_in is not None and attendance_date(new_in, shift_start, shift_end) != att_date:
            raise RegularizationRejected(
                "punch_in", f"punch_in must fall on the record's attendance date {record['date']} (R1)")
        if new_out is not None:
            seconds = int((to_whole_seconds(new_out) - to_whole_seconds(new_in)).total_seconds())
            if seconds <= 0:
                raise RegularizationRejected("punch_out", "punch_out must be after punch_in")
            if seconds > MAX_SHIFT_SECONDS:
                raise RegularizationRejected("punch_out", "punch_out must be within 24 hours of punch_in")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(new_status, new_in, new_out, att_date, shift_start, shift_end)}
    changes = {f: {"from": old[f], "to": new[f]} for f in CHANGE_FIELDS if old[f] != new[f]}
    if not any(f in changes for f in CLIENT_EDITABLE_FIELDS):
        raise RegularizationRejected("body", "the request changes nothing")
    return new, changes


# --------------------------------------------------------------------------- #
# Required indexes: created if missing at startup, and re-verified on every readiness check
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IndexSpec:
    collection: str
    name: str
    keys: tuple  # ((field, direction), ...)
    unique: bool = False


REQUIRED_INDEXES = (
    # uniqueness of emp_code (409 on duplicate create); every lookup by emp_code
    IndexSpec("employees", "uq_emp_code", (("emp_code", ASCENDING),), unique=True),
    # department filter + R9 headcount (joined_on <= end of period)
    IndexSpec("employees", "ix_department_joined_on", (("department", ASCENDING), ("joined_on", ASCENDING))),
    # Phase 4: R9 headcount WITHOUT a department filter (department summary for all departments). The compound index
    # above cannot serve a predicate on joined_on alone (it is not the leading key), so that pipeline would start with
    # a collection scan of `employees`. See DECISIONS.md, answer 1.
    IndexSpec("employees", "ix_joined_on", (("joined_on", ASCENDING),)),
    # at most one record per (emp_code, date): the punch-in race guard; also record lookups
    IndexSpec("attendance_logs", "uq_emp_code_date", (("emp_code", ASCENDING), ("date", ASCENDING)), unique=True),
    # GET /attendance sort (date desc, emp_code asc) and month-wide analytics scans by date
    IndexSpec("attendance_logs", "ix_date_desc_emp_code", (("date", DESCENDING), ("emp_code", ASCENDING))),
)


class IndexSetupError(RuntimeError):
    """A required index is missing, incompatible, or could not be created."""


# Keys of a listIndexes entry that describe a plain index. Anything else with a real value
# (partialFilterExpression, sparse, collation, expireAfterSeconds, hidden, ...) changes which
# documents are indexed or how they compare, so such an index is NOT equivalent to a full one.
_PLAIN_INFO_KEYS = frozenset({"v", "key", "name", "ns", "unique", "background"})


def _key_pattern(info: dict) -> tuple:
    """((field, direction), ...) in order. 1.0 -> 1 so a server that reports floats still matches."""
    return tuple((f, int(d) if isinstance(d, (int, float)) else d) for f, d in info["key"].items())


def _extra_options(info: dict) -> list:
    """Names of options that make an index differ from a plain full index."""
    return sorted(k for k, v in info.items() if k not in _PLAIN_INFO_KEYS and v is not False and v is not None)


def _describe(info: dict) -> str:
    extras = _extra_options(info)
    return f"{info.get('name')} keys={dict(_key_pattern(info))} unique={bool(info.get('unique', False))}" + (
        f" options={extras}" if extras else ""
    )


def _is_equivalent(info: dict, spec: IndexSpec) -> bool:
    """Same key pattern (fields, order, directions), same uniqueness, no extra options.
    The name is deliberately not compared: an equivalent index under another name is fine."""
    return (
        _key_pattern(info) == spec.keys
        and bool(info.get("unique", False)) == spec.unique
        and not _extra_options(info)
    )


def verify_indexes(database) -> list:
    """READ-ONLY check. Returns a list of problems; an empty list means every required index is
    present and equivalent. Never creates, changes or drops anything.
    Raises PyMongoError if the server cannot be read (the caller decides what that means), so an
    unreachable server fails after ONE timeout instead of one per index."""
    problems = []
    for spec in REQUIRED_INDEXES:
        label = f"{spec.collection}.{spec.name} {dict(spec.keys)} unique={spec.unique}"
        # list_indexes() on a collection that does not exist yet yields nothing, not an error
        infos = list(database[spec.collection].list_indexes())
        if any(_is_equivalent(info, spec) for info in infos):
            continue
        same_keys = [_describe(i) for i in infos if _key_pattern(i) == spec.keys]
        if same_keys:
            problems.append(f"incompatible index on the same keys ({'; '.join(same_keys)}), required: {label}")
        else:
            problems.append(f"missing index: {label}")
    return problems


def ensure_indexes(database) -> None:
    """Create each required index that is MISSING, then re-read everything with verify_indexes().
    Raises IndexSetupError on any problem.
    Never drops or replaces an index: if an index with the same keys but different options
    (not unique, partial, sparse, ...) already exists, that is an error for a human to resolve."""
    for spec in REQUIRED_INDEXES:
        label = f"{spec.collection}.{spec.name} {dict(spec.keys)} unique={spec.unique}"
        try:
            infos = list(database[spec.collection].list_indexes())
            if any(_is_equivalent(info, spec) for info in infos):
                continue  # already there: nothing to create
            clashing = [_describe(i) for i in infos if _key_pattern(i) == spec.keys]
            if clashing:
                raise IndexSetupError(
                    f"index on the same keys exists with different options ({'; '.join(clashing)}); "
                    f"not replacing it, required: {label}"
                )
            database[spec.collection].create_index(list(spec.keys), name=spec.name, unique=spec.unique)
        except PyMongoError as exc:
            raise IndexSetupError(f"could not create/verify index {label}: {exc}") from exc
    try:
        problems = verify_indexes(database)  # trust the server's answer, not the create_index return
    except PyMongoError as exc:
        raise IndexSetupError(f"could not verify indexes after creation: {exc}") from exc
    if problems:
        raise IndexSetupError("verification failed after creation: " + "; ".join(problems))


# --------------------------------------------------------------------------- #
# Readiness: every check re-reads the server; nothing is cached as "ready"
# --------------------------------------------------------------------------- #
REPAIR_RETRY_SECONDS = 30  # after a FAILED repair, wait this long before trying to create again


class IndexState:
    """What the most recent check found. Informational only: readiness is never answered from here."""

    ready: bool = False
    error: Optional[str] = None
    last_failed_repair: Optional[float] = None  # monotonic clock reading


index_state = IndexState()
_index_lock = threading.Lock()
_clock = monotonic  # indirection so tests can move time without sleeping


def initialize_indexes() -> bool:
    """Check the required indexes against the server RIGHT NOW; True only if all are verified.

    1. verify_indexes() (read-only listIndexes calls). All good -> True, nothing is created.
    2. Something missing -> try ensure_indexes() to create it, unless a repair failed less than
       REPAIR_RETRY_SECONDS ago (so a persistent failure, e.g. duplicate rows blocking a unique
       index, does not trigger a collection scan on every health poll).
    3. The result is False whenever verification failed; the reason is logged and kept in
       index_state.error.
    Called at startup and by every /health request."""
    with _index_lock:
        try:
            problems = verify_indexes(db)
        except PyMongoError as exc:  # server unreachable: nothing can be created either, so no repair
            problems = [f"could not read indexes: {exc}"]
        else:
            last = index_state.last_failed_repair
            if problems and (last is None or _clock() - last >= REPAIR_RETRY_SECONDS):
                try:
                    ensure_indexes(db)  # ends with its own fresh verification
                    problems, index_state.last_failed_repair = [], None
                except IndexSetupError as exc:
                    problems, index_state.last_failed_repair = [str(exc)], _clock()
        index_state.ready, index_state.error = not problems, ("; ".join(problems) or None)
        if problems:
            logger.error("Required MongoDB indexes are NOT ready: %s", index_state.error)
        return not problems


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The app starts either way so that /health can say *why* it is not ready (503) instead of the
    # process dying. It never reports ready until every index has been verified.
    if not initialize_indexes():
        logger.error("Starting WITHOUT verified indexes; /health returns 503 until they exist")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Request models and query-parameter types (validation failures are FastAPI's default 422)
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]  # the only ones punch-in accepts

PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX = 20, 100  # R10


def _check_calendar_date(value: str) -> str:
    parse_date(value)  # strict YYYY-MM-DD; a ValueError here becomes a 422
    return value


CalendarDate = Annotated[str, AfterValidator(_check_calendar_date)]


class EmployeeIn(BaseModel):
    """POST /employees body. The patterns are the ones in openapi.yaml (ASCII digits only).
    Unknown keys, such as a client-sent created_at, are ignored."""

    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_real_date(cls, value: str) -> str:
        return _check_calendar_date(value)

    @model_validator(mode="after")
    def _shift_start_differs_from_shift_end(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    """POST /attendance/punch-in body. Derived fields a client might send are ignored."""

    emp_code: str
    # Omitted -> None -> "use the current time". Pydantic does not validate a default, but an
    # explicit `null` is validated against the integer type and rejected (the schema is not nullable).
    punched_at: EpochMillis = Field(default=None)
    status: PresenceStatus = "PRESENT"

    @field_validator("emp_code")
    @classmethod
    def _emp_code_can_be_stored_and_queried(cls, value: str) -> str:
        # A JSON string such as "\ud800" (a lone surrogate) is accepted by Python but cannot be encoded as
        # BSON, so using it in a query would raise inside PyMongo. UnicodeEncodeError is a ValueError -> 422.
        value.encode("utf-8")
        return value


def _check_storable_text(value: str) -> str:
    """A JSON string such as "\\ud800" (a lone surrogate) is accepted by Python but cannot be encoded as BSON,
    so storing or querying it would raise inside PyMongo (a 500). UnicodeEncodeError is a ValueError -> 422."""
    value.encode("utf-8")
    return value


StorableStr = Annotated[str, AfterValidator(_check_storable_text)]


class PunchOutIn(BaseModel):
    """POST /attendance/punch-out body. Omitted `punched_at` means now. Unknown keys are ignored."""

    emp_code: StorableStr
    punched_at: EpochMillis = Field(default=None)  # same "omitted -> None, explicit null -> 422" idea as PunchInIn


class RegularizeIn(BaseModel):
    """PATCH /attendance/{emp_code}/{date} body. Only these fields can change a record. Derived fields (and any
    other unknown key) are ignored, as the contract says. An explicit `null` is a 422: the contract's schema is
    not nullable, and clearing punch times is done by setting status ABSENT / LEAVE."""

    status: Status = Field(default=None)
    punch_in: EpochMillis = Field(default=None)
    punch_out: EpochMillis = Field(default=None)
    reason: StorableStr = Field(min_length=5, max_length=200)
    regularized_by: StorableStr = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _absent_or_leave_clears_the_punch_times(self):
        # Needs only the body, so it is a plain 422 before any database access.
        if self.status in NON_PRESENCE_STATUSES and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError(f"status {self.status} clears both punch times; do not send punch_in or punch_out with it")
        return self


# --------------------------------------------------------------------------- #
# Stored document -> API JSON (the only place datetimes become epoch milliseconds)
# --------------------------------------------------------------------------- #
def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": optional_ms(doc.get("created_at")),
    }


def _history_value_to_api(value):
    """history[].changes values: BSON dates (punch_in / punch_out) become epoch ms, the rest is as stored."""
    return datetime_to_ms(value) if isinstance(value, datetime) else value


def record_to_api(doc: dict) -> dict:
    """An attendance_logs document as the AttendanceRecord JSON. Seeded legacy documents may omit
    late_minutes, overtime_minutes, half_day and history: missing means 0 / 0 / false / empty."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": optional_ms(doc.get("punch_in")),
        "punch_out": optional_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [
            {
                "at": optional_ms(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": {
                    field: {key: _history_value_to_api(value) for key, value in change.items()}
                    for field, change in (entry.get("changes") or {}).items()
                },
            }
            for entry in (doc.get("history") or [])
        ],
    }


# --------------------------------------------------------------------------- #
# Pagination, done by the database (R10)
# --------------------------------------------------------------------------- #
def fetch_page(collection, query: dict, sort: list, page: int, page_size: int) -> tuple:
    """(documents of this page, total matching the filter). count + skip + limit run in MongoDB,
    nothing is loaded and sliced in Python. The sort must be a total order, otherwise pages can
    overlap or skip rows.
    When the page starts at or after the end we do not query at all. That is correct (an empty page)
    and it also means an absurd `page` never reaches MongoDB as an out-of-range skip."""
    total = collection.count_documents(query)
    skip = (page - 1) * page_size
    if skip >= total:
        return [], total
    cursor = collection.find(query, {"_id": 0}).sort(sort).skip(skip).limit(page_size)
    return list(cursor), total


def require_unique_index(collection_name: str) -> None:
    """Refuse to write unless the unique index behind this endpoint's 409 really exists right now.
    Without it two simultaneous requests could both insert and both get 201. One listIndexes call, never
    creates or changes anything. A missing / incompatible index (see _is_equivalent) means the service is
    not in a state to accept this write, so: 503 and nothing is stored."""
    spec = next(i for i in REQUIRED_INDEXES if i.collection == collection_name and i.unique)
    if not any(_is_equivalent(info, spec) for info in db[collection_name].list_indexes()):
        logger.error("Unique index %s.%s is missing or invalid; refusing the write", spec.collection, spec.name)
        raise HTTPException(503, "required unique index is missing; the write was refused")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """200 only if MongoDB answers a ping AND every required index is verified on THIS request
    (verification is a fresh read of the server's index list, never a remembered result)."""
    try:
        db.client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not reachable")
    if not initialize_indexes():  # re-verifies; creates only what is missing (see its docstring)
        raise HTTPException(503, "Required MongoDB indexes are missing, invalid or could not be created")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Endpoints: employees and attendance (Phase 2)
# --------------------------------------------------------------------------- #
@app.exception_handler(RequestValidationError)
async def validation_failed(_request: Request, exc: RequestValidationError):
    """422 in FastAPI's usual shape, {"detail": [{"type", "loc", "msg"}, ...]}, without the echoed `input`.
    FastAPI's default handler copies each offending value into the response. A body such as
    {"punched_at": NaN}, a deeply nested array, or a string containing the escape \\ud800 (a lone surrogate) cannot be serialized back to JSON,
    so a validation failure turned into a 500."""
    errors = [{"type": e["type"], "loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """Unchanged FastAPI behaviour for every status except 400. The contract defines no 400: an invalid body is
    a 422. The only 400 this application can produce is FastAPI's "error parsing the body", which happens when
    the bytes are not valid UTF-8 (truncated or non-JSON text is already a 422), so that case becomes a 422 too."""
    if exc.status_code == 400:
        return JSONResponse(status_code=422, content={
            "detail": [{"type": "json_invalid", "loc": ["body"], "msg": "Request body is not valid JSON"}]})
    return await http_exception_handler(request, exc)


@app.exception_handler(PyMongoError)
async def database_unavailable(_request: Request, exc: PyMongoError):
    """A database failure that an endpoint did not handle itself (e.g. MongoDB went away) is a 503,
    not a 500. DuplicateKeyError never gets here: the endpoints turn it into a 409."""
    logger.error("Database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


@app.post("/employees", status_code=201, responses={409: {"description": "emp_code already exists"}})
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    require_unique_index("employees")
    try:
        # No "does it exist?" check first: that is a race. The unique index on emp_code decides,
        # atomically, and the loser of two simultaneous requests gets DuplicateKeyError.
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
):
    query = {} if department is None else {"department": department}  # exact, case-sensitive
    docs, total = fetch_page(db.employees, query, [("emp_code", ASCENDING)], page, page_size)
    return {"items": [employee_to_api(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201,
          responses={404: {"description": "unknown employee"}, 409: {"description": "already has a record for that date"}})
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")

    # R1: whole seconds first, then the IST attendance date (overnight shifts included).
    punched_in = now_utc() if body.punched_at is None else ms_to_datetime(body.punched_at)
    att_date = attendance_date(punched_in, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": punched_in,
        "punch_out": None,
        # work_hours None, late_minutes from R2, overtime 0, half_day False (the DATA_MODEL.md table)
        **derive_fields(body.status, punched_in, None, att_date, emp["shift_start"], emp["shift_end"]),
        "history": [],
    }
    require_unique_index("attendance_logs")
    try:
        # Same idea as create_employee: the unique index on (emp_code, date) arbitrates the race.
        # It also rejects the day if a seeded ABSENT / LEAVE record already exists for it.
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_to_api(doc)


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


def check_date_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    """date_from > date_to is a 422 (ISO dates compare as text). Shared by GET /attendance and its explain."""
    if date_from is not None and date_to is not None and date_from > date_to:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "date_from"), "msg": "date_from must not be after date_to",
              "input": date_from}]
        )


def attendance_list_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str],
                          status: Optional[str]) -> dict:
    """The GET /attendance filter. Used by the endpoint AND by /admin/explain/attendance_list, so the two cannot differ."""
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from  # inclusive
        if date_to is not None:
            query["date"]["$lte"] = date_to  # inclusive
    if status is not None:
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[CalendarDate] = None,
    date_to: Optional[CalendarDate] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
):
    check_date_range(date_from, date_to)
    query = attendance_list_query(emp_code, date_from, date_to, status)
    # (date desc, emp_code asc) is unique per record, so the order is total and pages are stable.
    # It is also exactly the key order of the ix_date_desc_emp_code index.
    docs, total = fetch_page(db.attendance_logs, query, ATTENDANCE_SORT, page, page_size)
    return {"items": [record_to_api(d) for d in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Endpoints: punch-out and regularization (Phase 3)
#
# Both update ONE existing document with a compare-and-set: the update filter repeats the values the decision was
# based on (status, punch_in, punch_out and, for corrections, how many history entries there were). If another
# request changed the record after we read it, the filter matches nothing, the write is skipped, and we read the
# record again and redo the decision (at most MAX_WRITE_ATTEMPTS times, then 409). The write itself is a single
# update_one, so MongoDB applies the $set and the history $push together or not at all, which is what keeps two
# concurrent corrections from losing each other's audit entry. Success is `matched_count == 1`. The response is built
# from the document that was written (the filter guarantees the stored record was exactly the one we read). It does
# not use find_one_and_update(return_document=AFTER): mongomock implements that by re-running the filter AFTER the
# update, and our filter mentions the very fields the update changes, so it would wrongly report a lost race.
# --------------------------------------------------------------------------- #
MAX_WRITE_ATTEMPTS = 5


def unprocessable(loc: tuple, message: str):
    """Raise a 422 in the same shape as every other validation failure (see validation_failed)."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": message, "input": None}])


@app.post("/attendance/punch-out",
          responses={404: {"description": "unknown employee, or no punch-in found"},
                     409: {"description": "that record is already punched out"},
                     422: {"description": "punched_at is not after punch_in, or is more than 24 hours after it"}})
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    punched_out = now_utc() if body.punched_at is None else ms_to_datetime(body.punched_at)  # whole seconds (R1)

    for _ in range(MAX_WRITE_ATTEMPTS):
        # The employee's MOST RECENT presence record whose punch_in is at or before punched_at. Ordering by punch_in
        # (not by `date`) is what makes this work across midnight. `punch_in < punched_out + 1 s` is "the
        # truncated punch_in <= punched_at" for a stored value that carries milliseconds. Only this one record is
        # considered: if it is already closed the answer is 409, we do NOT fall back to an older open record.
        record = db.attendance_logs.find_one(
            {"emp_code": body.emp_code,
             "status": {"$in": sorted(PRESENCE_STATUSES)},
             "punch_in": {"$lt": punched_out + timedelta(seconds=1)}},
            {"_id": 0},
            sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
        )
        if record is None:
            raise HTTPException(404, "no punch-in found for this employee at or before punched_at")
        if record.get("punch_out") is not None:
            raise HTTPException(409, "already punched out")

        punched_in = to_whole_seconds(record["punch_in"])
        seconds = int((punched_out - punched_in).total_seconds())
        if seconds <= 0:
            unprocessable(("body", "punched_at"), "punched_at must be after punch_in")
        if seconds > MAX_SHIFT_SECONDS:
            unprocessable(("body", "punched_at"), "punched_at must be within 24 hours of punch_in")

        # R3-R5 from the record's own attendance date (for an overnight shift that is the day it STARTED).
        # late_minutes is deliberately not touched: it belongs to punch-in and to corrections.
        derived = derive_fields(record["status"], punched_in, punched_out, parse_date(record["date"]),
                                emp["shift_start"], emp["shift_end"])
        written = {"punch_out": punched_out, "work_hours": derived["work_hours"],
                   "overtime_minutes": derived["overtime_minutes"], "half_day": derived["half_day"]}
        result = db.attendance_logs.update_one(
            # `punch_out: None` matches a null or missing field: this is what lets exactly one of two simultaneous
            # punch-outs win. status and punch_in pin the values the calculation used (a correction could have
            # changed them since we read the record).
            {"emp_code": record["emp_code"], "date": record["date"], "status": record["status"],
             "punch_in": record["punch_in"], "punch_out": None},
            {"$set": written},
        )
        if result.matched_count == 1:
            return record_to_api({**record, **written})
        # Nothing matched: the record changed after we read it. Read it again and decide again.
    raise HTTPException(409, "the record kept changing; try again")


def _history_guard(record: dict) -> dict:
    """The update filter's view of `history`. History only ever grows by one entry per successful correction, so
    its length says whether another correction got in first. Missing / null (a legacy record) is its own state."""
    history = record.get("history")
    return {"history": {"$size": len(history)}} if isinstance(history, list) else {"history": None}


@app.patch("/attendance/{emp_code}/{date}",  # the path parameter must be called `date`; the date class is not used here
           responses={404: {"description": "unknown employee, or no record for that date"},
                      409: {"description": "the record kept being changed by other requests"},
                      422: {"description": "invalid correction, or a correction that changes nothing"}})
def regularize_attendance(emp_code: StorableStr, date: CalendarDate, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    supplied = {
        "status": body.status,
        "punch_in": None if body.punch_in is None else ms_to_datetime(body.punch_in),     # whole seconds (R1)
        "punch_out": None if body.punch_out is None else ms_to_datetime(body.punch_out),
    }

    for _ in range(MAX_WRITE_ATTEMPTS):
        record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})
        if record is None:
            raise HTTPException(404, "no attendance record for that employee and date")
        try:
            new, changes = plan_regularization(record, emp["shift_start"], emp["shift_end"], **supplied)
        except RegularizationRejected as rejected:
            unprocessable(("body",) if rejected.field == "body" else ("body", rejected.field), rejected.message)

        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        history = record.get("history")
        written = {**new}  # status, punch_in, punch_out and the four recomputed fields, all written together
        update = {"$set": written}
        if isinstance(history, list):
            update["$push"] = {"history": entry}          # append, never overwrite
            new_history = [*history, entry]
        else:
            written["history"] = new_history = [entry]    # legacy record without a history array: start one
        result = db.attendance_logs.update_one(
            # Compare-and-set on everything the decision above was based on: if any of it changed in the meantime
            # (another correction, or a punch-out), nothing matches and nothing is written.
            {"emp_code": emp_code, "date": date, "status": record["status"],
             "punch_in": record.get("punch_in"), "punch_out": record.get("punch_out"), **_history_guard(record)},
            update,
        )
        if result.matched_count == 1:
            return record_to_api({**record, **new, "history": new_history})
    raise HTTPException(409, "the record kept being changed by other requests; try again")


# --------------------------------------------------------------------------- #
# Phase 4: analytics (MongoDB aggregation pipelines) and explain
#
# Conventions shared by the four analytics endpoints (see openapi.yaml and README.md):
#   * A month / range is matched on the string `date` field with inclusive bounds ("2026-07-01" .. "2026-07-31"),
#     so the (emp_code, date) and (date, emp_code) indexes serve it. Dates are IST calendar strings: no time zone
#     conversion is involved.
#   * Analytics read the STORED derived values (late_minutes, overtime_minutes, half_day, work_hours); nothing is
#     recomputed from punch times (DATA_MODEL.md). A missing late_minutes / overtime_minutes / half_day is 0 / 0 / false.
#   * Presence is counted in HALF-DAY UNITS (integers): a present Mon-Fri record is 2 units, a half day 1 unit, and
#     present_days = units / 2. Integer sums are exact, so the rounding below never depends on float addition order.
#   * Rounding is half-up (R8) and done with integer arithmetic (half_up_div). The pipelines return integers; MongoDB's
#     own $round is not used for reported numbers because it rounds ties to even.
#   * The pipelines are built by builder functions, and /admin/explain calls the SAME builders.
# --------------------------------------------------------------------------- #
MonthStr = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                     AfterValidator(lambda value: (parse_month(value), value)[1])]  # also rejects year 0000
LEADERBOARD_LIMIT_DEFAULT, LEADERBOARD_LIMIT_MAX = 10, 50
TREND_MAX_DAYS = 92
MOVING_WINDOW_ROWS = 7  # the current row and up to 6 before it


def half_up_div(numerator: int, denominator: int) -> int:
    """numerator / denominator rounded half-up to a whole number, for integers with denominator > 0.
    floor(n/d + 1/2) == (2n + d) // 2d. Python floor division is exact, so there is no float rounding to worry about."""
    return (2 * numerator + denominator) // (2 * denominator)


def count_working_days(year: int, month: int, joined_on: str) -> int:
    """R7: Monday-Friday days of the month, counted from joined_on for a mid-month joiner (0 if the employee joins
    after the month). No holiday calendar. Pure calendar arithmetic over at most 31 dates, no stored data involved."""
    last = calendar.monthrange(year, month)[1]
    joined = parse_date(joined_on)
    first_day = 1 if (joined.year, joined.month) < (year, month) else joined.day
    if (joined.year, joined.month) > (year, month):
        return 0
    return sum(1 for day in range(first_day, last + 1) if date(year, month, day).weekday() < 5)


def weekend_dates(year: int, month: int) -> list:
    """The Saturdays and Sundays of the month as 'YYYY-MM-DD' strings (passed to the pipeline as a literal list)."""
    last = calendar.monthrange(year, month)[1]
    return [f"{year:04d}-{month:02d}-{day:02d}" for day in range(1, last + 1) if date(year, month, day).weekday() >= 5]


def run_aggregate(collection_name: str, pipeline: list) -> list:
    return list(db[collection_name].aggregate(pipeline))


# ---- expression fragments (MongoDB aggregation expressions, evaluated per attendance record) ---- #
def _is_late():
    return {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}


def _present_units(weekend: Optional[list]):
    """2 for a present record, 1 for a present half day, 0 otherwise. With `weekend` given, records on those dates
    count 0 (R7: present_days only counts Mon-Fri records). Present = PRESENT / WFH / ON_DUTY (R6)."""
    present = {"$in": ["$status", sorted(PRESENCE_STATUSES)]}
    if weekend is not None:
        present = {"$and": [present, {"$eq": [{"$in": ["$date", weekend]}, False]}]}  # ($eq .. False, not $not: see tests)
    return {"$cond": [present, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}


def _month_match(start: str, end: str) -> dict:
    return {"date": {"$gte": start, "$lte": end}}


# ---- employee monthly ---- #
def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    year, mon = parse_month(month)
    start, end = month_bounds(month)
    return [
        # served by the unique (emp_code, date) index: equality on emp_code, range on date
        {"$match": {"emp_code": emp_code, **_month_match(start, end)}},
        {"$group": {
            "_id": None,
            "present_units": {"$sum": _present_units(weekend_dates(year, mon))},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [_is_late(), 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [_is_late(), "$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly",
         responses={404: {"description": "unknown employee"}})
def employee_monthly(emp_code: StorableStr, month: MonthStr):
    year, mon = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rows = run_aggregate("attendance_logs", employee_monthly_pipeline(emp_code, month))
    agg = rows[0] if rows else {}   # an employee with no records in the month: all zeros
    units = int(agg.get("present_units") or 0)
    working_days = count_working_days(year, mon, emp["joined_on"])
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": units / 2,
        "leave_days": int(agg.get("leave_days") or 0),
        "late_count": int(agg.get("late_count") or 0),
        "total_late_minutes": int(agg.get("total_late_minutes") or 0),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes") or 0),
        # percent = (units / 2) / working_days * 100 = units * 50 / working_days; 2 decimals half-up (R8)
        "attendance_pct": None if working_days == 0 else half_up_div(units * 5000, working_days) / 100,
    }


# ---- department summary ---- #
def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    year, mon = parse_month(month)
    start, end = month_bounds(month)
    is_present = {"$in": ["$status", sorted(PRESENCE_STATUSES)]}
    has_hours = {"$and": [is_present, {"$isNumber": "$work_hours"}]}
    employees_match = {"joined_on": {"$lte": end}}  # R9: joined on or before the last day of the month
    if department is not None:
        employees_match["department"] = department
    return [
        # employees first, so people with no logs still count; IXSCAN on (department, joined_on) or (joined_on)
        {"$match": employees_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            # the sub-pipeline's date range is combined with the emp_code equality: (emp_code, date) index
            "pipeline": [
                {"$match": _month_match(start, end)},
                {"$group": {
                    "_id": None,
                    "present_units": {"$sum": _present_units(weekend_dates(year, mon))},
                    # work_hours is stored with 2 decimals; hundredths are summed as integers so the mean is exact
                    "work_hundredths": {"$sum": {"$cond": [has_hours, {"$toLong": {"$round": [
                        {"$multiply": ["$work_hours", 100]}, 0]}}, 0]}},
                    "work_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [_is_late(), 1, 0]}},
                    "total_late_minutes": {"$sum": {"$cond": [_is_late(), "$late_minutes", 0]}},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "agg",
        }},
        {"$project": {"_id": 0, "department": 1, "a": {"$arrayElemAt": ["$agg", 0]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{field: {"$sum": {"$ifNull": [f"$a.{field}", 0]}} for field in (
                "present_units", "work_hundredths", "work_n", "late_count", "total_late_minutes",
                "leave_count", "on_duty_count")},
        }},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthStr, department: Optional[StorableStr] = None):
    rows = run_aggregate("employees", department_summary_pipeline(month, department))
    items = []
    for row in rows:
        n = int(row["work_n"])
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": int(row["present_units"]) / 2,
            # mean over RECORDS (not an average of averages): sum of hundredths / n, half-up to 2 decimals (R8)
            "avg_work_hours": None if n == 0 else half_up_div(int(row["work_hundredths"]), n) / 100,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}


# ---- late leaderboard ---- #
def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline = [
        # IXSCAN on (date desc, emp_code) for the month; only late records matter
        {"$match": {**_month_match(start, end), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        # unique emp_code index; $unwind drops logs whose employee does not exist
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # ranking happens inside the department only
    pipeline += [
        # $rank is standard competition ranking (1, 2, 2, 4). It must run BEFORE the limit.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the RANK, so ties at the cutoff are all kept
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthStr,
    limit: int = Query(LEADERBOARD_LIMIT_DEFAULT, ge=1, le=LEADERBOARD_LIMIT_MAX),
    department: Optional[StorableStr] = None,
):
    rows = run_aggregate("attendance_logs", late_leaderboard_pipeline(month, limit, department))
    return {"month": month, "items": [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
         "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])} for r in rows]}


# ---- department trend ---- #
def check_trend_range(frm: str, to: str) -> tuple:
    """Validated (from, to) as dates. 422 if to < from or the range holds more than 92 calendar days (inclusive count)."""
    start, end = parse_date(frm), parse_date(to)
    if end < start:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "to"), "msg": "to must not be before from", "input": None}])
    if (end - start).days + 1 > TREND_MAX_DAYS:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "to"),
              "msg": f"the range must not exceed {TREND_MAX_DAYS} days", "input": None}])
    if end >= date.max:  # the pipeline needs the day after `to` as an exclusive upper bound
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "to"), "msg": "to is out of the supported range", "input": None}])
    return start, end


def department_trend_pipeline(department: str, start: date, end: date) -> list:
    frm, to = start.isoformat(), end.isoformat()
    lower = datetime(start.year, start.month, start.day, tzinfo=UTC)
    upper = datetime.combine(end + timedelta(days=1), time(0), tzinfo=UTC)  # $densify's upper bound is exclusive
    window_all_before = {"documents": ["unbounded", "current"]}
    window_7 = {"documents": [-(MOVING_WINDOW_ROWS - 1), 0]}
    return [
        # every employee of the department (IXSCAN on (department, joined_on)): each one yields at least one row below,
        # so the pipeline never ends up empty for a department that exists
        {"$match": {"department": department}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": _month_match(frm, to)},
                {"$project": {"_id": 0, "date": 1, "units": _present_units(None),
                              "late": {"$cond": [_is_late(), 1, 0]}}},
            ],
            "as": "logs",
        }},
        # one "joined" row (headcount +1 on the day the employee counts; before `from` it counts from `from`; an
        # employee joining after `to` adds 0) plus one row per attendance record
        {"$project": {"_id": 0, "rows": {"$concatArrays": [
            [{"date": {"$cond": [{"$lt": ["$joined_on", frm]}, frm, {"$cond": [{"$gt": ["$joined_on", to]}, frm, "$joined_on"]}]},
              "head": {"$cond": [{"$lte": ["$joined_on", to]}, 1, 0]}, "units": 0, "late": 0}],
            {"$map": {"input": "$logs", "as": "l",
                      "in": {"date": "$$l.date", "head": 0, "units": "$$l.units", "late": "$$l.late"}}},
        ]}}},
        {"$unwind": "$rows"},
        {"$group": {"_id": "$rows.date", "head": {"$sum": "$rows.head"}, "units": {"$sum": "$rows.units"},
                    "late": {"$sum": "$rows.late"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC",
                                                  "onError": None, "onNull": None}}}},
        {"$match": {"d": {"$type": "date"}}},
        # exactly one document per calendar day in [from, to], weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        # headcount on a day = employees with joined_on <= that day (R9): running sum of the "joined" rows
        {"$setWindowFields": {"sortBy": {"d": 1},
                              "output": {"headcount": {"$sum": "$head", "window": window_all_before}}}},
        {"$addFields": {"is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                        "units": {"$ifNull": ["$units", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        # attendance_rate in units of 1e-4, half-up, integer arithmetic: floor((units * 10000 + h) / (2h))
        {"$addFields": {"rate_e4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$units", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        # moving average over the (rounded) rates of this row and up to 6 earlier rows INSIDE the range; $sum ignores nulls
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "win_sum": {"$sum": "$rate_e4", "window": window_7},
            "win_n": {"$sum": {"$cond": [{"$isNumber": "$rate_e4"}, 1, 0]}, "window": window_7}}}},
        {"$addFields": {"moving_e4": {"$cond": [
            {"$eq": ["$win_n", 0]}, None,
            {"$floor": {"$divide": [{"$add": [{"$multiply": [2, "$win_sum"]}, "$win_n"]},
                                    {"$multiply": [2, "$win_n"]}]}}]}}},
        {"$sort": {"d": 1}},
        {"$project": {"_id": 0, "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d", "timezone": "UTC"}},
                      "is_working_day": 1, "headcount": 1, "units": 1, "late": 1, "rate_e4": 1, "moving_e4": 1}},
    ]


def _e4(value) -> Optional[float]:
    """An integer count of 1e-4 units (what the pipeline returns) as the reported 4-decimal number."""
    return None if value is None else int(value) / 10000


@app.get("/analytics/departments/{department}/trend",
         responses={404: {"description": "unknown department (no employees)"}})
def department_trend(department: StorableStr,
                     from_: Annotated[CalendarDate, Query(alias="from")], to: CalendarDate):
    start, end = check_trend_range(from_, to)
    if db.employees.find_one({"department": department}, {"_id": 0, "emp_code": 1}) is None:
        raise HTTPException(404, "unknown department")
    rows = run_aggregate("employees", department_trend_pipeline(department, start, end))
    return {"department": department, "items": [
        {"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
         "present_count": int(r["units"]) / 2, "late_count": int(r["late"]),
         "attendance_rate": _e4(r["rate_e4"]), "moving_avg_7d": _e4(r["moving_e4"])} for r in rows]}


# --------------------------------------------------------------------------- #
# GET /admin/explain/{endpoint}: MongoDB's explain (executionStats) of the endpoint's main query, raw
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard",
                        "department_trend"]
EXPLAIN_REQUIRED = {  # query parameters that endpoint needs; a missing one is a 422
    "attendance_list": (),
    "employee_monthly": ("emp_code", "month"),
    "department_summary": ("month",),
    "late_leaderboard": ("month",),
    "department_trend": ("department", "from", "to"),
}


def explain_as_json(raw: dict) -> dict:
    """The server's explain document as JSON, nothing dropped. Relaxed extended JSON: 64-bit integers are plain numbers,
    dates / ObjectIds / NaN become {"$date": ...} / {"$oid": ...} / {"$numberDouble": "NaN"}, so the result is always strict JSON."""
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


def run_explain(command: dict) -> dict:
    return explain_as_json(db.command({"explain": command, "verbosity": "executionStats"}))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[StorableStr] = None,
    month: Optional[MonthStr] = None,
    department: Optional[StorableStr] = None,
    limit: int = Query(LEADERBOARD_LIMIT_DEFAULT, ge=1, le=LEADERBOARD_LIMIT_MAX),
    date_from: Optional[CalendarDate] = None,
    date_to: Optional[CalendarDate] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[CalendarDate], Query(alias="from")] = None,
    to: Optional[CalendarDate] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
):
    """Parameters that do not belong to the named endpoint are accepted and ignored (like any unknown query parameter)."""
    given = {"emp_code": emp_code, "month": month, "department": department, "from": from_, "to": to}
    missing = [name for name in EXPLAIN_REQUIRED[endpoint] if given[name] is None]
    if missing:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None} for name in missing])

    if endpoint == "attendance_list":
        check_date_range(date_from, date_to)
        collection = "attendance_logs"
        command = {"find": collection, "filter": attendance_list_query(emp_code, date_from, date_to, status),
                   "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size,
                   "projection": {"_id": 0}}  # exactly what fetch_page sends for the page query
    else:
        if endpoint == "employee_monthly":
            collection, pipeline = "attendance_logs", employee_monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            collection, pipeline = "employees", department_summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            collection, pipeline = "attendance_logs", late_leaderboard_pipeline(month, limit, department)
        else:
            start, end = check_trend_range(from_, to)
            collection, pipeline = "employees", department_trend_pipeline(department, start, end)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
