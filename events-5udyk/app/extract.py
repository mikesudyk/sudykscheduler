import base64
import json
import os
import re
from pathlib import Path

import httpx
from pypdf import PdfReader
from PIL import Image, ImageOps

EXTRACT_PROMPT = """You extract youth sports schedules from a parent-uploaded photo or PDF.

Return ONLY valid JSON with this shape:
{
  "summary": "one sentence of what this document is",
  "sport": "soccer|baseball|basketball|football|volleyball|hockey|swim|track|other|unknown",
  "team_name": "string or null",
  "season": "string or null",
  "questions": [
    {"id": "q1", "prompt": "short question the parent can answer", "why": "what was unclear", "options": ["optional", "choices"], "allow_free_text": true}
  ],
  "events": [
    {
      "title": "short calendar title",
      "event_type": "game|practice|tournament|scrimmage|meeting|other",
      "date": "YYYY-MM-DD or null if unknown",
      "start_time": "HH:MM 24h or null",
      "end_time": "HH:MM 24h or null",
      "all_day": false,
      "location": "string or null",
      "opponent": "string or null",
      "notes": "string or null",
      "confidence": 0.0
    }
  ]
}

Rules:
- The events are for the child named: {kid_name} (parent/household: {parent_name})
- Timezone is {timezone}. Do not convert timezone, just read the printed times.
- Parent-provided context (trust this when the page is messy):
  sport: {sport}
  team name: {team_name}
  extra notes: {extra_notes}
- Prefer games and practices. Skip ads, fundraisers, and boilerplate unless they have a date/time.
- If a date is like "Sat 9/6" and year is missing, assume the upcoming occurrence in the next 12 months from today.
- If only a start time is printed, leave end_time null.
- Ask 2 to 6 clarifying questions for anything you had to guess: year, AM/PM, home vs away field, which team, whether bye weeks are events, arrival vs start time.
- Never invent opponents or fields you cannot see.
- Titles should be useful on a family calendar, e.g. "Malachi FLAG vs Falcons" not "Game 4".
- Date headers like "Sun, Sep 20 2026 - Grandville High School" apply to the game rows under them.
- Put the ISO date in "date", 24-hour "HH:MM" in start_time/end_time. Never leave date empty if a year is printed.
- If a team name was given, only keep rows for that team. Opponent is the other side (Home vs Away).
- Meet & Greet / picture day is event_type "meeting". Games are "game".
- Location should include school + field number + address when printed.
"""


def pdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages[:8]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _clean_key(raw: str) -> str:
    return raw.strip().strip('"').strip("'")


def _api_config() -> tuple[str, str, str] | None:
    xai = _clean_key(os.getenv("XAI_API_KEY", ""))
    if xai:
        return "https://api.x.ai/v1/chat/completions", xai, os.getenv("XAI_MODEL", "grok-4.6").strip() or "grok-4.6"
    oai = _clean_key(os.getenv("OPENAI_API_KEY", ""))
    if oai:
        return "https://api.openai.com/v1/chat/completions", oai, os.getenv("OPENAI_MODEL", "gpt-4o")
    return None


def _empty_result(reason: str) -> dict:
    return {
        "summary": reason,
        "sport": "unknown",
        "team_name": None,
        "season": None,
        "questions": [
            {
                "id": "q_manual",
                "prompt": "What is this schedule for (sport and season)?",
                "why": "Could not read the file automatically",
                "options": [],
                "allow_free_text": True,
            },
            {
                "id": "q_times",
                "prompt": "Are the printed times game start times, or arrival times?",
                "why": "Schedules mix these",
                "options": ["Start times", "Arrival times", "Not sure"],
                "allow_free_text": False,
            },
        ],
        "events": [],
        "needs_manual": True,
        "reason": reason,
    }


def _shrink_image(path: Path) -> tuple[bytes, str]:
    """Downscale phone photos so Grok + Cloudflare finish before a 524."""
    import io

    raw = path.read_bytes()
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=68, optimize=True)
        out = buf.getvalue()
        if len(out) < len(raw):
            return out, "image/jpeg"
    except Exception as exc:
        print(f"image shrink skipped: {exc}", flush=True)
    if len(raw) > 1_500_000:
        print(f"image still large: {len(raw)} bytes", flush=True)
    return raw, "image/jpeg"


def extract_schedule(
    path: Path,
    mime: str,
    kid_name: str,
    timezone: str,
    parent_name: str = "",
    sport: str = "",
    team_name: str = "",
    extra_notes: str = "",
) -> dict:
    cfg = _api_config()
    text = pdf_text(path) if path.suffix.lower() == ".pdf" else ""

    if not cfg:
        return _empty_result(
            "Grok API key is not set on Railway (XAI_API_KEY). Add the key from console.x.ai and redeploy."
        )

    url, api_key, model = cfg
    prompt = EXTRACT_PROMPT
    for field, value in {
        "kid_name": kid_name,
        "timezone": timezone,
        "parent_name": parent_name or "unknown",
        "sport": sport or "not specified",
        "team_name": team_name or "not specified",
        "extra_notes": extra_notes or "none",
    }.items():
        prompt = prompt.replace("{" + field + "}", str(value).replace("{", "(").replace("}", ")"))
    user_content: list[dict] = [{"type": "text", "text": prompt}]
    if text:
        user_content.append({"type": "text", "text": "Extracted PDF text:\n" + text[:12000]})

    if mime.startswith("image/") or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".heic"}:
        raw, media = _shrink_image(path)
        b64 = base64.b64encode(raw).decode("ascii")
        user_content.append(
            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}}
        )
    elif path.suffix.lower() == ".pdf" and not text:
        user_content.append(
            {
                "type": "text",
                "text": "This PDF had no extractable text (likely a scan). Ask the parent to also snap a photo of each page if you cannot read it.",
            }
        )

    models = []
    for m in (model, "grok-4.6"):
        if m and m not in models:
            models.append(m)

    last_err = None
    for try_model in models:
        try:
            r = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": try_model,
                    "temperature": 0.1,
                    "reasoning_effort": "low",
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "You extract structured youth sports schedules. Return only JSON."},
                        {"role": "user", "content": user_content},
                    ],
                },
                timeout=httpx.Timeout(15.0, read=75.0, write=30.0, pool=15.0),
            )
            print(f"xAI {try_model} HTTP {r.status_code} {r.text[:400]}", flush=True)
            if r.status_code >= 400:
                last_err = f"{try_model} HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code not in (400, 404):
                    break
                continue
            content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
            if not content:
                last_err = f"{try_model} returned empty content"
                continue
            ai = _parse_json(content)
            ai = _filter_to_team(ai, team_name)
            ai["reader"] = f"grok:{try_model}"
            return ai
        except httpx.TimeoutException:
            last_err = "Grok timed out reading the photo. Crop to just the schedule grid and try again."
            print(f"xAI timeout {try_model}", flush=True)
            break
        except Exception as exc:
            last_err = f"{try_model}: {exc}"
            print(f"xAI error {last_err}", flush=True)
            continue

    fallback = _empty_result(f"Grok API did not return a schedule. {last_err}")
    fallback["error"] = last_err
    fallback["reader"] = "failed"
    return fallback


def refine_with_answers(payload: dict, answers: dict, kid_name: str, sport: str = "", team_name: str = "") -> dict:
    """Second Grok pass: apply parent answers to the already-extracted events."""
    cfg = _api_config()
    if not cfg:
        payload["answers"] = answers
        payload["refined"] = True
        return payload
    url, api_key, model = cfg
    prompt = (
        f"You already extracted a youth sports schedule for {kid_name}. "
        f"Sport: {sport or payload.get('sport') or 'unknown'}. "
        f"Team: {team_name or payload.get('team_name') or 'unknown'}.\n"
        "The parent answered your clarifying questions. Apply those answers to EVERY event "
        "they affect. Examples: a home-field address goes on home games; arrival vs start "
        "time shifts start_time; a missing year fills date.\n"
        "Do not drop events. Do not invent new games. Keep the same JSON shape "
        "(summary, sport, team_name, season, questions, events).\n"
        "Leave questions as an empty list.\n"
        "Put ISO dates in date and 24-hour HH:MM in start_time/end_time.\n\n"
        f"Answers:\n{json.dumps(answers, indent=2)}\n\n"
        f"Current extraction:\n{json.dumps({k: payload.get(k) for k in ('summary','sport','team_name','season','events')}, indent=2)}"
    )
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model or "grok-4.6",
                "temperature": 0.1,
                "reasoning_effort": "low",
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "Update sports schedule JSON using parent answers. JSON only."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=httpx.Timeout(15.0, read=60.0, write=20.0, pool=15.0),
        )
        print(f"xAI refine HTTP {r.status_code} {r.text[:300]}", flush=True)
        r.raise_for_status()
        content = (r.json().get("choices") or [{}])[0].get("message", {}).get("content")
        updated = _parse_json(content)
        updated["answers"] = answers
        updated["refined"] = True
        updated["questions"] = []
        updated["reader"] = f"grok-refine:{model}"
        if not updated.get("events"):
            updated["events"] = payload.get("events") or []
        return updated
    except Exception as exc:
        print(f"xAI refine error {exc}", flush=True)
        payload["answers"] = answers
        payload["refined"] = True
        payload["refine_error"] = str(exc)
        return payload


def _parse_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    data = json.loads(content)
    data.setdefault("questions", [])
    data.setdefault("events", [])
    data.setdefault("needs_manual", False)
    return data


MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

HEADER_DATE_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\.?,?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b", re.I)
TEAM_RE = re.compile(
    r"([A-Za-z]+)\s*[-–]\s*(\d+)\s*[-–]\s*([A-Za-z][A-Za-z0-9]*)",
    re.I,
)
ADDR_RE = re.compile(r"\((\d{3,}[^)]+)\)")
FIELD_RE = re.compile(r"\bField\s+(\d+)\b|\b(?:^|\s)(\d{1,2})(?=\s+[A-Za-z])", re.I)


def _to_24h(hour: str, minute: str | None, ampm: str) -> str:
    h = int(hour)
    m = int(minute or "0")
    ap = ampm.lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{m:02d}"


def _header_date(line: str) -> tuple[str | None, str | None]:
    m = HEADER_DATE_RE.search(line)
    if not m:
        return None, None
    key = m.group(1).lower().rstrip(".")
    month = MONTHS.get(key) or MONTHS.get(key[:3])
    if not month:
        return None, None
    day = int(m.group(2))
    year = int(m.group(3))
    loc = line
    loc = HEADER_DATE_RE.sub("", loc)
    loc = re.sub(r"Meet\s*&\s*Greet|Picture Day|[-–|]+", " ", loc, flags=re.I)
    loc = re.sub(r"\s+", " ", loc).strip(" -–")
    addr = ADDR_RE.search(line)
    place = loc.split("(")[0].strip(" -–")
    if addr:
        place = f"{place}, {addr.group(1)}".strip(", ")
    return f"{year:04d}-{month:02d}-{day:02d}", place or None


def _team_tokens(team_name: str) -> list[str]:
    if not team_name:
        return []
    raw = re.split(r"[,/|()]+", team_name)
    out = []
    for part in raw:
        part = part.strip()
        if not part:
            continue
        out.append(part.lower())
        for word in re.findall(r"[A-Za-z]{3,}", part):
            if word.lower() not in {"the", "and", "flag", "team", "school"}:
                out.append(word.lower())
    return list(dict.fromkeys(out))


def _line_has_team(line: str, tokens: list[str]) -> bool:
    if not tokens:
        return True
    low = line.lower()
    return any(tok in low for tok in tokens)


def _opponents(line: str, tokens: list[str]) -> tuple[str | None, str | None]:
    found = TEAM_RE.findall(line)
    labels = []
    for _div, _num, nick in found:
        labels.append(nick.strip())
    if not labels:
        return None, None
    ours = None
    opp = None
    for lab in labels:
        if tokens and any(tok in lab.lower() for tok in tokens):
            ours = lab
        else:
            opp = lab
    if ours and not opp:
        for lab in labels:
            if lab != ours:
                opp = lab
    if not ours and len(labels) == 1:
        ours = labels[0]
    return ours, opp


def parse_schedule_text(text: str, kid_name: str, team_name: str = "", sport: str = "") -> dict:
    tokens = _team_tokens(team_name)
    events = []
    current_date = None
    current_place = None
    current_kind = "game"

    for raw in text.splitlines():
        line = re.sub(r"[🏈🏆]+", " ", raw).strip()
        if len(line) < 4:
            continue
        if re.search(r"highlighted in orange|last week of the season|copyright|©|trademark", line, re.I):
            continue
        if re.match(r"^(start time|end time|game time|field|home|away|team)$", line, re.I):
            continue

        date, place = _header_date(line)
        if date:
            current_date = date
            current_place = place
            current_kind = "meeting" if re.search(r"meet\s*&\s*greet|picture day", line, re.I) else "game"
            continue
        if current_place and re.match(r"^\(?\d{3,}.*(?:Ave|St|Rd|Dr|Blvd|MI|OH|IN)", line, re.I):
            addr = line.strip("()")
            if addr not in current_place:
                current_place = f"{current_place}, {addr}"
            continue

        times = list(TIME_RE.finditer(line))
        if not times or not current_date:
            continue
        if tokens and not _line_has_team(line, tokens) and current_kind != "meeting":
            continue

        start = _to_24h(times[0].group(1), times[0].group(2), times[0].group(3))
        end = None
        if len(times) >= 2:
            end = _to_24h(times[1].group(1), times[1].group(2), times[1].group(3))

        ours, opp = _opponents(line, tokens)
        rest = TIME_RE.sub("", line).strip()
        field_m = re.match(r"^(\d{1,2})\s+", rest)
        field = field_m.group(1) if field_m else None
        loc = current_place or ""
        if field:
            loc = f"{loc} · Field {field}".strip(" ·")

        kind = current_kind
        if re.search(r"meet\s*&\s*greet|picture", line, re.I):
            kind = "meeting"
        title_bits = [kid_name]
        if kind == "meeting":
            title_bits.append("Meet & Greet / Picture Day")
        elif opp:
            title_bits.append(f"FLAG vs {opp}")
        elif ours:
            title_bits.append(ours)
        else:
            title_bits.append(sport or "game")
        events.append(
            {
                "title": " ".join(title_bits),
                "event_type": kind,
                "date": current_date,
                "start_time": start,
                "end_time": end,
                "all_day": False,
                "location": loc or None,
                "opponent": opp,
                "notes": None if kind == "game" else "Picture day / meet & greet",
                "confidence": 0.85,
            }
        )

    sport_guess = sport or ("flag football" if re.search(r"flag football|nfl flag", text, re.I) else "unknown")
    return {
        "summary": f"Read {len(events)} event(s) for {team_name or kid_name} from the printed schedule.",
        "sport": sport_guess,
        "team_name": team_name or None,
        "season": None,
        "questions": [
            {
                "id": "q_team",
                "prompt": "Keep only this team’s games?",
                "why": "League PDFs list every team in the division",
                "options": ["Yes, only ours", "No, keep the whole page"],
                "allow_free_text": False,
            }
        ],
        "events": events[:40],
        "needs_manual": len(events) == 0,
        "source_text": text[:4000],
    }


def _events_have_dates(payload: dict) -> bool:
    evs = payload.get("events") or []
    if not evs:
        return False
    dated = sum(1 for e in evs if e.get("date") and e.get("start_time"))
    return dated >= max(1, len(evs) // 2)


def _filter_to_team(payload: dict, team_name: str) -> dict:
    tokens = _team_tokens(team_name)
    if not tokens:
        return payload
    kept = []
    for e in payload.get("events") or []:
        blob = " ".join(
            str(e.get(k) or "") for k in ("title", "opponent", "notes", "location")
        ).lower()
        if any(tok in blob for tok in tokens) or e.get("event_type") == "meeting":
            kept.append(e)
    if kept:
        payload["events"] = kept
    return payload
