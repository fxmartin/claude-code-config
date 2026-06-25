# ABOUTME: Sample-target string utilities the eval agent extends per ticket.
# ABOUTME: Deliberately small and dependency-free so the diff is easy to score.

from __future__ import annotations


def reverse(text: str) -> str:
    """Return ``text`` reversed."""
    return text[::-1]


def is_palindrome(text: str) -> bool:
    """Return True when ``text`` reads the same forwards and backwards."""
    return text == text[::-1]
