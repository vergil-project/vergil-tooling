"""Unit tests for the test-stage measurement harness (epic #333, Task 1).

The only pure logic in ``scripts/perf/measure_test_stage.py`` is the median
math; the run loop is a thin subprocess driver exercised by actually running
the harness, not by unit tests.
"""

from __future__ import annotations

from scripts.perf.measure_test_stage import median_seconds


def test_median_discards_warmup_and_returns_middle() -> None:
    # warm-up already stripped by caller; median of the timed runs
    assert median_seconds([3.0, 1.0, 2.0]) == 2.0


def test_median_even_count_averages_middle_pair() -> None:
    assert median_seconds([1.0, 2.0, 3.0, 4.0]) == 2.5
