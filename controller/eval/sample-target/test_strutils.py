# ABOUTME: Baseline tests for the sample-target string utilities.
# ABOUTME: A ticket's quality_cmd runs pytest here to score "tests still pass".

from __future__ import annotations

from strutils import is_palindrome, reverse


def test_reverse() -> None:
    assert reverse("abc") == "cba"


def test_is_palindrome() -> None:
    assert is_palindrome("racecar")
    assert not is_palindrome("racecars")
