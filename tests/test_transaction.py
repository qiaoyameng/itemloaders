from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import pytest
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import TakeFirst

HTML = (
    '<html><div class="product">'
    '<p id="price">$10</p>'
    '<p id="currency">USD</p>'
    '<p id="stock">5</p>'
    "</div></html>"
)


class Stats:
    def __init__(self) -> None:
        self.values: Counter[str] = Counter()

    def inc_value(self, key: str, count: int = 1) -> None:
        self.values[key] += count


class FailOnce:
    """Processor that raises on its first call and counts invocations."""

    def __init__(self, fail_on: int = 1) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def __call__(self, values: Any) -> Any:
        self.calls += 1
        if self.calls == self.fail_on:
            raise RuntimeError("boom")
        return values


def make_replace_loader() -> tuple[ItemLoader, FailOnce]:
    """Loader whose ``price`` input processor fails on its second call."""
    failer = FailOnce(fail_on=2)

    class ReplaceLoader(ItemLoader):
        price_in = failer

    return ReplaceLoader(), failer


@dataclass
class Product:
    name: str | None = None
    price: str | None = None
    currency: str | None = None
    stock: str | None = None


class ProductLoader(ItemLoader):
    default_output_processor = TakeFirst()


def make_loader(stats: Stats | None = None, **kw: Any) -> ItemLoader:
    return ItemLoader(selector=Selector(text=HTML), stats=stats, **kw)


def fill_and_load(loader: ItemLoader, stock_processor: Any = None) -> Any:
    with loader.transaction():
        loader.add_css("price", "#price::text")
        loader.add_css("currency", "#currency::text")
        if stock_processor is None:
            loader.add_css("stock", "#stock::text")
        else:
            loader.add_css("stock", "#stock::text", stock_processor)
        return loader.load_item()


class TestTransaction:
    def test_yields_loader(self):
        loader = ItemLoader()
        with loader.transaction() as context:
            assert context is loader

    def test_commit_keeps_changes(self):
        loader = ItemLoader()
        with loader.transaction():
            loader.add_value("price", "$10")
        assert loader.get_collected_values("price") == ["$10"]
        assert loader.load_item() == {"price": ["$10"]}

    def test_failed_segment_rolls_back_and_matches_control(self):
        stats, control_stats = Stats(), Stats()
        loader = make_loader(stats=stats)
        control = make_loader(stats=control_stats)
        failer = FailOnce()

        # The third processor fails after price and currency were collected.
        with pytest.raises(ValueError, match="FailOnce"):
            fill_and_load(loader, failer)
        assert failer.calls == 1
        assert loader.get_collected_values("price") == []
        assert loader.get_collected_values("currency") == []
        assert loader.get_collected_values("stock") == []

        # The same loader keeps working without being rebuilt.
        item = fill_and_load(loader, failer)
        control_item = fill_and_load(control)
        assert failer.calls == 2
        assert item == control_item == {
            "price": ["$10"],
            "currency": ["USD"],
            "stock": ["5"],
        }
        assert stats.values == control_stats.values

    def test_failed_replace_keeps_old_value(self):
        loader, failer = make_replace_loader()
        loader.add_value("price", "old")
        with pytest.raises(ValueError, match="FailOnce"):
            with loader.transaction():
                loader.add_value("currency", "USD")
                loader.replace_value("price", "new")
        # The input processor ran once per call, and the failed replace did
        # not drop the previously collected value.
        assert failer.calls == 2
        assert loader.get_collected_values("price") == ["old"]
        assert loader.get_collected_values("currency") == []
        assert loader.load_item() == {"price": ["old"]}

    def test_failed_replace_without_transaction_keeps_existing_behavior(self):
        loader, _ = make_replace_loader()
        loader.add_value("price", "old")
        with pytest.raises(ValueError, match="FailOnce"):
            loader.replace_value("price", "new")
        # Without a transaction the old value is still dropped first.
        assert loader.get_collected_values("price") == []

    def test_dataclass_item(self):
        stats, control_stats = Stats(), Stats()
        selector = Selector(text=HTML)
        loader = ProductLoader(item=Product(), selector=selector, stats=stats)
        control = ProductLoader(item=Product(), selector=selector, stats=control_stats)
        failer = FailOnce()

        with pytest.raises(ValueError, match="FailOnce"):
            fill_and_load(loader, failer)

        item = fill_and_load(loader, failer)
        control_item = fill_and_load(control)
        assert item == control_item == Product(price="$10", currency="USD", stock="5")
        assert stats.values == control_stats.values

    def test_load_item_assignment_failure_rolls_back(self):
        product = Product()
        loader = ProductLoader(item=product)
        loader.add_value("name", "TV")
        with pytest.raises(KeyError):
            with loader.transaction():
                loader.add_value("unknown_field", "x")
                loader.load_item()
        # Both the collected values and the item fields are restored.
        assert loader.get_collected_values("unknown_field") == []
        assert product.name is None
        # The loader remains usable afterwards.
        loader.add_value("price", "$10")
        assert loader.load_item() == Product(name="TV", price="$10")

    def test_nested_loader_shares_transaction_pool(self):
        stats = Stats()
        loader = make_loader(stats=stats)
        loader.add_value("currency", "USD")
        failer = FailOnce()
        with pytest.raises(ValueError, match="FailOnce"):
            with loader.transaction():
                loader.add_css("price", "#price::text")
                nested = loader.nested_css("div.product")
                nested.add_css("stock", "#stock::text", failer)
        assert loader.get_collected_values("price") == []
        assert loader.get_collected_values("stock") == []
        assert loader.get_collected_values("currency") == ["USD"]
        assert stats.values == Counter()

    def test_transaction_started_on_nested_loader(self):
        loader = make_loader()
        loader.add_value("currency", "USD")
        nested = loader.nested_css("div.product")
        with pytest.raises(ValueError, match="FailOnce"):
            with nested.transaction():
                nested.add_css("price", "#price::text")
                nested.add_css("stock", "#stock::text", FailOnce())
        assert loader.get_collected_values("price") == []
        assert loader.get_collected_values("currency") == ["USD"]

    def test_inner_commit_still_bound_by_outer_transaction(self):
        loader = ItemLoader()
        with pytest.raises(ValueError, match="outer"):
            with loader.transaction():
                loader.add_value("a", 1)
                with loader.transaction():
                    loader.add_value("b", 2)
                loader.add_value("c", 3)
                raise ValueError("outer")
        assert loader.get_collected_values("a") == []
        assert loader.get_collected_values("b") == []
        assert loader.get_collected_values("c") == []

    def test_inner_rollback_does_not_affect_outer(self):
        loader = ItemLoader()
        with loader.transaction():
            loader.add_value("a", 1)
            with pytest.raises(ValueError, match="inner"):
                with loader.transaction():
                    loader.add_value("b", 2)
                    raise ValueError("inner")
            loader.add_value("c", 3)
        assert loader.get_collected_values("a") == [1]
        assert loader.get_collected_values("b") == []
        assert loader.get_collected_values("c") == [3]

    def test_stats_rolled_back_on_load_item_failure(self):
        stats, control_stats = Stats(), Stats()
        selector = Selector(text=HTML)
        loader = ProductLoader(item=Product(), selector=selector, stats=stats)
        control = ProductLoader(item=Product(), selector=selector, stats=control_stats)
        with pytest.raises(KeyError):
            with loader.transaction():
                loader.add_css("price", "#price::text")
                loader.add_value("unknown_field", "x")
                loader.load_item()
        with loader.transaction():
            loader.add_css("price", "#price::text")
            loader.load_item()
        with control.transaction():
            control.add_css("price", "#price::text")
            control.load_item()
        assert loader.item == control.item == Product(price="$10")
        assert stats.values == control_stats.values
