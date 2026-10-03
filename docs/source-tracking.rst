.. _source-tracking:

Tracking value sources
======================

.. versionadded:: VERSION

When a field is fed through several ``add_xpath`` / ``add_css`` /
``add_jmes`` / ``add_value`` calls and one or more processors, the final
string returned by :meth:`~ItemLoader.load_item` often gives no clue about
which rule produced it. Pass ``track_sources=True`` to record, alongside the
regular data, where every collected value came from and which processor steps
it went through::

    loader = ItemLoader(selector=selector, track_sources=True)
    loader.add_xpath("price", "//span[@class='price']/text()",
                     MapCompose(str.strip, float))
    loader.load_item()
    trace = loader.get_field_trace("price")
    for value in trace.values:
        print(value.value, value.sources, value.steps)

Tracking is off by default and, when off, adds no behaviour or processing
overhead: processors keep receiving the exact same plain values, because the
tracking data is kept in separate structures rather than in wrapper objects.
The flag is inherited by :ref:`nested loaders <nested-loaders>`.

Reading a trace
---------------

.. automethod:: itemloaders.ItemLoader.get_field_trace

The returned :class:`~itemloaders.tracking.FieldTrace` is a frozen snapshot:

``trace.sources`` / ``trace.batches``
    Every origin expression and every ``add_*`` / ``replace_*`` / initial-item
    event that still contributes to the field. A
    :class:`~itemloaders.tracking.SourceInfo` carries the selector type, the
    expression, the index of the extracted element and, for nested loaders,
    the chain of selector expressions applied to reach it.

``trace.values``
    One :class:`~itemloaders.tracking.ValueInfo` per current collected value.
    Each lists its ``sources``, its ``batches`` and the processor ``steps``
    that produced it.

``trace.output``
    A single :class:`~itemloaders.tracking.ValueInfo` for the field output,
    populated after :meth:`~ItemLoader.load_item` or
    :meth:`~ItemLoader.get_output_value` is called. Use it to walk backwards
    from the final field value.

``trace.drops``
    Values that disappeared: a :class:`~itemloaders.processors.MapCompose`
    function returned ``None`` (``reason="none"``), a regex did not match
    (``reason="no_regex_match"``), an aggregator found no usable value, or the
    field input processor produced a falsy result.

``trace.errors``
    Processor failures captured before the usual ``ValueError`` is raised,
    with the stage (``add``, ``input`` or ``output``), the failing step, the
    value the function received and the original error.

Processor attribution
---------------------

Each :class:`~itemloaders.tracking.StepInfo` declares how it relates its
inputs to its outputs:

``"element"``
    The processor function was applied independently to each element
    (:class:`~itemloaders.processors.MapCompose` functions).

``"one_to_many"``
    One input element produced several output values; the
    :attr:`~itemloaders.tracking.StepInfo.expansions` attribute records the
    input index, the input value and the number of outputs. All resulting
    values share the same origin.

``"batch"``
    The processor consumed the whole batch as one value
    (:class:`~itemloaders.processors.Compose`, custom processors) or
    aggregated it (:class:`~itemloaders.processors.TakeFirst`,
    :class:`~itemloaders.processors.Join`). The step lists every source and
    batch it could depend on instead of pretending a per-element precision
    that does not exist. Aggregators additionally expose
    :attr:`~itemloaders.tracking.StepInfo.adopted`, the indices of the inputs
    actually used by the result (the chosen index for ``TakeFirst``, every
    index for ``Join``).

``replace_*`` methods drop the provenance of the values they replace from the
current result: only the replacement batch is reachable from the field
afterwards.
