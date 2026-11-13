from __future__ import annotations

from typing import Any

import pytest
from parsel import Selector

from itemloaders import DROP, EXPLICIT_DROP_CONTEXT_KEY, ItemLoader
from itemloaders.processors import Compose, Identity, MapCompose, TakeFirst


class TestExplicitDropContract:
    def test_drop_sentinel_is_singleton_and_falsy(self):
        assert repr(DROP) == "DROP"
        assert not DROP
        assert DROP is DROP

    def test_falsy_values_are_data(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", [None, "", 0, False, []])
        assert il.get_collected_values("field") == [None, "", 0, False, []]
        assert il.load_item() == {"field": [None, "", 0, False, []]}

    def test_scalar_none_is_collected(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", None)
        assert il.get_collected_values("field") == [None]
        assert il.load_item() == {"field": [None]}

    def test_drop_is_not_collected(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", ["keep", DROP])
        assert il.get_collected_values("field") == ["keep"]
        il.add_value("field", DROP)
        assert il.get_collected_values("field") == ["keep"]
        assert DROP not in il.load_item().values()

    def test_missing_field_differs_from_none_value(self):
        class NoneLoader(ItemLoader):
            field_out = TakeFirst()

        il = NoneLoader(explicit_drop=True)
        il.add_value("field", None)
        item = il.load_item()
        assert item == {"field": None}
        assert "missing" not in item
        assert il.get_collected_values("missing") == []
        assert il.get_output_value("missing") == []

    def test_get_value_keeps_none_and_short_circuits_drop(self):
        il = ItemLoader(explicit_drop=True)
        assert il.get_value(None, lambda value: "kept") == "kept"
        assert (
            il.get_value("x", lambda value: DROP, lambda value: 1 / 0) is DROP
        )

    def test_add_value_dict_with_drop(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value(None, {"a": 1, "b": DROP, "c": None})
        assert il.get_collected_values("a") == [1]
        assert il.get_collected_values("b") == []
        assert il.get_collected_values("c") == [None]


class TestReplaceWithDrop:
    def test_replace_drop_clears_collected_values(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", "old")
        il.replace_value("field", DROP)
        assert il.get_collected_values("field") == []
        assert "field" not in il.load_item()

    def test_replace_none_keeps_none_as_data(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", "old")
        il.replace_value("field", None)
        assert il.get_collected_values("field") == [None]
        assert il.load_item() == {"field": [None]}

    def test_replace_dict_drop_clears_only_named_fields(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value(None, {"a": 1, "b": 2})
        il.replace_value(None, {"a": DROP, "b": 3})
        assert il.get_collected_values("a") == []
        assert il.get_collected_values("b") == [3]


class TestExplicitProcessors:
    def test_mapcompose_keeps_none_drops_sentinel(self):
        def process(value: Any) -> Any:
            if value == "drop":
                return DROP
            if value == "none":
                return None
            return value

        proc = MapCompose(process, explicit_drop=True)
        assert proc([None, "none", "drop", 0, False, "keep"]) == [
            None,
            None,
            0,
            False,
            "keep",
        ]

    def test_mapcompose_all_dropped_returns_drop(self):
        proc = MapCompose(lambda value: DROP, explicit_drop=True)
        assert proc(["a", "b"]) is DROP

    def test_mapcompose_empty_list_is_data(self):
        proc = MapCompose(lambda value: value, explicit_drop=True)
        assert proc([[]]) == [[]]

    def test_mapcompose_loader_context_activates_mode(self):
        il = ItemLoader(explicit_drop=True)
        proc = MapCompose(lambda value: None)
        assert proc(["a"], il.context) == [None]

    def test_compose_keeps_processing_none(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value(
            "field",
            [None, "x"],
            Compose(TakeFirst(), lambda value: value or "was none"),
        )
        assert il.get_collected_values("field") == ["was none"]

    def test_compose_short_circuits_on_drop(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", "x", Compose(lambda value: DROP, lambda value: 1 / 0))
        assert il.get_collected_values("field") == []

    def test_take_first_returns_first_value_unchanged(self):
        proc = TakeFirst(explicit_drop=True)
        assert proc([None, "", 0, False]) is None
        assert proc([DROP, "one"]) == "one"
        assert proc([]) is DROP

    def test_take_first_loader_context_activates_mode(self):
        class Loader(ItemLoader):
            field_out = TakeFirst()

        il = Loader(explicit_drop=True)
        il.add_value("field", [None, "x"])
        assert il.get_output_value("field") is None
        assert il.load_item() == {"field": None}


class TestOutputDrop:
    def test_output_none_is_assigned_drop_is_not(self):
        class Loader(ItemLoader):
            none_out = lambda self, values: None  # noqa: E731
            drop_out = lambda self, values: DROP  # noqa: E731

        il = Loader(explicit_drop=True)
        il.add_value("none", ["x"])
        il.add_value("drop", ["x"])
        item = il.load_item()
        assert item["none"] is None
        assert "drop" not in item
        assert il.get_output_value("drop") is DROP

    def test_item_and_collected_values_stay_consistent(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("kept", [None, "", 0, False, [], DROP])
        il.add_value("dropped", "x", MapCompose(lambda value: DROP))
        item = il.load_item()
        assert item == {"kept": [None, "", 0, False, []]}
        assert il.get_collected_values("kept") == [None, "", 0, False, []]
        assert il.get_collected_values("dropped") == []


class TestNestedExplicitDrop:
    selector = Selector(text="<html><body><div><a>x</a></div></body></html>")

    def test_nested_loader_inherits_mode(self):
        il = ItemLoader(selector=self.selector, explicit_drop=True)
        nested = il.nested_xpath("//div")
        assert nested.explicit_drop is True
        assert nested.context[EXPLICIT_DROP_CONTEXT_KEY] is True

    def test_nested_loader_inherits_legacy_mode(self):
        il = ItemLoader(selector=self.selector)
        nested = il.nested_xpath("//div")
        assert nested.explicit_drop is False

    def test_nested_drop_never_reaches_item(self):
        il = ItemLoader(selector=self.selector, explicit_drop=True)
        nested = il.nested_xpath("//div")
        nested.add_value(
            "field",
            ["a", "b"],
            MapCompose(lambda value: DROP if value == "a" else value),
        )
        assert il.get_collected_values("field") == ["b"]
        item = il.load_item()
        assert DROP not in item.values()

    def test_nested_can_override_mode(self):
        il = ItemLoader(selector=self.selector, explicit_drop=True)
        nested = il.nested_xpath("//div", explicit_drop=False)
        assert nested.explicit_drop is False
        assert il.explicit_drop is True

    def test_empty_extraction_produces_no_value(self):
        il = ItemLoader(selector=self.selector, explicit_drop=True)
        il.add_xpath("missing", "//span/text()")
        il.add_value("empty", [])
        assert il.get_collected_values("missing") == []
        assert il.get_collected_values("empty") == []
        assert il.load_item() == {}

    def test_nested_empty_list_remains_data(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", [[]])
        assert il.get_collected_values("field") == [[]]

    def test_drop_output_allows_readding_the_field(self):
        il = ItemLoader(explicit_drop=True)
        il.add_value("field", "a", MapCompose(lambda value: DROP))
        assert "field" not in il.load_item()
        il.add_value("field", "b")
        assert il.load_item() == {"field": ["b"]}


class TestLegacyModeCompatibility:
    def test_explicit_drop_defaults_to_false(self):
        assert ItemLoader().explicit_drop is False

    def test_scalar_none_is_ignored(self):
        il = ItemLoader()
        il.add_value("field", None)
        assert il.get_collected_values("field") == []
        assert "field" not in il.load_item()

    def test_replace_none_keeps_old_value(self):
        il = ItemLoader()
        il.add_value("field", "old")
        il.replace_value("field", None)
        assert il.get_collected_values("field") == ["old"]

    def test_mapcompose_none_is_filtered(self):
        proc = MapCompose(lambda value: None if value == "drop" else value)
        assert proc(["drop", "keep"]) == ["keep"]

    def test_take_first_legacy_behaviour(self):
        proc = TakeFirst()
        assert proc([None, "", 0, False]) == 0
        assert proc([]) is None

    def test_drop_works_in_legacy_mode(self):
        il = ItemLoader()
        il.add_value("field", ["keep", DROP])
        assert il.get_collected_values("field") == ["keep"]
        il.replace_value("field", DROP)
        assert il.get_collected_values("field") == []

    def test_identity_keeps_falsy_values_in_both_modes(self):
        legacy = ItemLoader()
        legacy.add_value("field", [None, "", 0, False])
        assert legacy.get_collected_values("field") == [None, "", 0, False]

        explicit = ItemLoader(explicit_drop=True)
        explicit.default_input_processor = Identity()
        explicit.add_value("field", [None, "", 0, False])
        assert explicit.get_collected_values("field") == [None, "", 0, False]


class TestInitialItemValues:
    def test_initial_none_is_data_in_explicit_mode(self):
        il = ItemLoader(item={"field": None}, explicit_drop=True)
        assert il.get_collected_values("field") == [None]
        assert il.load_item() == {"field": [None]}

    def test_initial_none_legacy_behaviour_is_unchanged(self):
        il = ItemLoader(item={"field": None})
        assert il.get_collected_values("field") == []
        assert il.load_item() == {"field": []}

    def test_initial_falsy_values_are_kept(self):
        il = ItemLoader(
            item={"field": [None, "", 0, False]}, explicit_drop=True
        )
        assert il.get_collected_values("field") == [None, "", 0, False]
