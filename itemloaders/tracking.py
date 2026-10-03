"""Optional value-origin tracking for :class:`~itemloaders.ItemLoader`.

The tracking machinery in this module is only used when a loader is created
with ``track_sources=True``.  The regular processor pipeline never receives
wrapped objects: every processor is invoked with the exact same plain values
it would receive with tracking disabled, while provenance is kept in parallel
data structures.

The tracked graph is made of three kinds of nodes:

* sources -- one origin expression (xpath/css/jmes together with the nested
  selector chain) or a plain ``add_value`` call;
* batches -- one extraction/application event, grouping the values brought in
  by a single ``add_xpath``/``add_css``/``add_jmes``/``add_value`` call;
* steps -- one application of one processor (a MapCompose function, a Compose
  function, TakeFirst, Join, or an opaque/custom processor).

Collected values are mirrored by node objects: a node points at the steps
that produced it and, transitively, at the sources and batches of every
value that contributed to it.  When the exact correspondence cannot be
determined (custom processors, aggregation of several inputs) the node
records the whole contributing batch instead of pretending precision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from itemloaders.common import wrap_loader_context
from itemloaders.processors import Compose, Identity, Join, MapCompose, TakeFirst
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping, Sequence


# ---------------------------------------------------------------------------
# Public read-only snapshot types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SelectorRef:
    """One selector expression applied when extracting a value."""

    type: str
    expression: str
    index: int


@dataclass(frozen=True)
class SourceInfo:
    """A single value origin."""

    id: int
    type: str
    expression: str | None
    index: int
    chain: tuple[SelectorRef, ...] = ()
    value: Any = None


@dataclass(frozen=True)
class BatchInfo:
    """One ``add_*`` / ``replace_*`` / initial-item event for a field."""

    id: int
    kind: str
    action: str
    field: str | None
    sources: tuple[SourceInfo, ...]
    value: Any = None


@dataclass(frozen=True)
class ExpansionInfo:
    """An input element that a processor expanded into several outputs."""

    input_index: int
    output_count: int
    input_value: Any = None


@dataclass(frozen=True)
class StepInfo:
    """One application of one processor or processor function."""

    id: int
    name: str
    stage: str
    index: int
    attribution: str
    """One of ``"element"`` (per input element), ``"one_to_many"`` (the
    function expanded an element into several values) or ``"batch"`` (the
    processor consumed the whole batch and the precise mapping is unknown)."""
    sources: tuple[SourceInfo, ...]
    batches: tuple[BatchInfo, ...]
    adopted: tuple[int, ...] = ()
    """Indices, inside the step input list, of the values the result actually
    uses.  Populated by aggregating processors (TakeFirst, Join); empty means
    every input contributed."""
    input_value: Any = None
    expansions: tuple[ExpansionInfo, ...] = ()


@dataclass(frozen=True)
class DropInfo:
    """A value that disappeared from the pipeline."""

    reason: str
    sources: tuple[SourceInfo, ...]
    batches: tuple[BatchInfo, ...]
    step: StepInfo | None = None
    value: Any = None


@dataclass(frozen=True)
class ErrorInfo:
    """A processor failure captured before it was re-raised as ValueError."""

    stage: str
    step: StepInfo | None
    name: str
    value: Any
    error_type: str
    error_message: str


@dataclass(frozen=True)
class ValueInfo:
    """Provenance for one current collected (or output) value."""

    value: Any
    sources: tuple[SourceInfo, ...]
    batches: tuple[BatchInfo, ...]
    steps: tuple[StepInfo, ...]
    expansions: tuple[ExpansionInfo, ...]


@dataclass(frozen=True)
class FieldTrace:
    """Full trace snapshot for a single field."""

    field: str
    sources: tuple[SourceInfo, ...]
    batches: tuple[BatchInfo, ...]
    values: tuple[ValueInfo, ...]
    output: ValueInfo | None = None
    drops: tuple[DropInfo, ...] = ()
    errors: tuple[ErrorInfo, ...] = ()


# ---------------------------------------------------------------------------
# Internal mutable graph nodes
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Source:
    id: int
    type: str
    expression: str | None
    index: int
    chain: tuple[SelectorRef, ...]
    value: Any = None


@dataclass(eq=False)
class _Batch:
    id: int
    kind: str
    action: str
    field: str | None
    sources: list[_Source] = field(default_factory=list)
    value: Any = None


@dataclass(eq=False)
class _Step:
    id: int
    name: str
    stage: str
    index: int
    attribution: str
    sources: list[_Source] = field(default_factory=list)
    batches: list[_Batch] = field(default_factory=list)
    adopted: tuple[int, ...] = ()
    input_value: Any = None
    expansions: list[ExpansionInfo] = field(default_factory=list)


@dataclass(eq=False)
class _Drop:
    reason: str
    sources: list[_Source]
    batches: list[_Batch]
    step: _Step | None = None
    value: Any = None


@dataclass(eq=False)
class _Error:
    stage: str
    step: _Step | None
    name: str
    value: Any
    error_type: str
    error_message: str


@dataclass(eq=False)
class _Node:
    value: Any
    sources: list[_Source]
    batches: list[_Batch]
    steps: list[_Step] = field(default_factory=list)
    expansions: list[ExpansionInfo] = field(default_factory=list)


@dataclass(eq=False)
class _State:
    """Values plus parallel provenance at some point of a pipeline run."""

    values: list[Any]
    nodes: list[_Node]
    many: bool
    raw: Any = None

    @classmethod
    def from_nodes(cls, nodes: Sequence[_Node], many: bool) -> _State:
        values = [node.value for node in nodes]
        if many:
            return cls(values, list(nodes), many, values)
        return cls(values, list(nodes), many, values[0] if values else None)

    def input(self) -> Any:
        """The exact value the next processor must receive."""
        return self.raw if self.many else (self.values[0] if self.values else None)

    def result(self) -> Any:
        """The collected value at the end of a pipeline stage."""
        if self.many:
            return [node.value for node in self.nodes]
        return self.nodes[0].value if self.nodes else None


@dataclass(eq=False)
class FieldTracker:
    """Tracking state of one field, stored on the root loader."""

    field: str
    nodes: list[_Node] = field(default_factory=list)
    drops: list[_Drop] = field(default_factory=list)
    errors: list[_Error] = field(default_factory=list)
    output_nodes: list[_Node] = field(default_factory=list)
    output_many: bool = True


def _union(*groups: Sequence[_Source | _Batch]) -> list[Any]:
    seen: set[int] = set()
    result: list[Any] = []
    for group in groups:
        for item in group:
            marker = id(item)
            if marker not in seen:
                seen.add(marker)
                result.append(item)
    return result


def _sources_of(nodes: Sequence[_Node]) -> list[_Source]:
    return _union(*(node.sources for node in nodes))


def _batches_of(nodes: Sequence[_Node]) -> list[_Batch]:
    return _union(*(node.batches for node in nodes))


def _processor_name(func: Any) -> str:
    plain_name = getattr(func, "__name__", None)
    if plain_name == "<lambda>":
        return "<lambda>"
    cls = getattr(func, "__class__", None)
    qualname = getattr(func, "__qualname__", None)
    if qualname:
        return qualname
    name = getattr(cls, "__name__", None)
    if name and name not in {"function", "builtin_function_or_method", "method"}:
        return name
    module = getattr(func, "__module__", None)
    fname = getattr(func, "__name__", None) or "callable"
    return f"{module}.{fname}" if module else fname


def _as_many(value: Any) -> bool:
    """Whether the next processor should iterate *value* element-wise."""
    return value is not None and arg_to_iter(value) is value


class ProcessorFailure(Exception):
    """Internal signal: a processor raised while running a tracked pipeline.

    Carries the original processor object (before context wrapping), the raw
    value it received and the original exception, so the loader layer can
    reproduce its historical error messages."""

    def __init__(
        self,
        processor: Any,
        value: Any,
        original: BaseException,
        func: Any = None,
        func_value: Any = None,
    ) -> None:
        self.processor = processor
        self.value = value
        self.original = original
        self.func = func
        self.func_value = func_value
        super().__init__(str(original))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TrackingEngine:
    """Runs processors while mirroring provenance in parallel structures."""

    def __init__(self, context: MutableMapping[str, Any]) -> None:
        self.context = context
        self._sources: list[_Source] = []
        self._batches: list[_Batch] = []
        self._steps: list[_Step] = []
        self.fields: dict[str, FieldTracker] = {}

    def field(self, field_name: str) -> FieldTracker:
        tracker = self.fields.get(field_name)
        if tracker is None:
            tracker = FieldTracker(field_name)
            self.fields[field_name] = tracker
        return tracker

    def _new_step(
        self,
        name: str,
        stage: str,
        index: int,
        attribution: str,
        nodes: Sequence[_Node],
        input_value: Any,
        adopted: tuple[int, ...] = (),
    ) -> _Step:
        step = _Step(
            id=len(self._steps),
            name=name,
            stage=stage,
            index=index,
            attribution=attribution,
            sources=_sources_of(nodes) if nodes else [],
            batches=_batches_of(nodes) if nodes else [],
            adopted=adopted,
            input_value=input_value,
        )
        self._steps.append(step)
        return step

    def selector_entries(
        self,
        type_: str,
        expression: str,
        values: Sequence[Any],
        chain: Sequence[SelectorRef],
        field_name: str | None,
        action: str,
        kind: str,
    ) -> tuple[_Batch, list[_Node]]:
        """Create one batch with one source per extracted value."""
        batch = _Batch(
            id=len(self._batches),
            kind=kind,
            action=action,
            field=field_name,
            value=list(values),
        )
        self._batches.append(batch)
        nodes: list[_Node] = []
        for index, value in enumerate(values):
            source = _Source(
                id=len(self._sources),
                type=type_,
                expression=expression,
                index=index,
                chain=tuple(chain),
                value=value,
            )
            self._sources.append(source)
            batch.sources.append(source)
            nodes.append(_Node(value, [source], [batch]))
        return batch, nodes

    def value_batch(
        self,
        value: Any,
        field_name: str | None,
        action: str,
        kind: str = "value",
    ) -> tuple[_Batch, list[_Node]]:
        """Create one plain ``add_value`` batch, one source per value."""
        values = list(arg_to_iter(value))
        batch = _Batch(
            id=len(self._batches),
            kind=kind,
            action=action,
            field=field_name,
            value=value,
        )
        self._batches.append(batch)
        nodes: list[_Node] = []
        for index, item in enumerate(values):
            source = _Source(
                id=len(self._sources),
                type="value",
                expression=None,
                index=index,
                chain=(),
                value=item,
            )
            self._sources.append(source)
            batch.sources.append(source)
            nodes.append(_Node(item, [source], [batch]))
        return batch, nodes

    def initial_nodes(self, field_name: str, value: Any) -> list[_Node]:
        """Nodes for values already present on the item passed to the loader."""
        _, nodes = self.value_batch(value, field_name, action="initial", kind="item")
        return nodes

    def drop(
        self,
        tracker: FieldTracker,
        reason: str,
        nodes: Sequence[_Node],
        step: _Step | None = None,
        value: Any = None,
    ) -> None:
        tracker.drops.append(
            _Drop(
                reason=reason,
                sources=_sources_of(nodes) if nodes else [],
                batches=_batches_of(nodes) if nodes else [],
                step=step,
                value=value,
            )
        )

    def record_error(
        self,
        tracker: FieldTracker,
        stage: str,
        step: _Step | None,
        name: str,
        value: Any,
        error: BaseException,
    ) -> None:
        tracker.errors.append(
            _Error(
                stage=stage,
                step=step,
                name=name,
                value=value,
                error_type=type(error).__name__,
                error_message=str(error),
            )
        )

    # -- pipeline execution -------------------------------------------------

    @staticmethod
    def _align_nodes(elements: Sequence[Any], nodes: Sequence[_Node]) -> list[_Node]:
        """Ensure one provenance node per element.

        When an upstream batch processor yielded several elements from fewer
        nodes (e.g. Compose returning a list), each element inherits the union
        of provenance: a whole-batch dependency, not a fake per-element one.
        """
        if len(nodes) == len(elements):
            return list(nodes)
        if len(nodes) == 1 and elements:
            only = nodes[0]
            return [
                _Node(
                    element,
                    list(only.sources),
                    list(only.batches),
                    list(only.steps),
                    list(only.expansions),
                )
                for element in elements
            ]
        # Mismatch without a single origin: fall back to the whole batch.
        sources = _sources_of(nodes)
        batches = _batches_of(nodes)
        return [
            _Node(
                element,
                sources,
                batches,
                [s for node in nodes for s in node.steps],
                [e for node in nodes for e in node.expansions],
            )
            for element in elements
        ]

    def run_pipeline(
        self,
        processors: Sequence[Callable[..., Any]],
        state: _State,
        tracker: FieldTracker,
        stage: str,
    ) -> _State:
        for index, processor in enumerate(processors):
            if state.many and not state.values:
                break
            raw_input = state.input()
            try:
                state = self._run_processor(processor, state, tracker, stage, index)
            except ProcessorFailure:
                raise
            except Exception as error:
                raise ProcessorFailure(processor, raw_input, error) from error
        return state

    def flatten_regex(
        self,
        pattern: Any,
        state: _State,
        tracker: FieldTracker,
        stage: str = "add",
    ) -> _State:
        """Mirror :func:`parsel.utils.extract_regex` application with lineage."""
        from parsel.utils import extract_regex, flatten  # noqa: PLC0415

        raw_values = list(arg_to_iter(state.input()))
        extracted: list[list[str]] = [
            list(extract_regex(pattern, value)) for value in raw_values
        ]
        flat = flatten(extracted)
        next_nodes: list[_Node] = []
        entry_nodes = self._align_nodes(raw_values, state.nodes)
        for element_index, (node, matches) in enumerate(zip(entry_nodes, extracted)):
            if not matches:
                step = self._new_step(
                    "re",
                    stage,
                    0,
                    attribution="element",
                    nodes=[node],
                    input_value=node.value,
                )
                self.drop(
                    tracker,
                    reason="no_regex_match",
                    nodes=[node],
                    step=step,
                    value=node.value,
                )
                continue
            step = self._new_step(
                "re",
                stage,
                0,
                attribution="element" if len(matches) == 1 else "one_to_many",
                nodes=[node],
                input_value=node.value,
            )
            expansion = (
                ExpansionInfo(
                    input_index=element_index,
                    output_count=len(matches),
                    input_value=node.value,
                )
                if len(matches) > 1
                else None
            )
            for match in matches:
                next_nodes.append(
                    _Node(
                        match,
                        list(node.sources),
                        list(node.batches),
                        [*node.steps, step],
                        [*node.expansions, *([expansion] if expansion else [])],
                    )
                )
        state_out = _State.from_nodes(next_nodes, many=True)
        state_out.raw = list(flat)
        return state_out

    def _run_processor(
        self,
        processor: Callable[..., Any],
        state: _State,
        tracker: FieldTracker,
        stage: str,
        index: int,
    ) -> _State:
        if type(processor) is MapCompose:
            return self._run_map_compose(processor, state, tracker, stage, index)
        if type(processor) is Compose:
            return self._run_compose(processor, state, tracker, stage, index)
        if type(processor) is Identity:
            return state
        if type(processor) is TakeFirst:
            return self._run_take_first(processor, state, tracker, stage, index)
        if type(processor) is Join:
            return self._run_join(processor, state, tracker, stage, index)
        return self._run_opaque(processor, state, tracker, stage, index)

    def _run_map_compose(
        self,
        processor: MapCompose,
        state: _State,
        tracker: FieldTracker,
        stage: str,
        index: int,
    ) -> _State:
        from collections import ChainMap  # noqa: PLC0415

        context: MutableMapping[str, Any] = ChainMap(
            self.context, processor.default_loader_context
        )
        wrapped_funcs = [wrap_loader_context(f, context) for f in processor.functions]
        raw_input = state.input()
        elements = list(arg_to_iter(raw_input))
        nodes = self._align_nodes(elements, state.nodes)
        for func_index, original_func in enumerate(wrapped_funcs):
            next_nodes: list[_Node] = []
            for element_index, (value, node) in enumerate(zip(elements, nodes)):
                step = self._new_step(
                    _processor_name(original_func),
                    stage,
                    func_index,
                    attribution="element",
                    nodes=[node],
                    input_value=value,
                )
                try:
                    raw = original_func(value)
                except Exception as error:
                    step.attribution = "element"
                    self.record_error(
                        tracker, stage, step, _processor_name(original_func), value, error
                    )
                    raise ProcessorFailure(
                        processor,
                        raw_input,
                        error,
                        func=original_func,
                        func_value=raw_input,
                    ) from error
                expanded = list(arg_to_iter(raw))
                if not expanded:
                    self.drop(
                        tracker,
                        reason="none",
                        nodes=[node],
                        step=step,
                        value=value,
                    )
                    continue
                step.sources = _sources_of([node])
                step.batches = _batches_of([node])
                if len(expanded) > 1:
                    step.attribution = "one_to_many"
                    expansion = ExpansionInfo(
                        input_index=element_index,
                        output_count=len(expanded),
                        input_value=value,
                    )
                    step.expansions.append(expansion)
                else:
                    expansion = None
                for produced in expanded:
                    child = _Node(
                        produced,
                        list(node.sources),
                        list(node.batches),
                        [*node.steps, step],
                        [
                            *node.expansions,
                            *([expansion] if expansion else []),
                        ],
                    )
                    next_nodes.append(child)
            elements = [node.value for node in next_nodes]
            nodes = next_nodes
        # MapCompose always yields an iterable.
        return _State.from_nodes(nodes, many=True)

    def _run_compose(
        self,
        processor: Compose,
        state: _State,
        tracker: FieldTracker,
        stage: str,
        index: int,
    ) -> _State:
        from collections import ChainMap  # noqa: PLC0415

        context: MutableMapping[str, Any] = ChainMap(
            self.context, processor.default_loader_context
        )
        wrapped_funcs = [wrap_loader_context(f, context) for f in processor.functions]
        raw_input = state.input()
        current: Any = raw_input
        current_nodes = list(state.nodes)
        for func_index, original_func in enumerate(wrapped_funcs):
            if current is None and processor.stop_on_none:
                break
            func_input = current
            step = self._new_step(
                _processor_name(original_func),
                stage,
                func_index,
                attribution="batch",
                nodes=current_nodes,
                input_value=func_input,
            )
            try:
                current = original_func(func_input)
            except Exception as error:
                self.record_error(
                    tracker,
                    stage,
                    step,
                    _processor_name(original_func),
                    func_input,
                    error,
                )
                raise ProcessorFailure(
                    processor,
                    raw_input,
                    error,
                    func=original_func,
                    func_value=raw_input,
                ) from error
            # Compose pipes one value into the next function; a returned list
            # stays a single value for the following function.
            child_nodes = [
                _Node(
                    current,
                    _sources_of(current_nodes),
                    _batches_of(current_nodes),
                    [*[s for node in current_nodes for s in node.steps], step],
                    [e for node in current_nodes for e in node.expansions],
                )
            ]
            current_nodes = child_nodes
        if current is None:
            self.drop(
                tracker,
                reason="none",
                nodes=current_nodes or state.nodes,
                value=current,
            )
            return _State([], [], many=False)
        # Compose returns exactly one value, which may itself be a list. Keep
        # a single node carrying that list; the following processor receives
        # it as one argument (MapCompose aligns elements via _align_nodes).
        node = current_nodes[0] if current_nodes else None
        if node is not None:
            node.value = current
        return _State([current], current_nodes, False, current)

    def _run_take_first(
        self,
        processor: TakeFirst,
        state: _State,
        tracker: FieldTracker,
        stage: str,
        index: int,
    ) -> _State:
        chosen: int | None = None
        for element_index, value in enumerate(state.values):
            if value is not None and value != "":
                chosen = element_index
                break
        if chosen is None:
            step = self._new_step(
                "TakeFirst",
                stage,
                index,
                attribution="batch",
                nodes=state.nodes,
                input_value=state.input(),
            )
            self.drop(
                tracker,
                reason="none",
                nodes=state.nodes,
                step=step,
                value=state.input(),
            )
            return _State([], [], many=False)
        node = state.nodes[chosen]
        step = self._new_step(
            "TakeFirst",
            stage,
            index,
            attribution="batch",
            nodes=state.nodes,
            input_value=state.input(),
            adopted=(chosen,),
        )
        result_node = _Node(
            state.values[chosen],
            list(node.sources),
            list(node.batches),
            [*node.steps, step],
            list(node.expansions),
        )
        return _State.from_nodes([result_node], many=False)

    def _run_join(
        self,
        processor: Join,
        state: _State,
        tracker: FieldTracker,
        stage: str,
        index: int,
    ) -> _State:
        step = self._new_step(
            "Join",
            stage,
            index,
            attribution="batch",
            nodes=state.nodes,
            input_value=state.input(),
            adopted=tuple(range(len(state.values))),
        )
        result = processor(state.input())
        result_node = _Node(
            result,
            _sources_of(state.nodes),
            _batches_of(state.nodes),
            [*[s for node in state.nodes for s in node.steps], step],
            [e for node in state.nodes for e in node.expansions],
        )
        return _State.from_nodes([result_node], many=False)

    def _run_opaque(
        self,
        processor: Callable[..., Any],
        state: _State,
        tracker: FieldTracker,
        stage: str,
        index: int,
    ) -> _State:
        raw_input = state.input()
        wrapped = wrap_loader_context(processor, self.context)
        try:
            raw = wrapped(raw_input)
        except Exception as error:
            name = getattr(processor, "__class__", type(processor)).__name__
            self.record_error(tracker, stage, None, name, raw_input, error)
            raise
        step = self._new_step(
            _processor_name(processor),
            stage,
            index,
            attribution="batch",
            nodes=state.nodes,
            input_value=raw_input,
        )
        if raw is None:
            self.drop(
                tracker,
                reason="none",
                nodes=state.nodes,
                step=step,
                value=raw_input,
            )
            return _State([], [], many=False)
        produced = list(arg_to_iter(raw))
        result_nodes = [
            _Node(
                item,
                _sources_of(state.nodes),
                _batches_of(state.nodes),
                [*[s for node in state.nodes for s in node.steps], step],
                [e for node in state.nodes for e in node.expansions],
            )
            for item in produced
        ]
        # The untracked pipeline element-wise appends what arg_to_iter
        # expands (lists/tuples/sets/generators); everything else is a single
        # value. Provenance can only be attributed to the whole input batch.
        many = _as_many(raw)
        state_out = _State.from_nodes(result_nodes, many=many)
        state_out.raw = raw
        return state_out

    # -- snapshot -----------------------------------------------------------

    def _source_info(self, source: _Source) -> SourceInfo:
        return SourceInfo(
            id=source.id,
            type=source.type,
            expression=source.expression,
            index=source.index,
            chain=source.chain,
            value=source.value,
        )

    def _batch_info(self, batch: _Batch) -> BatchInfo:
        return BatchInfo(
            id=batch.id,
            kind=batch.kind,
            action=batch.action,
            field=batch.field,
            sources=tuple(self._source_info(source) for source in batch.sources),
            value=batch.value,
        )

    def _step_info(self, step: _Step) -> StepInfo:
        return StepInfo(
            id=step.id,
            name=step.name,
            stage=step.stage,
            index=step.index,
            attribution=step.attribution,
            sources=tuple(self._source_info(source) for source in step.sources),
            batches=tuple(self._batch_info(batch) for batch in step.batches),
            adopted=step.adopted,
            input_value=step.input_value,
            expansions=tuple(step.expansions),
        )

    def _value_info(self, node: _Node) -> ValueInfo:
        return ValueInfo(
            value=node.value,
            sources=tuple(self._source_info(source) for source in node.sources),
            batches=tuple(self._batch_info(batch) for batch in node.batches),
            steps=tuple(self._step_info(step) for step in node.steps),
            expansions=tuple(node.expansions),
        )

    def field_trace(
        self, tracker: FieldTracker, *, include_output: bool = True
    ) -> FieldTrace:
        sources = _union(*(node.sources for node in tracker.nodes))
        batches = _union(*(node.batches for node in tracker.nodes))
        drops_sources = _union(*(drop.sources for drop in tracker.drops))
        drops_batches = _union(*(drop.batches for drop in tracker.drops))
        all_sources = _union(sources, drops_sources)
        all_batches = _union(batches, drops_batches)
        output: ValueInfo | None = None
        if include_output and tracker.output_nodes:
            output_sources = _sources_of(tracker.output_nodes)
            output_batches = _batches_of(tracker.output_nodes)
            all_sources = _union(all_sources, output_sources)
            all_batches = _union(all_batches, output_batches)
            if tracker.output_many:
                values = tuple(self._value_info(node) for node in tracker.output_nodes)
                output = ValueInfo(
                    value=[info.value for info in values],
                    sources=tuple(
                        self._source_info(source) for source in output_sources
                    ),
                    batches=tuple(
                        self._batch_info(batch) for batch in output_batches
                    ),
                    steps=tuple(
                        self._step_info(step)
                        for step in _union(
                            *(node.steps for node in tracker.output_nodes)
                        )
                    ),
                    expansions=tuple(
                        expansion
                        for node in tracker.output_nodes
                        for expansion in node.expansions
                    ),
                )
            else:
                output = self._value_info(tracker.output_nodes[0])
        drop_infos = tuple(
            DropInfo(
                reason=drop.reason,
                sources=tuple(
                    self._source_info(source) for source in drop.sources
                ),
                batches=tuple(
                    self._batch_info(batch) for batch in drop.batches
                ),
                step=self._step_info(drop.step) if drop.step is not None else None,
                value=drop.value,
            )
            for drop in tracker.drops
        )
        error_infos = tuple(
            ErrorInfo(
                stage=error.stage,
                step=self._step_info(error.step) if error.step is not None else None,
                name=error.name,
                value=error.value,
                error_type=error.error_type,
                error_message=error.error_message,
            )
            for error in tracker.errors
        )
        return FieldTrace(
            field=tracker.field,
            sources=tuple(self._source_info(source) for source in all_sources),
            batches=tuple(self._batch_info(batch) for batch in all_batches),
            values=tuple(self._value_info(node) for node in tracker.nodes),
            output=output,
            drops=drop_infos,
            errors=error_infos,
        )
