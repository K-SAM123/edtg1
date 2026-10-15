"""
Dauphine London timetable -> edt.ics

Fetches the weekly timetable pages from dauphine.bulletscheduling.com for one
student group and writes a calendar file you can subscribe to (Apple Calendar,
Google Calendar...). Standard library only.

Usage:
    python edt.py                  # fetch from the website, write edt.ics
    python edt.py --file page.html # parse a saved page (for testing)
"""

import hashlib
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- settings
BASE_URL = "https://dauphine.bulletscheduling.com/Academia/schedules/"
# One line per calendar: (output file, file name before the date, calendar name)
GROUPS = [
    ("edt.ics", "student_group_Year_1_-_Group_1_2", "Dauphine EDT - Group 1"),
]
FIRST_WEEK = date(2026, 9, 14)                # first Monday to look at
WEEKS_AHEAD = 30                              # how far ahead to look from today
TZ = ZoneInfo("Europe/London")
# --------------------------------------------------------------------------

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")
ROOM = re.compile(r"^(\d+(\.\d+)?[A-Za-z]?|The Annex|Annex.*|Room .*|Amphi.*|Online|Zoom|Teams)$", re.I)


class TimetableParser(HTMLParser):
    """Collects the rows of the main timetable as lists of cells."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []          # list of rows; each row = list of cell dicts
        self.week = None
        self._cell = None
        self._row = None
        self._title_cell = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "td":
            self._cell = {
                "cls": a.get("class", ""),
                "colspan": int(a.get("colspan", 1) or 1),
                "rowspan": int(a.get("rowspan", 1) or 1),
                "parts": [""],
            }
        elif tag == "br" and self._cell is not None:
            self._cell["parts"].append("")

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._cell["parts"] = [p.strip() for p in self._cell["parts"] if p.strip()]
            if self._row is not None:
                self._row.append(self._cell)
            text = " ".join(self._cell["parts"])
            m = re.search(r"Weeks:\s*(\d{2})/(\d{2})/(\d{4})", text)
            if m:
                self.week = date(int(m[3]), int(m[2]), int(m[1]))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["parts"][-1] += unescape(data).replace("\xa0", " ")


def parse_week(html):
    """Return (monday, [event dicts]) for one weekly page."""
    p = TimetableParser()
    p.feed(html)
    if p.week is None:
        raise ValueError("week date not found in page")

    # Header row: tells us how many grid columns each day uses (Tuesday can be 2).
    header_i = next(i for i, r in enumerate(p.rows)
                    if r and r[0]["cls"] == "td_cabecalho" and r[0]["parts"] == ["Hours"])
    col_day = []
    for cell in p.rows[header_i][1:]:
        day = DAYS.index(cell["parts"][0])
        col_day += [day] * cell["colspan"]
    ncols = len(col_day)

    # Walk the grid, honouring rowspans, to know which column each cell lands in.
    occupied = {}  # (row, col) -> True
    events = []
    for r, row in enumerate(p.rows[header_i + 1:]):
        if not row or row[0]["cls"] != "td_lateral":
            continue
        col = 0
        for cell in row[1:]:
            while occupied.get((r, col)):
                col += 1
            if col >= ncols:
                break
            for dr in range(cell["rowspan"]):
                for dc in range(cell["colspan"]):
                    occupied[(r + dr, col + dc)] = True
            if cell["cls"].startswith("td_evento"):
                ev = parse_event(cell["parts"])
                if ev:
                    ev["day"] = col_day[col]
                    events.append(ev)
            col += cell["colspan"]

    # Same event drawn in two sub-columns of a day -> keep one.
    seen, unique = set(), []
    for ev in events:
        key = (ev["day"], ev["start"], ev["end"], ev["title"])
        if key not in seen:
            seen.add(key)
            unique.append(ev)
    return p.week, unique


def parse_event(parts):
    if not parts:
        return None
    m = TIME_RANGE.match(parts[0])
    if not m:
        return None
    start = (int(m[1]), int(m[2]))
    end = (int(m[3]), int(m[4]))

    name, teachers, rooms, groups, notes = None, [], [], [], []
    for part in parts[1:]:
        if part.startswith("[") and part.endswith("]"):
            inner = part[1:-1].strip()
            if re.match(r"^\(.*\)$", inner):
                teachers.append(inner.strip("()"))
            elif "Group" in inner:
                groups.append(inner)
            elif ROOM.match(inner):
                rooms.append(inner)
            else:
                notes.append(inner)
        elif name is None:
            name = part
        else:
            notes.append(part)

    if name is None and notes:
        title = notes.pop(0)
    elif name is None:
        title = "Cours" + (f" ({teachers[0]})" if teachers else "")
    elif name.upper() == "TUTORIAL":
        title = "Tutorial"
    else:
        title = name

    desc = []
    if teachers:
        desc.append("Prof : " + ", ".join(teachers))
    if notes:
        desc += notes
    if groups:
        desc.append("Groupes : " + "; ".join(groups))
    return {
        "start": start, "end": end, "title": title,
        "location": ", ".join(rooms), "description": "\n".join(desc),
    }


# ------------------------------------------------------------------ fetching
def fetch(group, monday):
    url = f"{BASE_URL}{group}_{monday:%Y%m%d}.html?{int(datetime.now().timestamp())}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (edt-sync)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def weeks_to_fetch():
    today = date.today()
    last = today + timedelta(weeks=WEEKS_AHEAD)
    d = FIRST_WEEK - timedelta(days=FIRST_WEEK.weekday())
    while d <= last:
        yield d
        d += timedelta(weeks=1)


# ------------------------------------------------------------------ ics output
def ics_escape(s):
    return (s.replace("\\", "\\\\").replace(";", "\\;")
             .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode())
        b = b" " + b[cut:]
    out.append(b.decode())
    return "\r\n".join(out)


def to_utc(day, hm):
    local = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=TZ)
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(all_events, calendar_name):
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//edt-sync//Dauphine London//FR",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:{calendar_name}", "X-WR-TIMEZONE:Europe/London",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for day, ev in all_events:
        uid_src = f"{day}|{ev['start']}|{ev['title']}"
        uid = hashlib.sha1(uid_src.encode()).hexdigest()[:20] + "@edt-sync"
        lines += [
            "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}",
            f"DTSTART:{to_utc(day, ev['start'])}", f"DTEND:{to_utc(day, ev['end'])}",
            f"SUMMARY:{ics_escape(ev['title'])}",
        ]
        if ev["location"]:
            lines.append(f"LOCATION:{ics_escape(ev['location'])}")
        if ev["description"]:
            lines.append(f"DESCRIPTION:{ics_escape(ev['description'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def build_group(output, group, calendar_name, pages=None):
    print(f"--- {calendar_name} ({output})")
    if pages is None:
        pages = []
        for monday in weeks_to_fetch():
            html = fetch(group, monday)
            if html:
                pages.append(html)
                print(f"week {monday}: ok")
            else:
                print(f"week {monday}: not published")

    all_events = []
    for html in pages:
        monday, events = parse_week(html)
        for ev in events:
            all_events.append((monday + timedelta(days=ev["day"]), ev))

    if not all_events:
        # Never replace a good calendar with an empty one (site down, URL changed...).
        print(f"No events found - keeping the previous {output}")
        return False

    all_events.sort(key=lambda x: (x[0], x[1]["start"]))
    with open(output, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(all_events, calendar_name))
    print(f"{len(all_events)} events written to {output}")
    return True


def main():
    if "--file" in sys.argv:
        page = open(sys.argv[sys.argv.index("--file") + 1], encoding="utf-8").read()
        ok = build_group("test.ics", None, "Test", pages=[page])
        sys.exit(0 if ok else 1)

    results = []
    for output, group, name in GROUPS:
        try:
            results.append(build_group(output, group, name))
        except Exception as e:  # one broken group must not block the others
            print(f"ERROR for {output}: {e}")
            results.append(False)
    if not any(results):
        sys.exit("No calendar could be built")


if __name__ == "__main__":
    main()
