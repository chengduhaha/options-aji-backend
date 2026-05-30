"""Site nav visibility merge tests."""
from __future__ import annotations

from app.services.site_nav import default_visibility, merge_visibility_update


def test_merge_group_hides_children() -> None:
    base = default_visibility()
    merged = merge_visibility_update(base, {"alt_data": False})
    assert merged["divergence"] is False
    assert merged["darkpool"] is False
    assert merged["congress"] is False
    assert merged["dash"] is True
