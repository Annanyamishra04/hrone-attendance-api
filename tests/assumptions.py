"""Small aggregation cases that pin down the MongoDB semantics the Phase 4 pipelines rely on. Each case is
(name, input documents, pipeline, expected output documents).

They run in two places:
  * tests/test_mini_agg_assumptions.py runs them through the test-only interpreter (tests/mini_agg.py). That proves the
    interpreter implements what this table says, NOT that MongoDB does.
  * tests/test_mongo_integration_analytics.py runs the SAME table on a real MongoDB (skipped without one). Until that has
    been run, every row here is an unverified assumption about MongoDB."""
from datetime import datetime, timezone

UTC = timezone.utc


def day(d):
    return datetime(2026, 7, d, tzinfo=UTC)


CASES = [
    ("rank is standard competition ranking (1, 2, 2, 4)",
     [{"k": "a", "t": 50}, {"k": "b", "t": 30}, {"k": "c", "t": 30}, {"k": "d", "t": 10}],
     [{"$setWindowFields": {"sortBy": {"t": -1}, "output": {"r": {"$rank": {}}}}}, {"$sort": {"k": 1}},
      {"$project": {"_id": 0, "k": 1, "r": 1}}],
     [{"k": "a", "r": 1}, {"k": "b", "r": 2}, {"k": "c", "r": 2}, {"k": "d", "r": 4}]),

    ("$round rounds an exact tie to the EVEN neighbour (so reported numbers must not use it)",
     [{"x": 2.5}, {"x": 3.5}],
     [{"$sort": {"x": 1}}, {"$project": {"_id": 0, "r": {"$round": ["$x", 0]}}}],
     [{"r": 2}, {"r": 4}]),

    ("$toLong of $round repairs a float product just below an integer (1.15 * 100 = 114.99999999999999)",
     [{"h": 1.15}, {"h": 4.35}, {"h": 8.2}],
     [{"$sort": {"h": 1}}, {"$project": {"_id": 0, "n": {"$toLong": {"$round": [{"$multiply": ["$h", 100]}, 0]}}}}],
     [{"n": 115}, {"n": 435}, {"n": 820}]),

    ("$densify: lower bound inclusive, upper bound EXCLUSIVE, generated documents hold only the densified field",
     [{"d": day(2), "v": 7}],
     [{"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [day(1), day(4)]}}},
      {"$sort": {"d": 1}}, {"$project": {"_id": 0, "d": 1, "v": {"$ifNull": ["$v", 0]}}}],
     [{"d": day(1), "v": 0}, {"d": day(2), "v": 7}, {"d": day(3), "v": 0}]),

    ("$setWindowFields $sum over [unbounded, current] is a running total",
     [{"d": 1, "x": 1}, {"d": 2, "x": 2}, {"d": 3, "x": 3}],
     [{"$setWindowFields": {"sortBy": {"d": 1}, "output": {"run": {"$sum": "$x", "window": {"documents": ["unbounded", "current"]}}}}},
      {"$sort": {"d": 1}}, {"$project": {"_id": 0, "d": 1, "run": 1}}],
     [{"d": 1, "run": 1}, {"d": 2, "run": 3}, {"d": 3, "run": 6}]),

    ("$setWindowFields documents [-2, 0] covers the current row and at most 2 before it; nulls are ignored by $sum",
     [{"d": 1, "x": 10}, {"d": 2, "x": None}, {"d": 3, "x": 20}, {"d": 4, "x": 30}],
     [{"$setWindowFields": {"sortBy": {"d": 1}, "output": {
         "s": {"$sum": "$x", "window": {"documents": [-2, 0]}},
         "n": {"$sum": {"$cond": [{"$isNumber": "$x"}, 1, 0]}, "window": {"documents": [-2, 0]}}}}},
      {"$sort": {"d": 1}}, {"$project": {"_id": 0, "d": 1, "s": 1, "n": 1}}],
     [{"d": 1, "s": 10, "n": 1}, {"d": 2, "s": 10, "n": 1}, {"d": 3, "s": 30, "n": 2}, {"d": 4, "s": 50, "n": 2}]),

    ("$group $sum ignores null and missing values",
     [{"g": 1, "x": 5}, {"g": 1, "x": None}, {"g": 1}],
     [{"$group": {"_id": "$g", "s": {"$sum": "$x"}, "n": {"$sum": 1}}}, {"$project": {"_id": 0, "s": 1, "n": 1}}],
     [{"s": 5, "n": 3}]),

    ("$unwind drops a document whose array is empty",
     [{"k": "a", "arr": [1, 2]}, {"k": "b", "arr": []}],
     [{"$unwind": "$arr"}, {"$sort": {"arr": 1}}, {"$project": {"_id": 0, "k": 1, "arr": 1}}],
     [{"k": "a", "arr": 1}, {"k": "a", "arr": 2}]),

    ("$dateFromString + $isoDayOfWeek: 2026-07-04 is a Saturday (6), 2026-07-06 a Monday (1)",
     [{"s": "2026-07-04"}, {"s": "2026-07-06"}],
     [{"$sort": {"s": 1}}, {"$project": {"_id": 0, "w": {"$isoDayOfWeek": {"$dateFromString": {
         "dateString": "$s", "format": "%Y-%m-%d", "timezone": "UTC"}}}}}],
     [{"w": 6}, {"w": 1}]),

    ("$dateFromString onError gives null for an impossible date instead of failing the pipeline",
     [{"s": "2026-02-30"}],
     [{"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": "$s", "format": "%Y-%m-%d", "timezone": "UTC", "onError": None}}}}],
     [{"d": None}]),

    ("$dateToString renders a UTC midnight as the same calendar day",
     [{"d": day(9)}],
     [{"$project": {"_id": 0, "s": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d", "timezone": "UTC"}}}}],
     [{"s": "2026-07-09"}]),

    ("$eq against false is how 'not in the list' is written (the array form of $not is avoided)",
     [{"s": "a"}, {"s": "z"}],
     [{"$sort": {"s": 1}}, {"$project": {"_id": 0, "o": {"$eq": [{"$in": ["$s", ["a", "b"]]}, False]}}}],
     [{"o": False}, {"o": True}]),

    ("$floor((u * 10000 + h) / (2h)) is half-up: 1 of 32 -> 313 (0.0313), not 312",
     [{"u": 2, "h": 32}],
     [{"$project": {"_id": 0, "r": {"$floor": {"$divide": [{"$add": [{"$multiply": ["$u", 10000]}, "$h"]}, {"$multiply": [2, "$h"]}]}}}}],
     [{"r": 313}]),

    ("$arrayElemAt of an empty array is missing, so a later $ifNull on its field falls back to 0 (as department_summary does)",
     [{"agg": []}, {"agg": [{"v": 4}]}],
     [{"$project": {"_id": 0, "a": {"$arrayElemAt": ["$agg", 0]}}},
      {"$project": {"v": {"$ifNull": ["$a.v", 0]}}}, {"$project": {"_id": 0, "v": 1}}],
     [{"v": 0}, {"v": 4}]),
]
