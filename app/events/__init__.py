"""Asynchronous event messaging for posted ledger transactions.

The pieces fit together in one direction only:

``ledger.post`` writes an ``outbox_events`` row on the same session as the
ledger entries, so the event is committed or rolled back with them and no
broker is contacted while that transaction is open. ``publisher`` moves unsent
rows to the broker and marks them sent afterwards. ``consumer`` reads them back,
guards itself with a unique index so a redelivery changes nothing, maintains the
per account read model, and dead letters a message it cannot handle.

Delivery is **at least once**. Nothing here achieves exactly once, and the
module is arranged so that the duplicates this implies are harmless rather than
pretended away.
"""
