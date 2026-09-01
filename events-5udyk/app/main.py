import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from zoneinfo import ZoneInfo

from .db import (
    DATA_DIR,
    FAMILIES,
    UPLOAD_DIR,
    add_event,
    add_kid,
    add_upload,
    delete_event,
    delete_kid,
    ensure_data_dirs,
    get_event,
    get_kid,
    get_upload,
    import_kids_csv,
    init_db,
    list_events,
    list_kids,
    save_extraction,
    seed_roster_if_empty,
)
from .extract import extract_schedule, refine_with_answers, _api_config
from .icsutil import build_calendar, event_google_url

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
ensure_data_dirs()

PASSWORD = os.getenv("SITE_PASSWORD", "change-me")
FAMILY = os.getenv("FAMILY_NAME", "Sudyk")
TIMEZONE = os.getenv("TIMEZONE", "America/Detroit")
CAL_TOKEN = os.getenv("CALENDAR_TOKEN", "change-the-calendar-token")
SECRET = os.getenv("SESSION_SECRET", secrets.token_hex(32))
PRODUCTION = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("ENV") == "production")

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic"}
COLORS = [
    "#2F6F4E",
    "#1F4E79",
    "#B4532A",
    "#6B3FA0",
    "#0F766E",
    "#9F1239",
    "#854D0E",
    "#1E3A5F",
]

APP_NAME = "Sudyk Spectator Scheduler"
app = FastAPI(title=APP_NAME)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET,
    same_site="lax",
    https_only=PRODUCTION,
)
app.mount("/static", StaticFiles(directory=str(ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))


@app.middleware("http")
async def no_cdn_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
    elif path.endswith(".ics"):
        response.headers["Cache-Control"] = "private, max-age=300"
    else:
        response.headers["Cache-Control"] = "private, no-store"
    return response


@app.on_event("startup")
def startup() -> None:
    init_db()
    seed_roster_if_empty(ROOT / "grandkids.csv")


def logged_in(request: Request) -> bool:
    return bool(request.session.get("ok"))


def require_login(request: Request):
    if not logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return None


def kids_grouped(kids: list[dict]) -> list[dict]:
    order: list[str] = []
    buckets: dict[str, list] = {}
    for k in kids:
        parent = k.get("parent") or "Other"
        if parent not in buckets:
            buckets[parent] = []
            order.append(parent)
        buckets[parent].append(k)
    return [{"parent": p, "kids": buckets[p]} for p in order]


def ctx(request: Request, **extra):
    kids = list_kids()
    data = {
        "request": request,
        "family": FAMILY,
        "app_name": APP_NAME,
        "timezone": TIMEZONE,
        "kids": kids,
        "kid_groups": kids_grouped(kids),
        "families": FAMILIES,
        "logged_in": logged_in(request),
    }
    data.update(extra)
    return data


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", ctx(request, error=None))


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    if secrets.compare_digest(password, PASSWORD):
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", ctx(request, error="That password is not right."), status_code=401
    )


@app.get("/health")
def health():
    cfg = _api_config()
    return {
        "ok": True,
        "data_dir": str(DATA_DIR),
        "grok": bool(cfg and "x.ai" in cfg[0]),
        "model": cfg[2] if cfg else None,
    }


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    kid: int | None = None,
    parent: str | None = None,
    family: str | None = None,
    expand: int | None = None,
):
    if not logged_in(request):
        return templates.TemplateResponse(request, "login.html", ctx(request, error=None))
    active_family = family or "Sudyk"
    if active_family.lower().replace(" ", "") in {"vanderveen", "vanderveen"}:
        active_family = "Vander Veen"
    elif active_family.lower() == "sudyk":
        active_family = "Sudyk"
    events = list_events(kid_id=kid, upcoming_only=True)
    if not kid:
        events = [e for e in events if (e.get("family_name") or "Sudyk") == active_family]
        if parent:
            events = [e for e in events if (e.get("parent_name") or "") == parent]
    grouped = _group_events(events)
    roster_open = bool(expand) or bool(parent) or bool(kid)
    return templates.TemplateResponse(
        request,
        "home.html",
        ctx(
            request,
            events=events,
            grouped=grouped,
            active_kid=kid,
            active_parent=parent,
            active_family=active_family,
            roster_open=roster_open,
            calendar_url=f"/calendar.ics?token={CAL_TOKEN}",
            kid_calendar_url=(
                f"/calendar/{kid}.ics?token={CAL_TOKEN}" if kid else None
            ),
        ),
    )


@app.get("/kids", response_class=HTMLResponse)
def kids_page(request: Request):
    gate = require_login(request)
    if gate:
        return gate
    return templates.TemplateResponse(request, "kids.html", ctx(request, colors=COLORS))


@app.post("/kids")
def kids_add(
    request: Request,
    name: str = Form(...),
    color: str = Form("#2F6F4E"),
    parent: str = Form(""),
    family: str = Form("Sudyk"),
):
    gate = require_login(request)
    if gate:
        return gate
    if name.strip():
        add_kid(
            name.strip(),
            color.strip() or COLORS[0],
            parent=parent.strip() or None,
            family=family.strip() or "Sudyk",
        )
    return RedirectResponse("/kids", status_code=303)


@app.post("/kids/import")
async def kids_import(request: Request, file: UploadFile = File(...)):
    gate = require_login(request)
    if gate:
        return gate
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    result = import_kids_csv(text)
    return RedirectResponse(
        f"/kids?added={result['added']}&skipped={result['skipped']}",
        status_code=303,
    )


@app.post("/kids/{kid_id}/delete")
def kids_delete(request: Request, kid_id: int):
    gate = require_login(request)
    if gate:
        return gate
    delete_kid(kid_id)
    return RedirectResponse("/kids", status_code=303)


@app.get("/add", response_class=HTMLResponse)
def add_start(request: Request):
    gate = require_login(request)
    if gate:
        return gate
    kids = list_kids()
    if not kids:
        return RedirectResponse("/kids", status_code=303)
    return templates.TemplateResponse(request, "add.html", ctx(request))


@app.post("/add", response_class=HTMLResponse)
async def add_upload_file(
    request: Request,
    kid_id: int = Form(...),
    file: UploadFile = File(...),
    team_name: str = Form(""),
    sport: str = Form(""),
    extra_notes: str = Form(""),
):
    gate = require_login(request)
    if gate:
        return gate
    kid = get_kid(kid_id)
    if not kid:
        return RedirectResponse("/add", status_code=303)

    suffix = Path(file.filename or "upload.bin").suffix.lower()
    if suffix not in ALLOWED_EXT:
        return templates.TemplateResponse(
            request,
            "add.html",
            ctx(request, error="Please upload a photo (JPG, PNG, HEIC, WebP) or a PDF."),
            status_code=400,
        )

    stored = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}{suffix}"
    dest = UPLOAD_DIR / stored
    try:
        raw = await file.read()
        if len(raw) > 12 * 1024 * 1024:
            return templates.TemplateResponse(
                request,
                "add.html",
                ctx(request, error="That file is too big. Save one week or print-to-PDF again."),
                status_code=400,
            )
        dest.write_bytes(raw)
        mime = file.content_type or ""
        original = file.filename or stored
        kid_name = kid["name"]
        parent_name = kid.get("parent") or ""
        sport_s = sport.strip()
        team_s = team_name.strip()
        notes_s = extra_notes.strip()
        upload_id = add_upload(kid_id, stored, original, mime)
        save_extraction(
            upload_id,
            {"pending": True, "sport": sport_s, "team_name": team_s, "parent_notes": notes_s},
            status="pending",
        )

        def _run():
            print(f"extract start upload={upload_id} file={dest} bytes={dest.stat().st_size}", flush=True)
            try:
                result = extract_schedule(
                    dest,
                    mime,
                    kid_name,
                    TIMEZONE,
                    parent_name=parent_name,
                    sport=sport_s,
                    team_name=team_s,
                    extra_notes=notes_s,
                )
                if team_s:
                    result["team_name"] = result.get("team_name") or team_s
                if sport_s:
                    result["sport"] = sport_s
                result["parent_notes"] = notes_s
                save_extraction(upload_id, result, status="extracted")
                print(f"extract done upload={upload_id} reader={result.get('reader')}", flush=True)
            except Exception as exc:
                print(f"extract fail upload={upload_id} {exc}", flush=True)
                save_extraction(
                    upload_id,
                    {
                        "summary": f"Grok failed: {exc}",
                        "error": str(exc),
                        "reader": "failed",
                        "events": [],
                        "questions": [],
                    },
                    status="extracted",
                )

        threading.Thread(target=_run, daemon=True).start()
        return RedirectResponse(f"/review/{upload_id}", status_code=303)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "add.html",
            ctx(
                request,
                error=f"Could not read that file. Try a screenshot or a simpler PDF. ({exc})",
            ),
            status_code=500,
        )


@app.get("/review/{upload_id}", response_class=HTMLResponse)
def review_page(request: Request, upload_id: int):
    gate = require_login(request)
    if gate:
        return gate
    upload = get_upload(upload_id)
    if not upload:
        return RedirectResponse("/", status_code=303)
    kid = get_kid(upload["kid_id"])
    import json

    payload = json.loads(upload["extraction_json"] or "{}")
    if upload.get("status") == "pending":
        return templates.TemplateResponse(
            request,
            "waiting.html",
            ctx(request, upload=upload, kid=kid),
        )
    return templates.TemplateResponse(
        request,
        "review.html",
        ctx(request, upload=upload, kid=kid, payload=payload),
    )


@app.post("/review/{upload_id}/refine")
async def review_refine(request: Request, upload_id: int):
    gate = require_login(request)
    if gate:
        return gate
    upload = get_upload(upload_id)
    if not upload:
        return RedirectResponse("/", status_code=303)
    form = await request.form()
    answers = {}
    for k, v in form.items():
        if not k.startswith("q_"):
            continue
        val = str(v).strip()
        if val:
            answers[k[2:]] = val
    import json

    payload = json.loads(upload["extraction_json"] or "{}")
    kid = get_kid(upload["kid_id"]) or {}
    updated = refine_with_answers(
        payload,
        answers,
        kid.get("name") or "",
        sport=payload.get("sport") or "",
        team_name=payload.get("team_name") or "",
    )
    save_extraction(upload_id, updated, status="extracted")
    return RedirectResponse(f"/review/{upload_id}", status_code=303)


@app.post("/review/{upload_id}")
async def review_save(request: Request, upload_id: int):
    gate = require_login(request)
    if gate:
        return gate
    upload = get_upload(upload_id)
    if not upload:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    answers = {k[2:]: v for k, v in form.items() if k.startswith("q_")}
    import json

    payload = json.loads(upload["extraction_json"] or "{}")
    payload["answers"] = answers
    tz = ZoneInfo(TIMEZONE)

    count = int(form.get("event_count") or 0)
    saved = 0
    for i in range(count):
        if form.get(f"e{i}_include") != "on":
            continue
        date = (form.get(f"e{i}_date") or "").strip()
        start_time = (form.get(f"e{i}_start") or "").strip()
        end_time = (form.get(f"e{i}_end") or "").strip()
        title = (form.get(f"e{i}_title") or "").strip()
        if not date or not title:
            continue
        all_day = form.get(f"e{i}_allday") == "on" or not start_time
        start_iso = _combine(date, start_time or "00:00", tz)
        end_iso = _combine(date, end_time, tz) if end_time else None
        if not end_iso and start_time:
            end_iso = (
                datetime.fromisoformat(start_iso) + timedelta(hours=1)
            ).isoformat()
        add_event(
            {
                "kid_id": upload["kid_id"],
                "upload_id": upload_id,
                "uid": f"{upload_id}-{i}-{uuid.uuid4().hex[:8]}@events.5udyk.com",
                "title": title,
                "event_type": form.get(f"e{i}_type") or "event",
                "start_iso": start_iso,
                "end_iso": end_iso,
                "all_day": all_day and not start_time,
                "location": (form.get(f"e{i}_location") or "").strip() or None,
                "opponent": (form.get(f"e{i}_opponent") or "").strip() or None,
                "notes": (form.get(f"e{i}_notes") or "").strip() or None,
            }
        )
        saved += 1

    payload["saved_count"] = saved
    save_extraction(upload_id, payload, status="published")
    return RedirectResponse("/?added=" + str(saved), status_code=303)


@app.post("/events/{event_id}/delete")
def event_delete(request: Request, event_id: int):
    gate = require_login(request)
    if gate:
        return gate
    delete_event(event_id)
    return RedirectResponse("/", status_code=303)


@app.get("/event/new", response_class=HTMLResponse)
def manual_new(request: Request):
    gate = require_login(request)
    if gate:
        return gate
    return templates.TemplateResponse(request, "manual.html", ctx(request))


@app.post("/event/new")
def manual_save(
    request: Request,
    kid_id: int = Form(...),
    title: str = Form(...),
    date: str = Form(...),
    start_time: str = Form(""),
    end_time: str = Form(""),
    location: str = Form(""),
    event_type: str = Form("event"),
    opponent: str = Form(""),
    notes: str = Form(""),
):
    gate = require_login(request)
    if gate:
        return gate
    tz = ZoneInfo(TIMEZONE)
    start_iso = _combine(date, start_time or "00:00", tz)
    end_iso = _combine(date, end_time, tz) if end_time else None
    add_event(
        {
            "kid_id": kid_id,
            "upload_id": None,
            "uid": f"manual-{uuid.uuid4().hex}@events.5udyk.com",
            "title": title.strip(),
            "event_type": event_type,
            "start_iso": start_iso,
            "end_iso": end_iso,
            "all_day": not start_time,
            "location": location.strip() or None,
            "opponent": opponent.strip() or None,
            "notes": notes.strip() or None,
        }
    )
    return RedirectResponse("/", status_code=303)


@app.get("/event/{event_id}.ics")
def calendar_one(event_id: int, token: str = ""):
    if not secrets.compare_digest(token, CAL_TOKEN):
        return Response("Forbidden", status_code=403)
    ev = get_event(event_id)
    if not ev:
        return Response("Not found", status_code=404)
    body = build_calendar([ev], ev["title"], TIMEZONE)
    return Response(body, media_type="text/calendar; charset=utf-8")


@app.get("/calendar.ics")
def calendar_all(token: str = ""):
    if not secrets.compare_digest(token, CAL_TOKEN):
        return Response("Forbidden", status_code=403)
    events = list_events()
    body = build_calendar(events, f"{FAMILY} family", TIMEZONE)
    return Response(body, media_type="text/calendar; charset=utf-8")


@app.get("/calendar/{kid_id}.ics")
def calendar_kid(kid_id: int, token: str = ""):
    if not secrets.compare_digest(token, CAL_TOKEN):
        return Response("Forbidden", status_code=403)
    kid = get_kid(kid_id)
    if not kid:
        return Response("Not found", status_code=404)
    events = list_events(kid_id=kid_id)
    body = build_calendar(events, f"{kid['name']} — {FAMILY}", TIMEZONE)
    return Response(body, media_type="text/calendar; charset=utf-8")


def _combine(date: str, time: str, tz: ZoneInfo) -> str:
    time = time or "00:00"
    if len(time) == 5:
        time = time + ":00"
    dt = datetime.fromisoformat(f"{date}T{time}")
    return dt.replace(tzinfo=tz).isoformat()


def _group_events(events: list[dict]) -> list[dict]:
    tz = ZoneInfo(TIMEZONE)
    groups: dict[str, list] = {}
    order: list[str] = []
    for ev in events:
        start = datetime.fromisoformat(ev["start_iso"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        else:
            start = start.astimezone(tz)
        key = start.strftime("%Y-%m-%d")
        ev = dict(ev)
        ev["local"] = start
        ev["day_label"] = start.strftime("%A, %B ") + str(start.day)
        ev["time_label"] = "All day" if ev.get("all_day") else start.strftime("%I:%M %p").lstrip("0")
        ev["google_url"] = event_google_url(ev, TIMEZONE)
        ev["ics_url"] = f"/event/{ev['id']}.ics?token={CAL_TOKEN}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(ev)
    return [{"date": k, "label": groups[k][0]["day_label"], "events": groups[k]} for k in order]
