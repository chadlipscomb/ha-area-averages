"""Subentry keys and the unique_id matching the migration depends on."""

import calc


def test_subentry_key_round_trips():
    key = calc.subentry_key("master_bathroom", "carbon_dioxide")
    assert calc.parse_subentry_key(key) == ("master_bathroom", "carbon_dioxide")


def test_subentry_key_separator_cannot_collide_with_an_area_id():
    """Area ids and device classes are slugs; the separator must not be one
    of the characters they can contain."""
    key = calc.subentry_key("master_bathroom", "carbon_dioxide")
    assert "|" in key and key.count("|") == 1


def test_parse_subentry_key_rejects_junk():
    assert calc.parse_subentry_key("no-separator") is None
    assert calc.parse_subentry_key("") is None
    assert calc.parse_subentry_key("a|b|c") is None


def test_match_average_unique_id_handles_underscores_on_both_sides():
    """Both 'master_bathroom' and 'carbon_dioxide' contain underscores, so
    splitting the unique_id on '_' cannot work."""
    unique_id = calc.average_unique_id("master_bathroom", "carbon_dioxide")
    match = calc.match_average_unique_id(
        unique_id,
        area_ids=["master_bathroom", "master", "kitchen"],
        device_classes=["temperature", "carbon_dioxide"],
    )
    assert match == ("master_bathroom", "carbon_dioxide")


def test_match_average_unique_id_returns_none_for_a_stranger():
    assert (
        calc.match_average_unique_id(
            "something_else", area_ids=["kitchen"], device_classes=["temperature"]
        )
        is None
    )


def test_match_average_unique_id_prefers_the_longest_area_match():
    """'master' and 'master_bathroom' both exist; the id belongs to the
    longer one, and a greedy short match would steal it."""
    unique_id = calc.average_unique_id("master_bathroom", "temperature")
    match = calc.match_average_unique_id(
        unique_id,
        area_ids=["master", "master_bathroom"],
        device_classes=["temperature"],
    )
    assert match == ("master_bathroom", "temperature")


def test_orphan_unique_ids_is_pure_set_arithmetic():
    orphans = calc.orphan_unique_ids(
        desired={"a_temperature_average"},
        existing=[
            ("sensor.a", "a_temperature_average"),
            ("sensor.b", "b_temperature_average"),
        ],
    )
    assert orphans == ["sensor.b"]
