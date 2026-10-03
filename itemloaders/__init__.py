"""
Item Loader

See documentation in :ref:`declaring-loaders`.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

from itemadapter import ItemAdapter
from parsel import Selector  # noqa: TC002  # for sphinx
from parsel.utils import extract_regex, flatten

from itemloaders.common import wrap_loader_context
from itemloaders.processors import Identity
from itemloaders.tracking import (
    BatchInfo,
    DropInfo,
    ErrorInfo,
    ExpansionInfo,
    FieldTrace,
    SelectorRef,
    SourceInfo,
    StepInfo,
    TrackingEngine,
    ValueInfo,
)
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, MutableMapping
    from re import Pattern

    # typing.Self requires Python 3.11
    from typing_extensions import Self


class _StatsCollector(Protocol):
    def inc_value(self, key: str, count: int = 1) -> None: ...


def unbound_method(method: Callable[..., Any]) -> Callable[..., Any]:
    """
    Allow to use single-argument functions as input or output processors
    (no need to define an unused first 'self' argument)
    """
    with suppress(AttributeError):
        if "." not in method.__qualname__:
            return method.__func__  # type: ignore[attr-defined, no-any-return]
    return method


class ItemLoader:
    """
    Return a new Item Loader for populating the given item. If no item is
    given, one is instantiated automatically using the class in
    :attr:`default_item_class`.

    When instantiated with a ``selector`` parameter the :class:`ItemLoader` class
    provides convenient mechanisms for extracting data from web pages
    using parsel_ selectors.

    :param item: The item instance to populate using subsequent calls to
        :meth:`~ItemLoader.add_xpath`, :meth:`~ItemLoader.add_css`,
        :meth:`~ItemLoader.add_jmes` or :meth:`~ItemLoader.add_value`.
    :type item: :class:`dict` object

    :param selector: The selector to extract data from, when using the
        :meth:`add_xpath` (resp. :meth:`add_css`, :meth:`add_jmes`) or :meth:`replace_xpath`
        (resp. :meth:`replace_css`, :meth:`replace_jmes`) method.
    :type selector: :class:`~parsel.selector.Selector` object

    The item, selector and the remaining keyword arguments are
    assigned to the Loader context (accessible through the :attr:`context` attribute).

    Pass *stats* to keep track of which parsing rules are being used, as
    described in :ref:`rule-usage`.

    Pass ``track_sources=True`` to record, separately from the loaded values,
    where every field value comes from (selector expressions, input batches
    and processor steps). The traces are read back with
    :meth:`get_field_trace`; see :ref:`source-tracking`. The flag is inherited
    by nested loaders and defaults to off, in which case there is no extra
    processing.

    .. attribute:: item

        The item object being parsed by this Item Loader.
        This is mostly used as a property so when attempting to override this
        value, you may want to check out :attr:`default_item_class` first.

    .. attribute:: context

        The currently active :ref:`Context <loaders-context>` of this Item Loader.
        Refer to :ref:`loaders-context` for more information about the Loader Context.

    .. attribute:: default_item_class

        An Item class (or factory), used to instantiate items when not given in
        the ``__init__`` method.

        .. versionchanged:: VERSION
            The field defaults of an item instantiated this way are no longer
            treated as loaded values.

        .. warning:: Currently, this factory/class needs to be
            callable/instantiated without any arguments.
            If you are using ``dataclasses``, please consider the following
            alternative::

                from dataclasses import dataclass, field
                from typing import Optional

                @dataclass
                class Product:
                    name: Optional[str] = field(default=None)
                    price: Optional[float] = field(default=None)

    .. attribute:: default_input_processor

        The default input processor to use for those fields which don't specify
        one.

    .. attribute:: default_output_processor

        The default output processor to use for those fields which don't specify
        one.

    .. attribute:: selector

        The :class:`~parsel.selector.Selector` object to extract data from.
        It's the selector given in the ``__init__`` method.
        This attribute is meant to be read-only.

    .. _parsel: https://parsel.readthedocs.io/en/latest/
    """

    default_item_class: type = dict
    default_input_processor: Callable[..., Any] = Identity()
    default_output_processor: Callable[..., Any] = Identity()

    def __init__(
        self,
        item: Any = None,
        selector: Selector | None = None,
        parent: ItemLoader | None = None,
        stats: _StatsCollector | None = None,
        track_sources: bool | None = None,
        _selector_scope: tuple[SelectorRef, ...] = (),
        **context: Any,
    ):
        self.selector: Selector | None = selector
        self.stats: _StatsCollector | None = (
            stats if stats is not None or parent is None else parent.stats
        )
        context.update(selector=selector)
        # An item built here has no data, only the field defaults of its class,
        # which must not be mistaken for loaded values.
        has_initial_values = item is not None
        if item is None:
            item = self.default_item_class()
        self._local_item = item
        context["item"] = item
        self.context: MutableMapping[str, Any] = context
        self.parent: ItemLoader | None = parent
        self._local_values: dict[str, list[Any]] = {}
        if track_sources is None:
            track_sources = bool(parent and parent.tracking_enabled)
        self.tracking_enabled: bool = track_sources
        if parent is not None:
            self._trace_engine: TrackingEngine | None = parent._trace_engine
        elif track_sources:
            self._trace_engine = TrackingEngine(context)
        else:
            self._trace_engine = None
        self._selector_scope: tuple[SelectorRef, ...] = _selector_scope
        if has_initial_values:
            for field_name, value in ItemAdapter(item).items():
                self._values.setdefault(field_name, [])
                self._values[field_name] += arg_to_iter(value)
                if self._trace_engine is not None:
                    tracker = self._trace_engine.field(field_name)
                    nodes = self._trace_engine.initial_nodes(field_name, value)
                    tracker.nodes.extend(nodes)

    @property
    def _values(self) -> dict[str, list[Any]]:
        if self.parent is not None:
            return self.parent._values
        return self._local_values

    @property
    def item(self) -> Any:
        if self.parent is not None:
            return self.parent.item
        return self._local_item

    def nested_xpath(self, xpath: str, **context: Any) -> Self:
        """
        Create a nested loader with an xpath selector.
        The supplied selector is applied relative to selector associated
        with this :class:`ItemLoader`. The nested loader shares the item
        with the parent :class:`ItemLoader` so calls to :meth:`add_xpath`,
        :meth:`add_value`, :meth:`replace_value`, etc. will behave as expected.
        """
        self._check_selector_method()
        assert self.selector is not None
        selector = self.selector.xpath(xpath)
        context = self._get_nested_context(context)
        context.update(selector=selector)
        scope = (
            *self._selector_scope,
            SelectorRef(type="xpath", expression=xpath, index=0),
        )
        return self.__class__(
            item=self.item, parent=self, _selector_scope=scope, **context
        )

    def nested_css(self, css: str, **context: Any) -> Self:
        """
        Create a nested loader with a css selector.
        The supplied selector is applied relative to selector associated
        with this :class:`ItemLoader`. The nested loader shares the item
        with the parent :class:`ItemLoader` so calls to :meth:`add_xpath`,
        :meth:`add_value`, :meth:`replace_value`, etc. will behave as expected.
        """
        self._check_selector_method()
        assert self.selector is not None
        selector = self.selector.css(css)
        context = self._get_nested_context(context)
        context.update(selector=selector)
        scope = (
            *self._selector_scope,
            SelectorRef(type="css", expression=css, index=0),
        )
        return self.__class__(
            item=self.item, parent=self, _selector_scope=scope, **context
        )

    def _get_nested_context(self, context: dict[str, Any]) -> dict[str, Any]:
        inherited = {key: value for key, value in self.context.items() if key != "item"}
        inherited.update(context)
        return inherited

    def add_value(
        self,
        field_name: str | None,
        value: Any,
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Process and then add the given ``value`` for the given field.

        The value is first passed through :meth:`get_value` by giving the
        ``processors`` and ``kwargs``, and then passed through the
        :ref:`field input processor <processors>` and its result
        appended to the data collected for that field. If the field already
        contains collected data, the new data is added.

        The given ``field_name`` can be ``None``, in which case values for
        multiple fields may be added. And the processed value should be a dict
        with field_name mapped to values.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader

        Examples::

            loader.add_value('name', 'Color TV')
            loader.add_value('colours', ['white', 'blue'])
            loader.add_value('length', '100')
            loader.add_value('name', 'name: foo', TakeFirst(), re='name: (.+)')
            loader.add_value(None, {'name': 'foo', 'sex': 'male'})

        """
        if self._trace_engine is not None:
            return self._add_value_traced(
                field_name, value, processors, re, kw, action="add"
            )
        value = self.get_value(value, *processors, re=re, **kw)
        if value is None:
            return self
        if not field_name:
            for k, v in value.items():
                self._add_value(k, v)
        else:
            self._add_value(field_name, value)
        return self

    def replace_value(
        self,
        field_name: str | None,
        value: Any,
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`add_value` but replaces the collected data with the
        new value instead of adding it.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader
        """
        if self._trace_engine is not None:
            return self._add_value_traced(
                field_name, value, processors, re, kw, action="replace"
            )
        value = self.get_value(value, *processors, re=re, **kw)
        if value is None:
            return self
        if not field_name:
            for k, v in value.items():
                self._replace_value(k, v)
        else:
            self._replace_value(field_name, value)
        return self

    def _add_value_traced(
        self,
        field_name: str | None,
        value: Any,
        processors: tuple[Callable[..., Any], ...],
        re: str | Pattern[str] | None,
        kw: dict[str, Any],
        action: str,
    ) -> Self:
        from itemloaders.tracking import _State  # noqa: PLC0415

        engine = self._trace_engine
        assert engine is not None
        if field_name:
            _, nodes = engine.value_batch(value, field_name, action=action)
            initial_values = list(arg_to_iter(value))
            state = _State(initial_values, nodes, True, value)
            tracker = engine.field(field_name)
            state = self._run_traced_pipeline(
                processors, state, tracker, re=re, kw=kw, stage="add"
            )
            self._commit_input_batch(field_name, state, action)
            return self
        # field_name is None: mapping-style add, value must resolve to a dict.
        processed = self.get_value(value, *processors, re=re, **kw)
        if processed is None:
            return self
        for k, v in processed.items():
            # Values are already processed; attribute the batch per field.
            _, nodes = engine.value_batch(v, k, action=action)
            field_state = _State(list(arg_to_iter(v)), nodes, True, v)
            self._commit_input_batch(k, field_state, action)
        return self

    def _add_value(self, field_name: str, value: Any) -> None:
        value = arg_to_iter(value)
        processed_value = self._process_input_value(field_name, value)
        if processed_value:
            self._values.setdefault(field_name, [])
            self._values[field_name] += arg_to_iter(processed_value)

    def _replace_value(self, field_name: str, value: Any) -> None:
        self._values.pop(field_name, None)
        self._add_value(field_name, value)

    def _commit_input_batch(self, field_name: str, state: Any, action: str) -> None:
        """Apply the field input processor to the whole batch, mirroring
        :meth:`_add_value`, and store values alongside provenance nodes."""
        engine = self._trace_engine
        assert engine is not None
        tracker = engine.field(field_name)
        if action == "replace":
            self._values.pop(field_name, None)
            tracker.nodes.clear()
        if not state.values:
            return
        proc = self.get_input_processor(field_name)
        raw_input = state.input()
        try:
            state = engine.run_pipeline((proc,), state, tracker, stage="input")
        except ProcessorFailure as failure:
            inner = self._inner_error_text(proc, failure)
            raise ValueError(
                f"Error with input processor {proc.__class__.__name__}:"
                f" field={field_name!r} value={failure.value!r} error='{inner}'"
            ) from failure.original
        result = state.result()
        if result:
            self._values.setdefault(field_name, [])
            collected = list(arg_to_iter(result))
            stored_nodes = engine._align_nodes(collected, state.nodes)
            self._values[field_name] += collected
            tracker.nodes.extend(stored_nodes)
        else:
            engine.drop(
                tracker,
                reason="falsy_input_result",
                nodes=state.nodes,
                value=raw_input,
            )

    def _run_traced_pipeline(
        self,
        processors: tuple[Callable[..., Any], ...],
        state: Any,
        tracker: Any,
        *,
        re: str | Pattern[str] | None,
        kw: dict[str, Any],
        stage: str,
    ) -> Any:
        engine = self._trace_engine
        assert engine is not None
        if re:
            state = engine.flatten_regex(re, state, tracker, stage=stage)
        from itemloaders.tracking import ProcessorFailure  # noqa: PLC0415

        try:
            return engine.run_pipeline(processors, state, tracker, stage)
        except ProcessorFailure as failure:
            raise self._format_processor_failure(failure, stage) from failure.original

    @staticmethod
    def _inner_error_text(processor: Any, failure: Any) -> str:
        from itemloaders.processors import Compose, MapCompose  # noqa: PLC0415

        original = failure.original
        if isinstance(processor, MapCompose | Compose):
            return self._compose_error_text(processor, failure)
        return f"{type(original).__name__}: {original!s}"

    @staticmethod
    def _compose_error_text(processor: Any, failure: Any) -> str:
        original = failure.original
        return (
            f"Error in {processor.__class__.__name__} "
            f"with {failure.func!s} value={failure.func_value!r} "
            f"error='{type(original).__name__}: {original!s}'"
        )

    def _format_processor_failure(self, failure: Any, stage: str) -> ValueError:
        from itemloaders.processors import Compose, MapCompose  # noqa: PLC0415

        processor = failure.processor
        original = failure.original
        if isinstance(processor, MapCompose | Compose):
            inner = "ValueError: " + self._compose_error_text(processor, failure)
        else:
            inner = f"{type(original).__name__}: {original!s}"
        return ValueError(
            f"Error with processor {processor.__class__.__name__} "
            f"value={failure.value!r} error='{inner}'"
        )

    def get_value(
        self,
        value: Any,
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Any:
        """
        Process the given ``value`` by the given ``processors`` and keyword
        arguments.

        Available keyword arguments:

        :param re: a regular expression to use for extracting data from the
            given value using :func:`~parsel.utils.extract_regex` method,
            applied before processors
        :type re: str or typing.Pattern[str]

        Examples:

        >>> from itemloaders import ItemLoader
        >>> from itemloaders.processors import TakeFirst
        >>> loader = ItemLoader()
        >>> loader.get_value('name: foo', TakeFirst(), str.upper, re='name: (.+)')
        'FOO'
        """
        if re:
            value = arg_to_iter(value)
            value = flatten(extract_regex(re, x) for x in value)

        for proc in processors:
            if value is None:
                break
            _proc = proc
            proc = wrap_loader_context(proc, self.context)  # noqa: PLW2901
            try:
                value = proc(value)
            except Exception as e:
                raise ValueError(
                    f"Error with processor {_proc.__class__.__name__} "
                    f"value={value!r} error='{type(e).__name__}: {e!s}'"
                ) from e
        return value

    def load_item(self) -> Any:
        """
        Populate the item with the data collected so far, and return it. The
        data collected is first passed through the :ref:`output processors
        <processors>` to get the final value to assign to each item field.
        """
        adapter = ItemAdapter(self.item)
        for field_name in tuple(self._values):
            value = self.get_output_value(field_name)
            if value is not None:
                adapter[field_name] = value

        return adapter.item

    def get_output_value(self, field_name: str) -> Any:
        """
        Return the collected values parsed using the output processor, for the
        given field. This method doesn't populate or modify the item at all.
        """
        proc = self.get_output_processor(field_name)
        engine = self._trace_engine
        value = self._values.get(field_name, [])
        if engine is None:
            wrapped = wrap_loader_context(proc, self.context)
            try:
                return wrapped(value)
            except Exception as e:
                raise ValueError(
                    f"Error with output processor: field={field_name!r} "
                    f"value={value!r} error='{type(e).__name__}: {e!s}'"
                ) from e
        from itemloaders.tracking import ProcessorFailure, _State  # noqa: PLC0415

        tracker = engine.field(field_name)
        state = _State.from_nodes(list(tracker.nodes), many=True)
        try:
            state = engine.run_pipeline((proc,), state, tracker, stage="output")
        except ProcessorFailure as failure:
            inner = self._inner_error_text(proc, failure)
            raise ValueError(
                f"Error with output processor: field={field_name!r} "
                f"value={failure.value!r} error='{inner}'"
            ) from failure.original
        result: Any
        # Output processors return the final field value as-is: lists stay
        # lists, sets stay sets, scalars stay scalars (no arg_to_iter here).
        if state.nodes:
            result = state.nodes[0].value if not state.many else state.raw
        else:
            result = None
        tracker.output_nodes = list(state.nodes)
        tracker.output_many = state.many
        return result

    def get_collected_values(self, field_name: str) -> list[Any]:
        """Return the collected values for the given field."""
        return self._values.get(field_name, [])

    def get_field_trace(self, field_name: str) -> FieldTrace:
        """Return the value-origin trace for *field_name*.

        The returned :class:`~itemloaders.tracking.FieldTrace` lets you walk
        backwards from the current collected values (or the field output, once
        :meth:`load_item` or :meth:`get_output_value` has been called) to the
        selector expressions, input batches and processor steps that produced
        them.

        :raises RuntimeError: if the loader (and its parents, for nested
            loaders) was created without ``track_sources=True``.
        """
        engine = self._trace_engine
        if engine is None:
            raise RuntimeError(
                "Source tracking is disabled. Create the root loader with "
                "track_sources=True to inspect field traces."
            )
        return engine.field_trace(engine.field(field_name))

    def get_input_processor(self, field_name: str) -> Callable[..., Any]:
        proc = getattr(self, f"{field_name}_in", None)
        if not proc:
            proc = self._get_item_field_attr(
                field_name, "input_processor", self.default_input_processor
            )
        return unbound_method(proc)

    def get_output_processor(self, field_name: str) -> Callable[..., Any]:
        proc = getattr(self, f"{field_name}_out", None)
        if not proc:
            proc = self._get_item_field_attr(
                field_name, "output_processor", self.default_output_processor
            )
        return unbound_method(proc)

    def _get_item_field_attr(
        self, field_name: str, key: Any, default: Any = None
    ) -> Any:
        field_meta = ItemAdapter(self.item).get_field_meta(field_name)
        return field_meta.get(key, default)

    def _process_input_value(self, field_name: str, value: Any) -> Any:
        proc = self.get_input_processor(field_name)
        _proc = proc
        proc = wrap_loader_context(proc, self.context)
        try:
            return proc(value)
        except Exception as e:
            raise ValueError(
                f"Error with input processor {_proc.__class__.__name__}:"
                f" field={field_name!r} value={value!r} error='{type(e).__name__}: {e!s}'"
            ) from e

    def _track_rule(
        self, field_name: str | None, rule_type: str, rule: str, values: list[Any]
    ) -> list[Any]:
        if self.stats is not None:
            key = f"parser/{field_name or '<none>'}/{rule_type}/{rule}"
            self.stats.inc_value(key, 1 if values else 0)
        return values

    def _check_selector_method(self) -> None:
        if self.selector is None:
            raise RuntimeError(
                f"To use XPath or CSS selectors, {self.__class__.__name__} "
                f"must be instantiated with a selector"
            )

    def _add_selector_traced(
        self,
        type_: str,
        field_name: str | None,
        expressions: str | Iterable[str],
        processors: tuple[Callable[..., Any], ...],
        re: str | Pattern[str] | None,
        kw: dict[str, Any],
        action: str,
    ) -> Self:
        from itemloaders.tracking import _State  # noqa: PLC0415

        engine = self._trace_engine
        assert engine is not None
        if not field_name:
            extracted: list[Any] = []
            for expression in arg_to_iter(expressions):
                extracted.extend(
                    self._track_rule(
                        None,
                        type_,
                        expression,
                        self._extract_selector(type_, expression, kw),
                    )
                )
            target = (
                self.replace_value if action == "replace" else self.add_value
            )
            return target(None, extracted, *processors, re=re, **kw)
        expressions_list = list(arg_to_iter(expressions))
        tracker = engine.field(field_name)
        if action == "replace":
            self._values.pop(field_name, None)
            tracker.nodes.clear()
        states: list[Any] = []
        for expression in expressions_list:
            extracted = self._track_rule(
                field_name,
                type_,
                expression,
                self._extract_selector(type_, expression, kw),
            )
            chain = (
                *self._selector_scope,
                SelectorRef(type=type_, expression=expression, index=0),
            )
            batch, nodes = engine.selector_entries(
                type_,
                expression,
                extracted,
                chain,
                field_name,
                action,
                kind=type_,
            )
            state = _State(list(extracted), nodes, True, list(extracted))
            state = self._run_traced_pipeline(
                processors, state, tracker, re=re, kw=kw, stage="add"
            )
            states.append(state)
        merged_values: list[Any] = []
        merged_nodes: list[Any] = []
        for state in states:
            merged_values.extend(state.values)
            merged_nodes.extend(state.nodes)
        # The per-call processors already ran above; run the field input
        # processor once on the whole merged batch, as the untracked path does.
        merged = _State(list(merged_values), list(merged_nodes), True, list(merged_values))
        merged.raw = merged_values
        self._commit_input_batch(field_name, merged, action="add")
        return self

    def _extract_selector(self, type_: str, expression: str, kw: dict[str, Any]) -> list[Any]:
        assert self.selector is not None
        if type_ == "xpath":
            return self.selector.xpath(expression, **kw).getall()
        if type_ == "css":
            return self.selector.css(expression).getall()
        return self.selector.jmespath(expression).getall()

    def add_xpath(
        self,
        field_name: str | None,
        xpath: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`ItemLoader.add_value` but receives an XPath instead of a
        value, which is used to extract a list of strings from the
        selector associated with this :class:`ItemLoader`.

        *xpath* may also be an iterable of XPath expressions, in which case the
        strings extracted by all of them are collected.

        See :meth:`get_xpath` for ``kwargs``.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader

        Examples::

            # HTML snippet: <p class="product-name">Color TV</p>
            loader.add_xpath('name', '//p[@class="product-name"]')
            # HTML snippet: <p id="price">the price is $1200</p>
            loader.add_xpath('price', '//p[@id="price"]', re='the price is (.*)')

        """
        if self._trace_engine is not None:
            return self._add_selector_traced(
                "xpath", field_name, xpath, processors, re, kw, action="add"
            )
        values = self._get_xpathvalues(xpath, field_name, **kw)
        return self.add_value(field_name, values, *processors, re=re, **kw)

    def replace_xpath(
        self,
        field_name: str | None,
        xpath: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`add_xpath` but replaces collected data instead of adding it.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader

        """
        if self._trace_engine is not None:
            return self._add_selector_traced(
                "xpath",
                field_name,
                xpath,
                processors,
                re,
                kw,
                action="replace",
            )
        values = self._get_xpathvalues(xpath, field_name, **kw)
        return self.replace_value(field_name, values, *processors, re=re, **kw)

    def get_xpath(
        self,
        xpath: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Any:
        """
        Similar to :meth:`ItemLoader.get_value` but receives an XPath instead of a
        value, which is used to extract a list of strings from the
        selector associated with this :class:`ItemLoader`.

        *xpath* may also be an iterable of XPath expressions, in which case the
        strings extracted by all of them are collected.

        *re* is a regular expression, as a string or a compiled pattern, used to
        further extract data from the selected XPath region.

        Examples::

            # HTML snippet: <p class="product-name">Color TV</p>
            loader.get_xpath('//p[@class="product-name"]')
            # HTML snippet: <p id="price">the price is $1200</p>
            loader.get_xpath('//p[@id="price"]', TakeFirst(), re='the price is (.*)')

        """
        values = self._get_xpathvalues(xpath, **kw)
        return self.get_value(values, *processors, re=re, **kw)

    def _get_xpathvalues(
        self,
        xpaths: str | Iterable[str],
        field_name: str | None = None,
        **kw: Any,
    ) -> list[Any]:
        self._check_selector_method()
        assert self.selector is not None
        xpaths = arg_to_iter(xpaths)
        return flatten(
            self._track_rule(
                field_name, "xpath", xpath, self.selector.xpath(xpath, **kw).getall()
            )
            for xpath in xpaths
        )

    def add_css(
        self,
        field_name: str | None,
        css: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`ItemLoader.add_value` but receives a CSS selector
        instead of a value, which is used to extract a list of strings
        from the selector associated with this :class:`ItemLoader`.

        *css* may also be an iterable of CSS selectors, in which case the
        strings extracted by all of them are collected.

        See :meth:`get_css` for ``kwargs``.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader

        Examples::

            # HTML snippet: <p class="product-name">Color TV</p>
            loader.add_css('name', 'p.product-name')
            # HTML snippet: <p id="price">the price is $1200</p>
            loader.add_css('price', 'p#price', re='the price is (.*)')

        """
        if self._trace_engine is not None:
            return self._add_selector_traced(
                "css", field_name, css, processors, re, kw, action="add"
            )
        values = self._get_cssvalues(css, field_name)
        return self.add_value(field_name, values, *processors, re=re, **kw)

    def replace_css(
        self,
        field_name: str | None,
        css: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`add_css` but replaces collected data instead of adding it.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader

        """
        if self._trace_engine is not None:
            return self._add_selector_traced(
                "css", field_name, css, processors, re, kw, action="replace"
            )
        values = self._get_cssvalues(css, field_name)
        return self.replace_value(field_name, values, *processors, re=re, **kw)

    def get_css(
        self,
        css: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Any:
        """
        Similar to :meth:`ItemLoader.get_value` but receives a CSS selector
        instead of a value, which is used to extract a list of strings
        from the selector associated with this :class:`ItemLoader`.

        *css* may also be an iterable of CSS selectors, in which case the
        strings extracted by all of them are collected.

        *re* is a regular expression, as a string or a compiled pattern, used to
        further extract data from the selected CSS region.

        Examples::

            # HTML snippet: <p class="product-name">Color TV</p>
            loader.get_css('p.product-name')
            # HTML snippet: <p id="price">the price is $1200</p>
            loader.get_css('p#price', TakeFirst(), re='the price is (.*)')
        """
        values = self._get_cssvalues(css)
        return self.get_value(values, *processors, re=re, **kw)

    def _get_cssvalues(
        self, csss: str | Iterable[str], field_name: str | None = None
    ) -> list[Any]:
        self._check_selector_method()
        assert self.selector is not None
        csss = arg_to_iter(csss)
        return flatten(
            self._track_rule(field_name, "css", css, self.selector.css(css).getall())
            for css in csss
        )

    def add_jmes(
        self,
        field_name: str | None,
        jmes: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`ItemLoader.add_value` but receives a JMESPath selector
        instead of a value, which is used to extract a list of strings
        from the selector associated with this :class:`ItemLoader`.

        *jmes* may also be an iterable of JMESPath selectors, in which case the
        strings extracted by all of them are collected.

        See :meth:`get_jmes` for ``kwargs``.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader

        Examples::

            # JSON snippet: {"name": "Color TV"}
            loader.add_jmes('name', 'name')
            # JSON snippet: {"price": "the price is $1200"}
            loader.add_jmes('price', 'price', TakeFirst(), re='the price is (.*)')
        """
        if self._trace_engine is not None:
            return self._add_selector_traced(
                "jmes", field_name, jmes, processors, re, kw, action="add"
            )
        values = self._get_jmesvalues(jmes, field_name)
        return self.add_value(field_name, values, *processors, re=re, **kw)

    def replace_jmes(
        self,
        field_name: str | None,
        jmes: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Self:
        """
        Similar to :meth:`add_jmes` but replaces collected data instead of adding it.

        :returns: The current ItemLoader instance for method chaining.
        :rtype: ItemLoader
        """
        if self._trace_engine is not None:
            return self._add_selector_traced(
                "jmes", field_name, jmes, processors, re, kw, action="replace"
            )
        values = self._get_jmesvalues(jmes, field_name)
        return self.replace_value(field_name, values, *processors, re=re, **kw)

    def get_jmes(
        self,
        jmes: str | Iterable[str],
        *processors: Callable[..., Any],
        re: str | Pattern[str] | None = None,
        **kw: Any,
    ) -> Any:
        """
        Similar to :meth:`ItemLoader.get_value` but receives a JMESPath selector
        instead of a value, which is used to extract a list of strings
        from the selector associated with this :class:`ItemLoader`.

        *jmes* may also be an iterable of JMESPath selectors, in which case the
        strings extracted by all of them are collected.

        *re* is a regular expression, as a string or a compiled pattern, used to
        further extract data from the selected JMESPath.

        Examples::

            # JSON snippet: {"name": "Color TV"}
            loader.get_jmes('name')
            # JSON snippet: {"price": "the price is $1200"}
            loader.get_jmes('price', TakeFirst(), re='the price is (.*)')
        """
        values = self._get_jmesvalues(jmes)
        return self.get_value(values, *processors, re=re, **kw)

    def _get_jmesvalues(
        self, jmess: str | Iterable[str], field_name: str | None = None
    ) -> list[Any]:
        self._check_selector_method()
        assert self.selector is not None
        jmess = arg_to_iter(jmess)
        if not hasattr(self.selector, "jmespath"):
            raise AttributeError(
                "Please install parsel >= 1.8.1 to get JMESPath support"
            )
        return flatten(
            self._track_rule(
                field_name, "jmes", jmes, self.selector.jmespath(jmes).getall()
            )
            for jmes in jmess
        )
