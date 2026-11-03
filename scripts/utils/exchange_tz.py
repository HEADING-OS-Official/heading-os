"""Calendar items whose Windows time zone ID exchangelib does not know.

An item whose StartTimeZone or EndTimeZone carries an ID absent from
exchangelib's CLDR map (Outlook writes "Customized Time Zone" for a zone it
defined itself) made `TimeZoneField.from_xml` print a multi-line `UserWarning`
to the terminal and leave the zone at None. Start and end of a timed meeting
arrive in UTC and are untouched by that, but an ALL-DAY item is turned into a
date with `astimezone(None)`, i.e. in the zone of the host process, not the one
the caller works in. MEASURED 2026-11-01 on exchangelib 5.6.0 with a hand-built
item (`2026-11-03T20:00Z` start, all day): TZ=UTC gave 2026-11-03, a host zone four hours east of UTC
2026-11-04, the known ID "Arabian Standard Time" 2026-11-04 on either host.

HOS-293 fixed that inside `scripts/sync-exchange.py`; HOS-299 moved it here so
every engine read of Exchange items that can parse those fields uses one
mechanism. A `MeetingRequest` carries the same zone fields as a `CalendarItem`,
and exchangelib requests them for every read with no `.only()` from a mail
folder (`allowed_item_fields` covers all of `MESSAGE_ITEM_CLASSES`) and for
every `.only()` naming `start` or `end` (`QuerySet._additional_fields` adds
them). So an invite sitting in the Inbox reaches a plain mail read too.
MEASURED 2026-11-01 with hand-built MeetingRequest XML, before this module was
used there: the mail reads of sync-exchange, Sentinel, send-email and inbox
pulse each printed the raw warning, and so did the calendar view of
comms-history-export.

`UnknownZones.read(items)` iterates any exchangelib result, and
`UnknownZones.one(fetch)` runs a single-item read; both read every
unknown ID as the caller's zone, only during the step that parses an item, so
the exchangelib map is the original again whenever the caller's own code runs,
including after an early `return` out of the loop. `report()` names the affected
items once per instance by a 12-character hash of their EWS id, never by their
subject. exchangelib is imported lazily, so importing this module stays pure.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager

# The map is one class attribute shared by every thread of the process. Two
# overlapping substitutions would restore in the wrong order and leave a
# fallback installed for good, so only one is in place at a time.
_MAP_LOCK = threading.RLock()


@contextmanager
def unknown_zones_as(zone_name):
    """Read every Windows zone ID exchangelib does not know as `zone_name`.

    Mapping the unknown ID to the caller's own zone makes an all-day date agree
    with the zone every other date the caller handles is in. The true zone of a
    custom definition is not in the response, so for an all-day event organised
    elsewhere this remains a guess, now a stated one. An ID containing "/" still
    raises KeyError, so exchangelib keeps trying it as an IANA key itself.
    Yields the list of IDs substituted, one entry per lookup.
    """
    from exchangelib import EWSTimeZone

    hits = []

    with _MAP_LOCK:
        original = EWSTimeZone.MS_TO_IANA_MAP

        class _Fallback(dict):
            def __missing__(self, ms_id):
                if "/" in ms_id:
                    raise KeyError(ms_id)
                hits.append(ms_id)
                return zone_name

        EWSTimeZone.MS_TO_IANA_MAP = _Fallback(original)
        try:
            yield hits
        finally:
            EWSTimeZone.MS_TO_IANA_MAP = original


def item_ref(item):
    """A short, stable name for an item that does not reveal its content."""
    return hashlib.sha256(str(item.id).encode()).hexdigest()[:12]


class UnknownZones:
    """Reads items with unknown zones as `zone_name` and remembers which.

    exchangelib parses an item inside the iteration step that yields it, so a
    substitution made during that step belongs to the item it yields.
    """

    def __init__(self, zone_name):
        self.zone_name = zone_name
        self._reported = set()
        self._pending = []
        self._zone_ids = set()

    def read(self, items):
        """Yield every item of `items`, unknown zones read as `zone_name`."""
        iterator = iter(items)
        while True:
            with unknown_zones_as(self.zone_name) as hits:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            self._note(item, hits)
            yield item

    def one(self, fetch):
        """Return `fetch()`, a single-item read such as `folder.get(id=...)`, parsed the same way."""
        with unknown_zones_as(self.zone_name) as hits:
            item = fetch()
        self._note(item, hits)
        return item

    def _note(self, item, hits):
        if hits:
            ref = item_ref(item)
            if ref not in self._reported and ref not in self._pending:
                self._pending.append(ref)
                self._zone_ids.update(hits)

    def report(self):
        """One line naming the items not yet reported, or None when there are none."""
        if not self._pending:
            return None
        refs, zone_ids = self._pending, sorted(self._zone_ids)
        self._reported.update(refs)
        self._pending, self._zone_ids = [], set()
        return (f"{len(refs)} calendar item(s) {', '.join(refs)} carry unknown "
                f"Windows time zone ID(s) {zone_ids}, read as "
                f"{self.zone_name}; all-day dates of those items follow that zone")
