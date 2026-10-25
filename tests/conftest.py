"""Shared fixtures."""

from unittest import mock

import pytest

from src.common.ledger import Ledger


@pytest.fixture
def ledger(tmp_path):
    """A Ledger whose DB, artifacts and audit log all live in tmp_path."""
    with mock.patch("src.common.ledger.DATA_DIR", tmp_path), \
         mock.patch("src.common.ledger.AUDIT_LOG", tmp_path / "audit.log"):
        yield Ledger(db_path=tmp_path / "ledger.db")
