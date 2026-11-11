from functools import partial

import pytest

from itemloaders import ItemLoader
from itemloaders.processors import (
    Compose,
    Identity,
    Join,
    MapCompose,
    TakeFirst,
    batch,
)


def test_take_first():
    proc = TakeFirst()
    assert proc([None, "", "hello", "world"]) == "hello"
    assert proc([None, "", 0, "hello", "world"]) == 0


def test_identity():
    proc = Identity()
    assert proc([None, "", "hello", "world"]) == [None, "", "hello", "world"]


def test_join():
    proc = Join()
    with pytest.raises(TypeError):
        proc([None, "", "hello", "world"])
    assert proc(["", "hello", "world"]) == " hello world"
    assert proc(["hello", "world"]) == "hello world"
    assert isinstance(proc(["hello", "world"]), str)


def test_compose():
    proc = Compose(lambda v: v[0], str.upper)
    assert proc(["hello", "world"]) == "HELLO"
    proc = Compose(str.upper)
    assert proc(None) is None
    proc = Compose(str.upper, stop_on_none=False)
    with pytest.raises(
        ValueError,
        match=r"Error in Compose with .* error='TypeError: (descriptor 'upper'|'str' object expected)",
    ):
        proc(None)
    proc = Compose(str.upper, lambda x: x + 1)
    with pytest.raises(
        ValueError,
        match=r"Error in Compose with .* error='TypeError: (can only|unsupported operand)",
    ):
        proc("hello")


def test_mapcompose():
    def filter_world(x):
        return None if x == "world" else x

    proc = MapCompose(filter_world, str.upper)
    assert proc(["hello", "world", "this", "is", "scrapy"]) == [
        "HELLO",
        "THIS",
        "IS",
        "SCRAPY",
    ]
    proc = MapCompose(filter_world, str.upper)
    assert proc(None) == []
    proc = MapCompose(filter_world, str.upper)
    with pytest.raises(
        ValueError,
        match=r"Error in MapCompose with .* error='TypeError: (descriptor 'upper'|'str' object expected)",
    ):
        proc([1])
    proc = MapCompose(filter_world, lambda x: x + 1)
    with pytest.raises(
        ValueError,
        match=r"Error in MapCompose with .* error='TypeError: (can only|unsupported operand)",
    ):
        proc("hello")


@pytest.mark.parametrize("processor", [Compose, MapCompose])
def test_partial_processor_keeps_context_with_matching_value(processor):
    def prefix_text(prefix, value, loader_context):
        return prefix + value + loader_context["suffix"]

    proc = processor(partial(prefix_text, "loader_context"))
    value = ["text"] if processor is MapCompose else "text"
    expected = (
        ["loader_contexttext!"] if processor is MapCompose else "loader_contexttext!"
    )
    assert proc(value, {"suffix": "!"}) == expected


def test_mapcompose_batch_matches_per_value_chain():
    values = [f"  value {i}  " for i in range(25)]
    per_value_calls = 0
    batch_calls = 0

    def clean(value):
        nonlocal per_value_calls
        per_value_calls += 1
        return value.strip()

    @batch
    def clean_batch(batch_values):
        nonlocal batch_calls
        batch_calls += 1
        return [value.strip() for value in batch_values]

    old_chain = MapCompose(clean, str.upper)
    new_chain = MapCompose(clean_batch, str.upper, batch_size=10)
    assert new_chain(values) == old_chain(values)
    assert per_value_calls == len(values)
    assert batch_calls == 3  # batches of 10, 10 and 5


def test_mapcompose_batch_multiple_outputs_and_filtering():
    @batch
    def split_and_filter(batch_values):
        return [
            None if "skip" in value else value.split()
            for value in batch_values
        ]

    proc = MapCompose(split_and_filter, str.upper, batch_size=2)
    assert proc(["a b", "skip me", "c"]) == ["A", "B", "C"]


def test_mapcompose_batch_partial_last_batch():
    batches_seen = []

    @batch
    def identity_batch(batch_values):
        batches_seen.append(list(batch_values))
        return batch_values

    proc = MapCompose(identity_batch, batch_size=4)
    assert proc([1, 2, 3, 4, 5, 6]) == [1, 2, 3, 4, 5, 6]
    assert batches_seen == [[1, 2, 3, 4], [5, 6]]


def test_mapcompose_batch_state_is_not_shared_between_batches():
    batches_seen = []

    @batch
    def mutating_batch(batch_values):
        batches_seen.append(list(batch_values))
        size = len(batch_values)
        batch_values.clear()  # mutating one batch must not affect later batches
        return [str(i) for i in range(size)]

    proc = MapCompose(mutating_batch, batch_size=2)
    assert proc([1, 2, 3, 4]) == ["0", "1", "0", "1"]
    assert batches_seen == [[1, 2], [3, 4]]


def test_mapcompose_batch_consumes_input_once():
    consumed = []

    def source():
        for i in range(5):
            consumed.append(i)
            yield f" {i} "

    @batch
    def clean_batch(batch_values):
        return [value.strip() for value in batch_values]

    proc = MapCompose(clean_batch, batch_size=2)
    assert proc(source()) == ["0", "1", "2", "3", "4"]
    assert consumed == [0, 1, 2, 3, 4]


def test_mapcompose_batch_result_length_mismatch():
    @batch
    def dropping(batch_values):
        return batch_values[:-1]  # one result short

    proc = MapCompose(dropping, batch_size=3)
    with pytest.raises(
        ValueError,
        match=r"dropping.*2 results for 3 input values at offset 0: \['a', 'b', 'c'\]",
    ):
        proc(["a", "b", "c"])


def test_mapcompose_batch_size_must_be_positive():
    with pytest.raises(ValueError, match="batch_size"):
        MapCompose(str.upper, batch_size=0)


def test_mapcompose_batch_loader_context():
    batches_seen = []

    @batch
    def prefix_batch(batch_values, loader_context):
        batches_seen.append(list(batch_values))
        return [loader_context["prefix"] + value for value in batch_values]

    proc = MapCompose(prefix_batch, prefix=">", batch_size=2)
    assert proc(["a", "b", "c"]) == [">a", ">b", ">c"]
    # the marker must survive the loader-context partial wrapping
    assert batches_seen == [["a", "b"], ["c"]]
    assert proc(["a"], {"prefix": "<"}) == ["<a"]


def test_mapcompose_batch_error_is_atomic_in_loader():
    @batch
    def failing_batch(batch_values):
        if batch_values == ["c", "d"]:
            raise RuntimeError("boom")
        return batch_values  # the first batch succeeds but must not leak

    class ProductLoader(ItemLoader):
        default_item_class = dict
        name_in = MapCompose(str.strip)
        tags_in = MapCompose(failing_batch, batch_size=2)

    loader = ProductLoader()
    loader.add_value("name", "  kept  ")
    with pytest.raises(ValueError, match="failing_batch"):
        loader.add_value("tags", ["a", "b", "c", "d"])
    assert loader.get_collected_values("tags") == []
    assert loader.load_item() == {"name": ["kept"]}
