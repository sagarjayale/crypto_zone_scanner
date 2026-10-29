"""Zone status on each new closed candle. Two rule sets, chosen by the marking rule.

scanner (scanner.py v6.4, the default):
test:      every closed candle whose wick reaches the proximal line counts as one test, also several in a row.
consumed:  a candle closes strictly inside the zone. The zone is removed one candle later, so that the next candle can
           still give entry type 2 or 3 (the entries start with exactly such a close).
           A close beyond the distal line does not end the zone; it is only ignored while price is beyond it.
           Many tests do not end it either: they lower its score (two or more make an execution zone unusable).

pdf (the GTF course PDF):

violated:  a candle closes beyond the distal line.
test:      price comes back to the zone. Candles in a row that reach the proximal line are one test, however many
           there are; the test is over on the first candle that stays clear of the zone.
consumed:  the zone has been tested more often than `max_tests` allows and that last test is over ("tested twice").
entry 2:   one candle closes inside the zone and the next candle opens inside it. Entry at that open.
entry 3:   one candle closes inside the zone and the next candle closes back beyond the proximal line. Entry at that close.

A candle closing inside the zone does not end the zone: it is what entry types 2 and 3 wait for.

Under both, the entries found belong to the test (visit) that is going on; a new visit starts with none."""

ACTIVE = ("fresh", "tested")


def prior_tests(z) -> int:
    """Tests that are over. The test going on now is the opportunity itself, so it does not count against the zone yet."""
    return int(z.touches or 0) - (1 if z.in_visit else 0)


def scanner_rules(th: dict | None) -> bool:
    return (th or {}).get("marking_rule") != "pdf"


def counted_tests(z, th: dict | None) -> int:
    """The tests that count against a zone. scanner.py counts every closed candle that reached the proximal line;
    the course does not count the test that is going on."""
    return int(z.touches or 0) if scanner_rules(th) else max(prior_tests(z), 0)


def update(z, o: float, h: float, l: float, c: float, ts, max_tests: int = 1, rule: str = "scanner") -> list[tuple]:
    """Mutates zone z (any object with the zone attributes). Returns events as (event_type, price, old_value, new_value)."""
    if z.status not in ACTIVE:
        return []
    if rule != "pdf":
        return _update_scanner(z, o, h, l, c, ts)
    demand = z.side == "demand"
    lo, hi = sorted((z.proximal, z.distal))
    armed, fired, events = bool(z.inside_armed), False, []
    if armed and z.t2_entry is None and lo <= o <= hi:            # entry type 2: this candle opens inside the zone
        z.t2_entry, z.t2_ts, fired = float(o), ts, True
        events.append(("entry2", o, None, str(z.touches)))
    if (c < z.distal) if demand else (c > z.distal):
        old, z.status, z.inside_armed, z.in_visit = z.status, "violated", False, False
        events.append(("violated", c, old, "violated"))
        return events
    if armed and z.t3_entry is None and ((c > z.proximal) if demand else (c < z.proximal)):   # entry type 3: it leaves the zone
        z.t3_entry, z.t3_ts, fired = float(c), ts, True
        events.append(("entry3", c, None, str(z.touches)))
    reaches = (l <= z.proximal) if demand else (h >= z.proximal)
    if reaches and not z.in_visit:                                # a new test begins
        z.touches = int(z.touches or 0) + 1
        z.in_visit, z.status = True, "tested"
        z.t2_entry = z.t2_ts = z.t3_entry = z.t3_ts = None
        events.append(("touched", z.proximal, str(z.touches - 1), str(z.touches)))
    elif not reaches and not fired and z.in_visit:                # the test is over
        z.in_visit = False
        if z.touches > max_tests:
            z.status, z.inside_armed = "consumed", False
            events.append(("consumed", c, "tested", "consumed"))
            return events
    inside = lo <= c <= hi
    if inside and not armed:
        events.append(("closed_inside", c, None, None))
    z.inside_armed = inside
    if inside:
        z.inside_ts, z.inside_close = ts, float(c)
    return events


def _update_scanner(z, o: float, h: float, l: float, c: float, ts) -> list[tuple]:
    """scanner.py v6.4: a close strictly inside the zone uses it up (one candle later, for entry types 2 and 3), and every
    candle that reaches the proximal line is a test."""
    demand = z.side == "demand"
    lo, hi = sorted((z.proximal, z.distal))
    armed, events = bool(z.inside_armed), []
    if armed and z.t2_entry is None and lo <= o <= hi:            # entry type 2: this candle opens inside the zone
        z.t2_entry, z.t2_ts = float(o), ts
        events.append(("entry2", o, None, str(z.touches)))
    if armed and z.t3_entry is None and ((c > z.proximal) if demand else (c < z.proximal)):   # entry type 3: it closes back out
        z.t3_entry, z.t3_ts = float(c), ts
        events.append(("entry3", c, None, str(z.touches)))
    if armed:                                                     # the candle after the close inside: the zone is used up now
        old, z.status, z.inside_armed, z.in_visit = z.status, "consumed", False, False
        events.append(("consumed", c, old, "consumed"))
        return events
    if (l <= z.proximal) if demand else (h >= z.proximal):        # every candle that reaches the proximal line is a test
        z.touches = int(z.touches or 0) + 1
        z.status = "tested"
        if not z.in_visit:                                        # the first candle of a visit: the entries start afresh
            z.in_visit = True
            z.t2_entry = z.t2_ts = z.t3_entry = z.t3_ts = None
            events.append(("touched", z.proximal, str(z.touches - 1), str(z.touches)))
        else:
            events.append(("touched_again", z.proximal, str(z.touches - 1), str(z.touches)))
    elif z.in_visit:
        z.in_visit = False
    if lo < c < hi:                                               # a close strictly inside: used up after the next candle
        z.inside_armed, z.inside_ts, z.inside_close = True, ts, float(c)
        events.append(("closed_inside", c, None, None))
    return events
