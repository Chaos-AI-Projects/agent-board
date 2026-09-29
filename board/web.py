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

Files can be attached when an issue is created or edited and when a note is
added (MS-643). The bytes go to `BOARD_ATTACHMENT_DIR`, named by SHA-256, and
only their metadata goes to the database; each file is capped at
`BOARD_MAX_UPLOAD_MB` (default 25). `/attachments/<id>` serves a raster image
inline and everything else, HTML and SVG included, as a download: the board
writes as the reader, so an uploaded page opening on its origin could act as them.

Times are stored in UTC and shown in a zone: the one saved in `board_prefs`,
else `BOARD_TIMEZONE`, else UTC. An unknown name in either is skipped, and
one in the environment is logged rather than stopping the board.

With Google sign-in configured (`board.signin`, MS-648) the board logs a browser
in by itself. The actor is then, in order: a verified assertion, a Bearer
token, the session cookie. Local mode is off, and every path but `/login` and
`/auth/callback` needs one of the three. An anonymous browser GET is sent to
`/login` and comes back to where it was; anything else gets 401.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from mcp.server.auth.provider import construct_redirect_uri

from board import auth, core, oauth, remote, signin, store

ACTOR_HEADER = "Cf-Access-Authenticated-User-Email"
ACTOR_ENV = "BOARD_WEB_ACTOR"
HUMAN = "human"
DEFAULT_PORT = 28090
PORT_ENV = "BOARD_WEB_PORT"
HOSTS_ENV = "BOARD_WEB_HOSTS"
PREFS_COOKIE = "board_prefs"
PREFS_MAX_AGE = 365 * 24 * 3600
TZ_ENV = "BOARD_TIMEZONE"
ATTACH_DIR_ENV = "BOARD_ATTACHMENT_DIR"
MAX_UPLOAD_ENV = "BOARD_MAX_UPLOAD_MB"
DEFAULT_MAX_UPLOAD_MB = 25
# Raster images only: an SVG can carry script, so it downloads like HTML does.
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
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


def filesize(n: int) -> str:
    """A byte count as B, KB or MB, one decimal above bytes."""
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def project_colour(key: str, bucket: int | None = None) -> dict:
    """A project's card colours: a hue from its bucket, a shade from its key (MS-655).

    The bucket is the one stored at creation; None falls back to the key's
    hash bucket. The key, not the name, sets the shade, because a rename must
    not recolour every card, and sha256 rather than hash(), which Python salts
    per process. Two projects sharing a bucket differ by up to 10 deg of hue
    and a small lightness step; saturation stays MS-652's so dark text reads
    on every tint.
    """
    if bucket is None:
        bucket = core.key_bucket(key)
    shade = int.from_bytes(hashlib.sha256(key.encode()).digest()[4:8], "big")
    h = (bucket * 360 // core.BUCKETS + shade % 21 - 10) % 360
    step = (shade // 21) % 3 - 1
    return {"bg": f"hsl({h} 70% {92 + 3 * step}%)", "border": f"hsl({h} 65% {45 + 4 * step}%)"}


templates.env.globals["project_colour"] = project_colour
templates.env.filters["markdown"] = markdown
templates.env.filters["filesize"] = filesize
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


# The chart draws this many steps either side of the current one; the rest collapse.
DIAGRAM_REACH = 3
# The collapsed full step list under the chart, which a collapse node opens.
STEP_LIST_ANCHOR = "workflow-steps"
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


def _diagram_window(wf: dict, here: str | None) -> tuple[int, int]:
    """The `[lo, hi)` slice of steps the chart draws.

    It centres on the step whose id is `here`, else (an origin issue's page)
    on the step the workflow's link opens, and reaches `DIAGRAM_REACH` steps
    either side. Near an end it is cut short there rather than slid inward,
    so step 1 of 12 draws steps 1 to 4 (Chaos, 2026-09-29).
    """
    steps = wf["steps"]
    ids = [st["id"] for st in steps]
    centre = here if here in ids else workflow_target(wf)
    at = ids.index(centre) if centre in ids else 0
    return _reach(len(steps), at)


def _reach(count: int, at: int) -> tuple[int, int]:
    """`[lo, hi)` around `at`, `DIAGRAM_REACH` either side, cut to `[0, count)`."""
    return max(0, at - DIAGRAM_REACH), min(count, at + DIAGRAM_REACH + 1)


def _dependency_layers(steps: list[dict]) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Each step's layer, and its dependencies inside the workflow (MS-646).

    A step with no dependency inside the workflow is layer 0; any other sits
    one layer past its deepest dependency. A dependency on an issue outside
    the workflow is not drawn: it only frees the workflow from strict order.
    Core refuses cycles, so the steps always settle.
    """
    ids = {st["id"] for st in steps}
    deps = {st["id"]: [d for d in st.get("depends_on", ()) if d in ids] for st in steps}
    layer: dict[str, int] = {}
    while len(layer) < len(deps):
        settled = len(layer)
        for i, on in deps.items():
            if i not in layer and all(d in layer for d in on):
                layer[i] = 1 + max((layer[d] for d in on), default=-1)
        if len(layer) == settled:
            raise ValueError("dependency cycle in workflow")
    return layer, deps


def _layer_window(layers: int, at: int) -> tuple[int, int]:
    """The `[lo, hi)` layers the chart draws, `_diagram_window`'s rule over layers."""
    return _reach(layers, at)


def workflow_diagram(wf: dict, here: str | None) -> str:
    """The workflow as Mermaid `flowchart LR` source in BPMN notation (MS-645).

    A start event (circle), one rounded task per step, an end event (double
    circle). Only the window around the current step is drawn: the steps
    outside it collapse into a "+N earlier" / "+N later" node that opens the
    full step list. Node ids are positional (`s0`, `s1`, ...) so no issue id
    is ever parsed as syntax; the id, title and state appear only inside the
    escaped label. The step whose id is `here` is the one outlined.

    A workflow with no declared dependencies is strictly ordered: one sequence
    flow, no gateways. Once any step declares one (MS-646) the chart draws the
    dependency graph instead, with a parallel gateway at every fork and join,
    and the window counts dependency layers rather than positions.
    """
    lines = ["flowchart LR"]
    for state, fill in _STATE_FILLS.items():
        lines.append(f"  classDef {_state_class(state)} fill:{fill},stroke:#42526e")
    lines.append("  classDef here stroke:#0052cc,stroke-width:3px")
    lines.append("  classDef event fill:#ffffff,stroke:#42526e,stroke-width:2px")
    lines.append("  classDef more fill:#ffffff,stroke:#6b778c,stroke-dasharray:4 3")
    lines.append("  classDef gateway fill:#ffffff,stroke:#42526e")
    steps = wf["steps"]
    # `end` is a flowchart keyword, so the events carry a prefix. The event,
    # collapse and gateway labels are one line, which mermaid renders without
    # markdown, so they take a bare `#43;` for `+`: `_mermaid_text`'s `#92;` would show.
    # Both events are declared before any flow names them, so each keeps its shape.
    lines.append('  ev_start(("start"))')
    lines.append('  ev_end((("end")))')
    if any(st.get("depends_on") for st in steps):
        shown, more = _dag_flows(lines, wf, here)
    else:
        shown, more = _strict_flows(lines, wf, here)
    for n in shown:
        step = steps[n]
        lines.append(f'  click s{n} "/issues/{quote(step["id"], safe="")}"')
        # One class per line: Mermaid reads `a,b` as a single class name.
        lines.append(f"  class s{n} {_state_class(step['state'])}")
        if step["id"] == here:
            lines.append(f"  class s{n} here")
    for node in more:
        lines.append(f'  click {node} "#{STEP_LIST_ANCHOR}"')
        lines.append(f"  class {node} more")
    lines.append("  class ev_start event")
    lines.append("  class ev_end event")
    return "\n".join(lines)


def _task(lines: list[str], steps: list[dict], n: int) -> None:
    step = steps[n]
    label = "<br/>".join(_mermaid_text(part)
                         for part in (step["id"], step["title"], step["state"]))
    lines.append(f'  s{n}("{label}")')


def _strict_flows(lines: list[str], wf: dict, here: str | None):
    """A strictly ordered workflow: one chain through the window of positions."""
    steps = wf["steps"]
    lo, hi = _diagram_window(wf, here)
    chain = ["ev_start"]
    if lo:
        lines.append(f'  more_before["#43;{lo} earlier"]')
        chain.append("more_before")
    for n in range(lo, hi):
        _task(lines, steps, n)
        chain.append(f"s{n}")
    if hi < len(steps):
        lines.append(f'  more_after["#43;{len(steps) - hi} later"]')
        chain.append("more_after")
    chain.append("ev_end")
    lines.append("  " + " --> ".join(chain))
    return range(lo, hi), [n for n in ("more_before", "more_after") if n in chain]


def _dag_flows(lines: list[str], wf: dict, here: str | None):
    """A workflow with declared dependencies: its graph through a window of layers.

    Every dependency inside the workflow is a sequence flow; a step with none
    starts from the start event, and a step nothing waits on flows to the end.
    Steps outside the window collapse into the "+N" nodes and their flows
    follow them. A node with several outgoing flows forks through a parallel
    gateway (diamond, "+"), and one with several incoming flows joins through one.
    """
    steps = wf["steps"]
    layer, deps = _dependency_layers(steps)
    ids = [st["id"] for st in steps]
    centre = here if here in layer else workflow_target(wf)
    lo, hi = _layer_window(max(layer.values()) + 1, layer.get(centre, 0))
    node = {}
    for n, i in enumerate(ids):
        node[i] = ("more_before" if layer[i] < lo
                   else "more_after" if layer[i] >= hi else f"s{n}")
    before = sum(1 for i in ids if layer[i] < lo)
    after = sum(1 for i in ids if layer[i] >= hi)
    if before:
        lines.append(f'  more_before["#43;{before} earlier"]')
    shown = [n for n, i in enumerate(ids) if lo <= layer[i] < hi]
    for n in shown:
        _task(lines, steps, n)
    if after:
        lines.append(f'  more_after["#43;{after} later"]')

    waited_on = {d for on in deps.values() for d in on}
    flows: list[tuple[str, str]] = []
    for i in ids:
        sources = [node[d] for d in deps[i]] or ["ev_start"]
        for a in sources:
            if a != node[i] and (a, node[i]) not in flows:
                flows.append((a, node[i]))
        if i not in waited_on and (node[i], "ev_end") not in flows:
            flows.append((node[i], "ev_end"))
    out = {a: sum(1 for x, _ in flows if x == a) for a, _ in flows}
    into = {b: sum(1 for _, y in flows if y == b) for _, b in flows}
    forks = [a for a in dict.fromkeys(a for a, _ in flows) if out[a] > 1]
    joins = [b for b in dict.fromkeys(b for _, b in flows) if into[b] > 1]
    for g in [f"gf_{a}" for a in forks] + [f"gj_{b}" for b in joins]:
        lines.append(f'  {g}{{"#43;"}}')
    for a in forks:
        lines.append(f"  {a} --> gf_{a}")
    for a, b in flows:
        lines.append(f"  {'gf_' + a if a in forks else a} --> {'gj_' + b if b in joins else b}")
    for b in joins:
        lines.append(f"  gj_{b} --> {b}")
    for g in [f"gf_{a}" for a in forks] + [f"gj_{b}" for b in joins]:
        lines.append(f"  class {g} gateway")
    return shown, [n for n in ("more_before", "more_after") if n in node.values()]


def _local(request: Request) -> auth.Identity | None:
    """Local mode's human: Access's bare header, else BOARD_WEB_ACTOR."""
    email = request.headers.get(ACTOR_HEADER) or os.environ.get(ACTOR_ENV, "").strip()
    return auth.Identity(email, HUMAN) if email else None


def _port() -> int:
    return int(os.environ.get(PORT_ENV, DEFAULT_PORT))


def _hosts_env() -> list[str]:
    return [n.strip().lower() for n in os.environ.get(HOSTS_ENV, "").split(",") if n.strip()]


def allowed_hosts() -> frozenset[str]:
    """The Host values this server answers: loopback on its port, plus BOARD_WEB_HOSTS."""
    port = _port()
    return frozenset([f"127.0.0.1:{port}", f"localhost:{port}", *_hosts_env()])


def _same_origin(request: Request) -> bool:
    """Refuse a cross-site form post; Access's cookie would otherwise carry it."""
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


class TooLarge(Exception):
    pass


def safe_filename(name: str | None) -> str:
    """A client's filename cut to its last path component, without control characters."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C").strip()
    return name[:255] if name not in ("", ".", "..") else "file"


def _max_upload_bytes() -> int:
    """The cap in bytes. A value that is not a positive finite number means the default."""
    try:
        mb = float(os.environ.get(MAX_UPLOAD_ENV, "").strip() or DEFAULT_MAX_UPLOAD_MB)
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    if not (0 < mb < float("inf")):
        mb = DEFAULT_MAX_UPLOAD_MB
    return int(mb * 1024 * 1024)


async def save_uploads(form) -> list[dict]:
    """Write the form's `files` to BOARD_ATTACHMENT_DIR and describe them for `core`.

    Every file is checked against the cap before any is kept, so a refused
    upload leaves nothing behind. A file is stored under its SHA-256, so the
    same bytes uploaded twice take one file.
    """
    uploads = [f for f in form.getlist("files")
               if not isinstance(f, str) and f.filename]
    if not uploads:
        return []
    root = os.environ.get(ATTACH_DIR_ENV, "").strip()
    if not root:
        raise core.BoardError(f"{ATTACH_DIR_ENV} is not set, so this board takes no files")
    cap = _max_upload_bytes()
    for up in uploads:
        if up.size is not None and up.size > cap:
            raise TooLarge(safe_filename(up.filename))
    blobs = []
    for up in uploads:
        data = await up.read(cap + 1)
        if len(data) > cap:
            raise TooLarge(safe_filename(up.filename))
        blobs.append((up, data))
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    described = []
    for up, data in blobs:
        digest = hashlib.sha256(data).hexdigest()
        target = root / digest
        if not target.exists():
            fd, tmp = tempfile.mkstemp(dir=root)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
            os.replace(tmp, target)
        described.append({"filename": safe_filename(up.filename),
                          "content_type": (up.content_type or "application/octet-stream")[:200],
                          "size": len(data), "sha256": digest})
    return described


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


_FROM_ENV = object()
# Reachable signed out: the sign-in flow itself, and signing out.
SIGNIN_PATHS = frozenset({"/login", signin.CALLBACK_PATH, "/logout"})


def create_app(engine=None, authenticator: auth.Authenticator | None = None,
               sign_in=_FROM_ENV) -> FastAPI:
    engine = engine if engine is not None else store.make_engine()
    authn = authenticator if authenticator is not None else auth.Authenticator.from_env(os.environ)
    # A half-configured sign-in raises here, so the board refuses to start.
    google = signin.SignIn.from_env(os.environ) if sign_in is _FROM_ENV else sign_in
    # The remote-MCP authorization server and /mcp exist only beside sign-in (MS-649).
    provider = oauth.Provider(engine, secret=google.secret) if google is not None else None
    mcp_routes, lifespan = [], None
    if provider is not None:
        mcp_routes, lifespan = remote.build(
            engine, provider, authn, lambda email: email in google.allowed,
            remote.issuer(_hosts_env(), f"127.0.0.1:{_port()}"))
    app = FastAPI(title="agent-board", lifespan=lifespan)
    app.router.routes.extend(mcp_routes)
    app.state.oauth = provider
    default_tz = zone(os.environ.get(TZ_ENV))
    if default_tz is None and os.environ.get(TZ_ENV, "").strip():
        log.warning("%s=%r is not a known timezone; showing times in UTC",
                    TZ_ENV, os.environ[TZ_ENV])
    default_tz = default_tz or UTC

    def who(request) -> auth.Identity | None:
        # Resolved once per request: a Bearer token costs a Google round trip.
        if not hasattr(request.state, "identity"):
            found = authn.identify(request.headers) if authn.verified else None
            if found is None and google is not None:
                found = google.session(request.cookies.get(signin.SESSION_COOKIE))
            if found is None and not authn.verified and google is None:
                found = _local(request)
            request.state.identity = found
        return request.state.identity

    def actor(request) -> auth.Identity:
        found = who(request)
        if found is None:
            raise Unauthenticated()
        return found

    def page(request, name, status=200, **ctx):
        me = who(request)
        saved_tz = read_prefs(request.cookies.get(PREFS_COOKIE))["timezone"]
        buckets = core.colour_buckets(engine)
        ctx |= {"me": me.email if me else None, "lease_status": lease_status,
                "project_colour": lambda key: project_colour(key, buckets.get(key)),
                "tz": zone(saved_tz) or default_tz, "inline_types": INLINE_TYPES,
                "sign_in": google is not None,
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
        # The remote-MCP paths carry their own authentication, or grant nothing.
        if google is not None and request.url.path in remote.PATHS:
            return await call_next(request)
        if request.method == "POST" and not _same_origin(request):
            return HTMLResponse("cross-origin post refused", status_code=403)
        if (google is not None and request.url.path not in SIGNIN_PATHS
                and await run_in_threadpool(who, request) is None):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=303)
            return HTMLResponse("sign in first", status_code=401)
        return await call_next(request)

    if google is not None:
        @app.get("/login")
        def login(request: Request, next: str = "/"):
            url, flow = google.start(request.headers["host"], next)
            r = RedirectResponse(url, status_code=303)
            r.set_cookie(signin.FLOW_COOKIE, flow, max_age=signin.FLOW_SECONDS,
                         path=signin.CALLBACK_PATH, httponly=True, secure=True, samesite="lax")
            return r

        @app.get(signin.CALLBACK_PATH)
        def callback(request: Request):
            done = google.finish(request.query_params, request.cookies.get(signin.FLOW_COOKIE))
            if done is None:
                r = error(request, 401, "Sign-in failed. Start again from /login.")
            else:
                identity, next_path = done
                r = RedirectResponse(next_path, status_code=303)
                r.set_cookie(signin.SESSION_COOKIE, google.session_cookie(identity),
                             max_age=google.session_seconds, httponly=True, secure=True,
                             samesite="lax")
            r.delete_cookie(signin.FLOW_COOKIE, path=signin.CALLBACK_PATH, httponly=True,
                            secure=True, samesite="lax")
            return r

        @app.post("/logout")
        def logout(request: Request):
            request.state.identity = None
            r = page(request, "signed_out.html")
            r.delete_cookie(signin.SESSION_COOKIE, httponly=True, secure=True, samesite="lax")
            return r

        async def consent_request(req):
            """The registered client and request a consent token names, or None."""
            found = provider.pending(req)
            client = await provider.get_client(found[0]) if found else None
            return (client, found[1]) if client is not None else None

        def bad_consent(request, message="This authorization request is invalid or has "
                        "expired. Start again from the app that sent you here."):
            return error(request, 400, message)

        @app.get(oauth.CONSENT_PATH, response_class=HTMLResponse)
        async def consent_page(request: Request, req: str = ""):
            found = await consent_request(req)
            if found is None:
                return bad_consent(request)
            client, params = found
            r = page(request, "consent.html", req=req, params=params,
                     client_name=client.client_name or client.client_id)
            r.headers["content-security-policy"] = "frame-ancestors 'none'"
            return r

        @app.post(oauth.CONSENT_PATH)
        async def consent(request: Request):
            form = await request.form()
            found = await consent_request(form.get("req"))
            if found is None:
                return bad_consent(request)
            client, params = found
            # The session cookie only: a Bearer or Access credential is not a
            # human at a browser, and would turn an hour-long Google token
            # into a 30-day board grant.
            me = google.session(request.cookies.get(signin.SESSION_COOKIE))
            if me is None:
                raise Unauthenticated()
            decision = form.get("decision")
            if decision == "approve":
                code = await run_in_threadpool(provider.issue_code, client.client_id,
                                               params, me.email)
                target = construct_redirect_uri(str(params.redirect_uri), code=code,
                                                state=params.state)
            elif decision == "deny":
                target = construct_redirect_uri(str(params.redirect_uri),
                                                error="access_denied", state=params.state)
            else:
                return bad_consent(request, "Choose Allow or Deny.")
            return RedirectResponse(target, status_code=303)

    @app.exception_handler(Unauthenticated)
    async def unauthenticated(request, _exc):
        if google is not None:
            return error(request, 401, "Sign in first, at /login.")
        if authn.verified:
            return error(request, 401, "No verified, allowed credential on this request: "
                         "writes need an IAP or Access assertion, or a Google token.")
        return error(request, 401, f"No {ACTOR_HEADER} header and no {ACTOR_ENV} set: "
                     "writes need Cloudflare Access or a local actor.")

    @app.exception_handler(TooLarge)
    async def too_large(request, exc):
        mb = _max_upload_bytes() / (1024 * 1024)
        return error(request, 413, f"{exc} is over the {mb:g} MB limit for one file. "
                     "Nothing was saved.")

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
                projects = wf_projects[wf["id"]]
                workflows.append(wf | {"current": current, "target": workflow_target(wf),
                                       "project": next(iter(projects)) if len(projects) == 1 else None})
        loose = [i for i in view["issues"]
                 if i["workflow_id"] is None and i["project"] in tracked]
        # The workflow is the card: its computed state picks the column, and
        # its steps show on their issue pages rather than as loose cards here.
        columns = [(state, [w for w in workflows if w["state"] == state],
                    [i for i in loose if i["state"] == state])
                   for state in prefs["lanes"]]
        hidden = [s for s in core.TRANSITIONS if s not in prefs["lanes"]]
        return page(request, "board.html", view=view, columns=columns, now=view["now"],
                    tracked=prefs["projects"], hidden=hidden)

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", project: str = "", label: str = "",
                    assignee: str = ""):
        filters = {"q": q.strip(), "project": project, "label": label, "assignee": assignee}
        view = core.overview(engine, **filters)
        # With no filter at all the page is just the form, not every issue.
        searched = any(filters.values())
        return page(request, "search.html", view=view, filters=filters, searched=searched,
                    header_q=filters["q"],
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
        return saved(prefs)

    @app.post("/preferences/projects")
    async def save_project_filter(request: Request):
        """The dashboard banner's save: projects only, lanes and timezone kept."""
        f = await request.form()
        known = [p["key"] for p in core.overview(engine)["projects"]]
        picked = [k for k in known if k in f.getlist("project")]
        # An empty board is never what was meant; on /preferences an empty
        # group means all, but here it would read as a filter that hid everything.
        if not picked:
            return error(request, 422, "Tick at least one project to track.")
        prefs = read_prefs(request.cookies.get(PREFS_COOKIE))
        prefs["projects"] = [] if len(picked) == len(known) else picked
        return saved(prefs)

    @app.post("/preferences/lanes/{action}")
    async def save_lane_toggle(request: Request, action: str):
        """A lane's hide control, or a hidden lane's show button: lanes only."""
        if action not in ("hide", "show"):
            return error(request, 404, "No such lane action.")
        lane = (await request.form()).get("lane")
        known = list(core.TRANSITIONS)
        if lane not in known:
            return error(request, 422, "No such lane.")
        prefs = read_prefs(request.cookies.get(PREFS_COOKIE))
        shown = set(_chosen(prefs["lanes"], known))
        if action == "hide":
            # [] means every lane, so hiding the last one would bring them all back.
            if shown == {lane}:
                return error(request, 422, "The last visible lane cannot be hidden.")
            shown.discard(lane)
        else:
            shown.add(lane)
        picked = [k for k in known if k in shown]
        prefs["lanes"] = [] if len(picked) == len(known) else picked
        return saved(prefs)

    def saved(prefs):
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
        # A step shows the workflow it belongs to; an origin shows its plan (MS-644).
        wf = issue["workflow"] or issue["plan"]
        return page(request, "issue.html", issue=issue, now=core.overview(engine)["now"],
                    states=list(core.TRANSITIONS), wf=wf,
                    diagram=workflow_diagram(wf, issue["id"]) if wf else None,
                    steps=workflow_list(wf, issue["id"]) if wf else None)

    @app.post("/issues")
    async def create_issue(request: Request):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        files = await save_uploads(f)
        issue = core.create(engine, f.get("project", ""), f.get("title", ""), actor=me.email,
                            actor_kind=me.kind, body=f.get("body", ""),
                            state=f.get("state") or "backlog", rank=_form_int(f.get("rank")),
                            labels=_form_labels(f.get("labels")) or (), attachments=files)
        return back(issue["id"])

    @app.post("/workflows")
    async def instantiate(request: Request):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        core.instantiate(engine, f.get("template", ""), f.get("project", ""), actor=me.email,
                         actor_kind=me.kind, title=f.get("title") or None)
        return back()

    @app.post("/issues/{issue_id}/plan")
    async def plan(request: Request, issue_id: str):
        """Break an issue into a workflow, one step per non-blank line."""
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        titles = [line.strip() for line in f.get("steps", "").splitlines() if line.strip()]
        core.plan(engine, issue_id, [{"title": t} for t in titles], actor=me.email,
                  actor_kind=me.kind)
        return back(issue_id)

    @app.post("/issues/{issue_id}/edit")
    async def edit(request: Request, issue_id: str):
        me = await run_in_threadpool(actor, request)
        f = await request.form()
        current = core.show(engine, issue_id)
        state = f.get("state")
        files = await save_uploads(f)
        try:
            core.edit(engine, issue_id, actor=me.email, actor_kind=me.kind,
                      expected_version=int(f.get("version", -1)),
                      preempt=f.get("preempt") == "1",
                      title=f.get("title"), body=f.get("body"),
                      rank=_form_int(f.get("rank")), labels=_form_labels(f.get("labels")),
                      state=state if state and state != current["state"] else None,
                      note=f.get("note") or None, attachments=files)
        except core.LeaseHeld as held:
            # A file input cannot be refilled, so the page names the files to attach again.
            fields = {k: v for k, v in f.items() if k not in ("preempt", "files")}
            return page(request, "takeover.html", 409, issue=current, held=held,
                        fields=fields, dropped=[a["filename"] for a in files])
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
        files = await save_uploads(f)
        core.annotate(engine, issue_id, f.get("note", ""), actor=me.email, actor_kind=me.kind,
                      attachments=files)
        return back(issue_id)

    @app.get("/attachments/{attachment_id}")
    def download(attachment_id: int):
        a = core.attachment(engine, attachment_id)
        root = os.environ.get(ATTACH_DIR_ENV, "").strip()
        path = Path(root) / a["sha256"] if root else None
        if path is None or not path.is_file():
            raise core.NotFound(f"attachment {attachment_id}'s file is missing")
        inline = a["content_type"] in INLINE_TYPES
        return FileResponse(
            path, filename=a["filename"],
            media_type=a["content_type"] if inline else "application/octet-stream",
            content_disposition_type="inline" if inline else "attachment",
            headers={"X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "sandbox"})

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
