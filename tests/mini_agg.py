"""A TEST-ONLY interpreter for the handful of aggregation stages and operators the Phase 4 pipelines use.

Why it exists: mongomock cannot run $lookup with a sub-pipeline, $setWindowFields, $densify, $round, $dateFromString,
$isoDayOfWeek, $map, ... so without this the summary, leaderboard and trend pipelines would never execute in the default
test run, and a wrong field name or off-by-one would go unnoticed.

What it is NOT: MongoDB. It encodes the author's reading of the documented MongoDB semantics, so a misunderstanding of
MongoDB would be present in both the pipeline and this file. It supports only what app/main.py uses and raises
NotImplementedError for anything else (so a new operator in a pipeline cannot be silently ignored). The semantics it
relies on are listed in tests/test_mini_agg_assumptions.py, and every one of them is re-checked against a real server by
tests/test_mongo_integration_analytics.py (which skips when no server is reachable)."""
import copy
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_SENTINEL = object()


# ----------------------------------------------------------------------------------------------- #
# paths and expressions
# ----------------------------------------------------------------------------------------------- #
def get_path(doc, path):
    """Dotted path into nested dicts; a missing step gives None (this interpreter does not tell null from missing,
    which none of the pipelines depends on: $sum, $ifNull, $isNumber and $type all treat both alike)."""
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def ev(expr, doc, vars_=None):
    vars_ = vars_ or {}
    if isinstance(expr, str):
        if expr.startswith("$$"):
            name, _, rest = expr[2:].partition(".")
            base = vars_[name]
            return get_path(base, rest) if rest else base
        if expr.startswith("$"):
            return get_path(doc, expr[1:])
        return expr
    if isinstance(expr, list):
        return [ev(x, doc, vars_) for x in expr]
    if isinstance(expr, dict):
        if len(expr) == 1 and next(iter(expr)).startswith("$"):
            (op, arg), = expr.items()
            return _operator(op, arg, doc, vars_)
        return {k: ev(v, doc, vars_) for k, v in expr.items()}
    return expr


def _args(arg, doc, vars_):
    return [ev(a, doc, vars_) for a in arg] if isinstance(arg, list) else [ev(arg, doc, vars_)]


def _operator(op, arg, doc, vars_):
    if op == "$literal":
        return arg
    if op == "$cond":
        c, t, f = (arg["if"], arg["then"], arg["else"]) if isinstance(arg, dict) else arg
        return ev(t, doc, vars_) if ev(c, doc, vars_) else ev(f, doc, vars_)
    if op == "$and":
        return all(bool(ev(a, doc, vars_)) for a in arg)
    if op == "$ifNull":
        for a in arg[:-1]:
            v = ev(a, doc, vars_)
            if v is not None:
                return v
        return ev(arg[-1], doc, vars_)
    if op == "$map":
        out = []
        for item in ev(arg["input"], doc, vars_) or []:
            out.append(ev(arg["in"], doc, {**vars_, arg["as"]: item}))
        return out
    a = _args(arg, doc, vars_)
    if op == "$eq":
        x, y = a
        if is_number(x) and is_number(y):
            return x == y
        return type(x) is type(y) and x == y     # MongoDB: false != 0
    if op in ("$gt", "$gte", "$lt", "$lte"):
        x, y = a
        if x is None or y is None:
            return {"$gt": False, "$gte": x is y, "$lt": x is None and y is not None, "$lte": x is None}[op]
        return {"$gt": x > y, "$gte": x >= y, "$lt": x < y, "$lte": x <= y}[op]
    if op == "$in":
        return a[0] in a[1]
    if op == "$isNumber":
        return is_number(a[0])
    if op == "$toLong":
        return int(a[0])
    if op == "$round":
        return float(round(a[0], a[1])) if isinstance(a[0], float) else a[0]   # Python round: ties to even, as MongoDB
    if op == "$multiply":
        out = 1
        for x in a:
            out *= x
        return out
    if op == "$add":
        return sum(a)
    if op == "$divide":
        return a[0] / a[1]                         # float division, like MongoDB's $divide (a zero divisor is an error)
    if op == "$floor":
        return None if a[0] is None else float(int(a[0] // 1))   # MongoDB's $floor of a double is a double
    if op == "$arrayElemAt":
        arr, i = a
        return arr[i] if arr is not None and -len(arr) <= i < len(arr) else None
    if op == "$concatArrays":
        out = []
        for x in a:
            out += x
        return out
    if op == "$sum":                               # expression form: sums an array, ignores non-numbers
        vals = a[0] if len(a) == 1 and isinstance(a[0], list) else a
        return sum(v for v in vals if is_number(v))
    if op == "$dateFromString":
        spec = arg
        text = ev(spec["dateString"], doc, vars_)
        try:
            return datetime.strptime(text, spec["format"]).replace(tzinfo=UTC)
        except (TypeError, ValueError):
            if "onError" in spec:
                return ev(spec["onError"], doc, vars_)
            raise
    if op == "$isoDayOfWeek":
        return a[0].isoweekday()
    if op == "$dateToString":
        spec = arg
        return ev(spec["date"], doc, vars_).astimezone(UTC).strftime(spec["format"])
    raise NotImplementedError(f"expression operator {op}")


# ----------------------------------------------------------------------------------------------- #
# $match
# ----------------------------------------------------------------------------------------------- #
def _bson_type(v):
    if v is None:
        return "null"
    if isinstance(v, datetime):
        return "date"
    return type(v).__name__


def matches(doc, query):
    for field, cond in query.items():
        if field.startswith("$"):
            raise NotImplementedError(f"query operator {field}")
        value = get_path(doc, field)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$type":
                    ok = _bson_type(value) == operand
                elif value is None and op in ("$gt", "$gte", "$lt", "$lte"):
                    ok = False        # a missing / null field never satisfies a range comparison
                elif op == "$gt":
                    ok = value > operand
                elif op == "$gte":
                    ok = value >= operand
                elif op == "$lt":
                    ok = value < operand
                elif op == "$lte":
                    ok = value <= operand
                else:
                    raise NotImplementedError(f"query operator {op}")
                if not ok:
                    return False
        elif value != cond:
            return False
    return True


# ----------------------------------------------------------------------------------------------- #
# stages
# ----------------------------------------------------------------------------------------------- #
def run(pipeline, docs, db):
    """Execute `pipeline` over the list of documents `docs`. `db` resolves $lookup's `from` collection."""
    docs = [copy.deepcopy(d) for d in docs]
    for stage in pipeline:
        (name, spec), = stage.items()
        docs = _STAGES[name](spec, docs, db)
    return docs


def _match(spec, docs, db):
    return [d for d in docs if matches(d, spec)]


def _lookup(spec, docs, db):
    foreign = list(db[spec["from"]].find({}))
    out = []
    for d in docs:
        key = get_path(d, spec["localField"])
        found = [copy.deepcopy(f) for f in foreign if get_path(f, spec["foreignField"]) == key and key is not None]
        if "pipeline" in spec:
            found = run(spec["pipeline"], found, db)
        out.append({**d, spec["as"]: found})
    return out


def _project(spec, docs, db):
    out = []
    for d in docs:
        row = {} if spec.get("_id", 1) == 0 else ({"_id": d["_id"]} if "_id" in d else {})
        for field, how in spec.items():
            if field == "_id":
                if how not in (0, 1):
                    row["_id"] = ev(how, d)
                continue
            if how == 0:
                continue
            row[field] = get_path(d, field) if how == 1 else ev(how, d)
        out.append(row)
    return out


def _add_fields(spec, docs, db):
    out = []
    for d in docs:
        new = dict(d)
        for field, expr in spec.items():
            new[field] = ev(expr, d)
        out.append(new)
    return out


def _group(spec, docs, db):
    groups = {}
    for d in docs:
        key = ev(spec["_id"], d)
        groups.setdefault(repr(key), (key, []))[1].append(d)
    out = []
    for key, members in groups.values():
        row = {"_id": key}
        for field, acc in spec.items():
            if field == "_id":
                continue
            (op, expr), = acc.items()
            if op != "$sum":
                raise NotImplementedError(f"accumulator {op}")
            row[field] = sum(v for v in (ev(expr, m) for m in members) if is_number(v))
        out.append(row)
    return out


def _sort_key(value):
    return (0, 0) if value is None else (1, value)


def _sort(spec, docs, db):
    out = list(docs)
    for field, direction in reversed(list(spec.items())):    # stable multi-key sort, last key first
        out.sort(key=lambda d: _sort_key(get_path(d, field)), reverse=direction < 0)
    return out


def _unwind(spec, docs, db):
    path = spec[1:] if isinstance(spec, str) else spec["path"][1:]
    out = []
    for d in docs:
        arr = d.get(path)
        if isinstance(arr, list):
            out += [{**d, path: item} for item in arr]       # an empty or missing array drops the document
    return out


def _window_slice(rows, i, window):
    lo, hi = window["documents"]
    start = 0 if lo == "unbounded" else max(0, i + (0 if lo == "current" else lo))
    stop = len(rows) if hi == "unbounded" else min(len(rows), i + (0 if hi == "current" else hi) + 1)
    return rows[start:stop]


def _set_window_fields(spec, docs, db):
    (sort_field, direction), = spec["sortBy"].items()
    rows = _sort({sort_field: direction}, docs, db)
    for i, row in enumerate(rows):
        for field, out_spec in spec["output"].items():
            if "$rank" in out_spec:
                row[field] = 1 + sum(1 for other in rows if _before(other, row, sort_field, direction))
            elif "$sum" in out_spec:
                members = _window_slice(rows, i, out_spec["window"])
                row[field] = sum(v for v in (ev(out_spec["$sum"], m) for m in members) if is_number(v))
            else:
                raise NotImplementedError(f"window output {out_spec}")
    return rows


def _before(other, row, field, direction):
    a, b = get_path(other, field), get_path(row, field)
    return a > b if direction < 0 else a < b


def _densify(spec, docs, db):
    field, rng = spec["field"], spec["range"]
    if rng["unit"] != "day" or rng["step"] != 1:
        raise NotImplementedError("densify other than 1 day")
    lower, upper = rng["bounds"]                              # lower inclusive, upper EXCLUSIVE
    have = {d[field] for d in docs}
    out = list(docs)
    day = lower
    while day < upper:
        if day not in have:
            out.append({field: day})                           # a generated document holds ONLY the densified field
        day += timedelta(days=1)
    return _sort({field: 1}, out, db)


_STAGES = {"$match": _match, "$lookup": _lookup, "$project": _project, "$addFields": _add_fields, "$group": _group,
           "$sort": _sort, "$unwind": _unwind, "$setWindowFields": _set_window_fields, "$densify": _densify}


def aggregate(db, collection, pipeline):
    """Run `pipeline` on the documents of db[collection] (a mongomock database)."""
    return run(pipeline, list(db[collection].find({})), db)
