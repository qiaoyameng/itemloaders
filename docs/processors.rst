.. currentmodule:: itemloaders

.. _processors:

Input and Output processors
===========================

An Item Loader contains one input processor and one output processor for each
(item) field. The input processor processes the extracted data as soon as it's
received (through the :meth:`~ItemLoader.add_xpath`,
:meth:`~ItemLoader.add_css`, :meth:`~ItemLoader.add_jmes` or
:meth:`~ItemLoader.add_value` methods) and the result of the input processor is
collected and kept inside the ItemLoader. After collecting all data, the
:meth:`ItemLoader.load_item` method is called to populate and get the populated
item object.  That's when the output processor is
called with the data previously collected (and processed using the input
processor). The result of the output processor is the final value that gets
assigned to the item.

Let's see an example to illustrate how the input and output processors are
called for a particular field (the same applies for any other field)::

    l = ItemLoader(selector=some_selector)
    l.add_xpath('name', xpath1) # (1)
    l.add_xpath('name', xpath2) # (2)
    l.add_css('name', css) # (3)
    l.add_value('name', 'test') # (4)
    return l.load_item() # (5)

So what happens is:

1. Data from ``xpath1`` is extracted, and passed through the *input processor* of
   the ``name`` field. The result of the input processor is collected and kept in
   the Item Loader (but not yet assigned to the item).

2. Data from ``xpath2`` is extracted, and passed through the same *input
   processor* used in (1). The result of the input processor is appended to the
   data collected in (1) (if any).

3. This case is similar to the previous ones, except that the data is extracted
   from the ``css`` CSS selector, and passed through the same *input
   processor* used in (1) and (2). The result of the input processor is appended to the
   data collected in (1) and (2) (if any).

4. This case is also similar to the previous ones, except that the value to be
   collected is assigned directly, instead of being extracted from a XPath
   expression or a CSS selector.
   However, the value is still passed through the input processors. In this
   case, since the value is not iterable it is converted to an iterable of a
   single element before passing it to the input processor, because input
   processor always receive iterables.

5. The data collected in steps (1), (2), (3) and (4) is passed through
   the *output processor* of the ``name`` field.
   The result of the output processor is the value assigned to the ``name``
   field in the item.

It's worth noticing that processors are just callable objects, which are called
with the data to be parsed, and return a parsed value. So you can use any
function as input or output processor. The only requirement is that they must
accept one (and only one) positional argument, which will be an iterable.

.. note:: Both input and output processors must receive an iterable as their
   first argument. The output of those functions can be anything. The result of
   input processors will be appended to an internal list (in the Loader)
   containing the collected values (for that field). The result of the output
   processors is the value that will be finally assigned to the item.

The other thing you need to keep in mind is that the values returned by input
processors are collected internally (in lists) and then passed to output
processors to populate the fields.

Last, but not least, ``itemloaders`` comes with some :ref:`commonly used processors
<built-in-processors>` built-in for convenience.

.. _empty-values:

Empty values
------------

A field is only assigned to the item if at least one value was collected for
it, and ``None`` is never collected. It is ignored both when passed directly
and when returned by a processor, as are extractions that match nothing::

    l = ItemLoader(selector=selector)
    l.add_value('name', None)
    l.add_xpath('price', '//nonexistent/text()')
    l.load_item()
    # {}

The loaded item has neither field, so reading them raises :exc:`KeyError` on
item types where unassigned fields are absent, such as :class:`dict` or
:class:`scrapy.Item <scrapy.item.Item>`.

For a field to be present regardless of what is collected, use an item class
that gives its fields a default value, such as a :mod:`dataclasses` or
``attrs`` class.

.. _explicit-drop:

Explicit dropping with ``DROP``
-------------------------------

Sometimes an API explicitly returns ``None``, and that ``None`` must be
stored rather than confused with a value that was never extracted. Pass
``drop_none=False`` when creating the loader to switch to explicit-drop mode:
in that mode ``None``, ``""``, ``0`` and ``False`` are ordinary data values
that are collected and assigned to the item, while
:data:`~itemloaders.processors.DROP` is the only signal that a value must be
discarded::

    from itemloaders import DROP, ItemLoader
    from itemloaders.processors import MapCompose

    l = ItemLoader(drop_none=False)
    l.add_value("name", [None, "", 0, False])
    l.add_value(
        "price",
        ["12", DROP],
        MapCompose(int, lambda v: DROP if v > 100 else v),
    )
    l.load_item()
    # {'name': [None, '', 0, False], 'price': [12]}

With the default ``drop_none=True``, ``None`` keeps being ignored as before,
but :data:`~itemloaders.processors.DROP` is still honored, so processors can
drop values explicitly in either mode. The distinction between a field with
no value at all and a field whose value is ``None`` is then preserved::

    l = ItemLoader(drop_none=False)
    l.add_value("name", None)
    l.load_item()
    # {'name': [None]}
    l.get_collected_values("name")
    # [None]
    l.get_collected_values("missing")
    # []

The contract runs through the whole pipeline:

- :class:`~itemloaders.processors.MapCompose` drops the values for which a
  function returns :data:`~itemloaders.processors.DROP`, and keeps ``None``
  values when built with ``MapCompose(..., drop_none=False)``.
- :class:`~itemloaders.processors.Compose` stops the chain as soon as a
  function returns :data:`~itemloaders.processors.DROP`, and lets ``None``
  flow through the remaining functions with ``drop_none=False``.
- :class:`~itemloaders.processors.TakeFirst` always skips
  :data:`~itemloaders.processors.DROP`, and can return ``None`` when built
  with ``TakeFirst(drop_none=False)``.
- :meth:`~ItemLoader.replace_value` discards the data collected so far for a
  field when given :data:`~itemloaders.processors.DROP`. With
  ``drop_none=False``, replacing with ``None`` stores ``None`` like any other
  value; with the default mode the previous data is kept, as before.
- A field whose output processor returns
  :data:`~itemloaders.processors.DROP` is absent from the loaded item, and
  :data:`~itemloaders.processors.DROP` never appears inside the item even if
  an output processor passes it through in a list.

Nested loaders inherit the ``drop_none`` setting of their parent.

.. versionadded:: VERSION
    The :data:`~itemloaders.processors.DROP` sentinel and the ``drop_none``
    parameter.
