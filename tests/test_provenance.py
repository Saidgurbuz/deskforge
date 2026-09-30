"""A seed does not identify a scene on its own.

`compose_scene(930098)` is deterministic - three fresh processes return the same
five apps - but it indexes into the app pool, so dropping one app changes what
every seed means. Measured on that seed: two runs produced mousepad and thunar,
a third nautilus and thunderbird, purely because FileZilla had been removed from
the pool in between. Nothing was flaky. Nothing recorded the change either.

These tests pin the property that makes such a difference explainable rather
than mysterious: captures are comparable only when the build and the pool match.
"""

from deskshot.provenance import comparable, hash_config, hash_strings, stamp


def test_pool_order_changes_the_hash():
    """Order matters because the seed indexes into the pool."""
    assert hash_strings(["a", "b"]) != hash_strings(["b", "a"])


def test_pool_membership_changes_the_hash():
    assert hash_strings(["a", "b"]) != hash_strings(["a", "b", "c"])


def test_same_inputs_hash_the_same():
    assert hash_strings(["a", "b"]) == hash_strings(["a", "b"])
    assert hash_config({"x": 1, "y": 2}) == hash_config({"y": 2, "x": 1})


def test_captures_from_the_same_build_and_pool_are_comparable():
    a = stamp(app_pool=["mousepad", "thunar"], config={"theme": "dark"})
    b = stamp(app_pool=["mousepad", "thunar"], config={"theme": "dark"})
    assert comparable(a, b) is True


def test_a_changed_pool_makes_captures_incomparable():
    """The FileZilla case: same seed, different meaning."""
    before = stamp(app_pool=["mousepad", "thunar", "filezilla"])
    after = stamp(app_pool=["mousepad", "thunar"])
    assert comparable(before, after) is False


def test_a_different_commit_makes_captures_incomparable():
    a = stamp(app_pool=["mousepad"])
    b = stamp(app_pool=["mousepad"])
    b = {**b, "code": {**b["code"], "commit": "0" * 40}}
    assert comparable(a, b) is False


def test_missing_provenance_is_never_comparable():
    """Captures predating the stamp cannot be vouched for."""
    assert comparable({}, stamp(app_pool=["mousepad"])) is False
    assert comparable(stamp(app_pool=["mousepad"]), {}) is False


def test_stamp_records_whether_the_tree_was_dirty():
    """A capture from a modified tree cannot be reproduced from its commit, so
    it must not be trusted as a baseline however good its numbers look."""
    assert "dirty" in stamp()["code"]
