"""Shared configuration for local Codex bridge tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def private_test_files():
    """Token fixtures must be independent of the invoking user's umask."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)
