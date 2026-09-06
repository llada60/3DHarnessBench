import math

from evaluation.metrics.usage import aggregate, number


def test_number_rejects_bool_non_numeric_and_non_finite_values():
    assert number(True) is None
    assert number("1") is None
    assert number(math.inf) is None
    assert number(math.nan) is None


def test_aggregate_reports_partial_coverage_without_fabricating_average():
    result = aggregate([10, None, 20], n_instances=3)

    assert result == {
        "total": 30,
        "average_per_reported_instance": 15,
        "average_per_evaluated_instance": None,
        "n_reported": 2,
        "n_missing": 1,
    }


def test_aggregate_reports_full_coverage_average():
    result = aggregate([2, 4], n_instances=2)

    assert result["total"] == 6
    assert result["average_per_reported_instance"] == 3
    assert result["average_per_evaluated_instance"] == 3
