"""
This module provides some commonly used processors for Item Loaders.

See documentation in :ref:`declaring-loaders`.
"""

from __future__ import annotations

from collections import ChainMap
from itertools import islice
from typing import TYPE_CHECKING, Any

from itemloaders.common import wrap_loader_context
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, MutableMapping


class BatchProcessor:
    """
    Wraps a callable so that :class:`MapCompose` applies it to batches of
    values instead of to one value at a time.

    The wrapped callable receives a list of at most ``batch_size`` values and
    must return one result per input value, as an iterable of the same
    length. Each result is then flattened like the return value of a regular
    :class:`MapCompose` function: ``None`` produces no output value, a single
    value produces one output value, and an iterable produces multiple output
    values. Output values keep the order of the input values.

    The input iterable is consumed only once, in a single pass, and each
    batch is processed by an independent call: no state is shared between
    batches by this wrapper, so the wrapped callable must not rely on
    previous batches either.

    If the callable raises, or returns a number of results different from the
    number of input values in the batch, :class:`MapCompose` raises a
    :exc:`ValueError` reporting the processor and the input positions of the
    offending batch, and produces no partial output for that call.

    Example:

    >>> def uppercase_batch(values):
    ...     return [v.upper() for v in values]
    ...
    >>> from itemloaders.processors import BatchProcessor, MapCompose
    >>> proc = MapCompose(BatchProcessor(uppercase_batch, batch_size=2))
    >>> proc(['hello', 'world', 'scrapy'])
    ['HELLO', 'WORLD', 'SCRAPY']
    """

    def __init__(self, function: Callable[..., Any], batch_size: int = 100):
        if not callable(function):
            raise TypeError(
                f"BatchProcessor expects a callable, got {type(function).__name__!r}"
            )
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError(
                f"batch_size must be an integer, got {type(batch_size).__name__!r}"
            )
        if batch_size < 1:
            raise ValueError(f"batch_size must be a positive integer, got {batch_size}")
        self.function = function
        self.batch_size = batch_size

    def __call__(self, values: Any) -> Any:
        return self.function(values)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.function!r}, batch_size={self.batch_size})"

    __str__ = __repr__


class MapCompose:
    """
    A processor which is constructed from the composition of the given
    functions, similar to the :class:`Compose` processor. The difference with
    this processor is the way internal results are passed among functions,
    which is as follows:

    The input value of this processor is *iterated* and the first function is
    applied to each element. The results of these function calls (one for each element)
    are concatenated to construct a new iterable, which is then used to apply the
    second function, and so on, until the last function is applied to each
    value of the list of values collected so far. The output values of the last
    function are concatenated together to produce the output of this processor.

    Each particular function can return a value or a list of values, which is
    flattened with the list of values returned by the same function applied to
    the other input values. The functions can also return ``None`` in which
    case the output of that function is ignored for further processing over the
    chain.

    This processor provides a convenient way to compose functions that only
    work with single values (instead of iterables). For this reason the
    :class:`MapCompose` processor is typically used as input processor, since
    data is often extracted using the
    :meth:`~parsel.selector.Selector.extract` method of `parsel selectors`_,
    which returns a list of strings.

    The example below should clarify how it works:

    >>> def filter_world(x):
    ...     return None if x == 'world' else x
    ...
    >>> from itemloaders.processors import MapCompose
    >>> proc = MapCompose(filter_world, str.upper)
    >>> proc(['hello', 'world', 'this', 'is', 'something'])
    ['HELLO', 'THIS', 'IS', 'SOMETHING']

    Functions wrapped in :class:`BatchProcessor` are applied to batches of
    values instead of single values, and can be freely mixed with regular
    per-value functions, which do not need any signature change:

    >>> def upper_batch(values):
    ...     return [v.upper() for v in values]
    ...
    >>> proc = MapCompose(filter_world, BatchProcessor(upper_batch, batch_size=2))
    >>> proc(['hello', 'world', 'this'])
    ['HELLO', 'THIS']

    As with the Compose processor, functions can receive Loader contexts, and
    ``__init__`` method keyword arguments are used as default context values.
    See :class:`Compose` processor for more info.

    .. _`parsel selectors`: https://parsel.readthedocs.io/en/latest/parsel.html#parsel.selector.Selector.extract
    """

    def __init__(self, *functions: Callable[..., Any], **default_loader_context: Any):
        self.functions = functions
        self.default_loader_context = default_loader_context

    def __call__(
        self, value: Any, loader_context: MutableMapping[str, Any] | None = None
    ) -> Iterable[Any]:
        values = arg_to_iter(value)
        context: MutableMapping[str, Any]
        if loader_context:
            context = ChainMap(loader_context, self.default_loader_context)
        else:
            context = self.default_loader_context
        for func in self.functions:
            if isinstance(func, BatchProcessor):
                values = self._process_batches(func, values, context, value)
                continue
            func = wrap_loader_context(func, context)
            next_values: list[Any] = []
            for v in values:
                try:
                    next_values += arg_to_iter(func(v))
                except Exception as e:
                    raise ValueError(
                        f"Error in MapCompose with "
                        f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
                    ) from e
            values = next_values
        return values

    @staticmethod
    def _process_batches(
        processor: BatchProcessor,
        values: Iterable[Any],
        context: MutableMapping[str, Any],
        original_value: Any,
    ) -> list[Any]:
        func = wrap_loader_context(processor.function, context)
        next_values: list[Any] = []
        position = 0
        iterator = iter(values)
        while True:
            batch = list(islice(iterator, processor.batch_size))
            if not batch:
                break
            try:
                results = func(batch)
            except Exception as e:
                raise ValueError(
                    f"Error in MapCompose with "
                    f"{processor!s} value={original_value!r} "
                    f"error='{type(e).__name__}: {e!s}'"
                ) from e
            try:
                results = list(results)
            except TypeError as e:
                raise ValueError(
                    f"Error in MapCompose with {processor!s}: batch processor "
                    f"returned a non-iterable result for the batch of "
                    f"{len(batch)} input values at positions "
                    f"{position}-{position + len(batch) - 1}"
                ) from e
            if len(results) != len(batch):
                raise ValueError(
                    f"Error in MapCompose with {processor!s}: batch processor "
                    f"returned {len(results)} results for {len(batch)} input "
                    f"values at positions {position}-{position + len(batch) - 1}"
                )
            batch_values: list[Any] = []
            for result in results:
                batch_values += arg_to_iter(result)
            next_values += batch_values
            position += len(batch)
        return next_values


class Compose:
    """
    A processor which is constructed from the composition of the given
    functions. This means that each input value of this processor is passed to
    the first function, and the result of that function is passed to the second
    function, and so on, until the last function returns the output value of
    this processor.

    By default, stop process on ``None`` value. This behaviour can be changed by
    passing keyword argument ``stop_on_none=False``.

    Example:

    >>> from itemloaders.processors import Compose
    >>> proc = Compose(lambda v: v[0], str.upper)
    >>> proc(['hello', 'world'])
    'HELLO'

    Each function can optionally receive a ``loader_context`` parameter. For
    those which do, this processor will pass the currently active :ref:`Loader
    context <loaders-context>` through that parameter.

    The keyword arguments passed in the ``__init__`` method are used as the default
    Loader context values passed to each function call. However, the final
    Loader context values passed to functions are overridden with the currently
    active Loader context accessible through the :attr:`ItemLoader.context
    <itemloaders.ItemLoader.context>` attribute.
    """

    def __init__(self, *functions: Callable[..., Any], **default_loader_context: Any):
        self.functions = functions
        self.stop_on_none = default_loader_context.get("stop_on_none", True)
        self.default_loader_context = default_loader_context

    def __call__(
        self, value: Any, loader_context: MutableMapping[str, Any] | None = None
    ) -> Any:
        context: MutableMapping[str, Any]
        if loader_context:
            context = ChainMap(loader_context, self.default_loader_context)
        else:
            context = self.default_loader_context
        wrapped_funcs = [wrap_loader_context(f, context) for f in self.functions]
        for func in wrapped_funcs:
            if value is None and self.stop_on_none:
                break
            try:
                value = func(value)
            except Exception as e:
                raise ValueError(
                    f"Error in Compose with "
                    f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
                ) from e
        return value


class TakeFirst:
    """
    Returns the first non-null/non-empty value from the values received,
    so it's typically used as an output processor to single-valued fields.
    It doesn't receive any ``__init__`` method arguments, nor does it accept Loader contexts.

    Example:

    >>> from itemloaders.processors import TakeFirst
    >>> proc = TakeFirst()
    >>> proc(['', 'one', 'two', 'three'])
    'one'
    """

    def __call__(self, values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None


class Identity:
    """
    The simplest processor, which doesn't do anything. It returns the original
    values unchanged. It doesn't receive any ``__init__`` method arguments, nor does it
    accept Loader contexts.

    Example:

    >>> from itemloaders.processors import Identity
    >>> proc = Identity()
    >>> proc(['one', 'two', 'three'])
    ['one', 'two', 'three']
    """

    def __call__(self, values: Any) -> Any:
        return values


class SelectJmes:
    """
    Query the data structure for a *JMESPath* expression (given at instantiation), and return the answer.

    *Requires*: `JMESPath <https://github.com/jmespath/jmespath>`__

    .. note::

        ``SelectJmes`` accepts only one input element at a time.

    Example:

    >>> from itemloaders.processors import SelectJmes, Compose, MapCompose
    >>> proc = SelectJmes("foo") #for direct use on lists and dictionaries
    >>> proc({'foo': 'bar'})
    'bar'
    >>> proc({'foo': {'bar': 'baz'}})
    {'bar': 'baz'}

    Working with JSON:

    >>> import json
    >>> proc_single_json_str = Compose(json.loads, SelectJmes("foo"))
    >>> proc_single_json_str('{"foo": "bar"}')
    'bar'
    >>> proc_json_list = Compose(json.loads, MapCompose(SelectJmes('foo')))
    >>> proc_json_list('[{"foo":"bar"}, {"baz":"tar"}]')
    ['bar']
    """

    def __init__(self, json_path: str):
        self.json_path: str = json_path
        import jmespath.parser  # noqa: PLC0415

        self.compiled_path: jmespath.parser.ParsedResult = jmespath.compile(
            self.json_path
        )

    def __call__(self, value: Any) -> Any:
        """Query value for the JMESPath query and return answer
        :param value: a data structure (dict, list) to extract from
        :return: Element extracted according to JMESPath query
        """
        return self.compiled_path.search(value)


class Join:
    """
    Returns the values joined with the separator given in the ``__init__`` method, which
    defaults to ``' '``. It doesn't accept Loader contexts.

    When using the default separator, this processor is equivalent to the
    function: ``' '.join``

    Examples:

    >>> from itemloaders.processors import Join
    >>> proc = Join()
    >>> proc(['one', 'two', 'three'])
    'one two three'
    >>> proc = Join('<br>')
    >>> proc(['one', 'two', 'three'])
    'one<br>two<br>three'
    """

    def __init__(self, separator: str = " "):
        self.separator = separator

    def __call__(self, values: Any) -> str:
        return self.separator.join(values)
