"""Tests for the optional value-origin tracking feature."""

from __future__ import annotations

import pytest
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import Compose, Join, MapCompose, TakeFirst

HTML = """
<html><body>
<div class="product">
  <span class="price">$1,200</span>
  <span class="price"></span>
  <span class="price">$1,500</span>
  <ul class="tags"><li>new,hot</li><li>sale</li></ul>
</div>
</body></html>
"""


def _selector() -> Selector:
    return Selector(text=HTML)


class TestTrackingSwitch:
    def test_disabled_by_default_and_zero_api(self) -> None:
        loader = ItemLoader(selector=_selector())
        assert loader.tracking_enabled is False
        assert loader._trace_engine is None
        with pytest.raises(RuntimeError):
            loader.get_field_trace("price")

    def test_inherited_by_nested_loaders(self) -> None:
        loader = ItemLoader(selector=_selector(), track_sources=True)
        nested = loader.nested_xpath("//div[@class='product']")
        nested2 = nested.nested_xpath("ul")
        assert nested.tracking_enabled is True
        assert nested2.tracking_enabled is True
        assert nested._trace_engine is loader._trace_engine

    def test_can_be_enabled_explicitly_on_nested_root(self) -> None:
        loader = ItemLoader(selector=_selector())
        nested = loader.nested_xpath("//div[@class='product']")
        assert nested.tracking_enabled is False

    def test_processors_receive_plain_values(self) -> None:
        seen: list[object] = []

        def spy(value: object) -> object:
            seen.append(type(value))
            return value

        loader = ItemLoader(selector=_selector(), track_sources=True)
        loader.add_xpath("price", "//span[@class='price']/text()", MapCompose(spy))
        assert all(seen_type is str for seen_type in seen)


class TestNestedSplitFilterAggregate:
    """Full backtrace over nested selection, regex, split, filter, aggregate."""

    def test_full_trace(self) -> None:
        class ProductLoader(ItemLoader):
            tags_out = Join(",")
            first_price_out = TakeFirst()

        def clean_price(raw: str) -> str:
            return raw.replace(",", "")

        def split_tags(raw: str) -> list[str] | None:
            return raw.split(",") if raw else None

        loader = ProductLoader(selector=_selector(), track_sources=True)
        nested = loader.nested_xpath("//div[@class='product']")
        nested.add_xpath(
            "price",
            "span[@class='price']/text()",
            MapCompose(clean_price, float),
            re=r"\$([\d,]+)",
        )
        nested.add_xpath(
            "tags", "ul/li/text()", MapCompose(str.strip, split_tags)
        )
        item = loader.load_item()
        assert item["price"] == [1200.0, 1500.0]
        assert item["tags"] == "new,hot,sale"

        price = loader.get_field_trace("price")

        # Output reverse-lookup: each final value traces to its own expression
        # result, in order, with the nested selector chain.
        assert [value.value for value in price.values] == [1200.0, 1500.0]
        first, second = price.values
        assert first.sources[0].type == "xpath"
        assert first.sources[0].expression == "span[@class='price']/text()"
        assert first.sources[0].index == 0
        assert second.sources[0].index == 1
        assert [ref.expression for ref in first.sources[0].chain] == [
            "//div[@class='product']",
            "span[@class='price']/text()",
        ]
        assert [ref.type for ref in first.sources[0].chain] == ["xpath", "xpath"]

        # Steps explain regex extraction then every MapCompose function.
        step_names = [(step.stage, step.name, step.attribution) for step in first.steps]
        assert ("add", "re", "element") in step_names
        assert any(
            stage == "add" and name.endswith("clean_price")
            and attribution == "element"
            for stage, name, attribution in step_names
        )
        assert ("add", "float", "element") in step_names

        # The empty span never produced a text node, and regex drops would be
        # recorded when an extracted value does not match.
        assert all(drop.reason in {"none", "no_regex_match"} for drop in price.drops)

        tags = loader.get_field_trace("tags")
        # Split: one input element ("new,hot") expanded into two outputs which
        # both point back at the same source element.
        tag_values = {value.value: value for value in tags.values}
        assert set(tag_values) == {"new", "hot", "sale"}
        new = tag_values["new"]
        hot = tag_values["hot"]
        sale = tag_values["sale"]
        assert new.sources[0].index == hot.sources[0].index == 0
        assert sale.sources[0].index == 1
        expansion = new.expansions[0]
        assert expansion.input_value == "new,hot"
        assert expansion.output_count == 2
        assert hot.expansions[0] is expansion
        assert sale.expansions == ()

        # The one-to-many step declares itself as such.
        split_steps = [
            step
            for value in tags.values
            for step in value.steps
            if step.name.endswith("split_tags")
        ]
        assert {step.attribution for step in split_steps} == {
            "one_to_many",
            "element",
        }

        # Aggregation: Join declares it adopted every input, and the joined
        # output depends on the union of both source elements.
        assert tags.output is not None
        assert tags.output.value == "new,hot,sale"
        join_step = next(step for step in tags.output.steps if step.name == "Join")
        assert join_step.attribution == "batch"
        assert join_step.adopted == (0, 1, 2)
        assert {source.index for source in join_step.sources} == {0, 1}

    def test_take_first_adopts_only_chosen_input(self) -> None:
        class Loader(ItemLoader):
            price_out = TakeFirst()

        loader = Loader(selector=_selector(), track_sources=True)
        loader.add_xpath(
            "price",
            "//span[@class='price']/text()",
            MapCompose(lambda raw: raw.replace(",", "") or None, float),
            re=r"\$([\d,]+)",
        )
        assert loader.load_item()["price"] == 1200.0
        trace = loader.get_field_trace("price")
        assert trace.output is not None
        assert trace.output.value == 1200.0
        step = next(step for step in trace.output.steps if step.name == "TakeFirst")
        assert step.adopted == (0,)
        assert trace.output.sources[0].index == 0


class TestMapComposeSemantics:
    def test_none_return_is_filtered_and_explained(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader3 = ItemLoader(track_sources=True)
        loader3.add_value(
            "f",
            ["keep", "drop", "also-keep"],
            MapCompose(lambda raw: None if raw == "drop" else raw.upper()),
        )
        trace = loader3.get_field_trace("f")
        assert [value.value for value in trace.values] == ["KEEP", "ALSO-KEEP"]
        drop = trace.drops[0]
        assert drop.reason == "none"
        assert drop.value == "drop"
        assert drop.step is not None
        assert drop.step.name == "<lambda>"
        assert [source.value for source in drop.sources] == ["drop"]

    def test_one_to_many_expansion(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader.add_value(
            "f", ["a,b", "c"], MapCompose(lambda raw: raw.split(","))
        )
        trace = loader.get_field_trace("f")
        values = list(trace.values)
        assert [value.value for value in values] == ["a", "b", "c"]
        assert values[0].sources[0].index == values[1].sources[0].index == 0
        assert values[2].sources[0].index == 1
        assert values[0].expansions[0].output_count == 2
        step = values[0].steps[-1]
        assert step.attribution == "one_to_many"

    def test_processor_error_is_explained(self) -> None:
        loader = ItemLoader(track_sources=True)
        with pytest.raises(ValueError, match=r"Error in MapCompose"):
            loader.add_value("f", ["1", "x"], MapCompose(int))
        trace = loader.get_field_trace("f")
        (error,) = trace.errors
        assert error.stage == "add"
        assert error.error_type == "ValueError"
        assert error.value == "x"
        assert error.step is not None
        assert error.step.name == "int"

    def test_error_message_matches_untracked_format(self) -> None:
        messages = []
        for track in (False, True):
            loader = ItemLoader(track_sources=track)
            try:
                loader.add_value("f", ["1", "x"], MapCompose(int))
            except ValueError as exc:
                messages.append(str(exc))
        assert messages[0] == messages[1]


class TestCustomProcessors:
    def test_opaque_processor_records_batch_dependency(self) -> None:
        def merge(values: list[str]) -> list[str]:
            return [values[0] + "+" + values[1]]

        loader = ItemLoader(track_sources=True)
        loader.add_value("f", ["a", "b"], merge)
        trace = loader.get_field_trace("f")
        (value,) = trace.values
        assert value.value == "a+b"
        (step,) = value.steps
        assert step.attribution == "batch"
        # Whole-batch dependency: no fake per-element precision.
        assert {source.value for source in step.sources} == {"a", "b"}
        assert len(value.sources) == 2

    def test_compose_chain_steps(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader.add_value(
            "f", ["hello", "world"], Compose(lambda values: values[0], str.upper)
        )
        trace = loader.get_field_trace("f")
        (value,) = trace.values
        assert value.value == "HELLO"
        assert [step.name for step in value.steps] == ["<lambda>", "str.upper"]
        assert all(step.attribution == "batch" for step in value.steps)


class TestReplaceAndIsolation:
    def test_replace_removes_replaced_sources(self) -> None:
        loader = ItemLoader(selector=_selector(), track_sources=True)
        loader.add_xpath("f", "//span[@class='price']/text()")
        trace = loader.get_field_trace("f")
        assert len(trace.sources) == 2

        loader.replace_xpath("f", "//ul/li[1]/text()")
        trace = loader.get_field_trace("f")
        assert [value.value for value in trace.values] == ["new,hot"]
        # Only the replacement batch contributes to current values.
        assert all(source.expression == "//ul/li[1]/text()" for source in trace.sources)
        assert [batch.action for batch in trace.batches] == ["replace"]

    def test_nested_loaders_do_not_leak_sources_between_fields(self) -> None:
        loader = ItemLoader(selector=_selector(), track_sources=True)
        nested = loader.nested_xpath("//div[@class='product']")
        nested.add_xpath("price", "span[@class='price']/text()")
        nested.add_xpath("tags", "ul/li/text()")
        loader.load_item()
        price = loader.get_field_trace("price")
        tags = loader.get_field_trace("tags")
        assert all(
            source.expression == "span[@class='price']/text()"
            for source in price.sources
        )
        assert all(
            source.expression == "ul/li/text()" for source in tags.sources
        )
        assert {batch.id for batch in price.batches}.isdisjoint(
            {batch.id for batch in tags.batches}
        )

    def test_replace_value_plain(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader.add_value("f", "old")
        loader.replace_value("f", "new")
        trace = loader.get_field_trace("f")
        assert [value.value for value in trace.values] == ["new"]
        assert [batch.action for batch in trace.batches] == ["replace"]


class TestBatchesAndOrigins:
    def test_multiple_expressions_create_distinct_batches(self) -> None:
        loader = ItemLoader(selector=_selector(), track_sources=True)
        loader.add_xpath(
            "f",
            ["//span[@class='price']/text()", "//ul/li/text()"],
        )
        trace = loader.get_field_trace("f")
        expressions = {batch.sources[0].expression for batch in trace.batches}
        assert expressions == {
            "//span[@class='price']/text()",
            "//ul/li/text()",
        }

    def test_add_value_origin(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader.add_value("f", ["one", "two"])
        trace = loader.get_field_trace("f")
        assert [source.type for source in trace.sources] == ["value", "value"]
        assert trace.batches[0].kind == "value"
        assert trace.batches[0].action == "add"

    def test_mapping_add_without_field_name(self) -> None:
        for tracked in (False, True):
            loader = ItemLoader(track_sources=tracked)
            loader.add_value(None, {"a": 1, "b": [2, 3]})
            assert loader.load_item() == {"a": [1], "b": [2, 3]}
        loader = ItemLoader(track_sources=True)
        loader.add_value(None, {"a": 1})
        trace = loader.get_field_trace("a")
        assert trace.values[0].value == 1
        assert trace.batches[0].field == "a"

    def test_mapping_replace_without_field_name(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader.add_value("a", "old")
        loader.replace_value(None, {"a": "new"})
        trace = loader.get_field_trace("a")
        assert [value.value for value in trace.values] == ["new"]
        assert [batch.action for batch in trace.batches] == ["replace"]

    def test_initial_item_values_are_traced(self) -> None:
        loader = ItemLoader(item={"f": "preset"}, track_sources=True)
        trace = loader.get_field_trace("f")
        (value,) = trace.values
        assert value.value == "preset"
        assert value.sources[0].type == "value"
        assert trace.batches[0].kind == "item"


class TestSelectorTypesAndOutput:
    def test_nested_css_chain(self) -> None:
        selector = Selector(text="<div class='p'><span>12</span></div>")
        loader = ItemLoader(selector=selector, track_sources=True)
        nested = loader.nested_css("div.p")
        nested.add_css("f", "span::text")
        loader.load_item()
        trace = loader.get_field_trace("f")
        assert [(ref.type, ref.expression) for ref in trace.sources[0].chain] == [
            ("css", "div.p"),
            ("css", "span::text"),
        ]

    def test_jmes_origin(self) -> None:
        selector = Selector(text='{"prices": ["1", "2"]}', type="json")
        loader = ItemLoader(selector=selector, track_sources=True)
        loader.add_jmes("f", "prices")
        trace = loader.get_field_trace("f")
        assert [source.value for source in trace.sources] == ["1", "2"]
        assert trace.sources[0].type == "jmes"
        assert trace.sources[0].expression == "prices"

    def test_output_trace_is_stable_across_calls(self) -> None:
        loader = ItemLoader(track_sources=True)
        loader.add_value("f", ["a", "b"])
        assert loader.get_output_value("f") == ["a", "b"]
        assert loader.get_output_value("f") == ["a", "b"]
        trace = loader.get_field_trace("f")
        assert trace.output is not None
        assert trace.output.value == ["a", "b"]

    def test_take_first_none_output_records_drop(self) -> None:
        class Loader(ItemLoader):
            f_out = TakeFirst()

        loader = Loader(track_sources=True)
        loader.add_value("f", ["", None])
        assert loader.get_output_value("f") is None
        trace = loader.get_field_trace("f")
        assert trace.output is None
        assert any(drop.reason == "none" for drop in trace.drops)


@pytest.mark.parametrize(
    "add_processors",
    [
        (),
        (MapCompose(str.upper),),
        (MapCompose(lambda raw: None if raw == "drop" else raw),),
        (Compose(list, MapCompose(str.upper)),),
        (lambda values: list(reversed(values)),),
        (lambda values: values[0],),
        (MapCompose(lambda raw: raw.split(","), str.upper),),
    ],
)
@pytest.mark.parametrize(
    "output_processor",
    [None, TakeFirst(), Join("-"), MapCompose(str.upper), lambda v: list(v)],
)
def test_tracked_output_matches_untracked(
    add_processors: tuple, output_processor: object | None
) -> None:
    class TrackedLoader(ItemLoader):
        pass

    class UntrackedLoader(ItemLoader):
        pass

    if output_processor is not None:
        TrackedLoader.f_out = output_processor  # type: ignore[attr-defined]
        UntrackedLoader.f_out = output_processor  # type: ignore[attr-defined]

    tracked = TrackedLoader(track_sources=True)
    untracked = UntrackedLoader(track_sources=False)
    tracked.add_value("f", ["ab", "drop", "c,d"], *add_processors)
    untracked.add_value("f", ["ab", "drop", "c,d"], *add_processors)
    assert tracked.get_output_value("f") == untracked.get_output_value("f")
    assert tracked.load_item() == untracked.load_item()
