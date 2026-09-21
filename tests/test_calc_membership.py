"""Membership selection: which candidates end up inside an average.

These are pure-data tests -- no Home Assistant. ``select_members`` is the
one place that decides what exclude mode and include mode MEAN, so it is
the one place worth testing exhaustively.
"""

import calc


def cand(entity_id, labels=(), category=None):
    """A candidate row as sensor.py builds it from the entity registry."""
    return calc.Candidate(entity_id, frozenset(labels), category)


# ------------------------------------------------------------ exclude mode


def test_exclude_mode_keeps_every_plain_candidate():
    members, excluded = calc.select_members(
        [cand("sensor.a"), cand("sensor.b")], calc.MODE_EXCLUDE
    )
    assert members == ["sensor.a", "sensor.b"]
    assert excluded == []


def test_exclude_mode_drops_labelled_candidates_into_excluded():
    members, excluded = calc.select_members(
        [cand("sensor.a"), cand("sensor.b", labels={"skip"})],
        calc.MODE_EXCLUDE,
        exclude_label_ids={"skip"},
    )
    assert members == ["sensor.a"]
    assert excluded == ["sensor.b"]


def test_exclude_mode_hides_diagnostic_candidates_entirely():
    """A radio chip temperature is not 'excluded by the user' -- it was
    never a candidate the dashboard should list under 'Not in average'."""
    members, excluded = calc.select_members(
        [cand("sensor.a"), cand("sensor.lqi_temp", category="diagnostic")],
        calc.MODE_EXCLUDE,
    )
    assert members == ["sensor.a"]
    assert excluded == []


def test_exclude_label_matches_a_label_inherited_from_the_device():
    """sensor.py folds device labels into the candidate's label set."""
    members, excluded = calc.select_members(
        [cand("sensor.a", labels={"skip", "upstairs"})],
        calc.MODE_EXCLUDE,
        exclude_label_ids={"skip"},
    )
    assert members == []
    assert excluded == ["sensor.a"]


# ------------------------------------------------------------ include mode


def test_include_mode_keeps_only_picked_entities():
    members, excluded = calc.select_members(
        [cand("sensor.a"), cand("sensor.b"), cand("sensor.c")],
        calc.MODE_INCLUDE,
        include_entities=["sensor.b"],
    )
    assert members == ["sensor.b"]
    assert excluded == ["sensor.a", "sensor.c"]


def test_include_mode_also_takes_entities_carrying_the_include_label():
    members, excluded = calc.select_members(
        [cand("sensor.a", labels={"want"}), cand("sensor.b"), cand("sensor.c")],
        calc.MODE_INCLUDE,
        include_entities=["sensor.c"],
        include_label_ids={"want"},
    )
    assert members == ["sensor.a", "sensor.c"]
    assert excluded == ["sensor.b"]


def test_include_mode_honours_an_explicitly_picked_diagnostic_entity():
    """If you went and picked it by hand, you meant it."""
    members, excluded = calc.select_members(
        [cand("sensor.probe", category="diagnostic"), cand("sensor.b")],
        calc.MODE_INCLUDE,
        include_entities=["sensor.probe"],
    )
    assert members == ["sensor.probe"]
    assert excluded == ["sensor.b"]


def test_include_mode_ignores_a_pick_that_is_not_a_candidate():
    """Picked entity moved out of the area (or was deleted): it cannot be a
    member, and it must not be invented into the member list."""
    members, excluded = calc.select_members(
        [cand("sensor.a")], calc.MODE_INCLUDE, include_entities=["sensor.gone"]
    )
    assert members == []
    assert excluded == ["sensor.a"]


def test_include_mode_with_nothing_picked_selects_nothing():
    members, excluded = calc.select_members(
        [cand("sensor.a"), cand("sensor.b")], calc.MODE_INCLUDE
    )
    assert members == []
    assert excluded == ["sensor.a", "sensor.b"]


def test_include_mode_drops_unpicked_diagnostics_from_excluded_too():
    members, excluded = calc.select_members(
        [cand("sensor.lqi", category="diagnostic"), cand("sensor.b")],
        calc.MODE_INCLUDE,
        include_entities=["sensor.b"],
    )
    assert members == ["sensor.b"]
    assert excluded == []


# ------------------------------------------------------------- invariants


def test_members_and_excluded_are_sorted_and_disjoint():
    candidates = [
        cand("sensor.z"),
        cand("sensor.a", labels={"skip"}),
        cand("sensor.m"),
    ]
    members, excluded = calc.select_members(
        candidates, calc.MODE_EXCLUDE, exclude_label_ids={"skip"}
    )
    assert members == sorted(members)
    assert excluded == sorted(excluded)
    assert not set(members) & set(excluded)


def test_an_unknown_mode_is_treated_as_exclude():
    """Never silently produce an empty average because of a typo in data."""
    members, _ = calc.select_members([cand("sensor.a")], "nonsense")
    assert members == ["sensor.a"]


def test_duplicate_candidates_are_collapsed():
    members, excluded = calc.select_members(
        [cand("sensor.a"), cand("sensor.a")], calc.MODE_EXCLUDE
    )
    assert members == ["sensor.a"]
    assert excluded == []
