.. _provenance:

Tracking value provenance
=========================

.. versionadded:: VERSION

When a field output looks wrong, it can be hard to tell which ``add_xpath`` /
``add_css`` / ``add_jmes`` / ``add_value`` call brought a value in, and which
processors transformed it along the way. Pass ``track_provenance=True`` to
:class:`~itemloaders.ItemLoader` to record that information, and use
:meth:`~itemloaders.ItemLoader.get_provenance` to trace a field output back
to its sources::

    loader = ItemLoader(selector=selector, track_provenance=True)
    loader.add_xpath("price", '//span[@class="price"]/text()')
    item = loader.load_item()
    report = loader.get_provenance("price")
    print(report.explain())

The report for a field contains:

-   ``output``: the provenance tree of the field output. Leaves are
    :class:`~itemloaders.provenance.Source` objects recording the source kind,
    the selector expression, the raw extracted value and the input batch (one
    batch per ``add_*`` / ``replace_*`` call). Inner nodes are
    :class:`~itemloaders.provenance.ProcessorStep` objects recording the
    processor name, its output value and the inputs it used.
-   ``collected``: provenance of the collected values, before the output
    processor runs.
-   ``drops``: values discarded along the way, e.g. when a
    :class:`~itemloaders.processors.MapCompose` function returns ``None``.

Processor steps have a ``mode``:

-   ``"mapped"``: the output is traced to the exact input it came from, e.g.
    each :class:`~itemloaders.processors.MapCompose` function application, or
    the value picked by :class:`~itemloaders.processors.TakeFirst`.
-   ``"aggregate"``: the processor declares all inputs it combined, e.g.
    :class:`~itemloaders.processors.Join`.
-   ``"batch"``: the processor is opaque (any custom processor), so the
    output is honestly recorded as depending on the whole input batch,
    without pretending a finer-grained mapping is known.

Tracking data is stored separately from the collected values: processors
keep receiving plain values, exactly as without tracking. Nested loaders
share the tracker of their parent, keyed by field name, so provenance never
leaks across fields. :meth:`~itemloaders.ItemLoader.replace_value` (and its
selector variants) evicts the provenance of the replaced values.

When ``track_provenance`` is ``False`` (the default), the loader behaves
exactly as before, with no tracking overhead.

.. module:: itemloaders.provenance
   :synopsis: Value provenance tracking

.. autoclass:: Source
   :members:

.. autoclass:: ProcessorStep
   :members:

.. autoclass:: Drop
   :members:

.. autoclass:: ProvenanceReport
   :members:
