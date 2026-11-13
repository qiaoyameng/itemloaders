from functools import partial

import pytest

from itemloaders.processors import (
    BatchProcessor,
    Compose,
    Identity,
    Join,
    MapCompose,
    TakeFirst,
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


def test_batch_processor_validation():
    with pytest.raises(TypeError, match="expects a callable"):
        BatchProcessor("not callable")
    with pytest.raises(TypeError, match="batch_size must be an integer"):
        BatchProcessor(str.upper, batch_size="10")
    with pytest.raises(ValueError, match="batch_size must be a positive integer"):
        BatchProcessor(str.upper, batch_size=0)


def test_mapcompose_batch_matches_per_value_chain():
    single_calls = 0
    batch_calls = 0
    batch_sizes = []

    def clean(value):
        nonlocal single_calls
        single_calls += 1
        return value.strip().lower()

    def clean_batch(values):
        nonlocal batch_calls
        batch_calls += 1
        batch_sizes.append(len(values))
        return [value.strip().lower() for value in values]

    data = [" A", "B ", " c ", "D", " e "]
    old_proc = MapCompose(clean, str.upper)
    new_proc = MapCompose(BatchProcessor(clean_batch, batch_size=2), str.upper)
    assert new_proc(data) == old_proc(data)
    assert single_calls == len(data)
    assert batch_calls == 3
    assert batch_sizes == [2, 2, 1]  # last batch is partial


def test_mapcompose_batch_mixed_with_per_value_functions():
    def tag_batch(values):
        return [f"<{value}>" for value in values]

    proc = MapCompose(
        str.strip,
        BatchProcessor(tag_batch, batch_size=2),
        lambda value: value.upper(),
    )
    assert proc([" a ", "b ", " c"]) == ["<A>", "<B>", "<C>"]


def test_mapcompose_batch_one_input_many_outputs():
    def split_batch(values):
        return [value.split() for value in values]

    proc = MapCompose(BatchProcessor(split_batch, batch_size=10))
    assert proc(["a b", "c", "d e f"]) == ["a", "b", "c", "d", "e", "f"]


def test_mapcompose_batch_filters_values():
    def drop_even_batch(values):
        return [None if value % 2 == 0 else value for value in values]

    proc = MapCompose(BatchProcessor(drop_even_batch, batch_size=2))
    assert proc([1, 2, 3, 4, 5]) == [1, 3, 5]


def test_mapcompose_batch_consumes_input_once():
    produced = []

    def values():
        for i in range(5):
            produced.append(i)
            yield str(i)

    def identity_batch(batch):
        return list(batch)

    proc = MapCompose(BatchProcessor(identity_batch, batch_size=2))
    assert proc(values()) == ["0", "1", "2", "3", "4"]
    assert produced == [0, 1, 2, 3, 4]


def test_mapcompose_batch_result_length_mismatch():
    def shrinking_batch(values):
        return values[:-1]

    proc = MapCompose(BatchProcessor(shrinking_batch, batch_size=3))
    with pytest.raises(
        ValueError,
        match=r"returned 2 results for 3 input values at positions 0-2",
    ):
        proc(["a", "b", "c"])

    def lazy_shrinking_batch(values):
        return values if len(values) < 3 else values[:-1]

    proc = MapCompose(BatchProcessor(lazy_shrinking_batch, batch_size=3))
    with pytest.raises(
        ValueError,
        match=r"returned 2 results for 3 input values at positions 0-2",
    ):
        proc(["a", "b", "c", "d"])


def test_mapcompose_batch_result_not_iterable():
    def scalar_batch(values):
        return 42

    proc = MapCompose(BatchProcessor(scalar_batch, batch_size=2))
    with pytest.raises(ValueError, match="non-iterable result"):
        proc(["a", "b"])


def test_mapcompose_batch_error_reports_processor_and_value():
    def failing_batch(values):
        raise RuntimeError("boom")

    proc = MapCompose(BatchProcessor(failing_batch, batch_size=2))
    with pytest.raises(
        ValueError,
        match=r"Error in MapCompose with BatchProcessor\(.*batch_size=2\)"
        r" value=\['a', 'b'\] error='RuntimeError: boom'",
    ):
        proc(["a", "b"])


def test_mapcompose_batch_error_leaves_loader_without_partial_values():
    from itemloaders import ItemLoader

    def failing_batch(values):
        if "bad" in values:
            raise RuntimeError("boom")
        return [value.upper() for value in values]

    class MyLoader(ItemLoader):
        default_item_class = dict
        default_input_processor = MapCompose(
            BatchProcessor(failing_batch, batch_size=2)
        )
        default_output_processor = Identity()

    loader = MyLoader()
    with pytest.raises(ValueError, match="RuntimeError: boom"):
        loader.add_value("name", ["ok1", "ok2", "bad", "ok3"])
    assert loader.get_collected_values("name") == []

    loader = MyLoader()
    loader.add_value("name", ["ok1", "ok2", "ok3"])
    assert loader.get_collected_values("name") == ["OK1", "OK2", "OK3"]


def test_mapcompose_batch_loader_context():
    def suffix_batch(values, loader_context):
        return [value + loader_context["suffix"] for value in values]

    proc = MapCompose(BatchProcessor(suffix_batch, batch_size=2))
    assert proc(["a", "b", "c"], {"suffix": "!"}) == ["a!", "b!", "c!"]
