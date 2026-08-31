import base64
import json
import os
import re
from pathlib import Path

import httpx
from pypdf import PdfReader

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
- Titles should be useful on a family calendar, e.g. "Emma soccer vs Hawks" not "Game 4".
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


def _api_config() -> tuple[str, str, str] | None:
    xai = os.getenv("XAI_API_KEY", "").strip()
    if xai:
        return "https://api.x.ai/v1/chat/completions", xai, os.getenv("XAI_MODEL", "grok-4")
    oai = os.getenv("OPENAI_API_KEY", "").strip()
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
        guessed = _guess_from_text(text, kid_name) if text else _empty_result(
            "No AI key configured. Add events by hand on the next screen, or set XAI_API_KEY / OPENAI_API_KEY."
        )
        if text and guessed.get("events"):
            guessed["summary"] = "Read printed text from the PDF. Please confirm dates and times."
            guessed["needs_manual"] = False
        guessed["source_text"] = text[:4000]
        return guessed

    url, key, model = cfg
    prompt = EXTRACT_PROMPT
    for key, value in {
        "kid_name": kid_name,
        "timezone": timezone,
        "parent_name": parent_name or "unknown",
        "sport": sport or "not specified",
        "team_name": team_name or "not specified",
        "extra_notes": extra_notes or "none",
    }.items():
        prompt = prompt.replace("{" + key + "}", str(value).replace("{", "(").replace("}", ")"))
    user_content: list[dict] = [{"type": "text", "text": prompt}]
    if text:
        user_content.append({"type": "text", "text": "Extracted PDF text:\n" + text[:12000]})

    if mime.startswith("image/") or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".heic"}:
        raw = path.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        media = "image/jpeg"
        if path.suffix.lower() == ".png":
            media = "image/png"
        elif path.suffix.lower() == ".webp":
            media = "image/webp"
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

    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": "You extract structured sports schedules. JSON only."},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=90.0,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return _parse_json(content)
    except Exception as exc:
        fallback = _guess_from_text(text, kid_name) if text else _empty_result(f"AI read failed: {exc}")
        fallback["error"] = str(exc)
        return fallback


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


DATE_RE = re.compile(
    r"\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*)?[, ]*"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*|\d{1,2})[./\- ]+"
    r"(\d{1,2})(?:[./\- ]+(\d{2,4}))?",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def _guess_from_text(text: str, kid_name: str) -> dict:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 8:
            continue
        if not DATE_RE.search(line) and not TIME_RE.search(line):
            continue
        events.append(
            {
                "title": f"{kid_name} — {line[:80]}",
                "event_type": "game" if re.search(r"\bvs\.?\b|game", line, re.I) else "event",
                "date": None,
                "start_time": None,
                "end_time": None,
                "all_day": False,
                "location": None,
                "opponent": None,
                "notes": line[:240],
                "confidence": 0.25,
            }
        )
    questions = [
        {
            "id": "q_confirm",
            "prompt": "Does this look like the right season schedule?",
            "why": "Read from printed text only",
            "options": ["Yes", "No, I'll edit"],
            "allow_free_text": True,
        }
    ]
    return {
        "summary": f"Found {len(events)} possible lines in the document text.",
        "sport": "unknown",
        "team_name": None,
        "season": None,
        "questions": questions,
        "events": events[:40],
        "needs_manual": len(events) == 0,
        "source_text": text[:4000],
    }
