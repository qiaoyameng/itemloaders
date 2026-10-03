import pytest
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import Compose, Join, MapCompose, TakeFirst
from itemloaders.provenance import ProcessorStep, Source

HTML = """
<html>
<body>
<div class="product">
  <span class="name">  Color TV  </span>
  <span class="price">$1200</span>
  <span class="price">n/a</span>
  <span class="price">$300</span>
</div>
<div class="other">
  <span class="price">$9</span>
</div>
</body>
</html>
"""


def get_selector():
    return Selector(text=HTML)


def shout(values):
    # user processors keep receiving plain values, no wrappers
    shout.seen_types = [type(v) for v in values]
    return [v.upper() for v in values]


class TestFullBacktrace:
    """Nested selection -> split -> filter -> aggregate, traced end to end."""

    def test_nested_split_filter_aggregate(self):
        def split_price(value):
            # one value becomes many
            return [value, value[:-1]] if value.endswith("00") else value

        def drop_non_numeric(value):
            # returning None filters the value out
            return value if value.isdigit() else None

        class ProductLoader(ItemLoader):
            price_in = MapCompose(
                lambda s: s.lstrip("$"), split_price, drop_non_numeric
            )
            price_out = Join("+")

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        nested = loader.nested_xpath('//div[@class="product"]')
        nested.add_xpath("price", './/span[@class="price"]/text()')
        item = loader.load_item()
        assert item == {"price": "1200+120+300+30"}

        report = loader.get_provenance("price")

        # the aggregate output declares every input it used
        (join_step,) = report.output
        assert isinstance(join_step, ProcessorStep)
        assert join_step.processor == "Join"
        assert join_step.mode == "aggregate"
        assert join_step.value == "1200+120+300+30"
        assert len(join_step.inputs) == 4

        # each aggregated input traces back through the split to a source
        sources = report.sources()
        assert [s.kind for s in sources] == ["xpath"] * 4
        assert [s.expression for s in sources] == [
            './/span[@class="price"]/text()'
        ] * 4
        # all four values came from the same add_xpath call (one batch)
        assert len({s.batch for s in sources}) == 1
        # raw extracted values behind the final output
        assert [s.value for s in sources] == ["$1200", "$1200", "$300", "$300"]

        # the split step: '1200' and '120' share the same '$1200' source
        filter_steps = [
            node
            for node in join_step.inputs
            if isinstance(node, ProcessorStep) and node.processor == "drop_non_numeric"
        ]
        assert len(filter_steps) == 4
        first, second = filter_steps[0], filter_steps[1]
        assert first.value == "1200"
        assert second.value == "120"
        first_source = next(iter(first.inputs[0].iter_sources()))
        second_source = next(iter(second.inputs[0].iter_sources()))
        assert first_source is second_source
        assert first_source.value == "$1200"

        # the filtered value is explained as a drop with its source
        assert len(report.drops) == 1
        drop = report.drops[0]
        assert drop.processor == "drop_non_numeric"
        assert drop.value == "n/a"
        drop_sources = list(drop.source.iter_sources())
        assert [s.value for s in drop_sources] == ["n/a"]
        assert drop_sources[0].expression == './/span[@class="price"]/text()'

        # no provenance leaked from other fields or the other div
        assert report.expressions() == ['.//span[@class="price"]/text()']
        assert "$9" not in [s.value for s in sources]

    def test_explain_renders_tree(self):
        class ProductLoader(ItemLoader):
            name_in = MapCompose(str.strip)
            name_out = TakeFirst()

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        nested = loader.nested_xpath('//div[@class="product"]')
        nested.add_xpath("name", './/span[@class="name"]/text()')
        assert loader.load_item() == {"name": "Color TV"}
        text = loader.get_provenance("name").explain()
        assert "TakeFirst" in text
        assert "strip" in text
        assert "xpath" in text
        assert './/span[@class="name"]/text()' in text


class TestProcessorTracking:
    def test_take_first_declares_chosen_input(self):
        class ProductLoader(ItemLoader):
            default_output_processor = TakeFirst()

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        loader.add_xpath("price", '//div[@class="product"]//span[@class="price"]/text()')
        assert loader.load_item() == {"price": "$1200"}
        (step,) = loader.get_provenance("price").output
        assert isinstance(step, ProcessorStep)
        assert step.processor == "TakeFirst"
        assert step.mode == "mapped"
        (source,) = step.inputs
        assert isinstance(source, Source)
        assert source.value == "$1200"

    def test_mapcompose_error_is_explained(self):
        def boom(value):
            raise ZeroDivisionError("bad price")

        class ProductLoader(ItemLoader):
            price_in = MapCompose(str.strip, boom)

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        with pytest.raises(ValueError, match="Error with input processor MapCompose"):
            loader.add_xpath("price", '//span[@class="price"]/text()')

    def test_custom_processor_is_batch_dependency(self):
        class ProductLoader(ItemLoader):
            name_in = shout
            name_out = TakeFirst()

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        loader.add_xpath("name", './/span[@class="name"]/text()')
        assert loader.load_item() == {"name": "  COLOR TV  "}
        assert shout.seen_types == [str]

        report = loader.get_provenance("name")
        take_first = report.output[0]
        (custom_step,) = take_first.inputs
        assert isinstance(custom_step, ProcessorStep)
        assert custom_step.processor == "shout"
        assert custom_step.mode == "batch"
        assert [s.value for s in custom_step.iter_sources()] == ["  Color TV  "]

    def test_compose_steps_are_batch_dependencies(self):
        class ProductLoader(ItemLoader):
            name_out = Compose(TakeFirst(), str.upper)

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        loader.add_xpath("name", './/span[@class="name"]/text()')
        assert loader.load_item() == {"name": "  COLOR TV  "}
        upper_step = loader.get_provenance("name").output[0]
        assert upper_step.processor == "upper"
        assert upper_step.mode == "batch"
        (take_first_step,) = upper_step.inputs
        assert take_first_step.processor == "TakeFirst"
        assert take_first_step.mode == "mapped"

    def test_re_extraction_is_tracked(self):
        class ProductLoader(ItemLoader):
            default_output_processor = TakeFirst()

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        loader.add_value("price", "the price is $1200", re=r"the price is (.*)")
        assert loader.load_item() == {"price": "$1200"}
        report = loader.get_provenance("price")
        re_step = report.output[0].inputs[0]
        assert re_step.processor == "re:the price is (.*)"
        assert re_step.mode == "mapped"
        (source,) = re_step.inputs
        assert source.kind == "value"
        assert source.value == "the price is $1200"


class TestNestedAndReplace:
    def test_nested_loaders_do_not_mix_fields(self):
        class ProductLoader(ItemLoader):
            default_output_processor = TakeFirst()

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        nested = loader.nested_xpath('//div[@class="product"]')
        nested.add_xpath("name", './/span[@class="name"]/text()')
        loader.add_xpath("price", '//div[@class="other"]//span[@class="price"]/text()')
        assert loader.load_item() == {"name": "  Color TV  ", "price": "$9"}

        name_sources = loader.get_provenance("name").sources()
        assert [s.value for s in name_sources] == ["  Color TV  "]
        price_sources = loader.get_provenance("price").sources()
        assert [s.value for s in price_sources] == ["$9"]
        # batches are shared across the nested loader tree
        assert name_sources[0].batch != price_sources[0].batch

    def test_replace_value_evicts_replaced_sources(self):
        class ProductLoader(ItemLoader):
            default_output_processor = TakeFirst()

        loader = ProductLoader(selector=get_selector(), track_provenance=True)
        loader.add_xpath("price", '//div[@class="product"]//span[@class="price"]/text()')
        loader.replace_xpath("price", '//div[@class="other"]//span[@class="price"]/text()')
        assert loader.load_item() == {"price": "$9"}
        report = loader.get_provenance("price")
        assert [s.value for s in report.sources()] == ["$9"]
        assert [s.value for s in report.collected] == ["$9"]

    def test_replace_value_with_plain_values(self):
        class ProductLoader(ItemLoader):
            default_output_processor = TakeFirst()

        loader = ProductLoader(track_provenance=True)
        loader.add_value("price", "100")
        loader.replace_value("price", "200")
        assert loader.load_item() == {"price": "200"}
        report = loader.get_provenance("price")
        assert [s.value for s in report.sources()] == ["200"]
        assert report.sources()[0].kind == "value"


class TestTrackingDisabled:
    def test_get_provenance_requires_tracking(self):
        loader = ItemLoader()
        with pytest.raises(RuntimeError, match="track_provenance=True"):
            loader.get_provenance("price")

    def test_no_tracking_state_when_disabled(self):
        loader = ItemLoader(selector=get_selector())
        assert loader._tracker is None
        loader.add_xpath("price", '//div[@class="product"]//span[@class="price"]/text()')
        assert loader.load_item() == {"price": ["$1200", "n/a", "$300"]}

    def test_nested_loader_inherits_disabled_tracking(self):
        loader = ItemLoader(selector=get_selector())
        nested = loader.nested_xpath("//div")
        assert nested._tracker is None

    def test_nested_loader_inherits_enabled_tracking(self):
        loader = ItemLoader(selector=get_selector(), track_provenance=True)
        nested = loader.nested_xpath("//div")
        assert nested._tracker is loader._tracker
