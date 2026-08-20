"""Tests for eligible broker filtering."""

from broker_rating_collectors import (
    EXCLUDED_RATING_DEPARTMENTS,
    _is_active_user,
    _is_rop_user,
)


def test_is_active_user():
    assert _is_active_user({"ACTIVE": True}) is True
    assert _is_active_user({"ACTIVE": "Y"}) is True
    assert _is_active_user({"ACTIVE": "N"}) is False
    assert _is_active_user({"ACTIVE": False}) is False


def test_is_rop_user():
    assert _is_rop_user({"WORK_POSITION": "Руководитель отдела продаж (РОП)"}) is True
    assert _is_rop_user({"WORK_POSITION": "Брокер"}) is False


def test_excluded_departments_contain_to():
    assert "ТО" in EXCLUDED_RATING_DEPARTMENTS
