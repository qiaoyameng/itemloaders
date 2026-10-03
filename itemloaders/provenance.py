"""
Optional value provenance tracking for Item Loaders.

When an :class:`~itemloaders.ItemLoader` is created with
``track_provenance=True``, every collected value carries a parallel
provenance record describing where it came from (which ``add_xpath`` /
``add_css`` / ``add_jmes`` / ``add_value`` call, and which selector
expression) and which processor steps transformed it. The tracking data
is stored separately from the collected values, so user processors keep
receiving plain values exactly as they do without tracking.
"""

from __future__ import annotations

from collections import ChainMap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from itemloaders.common import wrap_loader_context
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, MutableMapping


@dataclass
class Source:
    """Leaf of a provenance tree: a value entering the loader.

    :attr:`kind` is one of ``"xpath"``, ``"css"``, ``"jmes"``, ``"value"``
    (plain :meth:`~itemloaders.ItemLoader.add_value`) or ``"item"``
    (pre-existing item data). :attr:`expression` is the selector expression
    for selector-based sources, ``None`` otherwise. :attr:`batch` identifies
    the individual ``add_*`` / ``replace_*`` call the value came from.
    """

    kind: str
    expression: str | None
    value: Any
    batch: int

    def iter_sources(self) -> Iterator[Source]:
        yield self


@dataclass
class ProcessorStep:
    """A processor application in a provenance tree.

    :attr:`mode` is ``"mapped"`` when each output is traced to the exact
    input(s) it was derived from, ``"aggregate"`` when the processor
    declares which inputs it combined (e.g. :class:`~.processors.Join`),
    or ``"batch"`` when the processor is opaque and the output honestly
    depends on the whole input batch.
    """

    processor: str
    value: Any
    inputs: list[Node]
    mode: str = "batch"

    def iter_sources(self) -> Iterator[Source]:
        for node in self.inputs:
            yield from node.iter_sources()


Node = Source | ProcessorStep


@dataclass
class Drop:
    """Record of a value discarded while processing a field.

    Currently produced when a :class:`~.processors.MapCompose` function
    returns ``None`` for a value, filtering it out of the chain.
    """

    processor: str
    value: Any
    source: Node


@dataclass
class ProvenanceReport:
    """Provenance information for a single field of a loader."""

    field_name: str
    output: list[Node] = field(default_factory=list)
    collected: list[Node] = field(default_factory=list)
    drops: list[Drop] = field(default_factory=list)

    def sources(self) -> list[Source]:
        """All leaf sources of the field output, in order."""
        seen: list[Source] = []
        for node in self.output:
            seen.extend(node.iter_sources())
        return seen

    def expressions(self) -> list[str]:
        """Selector expressions that contributed to the field output."""
        result: list[str] = []
        for source in self.sources():
            if source.expression is not None and source.expression not in result:
                result.append(source.expression)
        return result

    def explain(self) -> str:
        """Human-readable tree describing where the output came from."""
        lines: list[str] = []
        for node in self.output:
            _format_node(node, lines, indent=0)
        for drop in self.drops:
            lines.append(
                f"dropped by {drop.processor}: {drop.value!r} "
                f"(from {_source_summary(drop.source)})"
            )
        return "\n".join(lines)


def _source_summary(node: Node) -> str:
    if isinstance(node, Source):
        if node.expression is not None:
            return f"{node.kind} {node.expression!r} (batch {node.batch})"
        return f"{node.kind} (batch {node.batch})"
    return f"{node.processor} [{node.mode}]"


def _format_node(node: Node, lines: list[str], indent: int) -> None:
    prefix = "  " * indent
    if isinstance(node, Source):
        lines.append(f"{prefix}{_source_summary(node)}: {node.value!r}")
        return
    lines.append(f"{prefix}{node.processor} [{node.mode}] -> {node.value!r}")
    for child in node.inputs:
        _format_node(child, lines, indent + 1)


class ProvenanceTracker:
    """Holds all provenance state for a (possibly nested) loader tree.

    Keyed by field name, so nested loaders sharing the value pool of their
    parent never mix provenance across fields.
    """

    def __init__(self) -> None:
        self._batch_counter = 0
        self.collected: dict[str, list[Node]] = {}
        self.output: dict[str, list[Node]] = {}
        self.drops: dict[str, list[Drop]] = {}
        self.output_drops: dict[str, list[Drop]] = {}

    def next_batch(self) -> int:
        self._batch_counter += 1
        return self._batch_counter

    def field_drops(self, field_name: str) -> list[Drop]:
        return self.drops.setdefault(field_name, [])


def processor_name(proc: Callable[..., Any]) -> str:
    name = getattr(proc, "__name__", None)
    if name:
        return name
    return proc.__class__.__name__


def _align(provs: list[Node], count: int) -> list[Node]:
    """Align provenance records to *count* values, degrading to an explicit
    whole-batch dependency when a 1:1 mapping cannot be established."""
    if len(provs) == count:
        return list(provs)
    batch_node = ProcessorStep(
        processor="<values>", value=None, inputs=list(provs), mode="batch"
    )
    return [batch_node] * count


def apply_processor_tracked(
    proc: Callable[..., Any],
    value: Any,
    provs: list[Node],
    context: MutableMapping[str, Any],
    drops: list[Drop] | None = None,
) -> tuple[Any, list[Node]]:
    """Apply *proc* to *value* while tracking provenance.

    Returns the raw processor result (exactly what the untracked call would
    return) plus a list of provenance nodes aligned with
    ``arg_to_iter(result)``. Built-in processors get precise tracking;
    any other callable is applied unchanged and its outputs are recorded
    as depending on the whole input batch.
    """
    from itemloaders.processors import (  # noqa: PLC0415
        Compose,
        Identity,
        Join,
        MapCompose,
        TakeFirst,
    )

    if isinstance(proc, Identity):
        return value, _align(provs, len(arg_to_iter(value)))
    if isinstance(proc, MapCompose):
        return _map_compose_tracked(proc, value, provs, context, drops)
    if isinstance(proc, Compose):
        return _compose_tracked(proc, value, provs, context, drops)
    if isinstance(proc, TakeFirst):
        return _take_first_tracked(proc, value, provs)
    if isinstance(proc, Join):
        return _join_tracked(proc, value, provs)

    wrapped = wrap_loader_context(proc, context)
    result = wrapped(value)
    name = processor_name(proc)
    return result, [
        ProcessorStep(processor=name, value=v, inputs=list(provs), mode="batch")
        for v in arg_to_iter(result)
    ]


def _map_compose_tracked(
    proc: Any,
    value: Any,
    provs: list[Node],
    context: MutableMapping[str, Any],
    drops: list[Drop] | None,
) -> tuple[Any, list[Node]]:
    values = list(arg_to_iter(value))
    node_list = _align(provs, len(values))
    if context:
        ctx: MutableMapping[str, Any] = ChainMap(
            context, proc.default_loader_context
        )
    else:
        ctx = proc.default_loader_context
    wrapped_funcs = [wrap_loader_context(f, ctx) for f in proc.functions]
    for raw_func, func in zip(proc.functions, wrapped_funcs, strict=True):
        func_name = processor_name(raw_func)
        next_values: list[Any] = []
        next_provs: list[Node] = []
        for v, node in zip(values, node_list, strict=True):
            try:
                out = func(v)
            except Exception as e:
                raise ValueError(
                    f"Error in MapCompose with "
                    f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
                ) from e
            if out is None:
                if drops is not None:
                    drops.append(
                        Drop(processor=func_name, value=v, source=node)
                    )
                continue
            for o in arg_to_iter(out):
                next_values.append(o)
                next_provs.append(
                    ProcessorStep(
                        processor=func_name, value=o, inputs=[node], mode="mapped"
                    )
                )
        values = next_values
        node_list = next_provs
    return values, node_list


def _compose_tracked(
    proc: Any,
    value: Any,
    provs: list[Node],
    context: MutableMapping[str, Any],
    drops: list[Drop] | None,
) -> tuple[Any, list[Node]]:
    if context:
        ctx: MutableMapping[str, Any] = ChainMap(
            context, proc.default_loader_context
        )
    else:
        ctx = proc.default_loader_context
    wrapped_funcs = [wrap_loader_context(f, ctx) for f in proc.functions]
    node_list = _align(provs, len(arg_to_iter(value)))
    for raw_func, func in zip(proc.functions, wrapped_funcs, strict=True):
        if value is None and proc.stop_on_none:
            break
        try:
            # Known processors (TakeFirst, MapCompose, ...) get precise
            # tracking; anything else degrades to a whole-batch dependency.
            value, node_list = apply_processor_tracked(
                raw_func, value, node_list, ctx, drops
            )
        except Exception as e:
            raise ValueError(
                f"Error in Compose with "
                f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
            ) from e
    return value, node_list


def _take_first_tracked(
    proc: Any, value: Any, provs: list[Node]
) -> tuple[Any, list[Node]]:
    # TakeFirst iterates its argument as-is (e.g. a string yields its
    # characters), so replicate that exactly instead of using arg_to_iter.
    values = list(value)
    node_list = _align(provs, len(values))
    for v, node in zip(values, node_list, strict=True):
        if v is not None and v != "":
            return v, [
                ProcessorStep(
                    processor="TakeFirst", value=v, inputs=[node], mode="mapped"
                )
            ]
    return None, []


def _join_tracked(proc: Any, value: Any, provs: list[Node]) -> tuple[Any, list[Node]]:
    result = proc.separator.join(value)
    return result, [
        ProcessorStep(
            processor="Join", value=result, inputs=list(provs), mode="aggregate"
        )
    ]
