from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, vText


def build_calendar(events: list[dict], name: str, timezone: str) -> bytes:
    tz = ZoneInfo(timezone)
    cal = Calendar()
    cal.add("prodid", "-//5udyk Events//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", name)
    cal.add("x-wr-timezone", timezone)

    for row in events:
        ev = Event()
        ev.add("uid", row["uid"])
        ev.add("summary", _summary(row))
        start = _parse(row["start_iso"], tz)
        end = _parse(row["end_iso"], tz) if row.get("end_iso") else None
        if row.get("all_day"):
            ev.add("dtstart", start.date())
            if end:
                ev.add("dtend", end.date())
        else:
            ev.add("dtstart", start)
            ev.add("dtend", end or start)
        loc_bits = [row.get("location") or ""]
        if row.get("opponent"):
            loc_bits.append(f"vs {row['opponent']}")
        location = " · ".join(b for b in loc_bits if b)
        if location:
            ev.add("location", vText(location))
        desc = []
        if row.get("kid_name"):
            desc.append(row["kid_name"])
        if row.get("event_type"):
            desc.append(row["event_type"])
        if row.get("notes"):
            desc.append(row["notes"])
        if desc:
            ev.add("description", "\n".join(desc))
        ev.add("dtstamp", datetime.now(tz=tz))
        cal.add_component(ev)

    return cal.to_ical()


def _summary(row: dict) -> str:
    kid = row.get("kid_short") or row.get("kid_name") or ""
    title = row["title"]
    if kid and kid.lower() not in title.lower():
        return f"{kid}: {title}"
    return title


def event_google_url(row: dict, timezone: str) -> str:
    tz = ZoneInfo(timezone)
    start = _parse(row["start_iso"], tz)
    end = _parse(row["end_iso"], tz) if row.get("end_iso") else start
    def stamp(dt: datetime) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")
    title = _summary(row)
    details = " · ".join(
        p for p in (row.get("kid_name"), row.get("event_type"), row.get("notes")) if p
    )
    loc = row.get("location") or ""
    if row.get("opponent"):
        loc = f"{loc} vs {row['opponent']}".strip()
    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote(title)}"
        f"&dates={stamp(start)}/{stamp(end)}"
        f"&ctz={quote(timezone)}"
        f"&details={quote(details)}"
        f"&location={quote(loc)}"
    )


def _parse(iso: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)
