"""The web board (design sections 1, 6 and 7).

FastAPI with server-rendered Jinja, and SortableJS to drag a card between
columns. Every write goes through `board.core`; this module never touches the
database. When `board.auth` has a verifier configured, the actor on every
event is the email a verified IAP or Access assertion, Google access token or
service-account ID token proves, and nothing else is trusted. With none
configured, the board is in local mode: the human is the email Cloudflare
Access puts in `Cf-Access-Authenticated-User-Email`, else `BOARD_WEB_ACTOR`;
the header still wins when both are present, and with neither a write is
refused. That bare header is only trustworthy behind Access, which is why
`main` binds to 127.0.0.1.

Binding to loopback does not stop DNS rebinding: a page on a name the attacker
re-points at 127.0.0.1 is same-origin with itself, so it passes the Origin
check and writes as `BOARD_WEB_ACTOR`. So every request must name an allowed
Host: `127.0.0.1:<port>`, `localhost:<port>`, or one of the comma-separated
hosts in `BOARD_WEB_HOSTS`, which is where a tunnel's hostname goes. Anything
else is refused with 403.

A change under someone else's live lease is refused by `core.edit` and comes
back as a take-over page. Taking over resubmits the same form with `preempt`,
which leaves the card held by the human with no expiry until Release moves
it back to `ready`.

The board at `/` is a view only. Which projects and lanes it shows is saved per
browser in the `board_prefs` cookie, set from `/preferences`, and searching is
its own page at `/search`. Projects are created, renamed and deleted at
`/projects`; only the name can change, and only an empty project can go.

Times are stored in UTC and shown in a zone: the one saved in `board_prefs`,
else `BOARD_TIMEZONE`, else UTC. An unknown name in either is skipped, and
one in the environment is logged rather than stopping the board.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from board import auth, core, store

ACTOR_HEADER = "Cf-Access-Authenticated-User-Email"
ACTOR_ENV = "BOARD_WEB_ACTOR"
HUMAN = "human"
DEFAULT_PORT = 28090
PORT_ENV = "BOARD_WEB_PORT"
HOSTS_ENV = "BOARD_WEB_HOSTS"
PREFS_COOKIE = "board_prefs"
PREFS_MAX_AGE = 365 * 24 * 3600
TZ_ENV = "BOARD_TIMEZONE"
UTC = ZoneInfo("UTC")

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Agents write bodies and notes too, so raw HTML is escaped, not passed through.
# markdown-it's link validator already refuses javascript:, vbscript: and file:.
# Images are off too: an agent-written ![](url) would make the reader's browser
# fetch a third-party URL just by opening the issue.
_md = MarkdownIt("commonmark", {"html": False}).disable("image")


def markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


def zone(name: str | None) -> ZoneInfo | None:
    """The IANA zone called `name`, or None if it is blank or unknown."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        # ZoneInfoNotFoundError is a KeyError; a path-like name is a ValueError.
        return None


def localtime(at, tz: ZoneInfo) -> str:
    """A stored UTC time, ISO string or datetime, as `YYYY-MM-DD HH:MM <abbrev>` in tz."""
    if at is None:
        return ""
    dt = datetime.fromisoformat(at) if isinstance(at, str) else at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


templates.env.filters["markdown"] = markdown
templates.env.filters["localtime"] = localtime


class Unauthenticated(Exception):
    pass


def lease_status(issue: dict, now: str) -> str | None:
    """How a card's lease renders: None, "live", "expired", or "held".

    "held" is a lease with no expiry, which only a human take-over leaves.
    """
    if issue["lease_holder"] is None:
        return None
    if issue["lease_expires_at"] is None:
        return "held"
    expired = datetime.fromisoformat(issue["lease_expires_at"]) < datetime.fromisoformat(now)
    return "expired" if expired else "live"


# One fill per lane, so a step's colour on the workflow diagram names its state.
_STATE_FILLS = {
    "backlog": "#dfe1e6", "ready": "#deebff", "need-input": "#fffae6",
    "processing": "#b3d4ff", "onhold": "#eae6ff", "done": "#e3fcef", "cancelled": "#f4f5f7",
}


def _mermaid_text(text: str) -> str:
    """Text safe inside a quoted Mermaid label: every non-alphanumeric is an entity code.

    Agents write titles, so a quote, bracket, `-->`, `%%` or newline in one must
    not become flowchart syntax; Mermaid decodes `#34;` back to the character.
    It then runs the decoded label through markdown, KaTeX and Font Awesome, so:
    markdown punctuation also gets a backslash (`#92;`) that markdown consumes,
    or a backticked name would render as a code span; and a zero-width space
    follows every `$` and `:`, so no `$$...$$` or `fa:fa-x` survives to match.
    The HTML characters take no backslash: markdown would re-escape them.
    """
    out = []
    for c in text:
        if c.isascii() and (c.isalnum() or c == " "):
            out.append(c)
        elif c.isascii() and c.isprintable() and c not in "<>&\"'":
            out.append(f"#92;#{ord(c)};")
        else:
            out.append(f"#{ord(c)};")
        if c in "$:":
            out.append("#8203;")
    return "".join(out)


def _state_class(state: str) -> str:
    return "st_" + "".join(c if c.isalnum() else "_" for c in state)


def workflow_target(wf: dict) -> str | None:
    """The step a workflow's link opens: the first not yet done, else the first step."""
    steps = wf["steps"]
    open_steps = [st for st in steps if st["state"] != "done"]
    return (open_steps or steps or [{"id": None}])[0]["id"]


# Above this many steps the diagram is unreadable, so the page shows only the list.
DIAGRAM_MAX_STEPS = 4
# How many steps of a long workflow's list show before "Show N more".
LIST_WINDOW = 10


def workflow_list(wf: dict, here: str | None) -> dict:
    """A workflow's step list split into the visible window and the rest.

    The window holds `LIST_WINDOW` steps starting four before the step whose
    id is `here`, clamped to the list, so a late step's page still shows it.
    Each run is `(start, steps)`, `start` being the 1-based number of its
    first step; `before` and `after` are the hidden runs either side.
    """
    steps = wf["steps"]
    ids = [st["id"] for st in steps]
    at = ids.index(here) if here in ids else 0
    lo = min(max(0, at - 4), max(0, len(steps) - LIST_WINDOW))
    hi = lo + LIST_WINDOW
    return {"shown": (lo + 1, steps[lo:hi]), "before": (1, steps[:lo]),
            "after": (hi + 1, steps[hi:]), "hidden": lo + len(steps[hi:])}


def workflow_diagram(wf: dict, here: str | None) -> str:
    """The workflow as Mermaid `flowchart LR` source, one clickable node per step.

    Node ids are positional (`s0`, `s1`, ...) so no issue id is ever parsed as
    syntax; the id, title and state appear only inside the escaped label.
    The step whose id is `here` is the one outlined.
    """
    lines = ["flowchart LR"]
    for state, fill in _STATE_FILLS.items():
        lines.append(f"  classDef {_state_class(state)} fill:{fill},stroke:#42526e")
    lines.append("  classDef here stroke:#0052cc,stroke-width:3px")
    steps = wf["steps"]
    for n, step in enumerate(steps):
        label = "<br/>".join(_mermaid_text(part)
                             for part in (step["id"], step["title"], step["state"]))
        lines.append(f'  s{n}["{label}"]')
    if len(steps) > 1:
        lines.append("  " + " --> ".join(f"s{n}" for n in range(len(steps))))
    for n, step in enumerate(steps):
        lines.append(f'  click s{n} "/issues/{quote(step["id"], safe="")}"')
        # One class per line: Mermaid reads `a,b` as a single class name.
        lines.append(f"  class s{n} {_state_class(step['state'])}")
        if step["id"] == here:
            lines.append(f"  class s{n} here")
    return "\n".join(lines)


def _local(request: Request) -> auth.Identity | None:
    """Local mode's human: Access's bare header, else BOARD_WEB_ACTOR."""
    email = request.headers.get(ACTOR_HEADER) or os.environ.get(ACTOR_ENV, "").strip()
    return auth.Identity(email, HUMAN) if email else None


def _port() -> int:
    return int(os.environ.get(PORT_ENV, DEFAULT_PORT))


def allowed_hosts() -> frozenset[str]:
    """The Host values this server answers: loopback on its port, plus BOARD_WEB_HOSTS."""
    port = _port()
    names = [f"127.0.0.1:{port}", f"localhost:{port}",
             *os.environ.get(HOSTS_ENV, "").split(",")]
    return frozenset(n.strip().lower() for n in names if n.strip())


def _same_origin(request: Request) -> bool:
    """Refuse a cross-site form post; Access's cookie would otherwise carry it."""
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


def _form_int(value):
    return int(value) if value not in (None, "") else None


def _form_labels(value):
    if value is None:
        return None
    return [l.strip() for l in value.split(",") if l.strip()]


def read_prefs(raw: str | None) -> dict:
    """The saved view from a `board_prefs` cookie: projects, lanes and timezone.

    The value is JSON, percent-encoded so a browser sends it back unmangled.
    Anything unreadable, and any other key, is ignored. A list key that is
    absent or not a list of strings comes back as [], which means all; a
    timezone that is not a string comes back as "", which means the default.
    """
    try:
        data = json.loads(unquote(raw or ""))
    except ValueError:
        data = None
    data = data if isinstance(data, dict) else {}
    prefs = {}
    for key in ("projects", "lanes"):
        value = data.get(key)
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        prefs[key] = value if ok else []
    tz = data.get("timezone")
    prefs["timezone"] = tz if isinstance(tz, str) else ""
    return prefs


def _chosen(saved: list[str], known: list[str]) -> list[str]:
    """The known values the user saved, in known order; all of them if none survive."""
    picked = [k for k in known if k in saved]
    return picked or list(known)


def create_app(engine=None, authenticator: auth.Authenticator | None = None) -> FastAPI:
    engine = engine if engine is not None else store.make_engine()
    authn = authenticator if authenticator is not None else auth.Authenticator.from_env(os.environ)
    app = FastAPI(title="agent-board")
    default_tz = zone(os.environ.get(TZ_ENV))
    if default_tz is None and os.environ.get(TZ_ENV, "").strip():
        log.warning("%s=%r is not a known timezone; showing times in UTC",
                    TZ_ENV, os.environ[TZ_ENV])
    default_tz = default_tz or UTC

    def who(request) -> auth.Identity | None:
        # Resolved once per request: a Bearer token costs a Google round trip.
        if not hasattr(request.state, "identity"):
            request.state.identity = (authn.identify(request.headers) if authn.verified
                                      else _local(request))
        return request.state.identity

    def actor(request) -> auth.Identity:
        found = who(request)
        if found is None:
            raise Unauthenticated()
        return found

    def page(request, name, status=200, **ctx):
        me = who(request)
        saved_tz = read_prefs(request.cookies.get(PREFS_COOKIE))["timezone"]
        ctx |= {"me": me.email if me else None, "lease_status": lease_status,
                "tz": zone(saved_tz) or default_tz,
                "moves": lambda issue: sorted(core.TRANSITIONS[issue["state"]])}
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def error(request, status, message, issue_id=None):
        return page(request, "error.html", status, message=message, issue_id=issue_id)

    def back(issue_id=None):
        return RedirectResponse(f"/issues/{issue_id}" if issue_id else "/", status_code=303)

    hosts = allowed_hosts()

    @app.middleware("http")
    async def host_and_origin_check(request: Request, call_next):
        if request.headers.get("host", "").lower() not in hosts:
            return HTMLResponse("unknown host refused", status_code=403)
        if request.method == "POST" and not _same_origin(request):
            return HTMLResponse("cross-origin post refused", status_code=403)
        return await call_next(request)

    @app.exception_handler(Unauthenticated)
    async def unauthenticated(request, _exc):
        if authn.verified:
            return error(request, 401, "No verified, allowed credential on this request: "
                         "writes need an IAP or Access assertion, or a Google token.")
        return error(request, 401, f"No {ACTOR_HEADER} header and no {ACTOR_ENV} set: "
                     "writes need Cloudflare Access or a local actor.")

    @app.exception_handler(core.NotFound)
    async def not_found(request, exc):
        return error(request, 404, str(exc))

    @app.exception_handler(core.LeaseLost)
    async def lease_lost(request, exc):
        return error(request, 409, str(exc))

    @app.exception_handler(core.Conflict)
    async def conflict(request, exc):
        return error(request, 409, str(exc))

    @app.exception_handler(core.BoardError)
    async def board_rule(request, exc):
        return error(request, 422, str(exc))

    def view_prefs(request, projects):
        saved = read_prefs(request.cookies.get(PREFS_COOKIE))
        return {"projects": _chosen(saved["projects"], [p["key"] for p in projects]),
                "lanes": _chosen(saved["lanes"], list(core.TRANSITIONS)),
                "timezone": saved["timezone"]}

    @app.get("/", response_class=HTMLResponse)
    def board_page(request: Request):
        view = core.overview(engine)
        prefs = view_prefs(request, view["projects"])
        tracked = set(prefs["projects"])
        # A workflow has no project of its own; it belongs to its steps' projects.
        wf_projects = {}
        for i in view["issues"]:
            if i["workflow_id"] is not None:
                wf_projects.setdefault(i["workflow_id"], set()).add(i["project"])
        workflows = []
        for wf in view["workflows"]:
            if wf_projects.get(wf["id"], set()) & tracked:
                current = next((st for st in wf["steps"] if st["state"] != "done"), None)
                workflows.append(wf | {"current": current, "target": workflow_target(wf)})
        loose = [i for i in view["issues"]
                 if i["workflow_id"] is None and i["project"] in tracked]
        # The workflow is the card: its computed state picks the column, and
        # its steps show on their issue pages rather than as loose cards here.
        columns = [(state, [w for w in workflows if w["state"] == state],
                    [i for i in loose if i["state"] == state])
                   for state in prefs["lanes"]]
        return page(request, "board.html", view=view, columns=columns, now=view["now"])

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", project: str = "", label: str = "",
                    assignee: str = ""):
        filters = {"q": q.strip(), "project": project, "label": label, "assignee": assignee}
        view = core.overview(engine, **filters)
        # With no filter at all the page is just the form, not every issue.
        searched = any(filters.values())
        return page(request, "search.html", view=view, filters=filters, searched=searched,
                    results=view["issues"] if searched else [])

    @app.get("/preferences", response_class=HTMLResponse)
    def preferences_page(request: Request):
        view = core.overview(engine)
        return page(request, "preferences.html", view=view, lanes=list(core.TRANSITIONS),
                    prefs=view_prefs(request, view["projects"]), default_tz=default_tz)

    @app.post("/preferences")
    async def save_preferences(request: Request):
        f = await request.form()
        known = {"projects": [p["key"] for p in core.overview(engine)["projects"]],
                 "lanes": list(core.TRANSITIONS)}
        sent = {"projects": f.getlist("project"), "lanes": f.getlist("lane")}
        # Only known values are kept, in board order, so the cookie stays small.
        # Every box ticked is saved as [], which means all, so a project or lane
        # added later shows up rather than staying hidden.
        prefs = {}
        for key, values in known.items():
            picked = [v for v in values if v in sent[key]]
            prefs[key] = [] if len(picked) == len(values) else picked
        # An unknown zone is saved blank, which means the default.
        tz = (f.get("timezone") or "").strip()
        prefs["timezone"] = tz if zone(tz) else ""
        r = back()
        # safe="": a bare "/" (Asia/Tokyo) makes Starlette quote the whole value.
        r.set_cookie(PREFS_COOKIE, quote(json.dumps(prefs, separators=(",", ":")), safe=""),
                     max_age=PREFS_MAX_AGE, samesite="lax", httponly=True)
        return r

    @app.get("/projects", response_class=HTMLResponse)
    def projects_page(request: Request):
        return page(request, "projects.html", projects=core.projects(engine))

    @app.post("/projects")
    async def create_project(request: Request):
        await run_in_threadpool(actor, request)
        f = await request.form()
        core.create_project(engine, f.get("key", ""), f.get("name", ""))
        return RedirectResponse("/projects", status_code=303)

    @app.post("/projects/{key}/rename")
    async def rename_project(request: Request, key: str):
        await run_in_threadpool(actor, request)
        f = await request.form()
        core.rename_project(engine, key, f.get("name", ""))
        return RedirectResponse("/projects", status_code=303)

    @app.post("/projects/{key}/delete")
    def delete_project(request: Request, key: str):
        actor(request)
        core.delete_project(engine, key)
        return RedirectResponse("/projects", status_code=303)

    @app.get("/workflows/{workflow_id}")
    def workflow_page(workflow_id: int):
        """A workflow shows on its steps' issue pages; links already sent land on one."""
        target = workflow_target(core.workflow(engine, workflow_id))
        if target is None:
            raise core.NotFound(f"workflow {workflow_id} has no steps")
        return RedirectResponse(f"/issues/{quote(target, safe='')}", status_code=303)

    @app.get("/issues/{issue_id}", response_class=HTMLResponse)
    def issue_page(request: Request, issue_id: str):
        issue = core.show(engine, issue_id)
        wf = issue["workflow"]
        return page(request, "issue.html", issue=issue, now=core.overview(engine)["now"],
                    states=list(core.TRANSITIONS),
                    diagram=(workflow_diagram(wf, issue["id"])
                             if wf and len(wf["steps"]) <= DIAGRAM_MAX_STEPS else None),
                    steps=workflow_list(wf, issue["id"]) if wf else None)

    @app.post("/issues")
    async def create_issue(request: Request):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        issue = core.create(engine, f.get("project", ""), f.get("title", ""), actor=me.email,
                            actor_kind=me.kind, body=f.get("body", ""),
                            state=f.get("state") or "backlog", rank=_form_int(f.get("rank")),
                            labels=_form_labels(f.get("labels")) or ())
        return back(issue["id"])

    @app.post("/workflows")
    async def instantiate(request: Request):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        core.instantiate(engine, f.get("template", ""), f.get("project", ""), actor=me.email,
                         actor_kind=me.kind, title=f.get("title") or None)
        return back()

    @app.post("/issues/{issue_id}/edit")
    async def edit(request: Request, issue_id: str):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        current = core.show(engine, issue_id)
        state = f.get("state")
        try:
            core.edit(engine, issue_id, actor=me.email, actor_kind=me.kind,
                      expected_version=int(f.get("version", -1)),
                      preempt=f.get("preempt") == "1",
                      title=f.get("title"), body=f.get("body"),
                      rank=_form_int(f.get("rank")), labels=_form_labels(f.get("labels")),
                      state=state if state and state != current["state"] else None,
                      note=f.get("note") or None)
        except core.LeaseHeld as held:
            fields = {k: v for k, v in f.items() if k != "preempt"}
            return page(request, "takeover.html", 409, issue=current, held=held,
                        fields=fields)
        except core.Conflict:
            return error(request, 409, f"{issue_id} changed since you loaded it. "
                         "Reload the card to see what changed, then save again.", issue_id)
        return back(issue_id)

    @app.post("/issues/{issue_id}/move")
    async def move(request: Request, issue_id: str):
        """A drag between columns: a state change from the board, answered in JSON."""
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        try:
            issue = core.edit(engine, issue_id, actor=me.email, actor_kind=me.kind,
                              expected_version=int(f.get("version", -1)),
                              state=f.get("state"), note=f.get("note") or None)
        except core.BoardError as exc:
            status = 409 if isinstance(exc, (core.Conflict, core.LeaseLost)) else 422
            return JSONResponse({"error": str(exc)}, status_code=status)
        return {"id": issue["id"], "state": issue["state"], "version": issue["version"]}

    @app.post("/issues/{issue_id}/note")
    async def note(request: Request, issue_id: str):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        core.annotate(engine, issue_id, f.get("note", ""), actor=me.email, actor_kind=me.kind)
        return back(issue_id)

    @app.post("/issues/{issue_id}/link")
    async def link(request: Request, issue_id: str):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        core.link(engine, issue_id, f.get("ref", ""), kind=f.get("kind", ""), actor=me.email,
                  actor_kind=me.kind, closes=f.get("closes") == "1")
        return back(issue_id)

    @app.post("/issues/{issue_id}/release")
    def release(request: Request, issue_id: str):
        """Hand a card back to the queue. Refused under someone else's lease."""
        me = actor(request)
        core.transition(engine, issue_id, "ready", actor=me.email, actor_kind=me.kind)
        return back(issue_id)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=_port())
