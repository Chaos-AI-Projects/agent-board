# agent-board

An issue and kanban board built for agents first: a claim-with-lease `next`, an append-only event
history, and a web board Chaos can edit. The spec is `product-specs/agent-issue-board.md` in the
brain vault, and the technical design is `product-specs/agent-issue-board-design.md` beside it.

Slices 1 to 5 of 6 are in: the schema in `board.store`, the operations in `board.core`, the
`board` CLI in `board.cli`, the web board in `board.web`, and the MCP server in `board.mcp_server`.

## Operations

`board.core` holds `next`, `show`, `search`, `transition`, `annotate`, `link`, `create`,
`instantiate`, `plan`, `depend`, `undepend`, `heartbeat` and `edit`. Each is one transaction, returns a plain dict, and appends at least one event,
except `heartbeat`, which only moves a lease expiry, and an `edit` whose form changed nothing.
`plan` breaks an issue into a workflow of `ready` steps the caller lists. The issue goes `onhold`
and returns to `ready` when the last step is done, so `next` hands it back to be checked and closed.
An issue has at most one plan, and a step cannot be planned.
`next` returns `None` on an empty queue. Lease failures raise `LeaseLost` (the CLI's exit 4) and
conflicts raise `Conflict` (exit 5), including `LeaseHeld` when `edit` meets someone else's lease
without `preempt=True`. Only a human or the system edits; an agent works through `transition`,
`annotate` and `link` under its lease.

A `request_id` names one call. Repeating the call replays it and returns the issue as it is now,
not as the first call left it. Reusing the id with other arguments raises `Conflict`.

## CLI

`board` runs one operation and prints its result as JSON on stdout. A cron job gates on the exit
code:

| Exit | Meaning |
|------|---------|
| 0 | success |
| 1 | any other error: a board rule, the database, or `BOARD_DATABASE_URL` |
| 2 | usage error, printed by argparse as text |
| 3 | `next` found an empty queue, as `backlog.py next` does, or `search` matched nothing |
| 4 | lease lost |
| 5 | conflict, including a card held under someone else's lease |

Every other error prints one JSON object on stderr naming its class. The actor comes from `--actor` or
`BOARD_ACTOR`, before or after the subcommand, and `--actor-kind` defaults to `agent`. `next` claims
only as an agent. `migrate`, `create-project`, `show` and `search` need no actor.

`board search [Q] [--project P] [--label L] [--assignee A]` prints `{"issues": [...]}`. `Q` is a
case-insensitive substring of the id, title, body or any label, matched with plain `lower() LIKE` so
neither engine needs a full-text extension. The flags match exactly, and everything given must match. Nothing
writes an issue's `assignee` yet, so `--assignee` also matches the card's lease holder.
The web board's search page takes the same filters as `GET /search?q=&project=&label=&assignee=`.

```bash
export BOARD_DATABASE_URL=... BOARD_ACTOR=overlord-395149
board migrate
claim=$(board next) || exit        # exit 3 ends the run on an empty queue
id=$(jq -r .issue.id <<<"$claim"); token=$(jq -r .lease_token <<<"$claim")
board annotate "$id" --note "checkpoint" --token "$token"
board transition "$id" done --note "PR #600" --token "$token"
```

Without `--request-id`, a write under a lease gets the derived key from design section 8, so an
exact retry collapses to one event.

## Importing brain's backlog

`board import-backlog` copies brain's `backlog/*.md` onto the board once. It reads the files through
brain's own `backlog.py`, given by `--backlog-py`, so both read a file the same way. Each file becomes
a project keyed by its id prefix, each item an issue under its own id, and each `- notes:` line an
annotate event. A project that already exists refuses the whole import.

```bash
board --actor-kind system --actor import import-backlog --backlog-py /home/overlord/brain/backlog.py
```

After the import, `board next` picks the item `backlog.py next` picks. Rank is file order with every
`in-progress` item moved to the front, and an `in-progress` item arrives under an expired lease,
which `next` reclaims. The board names four states differently from `backlog.py`, so each item lands
in its lane: `open` in `backlog`, `in-progress` in `processing`, `blocked` in `need-input` and
`frozen` in `onhold`, while `ready` and `done` keep their names. `backlog/` stays the source of
truth until dev-queue is switched over.

## Web board

`board-web` serves the board on 127.0.0.1, port 28090 unless `BOARD_WEB_PORT` says otherwise. It is
its own server, apart from doc-review. The kanban has one column per lifecycle state, in the
order `backlog`, `ready`, `need-input`, `processing`, `onhold`, `done`, `cancelled`. A workflow
is one card, in the column its computed state picks, showing its step count and current step.
Clicking it opens the issue page of its current step, or of its first step once every step is
done. A step's issue page shows its workflow above the body as a BPMN-style Mermaid chart: a start
event, one task per step with that step outlined, and an end event. The chart draws the step and
three either side, stopping short at either end rather than drawing more on the other side; the
steps beyond collapse into a "+N earlier" or "+N later" node, which opens
the full step list kept collapsed under the chart as the no-JS fallback. `/workflows/<id>` redirects to
the same issue page the card opens. Loose issues sit below the workflows in each column, and dragging one to another column
is a state change.

The board at `/` is a view only, and searching is its own page. `/search` lists matching issues as a
flat list linking to each `/issues/<id>`, and shows nothing until a filter is given. `/preferences`
picks which projects the board tracks and which lanes it shows. The choice is saved per browser in
the `board_prefs` cookie: percent-encoded JSON, `{"projects": [...], "lanes": [...]}`, SameSite=Lax,
kept for a year. With no cookie, the board shows every project and every lane. Unknown keys, projects
and lanes are ignored, and a group left with nothing usable shows all of it. Ticking every box saves
"all", so a project added later is tracked too. A workflow card shows
when any of its steps is in a tracked project, because a workflow has no project of its own.

An issue page shows its body as rendered markdown. The raw body is editable only after opening
"Edit body", and a save that never opened it posts the body unchanged. Both note fields are
textareas.

Files can be attached from the new-issue form, the Edit issue form and the Note form. A note's
files show under that note in the history, and the rest are listed under Attachments on the issue
page. The web page is the only way to upload; the CLI and MCP server cannot. Two settings govern it:

- `BOARD_ATTACHMENT_DIR` is where the bytes go, one file per upload named by its SHA-256. Only the
  metadata is in the database. Unset, the board refuses any upload with 422.
- `BOARD_MAX_UPLOAD_MB`, default 25, caps each file. An over-cap upload gets 413 and saves nothing.

`/attachments/<id>` serves PNG, JPEG, GIF and WebP inline, and the issue page shows them as
thumbnails. Everything else downloads, SVG and HTML included, as `application/octet-stream` with
`X-Content-Type-Options: nosniff` and a sandbox CSP. The board writes as whoever reads it, so an
uploaded page opening on the board's origin could act as them.

Every write goes through `board.core`. Who made it depends on whether a verifier is configured.

With none configured, the board is in local mode. The human actor comes from Cloudflare Access's
`Cf-Access-Authenticated-User-Email` header. Off Access, `BOARD_WEB_ACTOR` names the human instead.
The header wins when both are present, and a write with neither gets 401. That header is only
trustworthy behind Access, which is why the server binds to localhost. A cross-origin form post gets
403.

Setting any of the variables below switches to verified mode. The bare header and `BOARD_WEB_ACTOR`
are then ignored, and a write needs one of three credentials:

- a signed proxy assertion, a human: `x-goog-iap-jwt-assertion` from Google IAP, checked against
  `BOARD_IAP_AUDIENCE`, or `Cf-Access-Jwt-Assertion` from Cloudflare Access, checked against
  `BOARD_CF_TEAM_DOMAIN` (`<team>.cloudflareaccess.com`) and `BOARD_CF_AUD`;
- `Authorization: Bearer <Google access token>`, resolved through Google's tokeninfo. This path is
  off until `BOARD_GOOGLE_CLIENT_IDS`, comma-separated, names the OAuth clients a token may come
  from, such as gcloud's. Unpinned, any site you signed in to with Google could replay your token;
- `Authorization: Bearer <service-account ID token>` with audience `BOARD_SA_AUDIENCE`. Only a
  `*.gserviceaccount.com` email counts. Mint the token with its email included
  (`gcloud auth print-identity-token --include-email`, or `includeEmail` in iamcredentials),
  because a token without one is refused.

The actor kind follows the email on every path: a `*.gserviceaccount.com` address is an agent and
anything else is a human. `BOARD_ALLOWED_EMAILS`, comma-separated, gates all three. A verifier with
an empty allowlist refuses everyone, and a half-configured one, such as a team domain with no AUD,
refuses rather than falling back to local mode.

A card's lease renders three ways:

- **live**, "Held by run-2 until T", with a solid blue edge;
- **expired**, "Lease expired", dashed amber, because `next` will reclaim it;
- **held**, a take-over with no expiry, which shows its holder a Release button.

Saving a change to a card someone else holds returns "Held by X until T. Take over?". Taking over
resubmits the same form with `preempt`, so the agent's next write exits 4. Release moves the card
back to `ready`. A note never needs the lease.

## MCP server

`board-mcp` serves the CLI's operations as MCP tools over stdio, for an agent working in a
conversation. The tools are `next`, `show`, `transition`, `annotate`, `link`, `create`, `instantiate`,
`plan`, `depend`, `undepend` and `heartbeat`, with the CLI's names and arguments. `migrate` and `edit` are left out.

One server is one agent. `BOARD_ACTOR` names it when the server starts, and every write is recorded
as `agent`. The lease token `next` returns is the `token` argument of each later write, and TTLs are
`ttl_minutes`.

`next` on an empty queue returns `{"issue": null}` as a normal result. Any other failure comes back
as a tool error whose text is the CLI's error JSON plus the exit code the CLI would have used:

```json
{"error": "LeaseLost", "message": "...", "code": 4}
```

```json
{"mcpServers": {"board": {"command": "board-mcp",
  "env": {"BOARD_DATABASE_URL": "...", "BOARD_ACTOR": "overlord"}}}}
```

## Database

The app reads one database URL from `BOARD_DATABASE_URL` and assumes nothing about the host.
Production is SQLite. PostgreSQL still works and its test leg still runs, but nothing deploys it. A
bare `postgresql://` or `postgres://` URL is pointed at psycopg 3, the driver this project installs.

On SQLite every connection sets a 30-second busy timeout and WAL. So `board-web` and an agent's CLI
or MCP server can share one file. Every transaction takes the write lock up front, reads included,
so each waits its turn instead of failing "database is locked".

```python
from board import store

engine = store.make_engine()   # reads BOARD_DATABASE_URL
store.upgrade(engine)          # applies the Alembic migrations
```

## Run it on your machine

```bash
cd agent-board
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
export BOARD_DATABASE_URL=sqlite:///$HOME/board.db
board migrate
BOARD_WEB_ACTOR=you@example.com board-web
```

Then open http://127.0.0.1:28090. A fresh board has no projects, and a card needs one. Create one
with `board create-project KEY NAME`, which prints it as JSON and exits 5 if the key is taken:

```bash
board create-project MS memory-solution
```

Or seed the board from brain's backlog, pointing `--backlog-py` at your brain checkout:

```bash
board --actor-kind system --actor import import-backlog --backlog-py <brain>/backlog.py
```

`BOARD_WEB_ACTOR` makes every request that lacks the Access header a write as that person. Leave it
unset on a deployment behind Cloudflare Access.

`board-web` answers only requests whose Host is `127.0.0.1:<port>` or `localhost:<port>`, and
refuses anything else with 403, so a DNS-rebinding page cannot write as `BOARD_WEB_ACTOR`. Behind a
tunnel or a port forward, add the name the browser uses to `BOARD_WEB_HOSTS`, comma-separated:
`BOARD_WEB_HOSTS=board.example.net` for a tunnel, `BOARD_WEB_HOSTS=localhost:8080` for
`ssh -L 8080:127.0.0.1:28090`. The port is `BOARD_WEB_PORT`, 28090 by default.

SQLite in WAL mode keeps recent writes in `board.db-wal` beside `board.db`, so a bare copy of
`board.db` can miss them. Back up with `sqlite3 board.db ".backup board-backup.db"`, or with
Python's `sqlite3.Connection.backup` where the `sqlite3` shell is not installed.

## Tests

Run from this directory:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

Every schema test runs twice, once on SQLite and once on PostgreSQL. The PostgreSQL leg runs only
when `BOARD_TEST_PG_URL` names a throwaway database, because it drops every board table before and
after each test. Without it, each PostgreSQL case is skipped and pytest prints why.

`tests/test_import.py` runs brain's real `backlog.py`, at `/home/overlord/brain/backlog.py` or
`BACKLOG_PY`, and skips when it is absent.
