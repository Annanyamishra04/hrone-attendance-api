"""tests/assumptions.py run through the test-only interpreter. This checks the interpreter against the table; it says
nothing about MongoDB (the same table is run on a real server by tests/test_mongo_integration_analytics.py)."""
import pytest

from tests import mini_agg
from tests.assumptions import CASES


@pytest.mark.parametrize("name,docs,pipeline,expected", CASES, ids=[c[0][:60] for c in CASES])
def test_interpreter_matches_the_assumption_table(name, docs, pipeline, expected):
    got = mini_agg.run(pipeline, docs, db=None)
    assert [dict(sorted(d.items())) for d in got] == [dict(sorted(d.items())) for d in expected], name


def test_the_interpreter_refuses_operators_it_does_not_know():
    with pytest.raises(NotImplementedError):
        mini_agg.run([{"$project": {"x": {"$notARealOperator": 1}}}], [{"a": 1}], db=None)
    with pytest.raises(KeyError):
        mini_agg.run([{"$bucketAuto": {}}], [{"a": 1}], db=None)
