"""The web UI against design section 10 row 7, plus the take-over round trip.

Row 7 is an HTML assertion: a card whose lease has expired renders
differently from one whose lease is live. The round trip is section 6 in a
browser: an edit under a live lease is refused and offers a take-over, the
take-over leaves the card held by the human with a Release button, and
Release hands it back to the queue.
"""

import json
import re
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from board import core, web

AGENT = "agent"
HUMAN = "human"
CHAOS = "owner@example.com"
AS_CHAOS = {web.ACTOR_HEADER: CHAOS}
LOCAL = f"http://127.0.0.1:{web.DEFAULT_PORT}"


@pytest.fixture(autouse=True)
def default_hosts(monkeypatch):
    """Every test starts on the default port with no tunnel hosts."""
    monkeypatch.delenv(web.PORT_ENV, raising=False)
    monkeypatch.delenv(web.HOSTS_ENV, raising=False)
    monkeypatch.delenv(web.TZ_ENV, raising=False)


@pytest.fixture
def board(migrated):
    core.create_project(migrated, "MS", "memory-solution")
    return migrated


@pytest.fixture
def client(board):
    return local_client(board)


def local_client(board, base_url=LOCAL):
    return TestClient(web.create_app(board), base_url=base_url, follow_redirects=False)


def ready(engine, title="item", **kw):
    return core.create(engine, "MS", title, actor=CHAOS, actor_kind=HUMAN,
                       state="ready", **kw)["id"]


def card(html, issue_id):
    """The markup of one card on the board page."""
    m = re.search(rf'<article[^>]*data-id="{issue_id}"[^>]*>.*?</article>', html, re.S)
    assert m, f"no card for {issue_id}"
    return m.group(0)


def column(html, state):
    m = re.search(rf'<section[^>]*data-state="{state}"[^>]*>.*?</section>', html, re.S)
    assert m, f"no column for {state}"
    return m.group(0)


def edit_form(issue, **fields):
    return {"version": str(issue["version"]), "title": issue["title"],
            "body": issue["body"], "rank": str(issue["rank"]),
            "labels": ", ".join(issue["labels"])} | fields


def note_form(issue, note="", **fields):
    return {"version": str(issue["version"]), "state": issue["state"],
            "from_state": issue["state"], "note": note} | fields


# --- the board page -------------------------------------------------------------


def test_the_board_is_kanban_by_lifecycle_state(board, client):
    a = ready(board, "a ready one")
    b = core.create(board, "MS", "an open one", actor=CHAOS, actor_kind=HUMAN)["id"]
    html = client.get("/").text
    for state in core.TRANSITIONS:
        column(html, state)
    assert f'data-id="{a}"' in column(html, "ready")
    assert f'data-id="{b}"' in column(html, "backlog")


def test_the_columns_are_the_seven_lanes_in_order(board, client):
    html = client.get("/").text
    shown = re.findall(r'<section[^>]*data-state="([^"]+)"', html)
    assert shown == ["backlog", "ready", "need-input", "processing", "onhold", "done",
                     "cancelled"]


def test_ms660_every_column_shows_its_lane_hint_without_hover(board, client):
    html = client.get("/").text
    for state, hint in core.LANE_HINTS.items():
        col = unescape(column(html, state))
        assert re.search(rf'<h2 title="{re.escape(hint)}"', col), state
        assert f'<p class="lane-hint">{hint}</p>' in col, state


def test_ms660_the_issue_page_explains_its_state_and_each_option(board, client):
    iid = ready(board)
    html = unescape(client.get(f"/issues/{iid}").text)
    assert re.search(r'State <b>ready</b> <span class="lane-hint">'
                     + re.escape(core.LANE_HINTS["ready"]) + "</span>", html)
    select = re.search(r'<select name="state".*?</select>', html, re.S).group(0)
    options = re.findall(r'<option title="([^"]*)"[^>]*>([^<]+)</option>', select)
    # MS-661: the select offers only where the card is and where it may go next.
    assert options == [(hint, state) for state, hint in core.LANE_HINTS.items()
                       if state == "ready" or state in core.TRANSITIONS["ready"]]


def test_row7_an_expired_lease_renders_differently_from_a_live_one(board, client):
    live = ready(board, "live")            # ranks first, so it is claimed first
    expired = ready(board, "expired")
    core.next(board, "run-4")
    core.next(board, "run-3", ttl=timedelta(seconds=-5))

    assert core.show(board, expired)["lease_holder"] == "run-3"
    assert core.show(board, live)["lease_holder"] == "run-4"
    html = client.get("/").text
    live_card, expired_card = card(html, live), card(html, expired)
    assert "lease-live" in live_card and "lease-expired" not in live_card
    assert "lease-expired" in expired_card and "lease-live" not in expired_card
    assert "Held by run-4 until" in live_card
    assert "Lease expired" in expired_card and "run-3" in expired_card


def release(engine, steps=("build", "test", "ship")):
    core.create_template(engine, "release", "Release", list(steps))
    return core.instantiate(engine, "release", "MS", actor=CHAOS, actor_kind=HUMAN)


def wf_card(html, wf_id):
    """The markup of one workflow's card on the board page."""
    m = re.search(rf'<article[^>]*data-workflow="{wf_id}"[^>]*>.*?</article>', html, re.S)
    assert m, f"no workflow card for {wf_id}"
    return m.group(0)


def test_a_workflow_is_one_card_in_the_column_its_state_picks(board, client):
    wf = release(board)
    html = client.get("/").text
    assert wf_card(column(html, "ready"), wf["id"])
    core.next(board, "w1")
    html = client.get("/").text
    assert f'data-workflow="{wf["id"]}"' not in column(html, "ready")
    assert wf_card(column(html, "processing"), wf["id"])


def test_a_workflow_card_shows_its_title_step_count_and_current_step(board, client):
    wf = release(board)
    text = re.sub(r"<[^>]+>", " ", wf_card(client.get("/").text, wf["id"]))
    text = " ".join(text.split())
    assert "Release" in text
    assert "3 steps" in text
    assert "current: build" in text


def test_workflow_steps_are_not_loose_cards_on_the_board(board, client):
    wf = release(board)
    html = client.get("/").text
    for step in wf["steps"]:
        assert f'data-id="{step["id"]}"' not in html


def test_loose_issues_show_as_their_own_group(board, client):
    release(board)
    loose = ready(board, "a loose one")
    col = column(client.get("/").text, "ready")
    group = re.search(r'<div class="cards"[^>]*>.*?</div>\s*</section>', col, re.S).group(0)
    assert f'data-id="{loose}"' in group
    assert "data-workflow=" not in group


def finish(engine, step_id, worker="w1"):
    claim = core.next(engine, worker)
    assert claim["issue"]["id"] == step_id
    core.transition(engine, step_id, "done", note="done", actor=worker, actor_kind=AGENT,
                    token=claim["lease_token"])


def test_a_workflow_card_opens_its_current_steps_issue_page(board, client):
    wf = release(board)
    build, test, _ = [st["id"] for st in wf["steps"]]
    c = wf_card(client.get("/").text, wf["id"])
    assert f'data-href="/issues/{build}"' in c and f'href="/issues/{build}"' in c
    assert "/workflows/" not in c
    # Its state is computed from the steps, so it is not dragged between columns.
    assert "data-moves=" not in c
    finish(board, build)
    c = wf_card(client.get("/").text, wf["id"])
    assert f'data-href="/issues/{test}"' in c and f'href="/issues/{test}"' in c


def test_a_finished_workflow_card_opens_its_first_step(board, client):
    wf = release(board, ["build", "ship"])
    build, ship = [st["id"] for st in wf["steps"]]
    finish(board, build)
    finish(board, ship)
    c = wf_card(client.get("/").text, wf["id"])
    assert f'data-href="/issues/{build}"' in c and f'href="/issues/{build}"' in c


def test_the_workflow_url_redirects_to_its_current_step(board, client):
    wf = release(board)
    build, test, _ = [st["id"] for st in wf["steps"]]
    r = client.get(f"/workflows/{wf['id']}")
    assert r.status_code == 303 and r.headers["location"] == f"/issues/{build}"
    finish(board, build)
    r = client.get(f"/workflows/{wf['id']}")
    assert r.status_code == 303 and r.headers["location"] == f"/issues/{test}"


def test_a_finished_workflows_url_redirects_to_its_first_step(board, client):
    # The link already mailed for workflow 1 must land on its first step.
    wf = release(board, ["build", "ship"])
    build, ship = [st["id"] for st in wf["steps"]]
    finish(board, build)
    finish(board, ship)
    r = client.get(f"/workflows/{wf['id']}")
    assert r.status_code == 303 and r.headers["location"] == f"/issues/{build}"


def workflow_section(html):
    m = re.search(r'<section class="workflow"[^>]*>.*?</section>', html, re.S)
    assert m, "no workflow section"
    return m.group(0)


def test_a_step_issue_page_shows_its_workflow_above_the_body(board, client):
    wf = release(board)
    build, test, ship = [st["id"] for st in wf["steps"]]
    core.edit(board, test, actor=CHAOS, actor_kind=HUMAN,
              expected_version=core.show(board, test)["version"], body="THE BODY")
    finish(board, build)
    html = client.get(f"/issues/{test}").text
    section = workflow_section(html)
    assert "Release" in section
    assert html.index('<section class="workflow"') < html.index("THE BODY")
    # The step list, in position order with each state, is the no-JS fallback.
    steps = re.findall(r'<li[^>]*data-step="([^"]+)"[^>]*>(.*?)</li>', section, re.S)
    assert [sid for sid, _ in steps] == [build, test, ship]
    assert "done" in steps[0][1] and "ready" in steps[1][1] and "ready" in steps[2][1]
    # This issue's step is the one marked, not the others.
    marked = re.findall(r'<li[^>]*class="[^"]*this-step[^"]*"[^>]*data-step="([^"]+)"', section)
    assert marked == [test]
    assert f'href="/issues/{build}"' in section and f'href="/issues/{ship}"' in section
    assert "/workflows/" not in html


def test_a_loose_issue_page_has_no_workflow_section(board, client):
    html = client.get(f"/issues/{ready(board)}").text
    assert '<section class="workflow"' not in html
    assert "mermaid.min.js" not in html


def test_an_unknown_workflow_is_404(client):
    assert client.get("/workflows/999").status_code == 404


def mermaid_source(html):
    """The diagram's Mermaid source as Mermaid reads it, HTML entities decoded."""
    m = re.search(r'<pre class="mermaid"[^>]*>(.*?)</pre>', html, re.S)
    assert m, "no mermaid diagram"
    return unescape(m.group(1)).strip()


def test_the_issue_page_draws_its_workflow_as_a_mermaid_flowchart(board, client):
    wf = release(board)
    core.next(board, "w1")
    html = client.get(f"/issues/{wf['steps'][1]['id']}").text
    src = mermaid_source(html).splitlines()
    assert src[0].strip() == "flowchart LR"
    ids = [st["id"] for st in wf["steps"]]
    nodes = [l.strip() for l in src if re.match(r"\s*s\d+\(", l)]
    assert len(nodes) == len(ids)
    # One node per step in position order, labelled id, title and state.
    for n, (node, st) in enumerate(zip(nodes, wf["steps"])):
        assert node.startswith(f's{n}("')
        assert core.show(board, st["id"])["state"] in node
        assert st["title"] in node
    assert "-->".join(f"s{n}" for n in range(len(ids))) in "".join(
        l.strip().replace(" ", "") for l in src)
    # Each node is a link to its issue page.
    clicks = [l.strip() for l in src if l.strip().startswith("click ")]
    assert clicks == [f'click s{n} "/issues/{i}"' for n, i in enumerate(ids)]
    # Styled by state, and this issue's node is the one highlighted.
    assert {"class s0 st_processing", "class s1 here"} <= {l.strip() for l in src}
    assert [l.strip() for l in src if l.strip().endswith(" here")] == ["class s1 here"]
    assert "classDef here " in "\n".join(src)
    assert not any(l.strip().startswith("class ") and "," in l for l in src)
    assert "class s1 st_ready" in [l.strip() for l in src]
    assert "classDef st_processing" in "\n".join(src)
    # Mermaid loads from the CDN like SortableJS, and the step list stays as fallback.
    assert "cdn.jsdelivr.net/npm/mermaid@" in html
    assert 'securityLevel: "strict"' in html
    assert f'data-step="{ids[0]}"' in workflow_section(html)


def step_lists(section):
    """The step ids the list shows once opened: (in the window, behind "Show N more")."""
    m = re.search(r'<details class="workflow-more"[^>]*>(.*?)</details>', section, re.S)
    hidden = re.findall(r'data-step="([^"]+)"', m.group(1)) if m else []
    visible = re.findall(r'data-step="([^"]+)"', section.replace(m.group(0), "") if m else section)
    return visible, hidden


def test_a_workflow_of_four_steps_keeps_its_diagram(board, client):
    wf = release(board, ["a", "b", "c", "d"])
    html = client.get(f"/issues/{wf['steps'][0]['id']}").text
    assert '<pre class="mermaid"' in html and "mermaid.min.js" in html
    visible, hidden = step_lists(workflow_section(html))
    assert visible == [st["id"] for st in wf["steps"]] and hidden == []


def test_a_workflow_over_four_steps_still_draws_its_diagram(board, client):
    # MS-645: the chart shows at every size; windowing replaced the >4 cut-off.
    wf = release(board, [f"step {n}" for n in range(1, 6)])
    html = client.get(f"/issues/{wf['steps'][0]['id']}").text
    assert '<pre class="mermaid"' in html and "mermaid.min.js" in html
    visible, hidden = step_lists(workflow_section(html))
    assert visible == [st["id"] for st in wf["steps"]] and hidden == []
    assert '<details class="workflow-more"' not in workflow_section(html)


def test_a_long_workflow_shows_ten_steps_and_the_rest_behind_show_more(board, client):
    wf = release(board, [f"step {n}" for n in range(1, 13)])
    ids = [st["id"] for st in wf["steps"]]
    section = workflow_section(client.get(f"/issues/{ids[0]}").text)
    visible, hidden = step_lists(section)
    assert visible == ids[:10]
    assert hidden == ids[10:]
    assert re.search(r"<summary>\s*Show 2 more\s*</summary>", section)
    # The list numbers each step by its position, wherever the list starts.
    assert '<ol class="workflow-steps" start="1"' in section
    assert '<ol class="workflow-steps" start="11"' in section


def test_a_late_steps_page_shows_the_window_that_holds_it(board, client):
    wf = release(board, [f"step {n}" for n in range(1, 13)])
    ids = [st["id"] for st in wf["steps"]]
    section = workflow_section(client.get(f"/issues/{ids[11]}").text)
    visible, hidden = step_lists(section)
    assert visible == ids[2:]
    assert hidden == ids[:2]
    marked = re.findall(r'class="[^"]*this-step[^"]*"[^>]*data-step="([^"]+)"', section)
    assert marked == [ids[11]]


def test_a_middle_steps_window_starts_four_before_it(board, client):
    wf = release(board, [f"step {n}" for n in range(1, 15)])
    ids = [st["id"] for st in wf["steps"]]
    section = workflow_section(client.get(f"/issues/{ids[6]}").text)
    visible, hidden = step_lists(section)
    assert visible == ids[2:12]
    # Hidden steps before and after the window keep their order and numbering.
    assert hidden == ids[:2] + ids[12:]
    assert re.search(r"<summary>\s*Show 4 more\s*</summary>", section)
    assert '<ol class="workflow-steps" start="13"' in section


# --- MS-645: the workflow as a BPMN-style chart, distant steps collapsed -----------


def chart(wf, here):
    """(task node ids in order, collapse nodes by id, the one sequence-flow chain)."""
    src = [l.strip() for l in web.workflow_diagram(wf, here).splitlines()]
    tasks = [re.match(r"(s\d+)\(", l).group(1) for l in src if re.match(r"s\d+\(", l)]
    more = {m.group(1): unescape_label(m.group(2)) for l in src
            if (m := re.fullmatch(r'(more_\w+)\["(.*)"\]', l))}
    chains = [l for l in src if "-->" in l]
    assert len(chains) == 1, chains
    return tasks, more, [n.strip() for n in chains[0].split("-->")], src


def unescape_label(label):
    """A Mermaid label as it renders: `#N;` entity codes decoded, markdown escapes dropped."""
    text = re.sub(r"#(\d+);", lambda m: chr(int(m.group(1))), label)
    return text.replace("\\", "")


def fake_wf(n, done=0):
    return {"steps": [{"id": f"MS-{i + 1}", "title": f"step {i + 1}",
                       "state": "done" if i < done else "ready"} for i in range(n)]}


def test_the_chart_opens_with_a_start_event_and_closes_with_an_end_event():
    tasks, more, chain, src = chart(fake_wf(3), "MS-2")
    assert src[0] == "flowchart LR"
    assert chain == ["ev_start", "s0", "s1", "s2", "ev_end"]
    # BPMN notation drawn in flowchart shapes: a circle, rounded tasks, a double circle.
    assert any(re.fullmatch(r'ev_start\(\(".*"\)\)', l) for l in src)
    assert any(re.fullmatch(r'ev_end\(\(\(".*"\)\)\)', l) for l in src)
    assert tasks == ["s0", "s1", "s2"] and more == {}
    # `end` is a Mermaid keyword, so no node may be called that.
    assert not any(re.match(r"end\b", l) for l in src)


def test_a_twelve_step_workflow_draws_seven_tasks_between_two_collapse_nodes():
    tasks, more, chain, src = chart(fake_wf(12), "MS-6")
    # Step 6 is s5; the window is it and three either side.
    assert tasks == [f"s{n}" for n in range(2, 9)]
    assert more == {"more_before": "+2 earlier", "more_after": "+3 later"}
    assert chain == ["ev_start", "more_before"] + tasks + ["more_after", "ev_end"]
    assert "class s5 here" in src
    # A collapse node opens the full step list below the chart.
    clicks = {l for l in src if l.startswith("click more_")}
    assert clicks == {'click more_before "#workflow-steps"',
                      'click more_after "#workflow-steps"'}
    assert "class more_before more" in src and "class more_after more" in src


@pytest.mark.parametrize("here, window, before, after", [
    # Chaos, 2026-09-29: at either end the chart stops at 3 per side rather
    # than sliding the window inward to keep drawing seven.
    ("MS-1", range(0, 4), None, "+8 later"),
    ("MS-2", range(0, 5), None, "+7 later"),
    ("MS-12", range(8, 12), "+8 earlier", None),
    ("MS-10", range(6, 12), "+6 earlier", None),
])
def test_at_either_end_the_window_stops_at_three_per_side(here, window, before, after):
    tasks, more, chain, _ = chart(fake_wf(12), here)
    assert tasks == [f"s{n}" for n in window]
    assert more.get("more_before") == before and more.get("more_after") == after
    assert chain[0] == "ev_start" and chain[-1] == "ev_end"


@pytest.mark.parametrize("n, here", [(4, "MS-1"), (4, "MS-4"), (7, "MS-4")])
def test_nothing_collapses_when_every_step_is_within_three(n, here):
    tasks, more, _, _ = chart(fake_wf(n), here)
    assert tasks == [f"s{k}" for k in range(n)] and more == {}


def test_an_origin_page_centres_on_the_first_step_not_done():
    # The origin issue is not a step, so the window follows the work instead.
    tasks, more, _, src = chart(fake_wf(12, done=8), "MS-99")
    assert tasks == [f"s{n}" for n in range(5, 12)]
    assert more == {"more_before": "+5 earlier"}
    assert not any(l.endswith(" here") and l.startswith("class ") for l in src)
    tasks, more, _, _ = chart(fake_wf(12, done=12), "MS-99")
    assert tasks == [f"s{n}" for n in range(4)]


def test_an_origin_page_with_a_long_plan_draws_the_chart_and_lists_every_step(board, client):
    iid = ready(board, "big job")
    view = core.plan(board, iid, [{"title": f"part {n}"} for n in range(1, 13)],
                     actor=CHAOS, actor_kind=HUMAN)
    ids = [s["id"] for s in view["plan"]["steps"]]
    html = client.get(f"/issues/{iid}").text
    src = mermaid_source(html)
    assert "more_after" in src and "more_before" not in src
    # The full list stays reachable under the chart, collapsed, as the collapse nodes' target.
    section = workflow_section(html)
    m = re.search(r'<details class="workflow-list" id="workflow-steps">(.*)</details>',
                  section, re.S)
    assert m, "the step list is not collapsed under the chart"
    assert re.findall(r'data-step="([^"]+)"', m.group(1)) == ids
    assert section.index('<pre class="mermaid"') < section.index('id="workflow-steps"')


def test_a_collapse_node_label_is_escaped_like_a_step_label():
    src = web.workflow_diagram(fake_wf(12), "MS-1")
    node = next(l.strip() for l in src.splitlines() if l.strip().startswith("more_after["))
    label = re.fullmatch(r'more_after\["(.*)"\]', node).group(1)
    assert re.fullmatch(r"[A-Za-z0-9 #;]*", label), label
    # One line, so mermaid 11.4.1 renders no markdown there: a `#92;` escape would
    # show as a literal backslash (checked in jsdom), unlike a step's 3-line label.
    assert label == "#43;8 later"


HOSTILE = 'a "quoted" [bracket] --> x;\nclick s0 "javascript:alert(1)" %%{init}%% <b>'


def test_a_hostile_step_title_cannot_break_or_inject_into_the_diagram(board, client):
    # Step titles come from the template, which an agent may write.
    wf = release(board, [HOSTILE, "ship"])
    build = wf["steps"][0]["id"]
    src = mermaid_source(client.get(f"/issues/{build}").text)
    lines = [l.strip() for l in src.splitlines()]
    # Still exactly two nodes, one edge, two clicks: nothing the title said became syntax.
    assert len([l for l in lines if re.match(r"s\d+\(", l)]) == 2
    assert [l for l in lines if l.startswith("click ")] == [
        f'click s0 "/issues/{build}"', f'click s1 "/issues/{wf["steps"][1]["id"]}"']
    assert "javascript" not in "".join(l for l in lines if l.startswith("click "))
    node = next(l for l in lines if l.startswith("s0("))
    label = re.fullmatch(r's0\("(.*)"\)', node).group(1)
    # Every character that means anything to Mermaid is an entity code.
    assert re.fullmatch(r'[A-Za-z0-9 #;]*(<br/>[A-Za-z0-9 #;]*)*', label), label
    assert "-->" not in label and "%%" not in label


def test_a_title_cannot_trigger_mermaids_markdown_katex_or_icons():
    # Mermaid decodes the label, then renders markdown, `$$` KaTeX and `fa:` icons.
    # Checked against mermaid 11.4.1 in jsdom: each of these rendered as literal text.
    text = web._mermaid_text("`code` *em* $$x$$ fa:fa-car")
    assert "#92;#96;code#92;#96;" in text and "#92;#42;em#92;#42;" in text
    assert "#92;#36;#8203;#92;#36;#8203;x" in text  # a zero-width space splits `$$`
    assert text.count("#8203;") == 5         # after each of four `$` and the `:`
    assert web._mermaid_text("<&>") == "#60;#38;#62;"


def test_the_click_target_cannot_carry_a_quote_even_from_an_issue_id():
    # Ids are generated today; the diagram does not rely on that.
    wf = {"steps": [{"id": 'X-1" x', "title": "t", "state": "ready"}]}
    clicks = [l.strip() for l in web.workflow_diagram(wf, None).splitlines() if "click" in l]
    assert clicks == ['click s0 "/issues/X-1%22%20x"']


# --- writes and the human actor --------------------------------------------------


def test_a_write_without_the_access_header_is_refused(board, client, monkeypatch):
    monkeypatch.delenv(web.ACTOR_ENV, raising=False)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "hi"})
    assert r.status_code == 401
    assert [e["kind"] for e in core.show(board, iid)["events"]] == ["create"]


def test_board_web_actor_names_the_human_when_the_header_is_absent(board, monkeypatch):
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    client = local_client(board)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "on my machine"})
    assert r.status_code == 303
    last = core.show(board, iid)["events"][-1]
    assert (last["actor"], last["actor_kind"]) == ("local@example.com", HUMAN)


def test_the_access_header_wins_over_board_web_actor(board, monkeypatch):
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    client = local_client(board)
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "via Access"}, headers=AS_CHAOS)
    assert r.status_code == 303
    assert core.show(board, iid)["events"][-1]["actor"] == CHAOS


def test_an_empty_board_web_actor_still_refuses(board, monkeypatch):
    monkeypatch.setenv(web.ACTOR_ENV, "  ")
    client = local_client(board)
    iid = ready(board)
    assert client.post(f"/issues/{iid}/note", data={"note": "x"}).status_code == 401


def test_the_actor_on_every_event_comes_from_the_access_header(board, client):
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "read the spec"}, headers=AS_CHAOS)
    assert r.status_code == 303
    last = core.show(board, iid)["events"][-1]
    assert (last["actor"], last["actor_kind"], last["note"]) == (CHAOS, HUMAN, "read the spec")


def test_create_goes_through_core(board, client):
    r = client.post("/issues", data={"project": "MS", "title": "new card", "body": "b",
                                     "state": "ready"}, headers=AS_CHAOS)
    assert r.status_code == 303
    issue = core.show(board, "MS-1")
    assert (issue["title"], issue["state"]) == ("new card", "ready")


def test_an_edit_saves_and_redirects(board, client):
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/edit", data=edit_form(issue, title="renamed",
                                                          labels="web, ui"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    issue = core.show(board, iid)
    assert issue["title"] == "renamed" and issue["labels"] == ["ui", "web"]


def test_a_stale_form_is_a_conflict_and_writes_nothing(board, client):
    iid = ready(board)
    stale = core.show(board, iid)
    core.edit(board, iid, actor=CHAOS, expected_version=stale["version"], title="one")
    r = client.post(f"/issues/{iid}/edit", data=edit_form(stale, title="two"),
                    headers=AS_CHAOS)
    assert r.status_code == 409
    assert "changed since" in r.text
    assert core.show(board, iid)["title"] == "one"


def test_a_board_rule_is_shown_not_raised(board, client):
    iid = ready(board)
    core.next(board, "run-2")
    core.edit(board, iid, actor=CHAOS, expected_version=core.show(board, iid)["version"],
              title="t", preempt=True)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/note", data=note_form(issue, state="done"),
                    headers=AS_CHAOS)
    assert r.status_code == 422
    assert "needs a note" in r.text


# --- section 6: take over and release --------------------------------------------


def test_take_over_and_release_round_trip(board, client):
    iid = ready(board)
    claim = core.next(board, "run-2")
    issue = core.show(board, iid)

    page = client.get(f"/issues/{iid}").text
    assert "Held by run-2 until" in page

    refused = client.post(f"/issues/{iid}/edit", data=edit_form(issue, body="new spec"),
                          headers=AS_CHAOS)
    assert refused.status_code == 409
    assert "Held by run-2 until" in refused.text and "Take over?" in refused.text
    assert 'name="preempt"' in refused.text
    assert core.show(board, iid)["body"] == ""

    taken = client.post(f"/issues/{iid}/edit",
                        data=edit_form(issue, body="new spec", preempt="1"),
                        headers=AS_CHAOS)
    assert taken.status_code == 303
    issue = core.show(board, iid)
    assert issue["body"] == "new spec"
    assert issue["lease_holder"] == CHAOS and issue["lease_expires_at"] is None
    assert issue["state"] == "processing"

    page = client.get(f"/issues/{iid}", headers=AS_CHAOS).text
    assert f"Held by {CHAOS}" in page
    assert re.search(rf'<form[^>]*action="/issues/{iid}/release"', page)
    other = client.get(f"/issues/{iid}", headers={web.ACTOR_HEADER: "someone@else"}).text
    assert f"Held by {CHAOS}" in other and "/release" not in other
    assert f"Held by {CHAOS}" in card(client.get("/", headers=AS_CHAOS).text, iid)

    with pytest.raises(core.LeaseLost):
        core.transition(board, iid, "done", note="old work", actor="run-2",
                        actor_kind=AGENT, token=claim["lease_token"])

    released = client.post(f"/issues/{iid}/release", headers=AS_CHAOS)
    assert released.status_code == 303
    issue = core.show(board, iid)
    assert issue["state"] == "ready" and issue["lease_holder"] is None
    assert core.next(board, "run-3")["issue"]["id"] == iid


def test_off_access_the_local_actor_sees_its_release_button(board, monkeypatch):
    monkeypatch.setenv(web.ACTOR_ENV, "local@example.com")
    client = local_client(board)
    iid = ready(board)
    core.next(board, "run-2")
    issue = core.show(board, iid)
    taken = client.post(f"/issues/{iid}/edit", data=edit_form(issue, preempt="1"))
    assert taken.status_code == 303
    assert core.show(board, iid)["lease_holder"] == "local@example.com"
    page = client.get(f"/issues/{iid}").text
    assert re.search(rf'<form[^>]*action="/issues/{iid}/release"', page)
    assert "not signed in" not in page


def test_release_under_an_agent_lease_is_refused(board, client):
    iid = ready(board)
    core.next(board, "run-2")
    r = client.post(f"/issues/{iid}/release", headers=AS_CHAOS)
    assert r.status_code == 409
    assert core.show(board, iid)["lease_holder"] == "run-2"


def test_a_note_under_a_live_lease_needs_no_take_over(board, client):
    iid = ready(board)
    core.next(board, "run-2")
    r = client.post(f"/issues/{iid}/note", data={"note": "see the new spec"},
                    headers=AS_CHAOS)
    assert r.status_code == 303
    assert core.show(board, iid)["lease_holder"] == "run-2"


def test_an_unknown_issue_is_404(client):
    assert client.get("/issues/MS-99").status_code == 404


def test_a_drag_between_columns_moves_the_state_and_a_refusal_is_json(board, client):
    iid = core.create(board, "MS", "draggable", actor=CHAOS, actor_kind=HUMAN)["id"]
    version = core.show(board, iid)["version"]
    r = client.post(f"/issues/{iid}/move", data={"version": version, "state": "ready"},
                    headers=AS_CHAOS)
    assert r.status_code == 200 and r.json()["state"] == "ready"
    r = client.post(f"/issues/{iid}/move", data={"version": version, "state": "onhold"},
                    headers=AS_CHAOS)
    assert r.status_code == 409 and "version" in r.json()["error"]
    assert core.show(board, iid)["state"] == "ready"


def test_a_cross_site_post_is_refused(board, client):
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "x"},
                    headers=AS_CHAOS | {"origin": "https://evil.example"})
    assert r.status_code == 403


# --- the Host allowlist ---------------------------------------------------------


def test_a_rebound_host_is_refused_even_when_its_origin_matches(board):
    """DNS rebinding: the attacker's page is same-origin with itself, so only Host tells."""
    iid = ready(board)
    evil = "http://attacker.example:28090"
    client = local_client(board, evil)
    assert client.get("/").status_code == 403
    assert client.get(f"/issues/{iid}").status_code == 403
    r = client.post(f"/issues/{iid}/note", data={"note": "x"},
                    headers=AS_CHAOS | {"origin": evil})
    assert r.status_code == 403
    assert [e["kind"] for e in core.show(board, iid)["events"]] == ["create"]


@pytest.mark.parametrize("host", ["127.0.0.1:28090", "localhost:28090", "LOCALHOST:28090"])
def test_loopback_names_on_the_board_port_are_served(board, client, host):
    # httpx lowercases a base_url's host, so the header is set by hand to test case.
    assert client.get("/", headers={"host": host}).status_code == 200


def test_a_loopback_host_on_another_port_is_refused(board):
    assert local_client(board, "http://127.0.0.1:9999").get("/").status_code == 403


def test_board_web_port_moves_the_allowed_port(board, monkeypatch):
    monkeypatch.setenv(web.PORT_ENV, "28091")
    assert local_client(board, "http://127.0.0.1:28091").get("/").status_code == 200
    assert local_client(board, "http://127.0.0.1:28090").get("/").status_code == 403


def test_board_web_hosts_admits_a_tunnel_host(board, monkeypatch):
    monkeypatch.setenv(web.HOSTS_ENV, " board.example.net , other.example:8443 ,")
    assert local_client(board, "https://board.example.net").get("/").status_code == 200
    assert local_client(board, "https://other.example:8443").get("/").status_code == 200
    assert local_client(board, "https://other.example").get("/").status_code == 403
    assert local_client(board, "https://third.example").get("/").status_code == 403


def test_a_request_without_a_host_header_is_refused(board, client):
    r = client.get("/", headers={"host": ""})
    assert r.status_code == 403


# --- MS-628: card click, markdown, drag targets, hideable closed columns ---------


def test_the_whole_card_links_to_its_issue_page(board, client):
    iid = ready(board)
    assert f'data-href="/issues/{iid}"' in card(client.get("/").text, iid)


def test_the_issue_body_renders_as_markdown(board, client):
    iid = ready(board, body="Some **bold** text\n\n- one\n- two")
    html = client.get(f"/issues/{iid}").text
    assert "<strong>bold</strong>" in html
    assert "<li>one</li>" in html


def issue_forms(html):
    """Each <form> on an issue page, keyed by its action."""
    return {m.group(1): m.group(0) for m in
            re.finditer(r'<form[^>]*action="([^"]*)"[^>]*>.*?</form>', html, re.S)}


def edit_control(html):
    """The closed-by-default Edit issue control: (details attributes, its contents)."""
    m = re.search(r"<details([^>]*)>\s*<summary>Edit issue</summary>(.*?)</details>", html, re.S)
    assert m, "the edit form sits behind one Edit issue control"
    return m.group(1), m.group(2), html.replace(m.group(0), "")


def test_ms637_the_issue_page_is_read_only_until_edit_issue_is_opened(board, client):
    iid = ready(board, body="Some **bold** text")
    html = client.get(f"/issues/{iid}").text
    attrs, inside, outside = edit_control(html)
    assert "open" not in attrs, "closed by default"
    assert f'action="/issues/{iid}/edit"' in inside
    for field in ("title", "body", "rank", "labels"):
        assert f'name="{field}"' in inside
        assert f'name="{field}"' not in outside, f"{field} is editable outside the control"
    assert re.search(r'<textarea name="body">Some \*\*bold\*\* text</textarea>', inside)
    assert "Edit body" not in html, "no nested Edit body control"
    assert "<strong>bold</strong>" in outside
    assert re.search(r"<h1>MS-\d+ item</h1>", outside), "the title reads as the heading"


def test_ms637_the_note_form_is_there_by_default(board, client):
    iid = ready(board)
    _, _, outside = edit_control(client.get(f"/issues/{iid}").text)
    assert re.search(r'<textarea name="note" required', issue_forms(outside)[f"/issues/{iid}/note"])


def test_ms639_edit_issue_comes_before_the_note_form(board, client):
    iid = ready(board)
    html = client.get(f"/issues/{iid}").text
    edit = html.index("<summary>Edit issue</summary>")
    note = html.index(f'action="/issues/{iid}/note"')
    assert edit < note, "the Edit issue control sits above the note form"


def test_ms661_edit_issue_has_no_state_select_and_no_note_box(board, client):
    iid = ready(board)
    _, inside, _ = edit_control(client.get(f"/issues/{iid}").text)
    assert 'name="state"' not in inside
    assert 'name="note"' not in inside
    assert "data-state-note" not in inside


def test_ms661_the_note_form_carries_the_state_select(board, client):
    """Only the moves allowed from here, plus where the issue is now, selected."""
    iid = ready(board)
    html = client.get(f"/issues/{iid}").text
    form = issue_forms(html)[f"/issues/{iid}/note"]
    select = re.search(r'<select name="state"[^>]*data-current="ready"[^>]*>(.*?)</select>',
                       form, re.S)
    assert select, "the note form has the state select"
    options = re.findall(r"<option[^>]*>([^<]+)</option>", select.group(1))
    allowed = [s for s in core.TRANSITIONS if s == "ready" or s in core.TRANSITIONS["ready"]]
    assert options == allowed
    assert re.search(r"<option[^>]*selected[^>]*>ready</option>", select.group(1))
    assert f'name="version" value="{core.show(board, iid)["version"]}"' in form
    assert len(re.findall(r'name="note"', form)) == 1, "one note box serves both"
    # Without JS the button reads Save; the script relabels it Add note.
    assert "<button>Save</button>" in form
    script = re.search(r"<script>(.*?)</script>", html[html.index(form):], re.S)
    assert script and "Add note" in script.group(1) and "data-current" in script.group(1)


def test_ms661_a_note_with_the_state_unchanged_annotates(board, client):
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/note", data=note_form(issue, "just a note"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    after = core.show(board, iid)
    assert after["state"] == "ready"
    assert [(e["kind"], e["note"]) for e in after["events"]][-1] == ("annotate", "just a note")


def test_ms661_a_note_with_a_new_state_moves_the_card_and_keeps_the_note(board, client):
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/note",
                    data=note_form(issue, "parking this for **now**", state="onhold"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    after = core.show(board, iid)
    assert after["state"] == "onhold"
    last = after["events"][-1]
    assert (last["kind"], last["from_state"], last["to_state"], last["note"]) == \
        ("transition", "ready", "onhold", "parking this for **now**")
    assert not any(e["kind"] == "annotate" for e in after["events"]), "one event, not two"
    assert "<strong>now</strong>" in client.get(f"/issues/{iid}").text


def test_ms661_a_state_change_needs_no_note_unless_core_demands_one(board, client):
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data=note_form(core.show(board, iid), state="onhold"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    assert core.show(board, iid)["state"] == "onhold"


def test_ms661_need_input_without_a_note_is_refused(board, client):
    iid = ready(board)
    core.next(board, "run-2")
    core.edit(board, iid, actor=CHAOS, expected_version=core.show(board, iid)["version"],
              title="t", preempt=True)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/note", data=note_form(issue, state="need-input"),
                    headers=AS_CHAOS)
    assert r.status_code == 422
    assert "needs a note" in r.text
    assert core.show(board, iid)["state"] == "processing"


def test_ms661_a_state_change_under_a_live_lease_offers_a_take_over(board, client):
    iid = ready(board)
    core.next(board, "run-2")
    issue = core.show(board, iid)
    refused = client.post(f"/issues/{iid}/note",
                          data=note_form(issue, "stop, spec changed", state="need-input"),
                          headers=AS_CHAOS)
    assert refused.status_code == 409 and "Take over?" in refused.text
    assert re.search(rf'<form[^>]*action="/issues/{iid}/note"', refused.text)
    assert core.show(board, iid)["state"] == "processing"
    taken = client.post(f"/issues/{iid}/note",
                        data=note_form(issue, "stop, spec changed", state="need-input",
                                       preempt="1"),
                        headers=AS_CHAOS)
    assert taken.status_code == 303
    assert core.show(board, iid)["state"] == "need-input"


def test_ms661_a_stale_state_change_is_a_conflict(board, client):
    iid = ready(board)
    stale = core.show(board, iid)
    core.edit(board, iid, actor=CHAOS, expected_version=stale["version"], title="one")
    r = client.post(f"/issues/{iid}/note", data=note_form(stale, state="onhold"),
                    headers=AS_CHAOS)
    assert r.status_code == 409 and "changed since" in r.text
    assert core.show(board, iid)["state"] == "ready"


def test_ms661_an_edit_post_carrying_a_state_does_not_change_it(board, client):
    """An old cached Edit issue form still posts state and note; both are ignored."""
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/edit",
                    data=edit_form(issue, title="renamed", state="done", note="finished"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    after = core.show(board, iid)
    assert (after["title"], after["state"]) == ("renamed", "ready")


def test_ms634_an_unopened_body_still_round_trips_through_edit(board, client):
    """The hidden textarea still posts, so a save that never opened it keeps the body."""
    iid = ready(board, body="keep me")
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/edit", data=edit_form(issue, title="renamed"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    after = core.show(board, iid)
    assert (after["title"], after["body"]) == ("renamed", "keep me")


def test_ms634_note_fields_are_textareas(board, client):
    iid = ready(board)
    html = client.get(f"/issues/{iid}").text
    assert not re.search(r'<input[^>]*name="note"', html)
    forms = issue_forms(html)
    assert re.search(r'<textarea name="note" required', forms[f"/issues/{iid}/note"])


def test_a_note_renders_as_markdown(board, client):
    iid = ready(board)
    core.annotate(board, iid, "see `backlog.py next`", actor=CHAOS, actor_kind=HUMAN)
    assert "<code>backlog.py next</code>" in client.get(f"/issues/{iid}").text


@pytest.mark.parametrize("hostile", [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "[click](javascript:alert(1))",
])
def test_markdown_from_an_agent_is_sanitised(board, client, hostile):
    iid = ready(board, body=hostile)
    core.annotate(board, iid, hostile, actor=CHAOS, actor_kind=HUMAN)
    html = client.get(f"/issues/{iid}").text
    assert "<script>alert" not in html
    assert "<img" not in html
    assert 'href="javascript:' not in html


def test_each_card_carries_the_columns_it_may_move_to(board, client):
    iid = core.create(board, "MS", "an open one", actor=CHAOS, actor_kind=HUMAN)["id"]
    m = re.search(r'data-moves="([^"]*)"', card(client.get("/").text, iid))
    assert m and set(m.group(1).split()) == core.TRANSITIONS["backlog"]


def test_a_markdown_image_does_not_fetch_a_third_party_url(board, client):
    iid = ready(board, body="![t](https://tracker.example/t.png)")
    assert "<img" not in client.get(f"/issues/{iid}").text


# --- search, its own page (MS-629, moved off the board by MS-633) ----------


def hits(html):
    """The issue ids a search page lists, in order."""
    return re.findall(r'<li class="hit"[^>]*><a href="/issues/([^"]+)">', html)


def main_of(html):
    """The page body without the header, whose search box is on every page."""
    return re.search(r"<main>.*</main>", html, re.S).group(0)


def test_the_board_has_no_search_and_ignores_filter_params(client, board):
    iid = ready(board, "Lease expiry")
    html = main_of(client.get("/", params={"q": "zzz", "project": "XX"}).text)
    assert f'data-id="{iid}"' in html
    assert not re.search(r'<input[^>]*name="q"', html)
    assert not re.search(r'<select name="label"', html)


def test_every_page_header_links_to_search_and_preferences(client, board):
    iid = ready(board)
    for path in ("/", f"/issues/{iid}", "/search", "/preferences", "/projects"):
        header = re.search(r"<header>.*?</header>", client.get(path).text, re.S).group(0)
        assert 'href="/search"' in header and 'href="/preferences"' in header
        assert 'href="/projects"' in header


def header_of(html):
    return re.search(r"<header>.*?</header>", html, re.S).group(0)


def test_every_page_header_has_a_search_box_and_an_advanced_search_link(client, board):
    iid = ready(board)
    for path in ("/", f"/issues/{iid}", "/search", "/preferences", "/projects"):
        header = header_of(client.get(path).text)
        form = re.search(r'<form[^>]*method="get"[^>]*action="/search"[^>]*>(.*?)</form>',
                         header, re.S)
        assert form, path
        assert re.search(r'<input[^>]*name="q"[^>]*placeholder="Search issues"', form.group(1))
        assert re.search(r"<button[^>]*>Search</button>", form.group(1))
        assert re.search(r'<a href="/search">Advanced search</a>', header), path
        assert not re.search(r'<a href="/search">Search</a>', header), path


def test_the_header_search_box_finds_an_issue_by_a_title_word(client, board):
    hit = ready(board, "Quokka migration")
    ready(board, "Lease expiry")
    assert hits(client.get("/search", params={"q": "quokka"}).text) == [hit]


def test_the_header_search_box_keeps_the_query_on_the_search_page(client, board):
    ready(board, "Quokka migration")
    header = header_of(client.get("/search", params={"q": "quokka"}).text)
    assert re.search(r'<input[^>]*name="q"[^>]*value="quokka"', header)
    assert not re.search(r'<input[^>]*name="q"[^>]*value="[^"]', header_of(client.get("/").text))


def test_the_search_page_is_titled_advanced_search(client, board):
    html = client.get("/search").text
    assert "<title>Advanced search -- agent-board</title>" in html
    assert re.search(r"<h1[^>]*>Advanced search</h1>", main_of(html))


def test_search_lists_hits_by_q_linking_to_their_issue_pages(client, board):
    hit = ready(board, "Search box", body="filter the board")
    miss = ready(board, "Lease expiry")
    html = client.get("/search", params={"q": "the BOARD"}).text
    assert hits(html) == [hit]
    assert miss not in hits(html)
    assert re.search(r'<input[^>]*name="q"[^>]*value="the BOARD"', html)
    assert 'class="kanban"' not in html


def test_search_filters_combine_and_keep_their_selection(client, board):
    core.create_project(board, "BR", "brain")
    ready(board, "web one", labels=["web"])
    b = core.create(board, "BR", "web two", actor=CHAOS, actor_kind=HUMAN,
                    labels=["web"])["id"]
    html = client.get("/search", params={"q": "web", "project": "BR", "label": "web",
                                         "assignee": ""}).text
    assert hits(html) == [b]
    assert re.search(r'<option value="BR" selected>', html)
    assert re.search(r'<select name="label"[^>]*>.*<option value="web" selected>', html, re.S)


def test_search_offers_every_label_and_assignee(client, board):
    ready(board, "one", labels=["web", "core"])
    html = client.get("/search").text
    labels = re.search(r'<select name="label"[^>]*>(.*?)</select>', html, re.S).group(1)
    assert '<option value="core">' in labels and '<option value="web">' in labels
    assert re.search(r'<select name="assignee"', html)


def test_search_finds_workflow_steps_too(client, board):
    wf = release(board, ["build", "ship"])
    assert hits(client.get("/search", params={"q": "ship"}).text) == [wf["steps"][1]["id"]]


def test_search_with_no_filter_lists_nothing_and_says_so(client, board):
    ready(board)
    html = client.get("/search").text
    assert hits(html) == []
    assert "no-hits" not in html


def test_a_search_matching_nothing_says_so(client, board):
    ready(board)
    html = client.get("/search", params={"q": "zzz"}).text
    assert hits(html) == [] and 'class="no-hits"' in html


# --- saved view preferences (MS-633) -------------------------------------------


def lanes(html):
    return re.findall(r'<section class="column[^"]*" data-state="([^"]+)"', html)


def two_projects(board):
    core.create_project(board, "BR", "brain")
    ms = ready(board, "ms card")
    br = core.create(board, "BR", "br card", actor=CHAOS, actor_kind=HUMAN,
                     state="ready")["id"]
    return ms, br


def test_without_a_cookie_the_board_shows_every_project_and_lane(client, board):
    ms, br = two_projects(board)
    html = client.get("/").text
    assert lanes(html) == list(core.TRANSITIONS)
    assert f'data-id="{ms}"' in html and f'data-id="{br}"' in html


def test_saving_preferences_sets_a_lax_year_long_cookie(client, board):
    two_projects(board)
    r = client.post("/preferences", data={"project": ["BR"], "lane": ["ready", "done"]})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{web.PREFS_COOKIE}=")
    assert "samesite=lax" in cookie.lower()
    assert "max-age=31536000" in cookie.lower()
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE)) == {
        "projects": ["BR"], "lanes": ["ready", "done"], "timezone": ""}


def test_the_board_shows_only_the_saved_projects_and_lanes(client, board):
    ms, br = two_projects(board)
    client.post("/preferences", data={"project": ["BR"], "lane": ["done", "ready"]})
    html = client.get("/").text
    # Lanes keep board order, whatever order the form sent them in.
    assert lanes(html) == ["ready", "done"]
    assert f'data-id="{br}"' in html and f'data-id="{ms}"' not in html


def test_the_preferences_form_shows_the_saved_choices(client, board):
    two_projects(board)
    client.post("/preferences", data={"project": ["BR"], "lane": ["ready"]})
    html = client.get("/preferences").text
    assert re.search(r'<input type="checkbox" name="project" value="BR" checked>', html)
    assert re.search(r'<input type="checkbox" name="project" value="MS">', html)
    assert re.search(r'<input type="checkbox" name="lane" value="ready" checked>', html)
    assert re.search(r'<input type="checkbox" name="lane" value="done">', html)


def test_without_a_cookie_the_form_checks_everything(client, board):
    two_projects(board)
    html = client.get("/preferences").text
    boxes = re.findall(r'<input type="checkbox" name="(?:project|lane)" value="[^"]+"( checked)?>',
                       html)
    assert len(boxes) == 2 + len(core.TRANSITIONS) and all(boxes)


def test_unknown_keys_projects_and_lanes_in_the_cookie_are_ignored(client, board):
    ms, br = two_projects(board)
    client.cookies.set(web.PREFS_COOKIE, json.dumps(
        {"lanes": ["ready", "bogus"], "projects": ["MS", "ZZ"], "theme": "dark"}))
    html = client.get("/").text
    assert lanes(html) == ["ready"]
    assert f'data-id="{ms}"' in html and f'data-id="{br}"' not in html


@pytest.mark.parametrize("value", ["not json", "[1, 2]", '{"lanes": "ready"}',
                                   '{"lanes": ["bogus"], "projects": []}'])
def test_a_cookie_that_names_nothing_usable_shows_everything(client, board, value):
    ms, br = two_projects(board)
    client.cookies.set(web.PREFS_COOKIE, value)
    html = client.get("/").text
    assert lanes(html) == list(core.TRANSITIONS)
    assert f'data-id="{ms}"' in html and f'data-id="{br}"' in html


def test_a_workflow_card_follows_the_project_of_its_steps(client, board):
    core.create_project(board, "BR", "brain")
    wf = release(board)
    marker = f'data-workflow="{wf["id"]}"'
    client.post("/preferences", data={"project": ["BR"], "lane": list(core.TRANSITIONS)})
    assert marker not in client.get("/").text
    client.post("/preferences", data={"project": ["MS"], "lane": list(core.TRANSITIONS)})
    assert marker in client.get("/").text


def test_ticking_every_project_tracks_projects_created_later(client, board):
    two_projects(board)
    client.post("/preferences", data={"project": ["BR", "MS"], "lane": ["ready"]})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE))["projects"] == []
    core.create_project(board, "XX", "later")
    later = core.create(board, "XX", "new", actor=CHAOS, actor_kind=HUMAN,
                        state="ready")["id"]
    assert f'data-id="{later}"' in client.get("/").text


def test_saving_keeps_only_known_projects_and_lanes(client, board):
    two_projects(board)
    client.post("/preferences", data={"project": ["BR", "ZZ"] * 300,
                                      "lane": ["done", "bogus", "ready"]})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE)) == {
        "projects": ["BR"], "lanes": ["ready", "done"], "timezone": ""}


def test_preferences_are_a_same_origin_post_only(client, board):
    r = client.post("/preferences", data={"lane": ["ready"]},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


# --- project filter banner on the dashboard (MS-650) ---------------------------


def banner(html):
    """The project filter banner above the kanban."""
    m = re.search(r'<details class="filter-banner"[^>]*>.*?</details>', html, re.S)
    assert m, "no filter banner"
    return m.group(0)


def banner_summary(html):
    m = re.search(r"<summary>(.*?)</summary>", banner(html), re.S)
    return " ".join(unescape(m.group(1)).split())


def test_the_banner_sits_above_the_kanban_collapsed(client, board):
    two_projects(board)
    html = client.get("/").text
    b = banner(html)
    assert html.index(b) < html.index('<div class="kanban">')
    assert "<details class=\"filter-banner\">" in b  # no open attribute
    assert 'action="/preferences/projects"' in b and 'method="post"' in b
    assert re.search(r'<input type="checkbox" name="project" value="BR" checked>', b)
    assert re.search(r'<input type="checkbox" name="project" value="MS" checked>', b)
    assert "<button>Apply</button>" in b


def test_the_banner_summary_reads_all_or_the_subset(client, board):
    two_projects(board)
    core.create_project(board, "DR", "doc-review")
    assert banner_summary(client.get("/").text) == "View"
    client.post("/preferences/projects", data={"project": ["MS", "BR"]})
    b = client.get("/").text
    # Board order, not form order.
    assert banner_summary(b) == "View: BR, MS"
    assert re.search(r'<input type="checkbox" name="project" value="DR">', banner(b))


def test_applying_a_subset_filters_loose_and_workflow_cards(client, board):
    ms, br = two_projects(board)
    wf = release(board)
    marker = f'data-workflow="{wf["id"]}"'
    r = client.post("/preferences/projects", data={"project": ["BR"]})
    assert r.status_code == 303 and r.headers["location"] == "/"
    html = client.get("/").text
    assert f'data-id="{br}"' in html and f'data-id="{ms}"' not in html
    assert marker not in html
    client.post("/preferences/projects", data={"project": ["MS"]})
    html = client.get("/").text
    assert f'data-id="{ms}"' in html and marker in html and f'data-id="{br}"' not in html


def test_the_banner_writes_the_same_cookie_as_the_preferences_page(client, board):
    two_projects(board)
    r = client.post("/preferences/projects", data={"project": ["BR"]})
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{web.PREFS_COOKIE}=")
    assert "samesite=lax" in cookie.lower() and "max-age=31536000" in cookie.lower()
    html = client.get("/preferences").text
    assert re.search(r'<input type="checkbox" name="project" value="BR" checked>', html)
    assert re.search(r'<input type="checkbox" name="project" value="MS">', html)


def test_a_banner_save_keeps_the_saved_lanes_and_timezone(client, board):
    two_projects(board)
    client.post("/preferences", data={"project": ["MS"], "lane": ["done", "ready"],
                                      "timezone": "Asia/Tokyo"})
    client.post("/preferences/projects", data={"project": ["BR"]})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE)) == {
        "projects": ["BR"], "lanes": ["ready", "done"], "timezone": "Asia/Tokyo"}
    assert lanes(client.get("/").text) == ["ready", "done"]


def test_a_banner_save_of_every_project_saves_all(client, board):
    two_projects(board)
    client.post("/preferences/projects", data={"project": ["BR"]})
    client.post("/preferences/projects", data={"project": ["MS", "BR", "ZZ"]})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE))["projects"] == []
    core.create_project(board, "XX", "later")
    later = core.create(board, "XX", "new", actor=CHAOS, actor_kind=HUMAN,
                        state="ready")["id"]
    assert f'data-id="{later}"' in client.get("/").text


@pytest.mark.parametrize("sent", [{}, {"project": ["ZZ"]}])
def test_a_banner_save_with_no_project_ticked_is_refused(client, board, sent):
    two_projects(board)
    client.post("/preferences/projects", data={"project": ["BR"]})
    r = client.post("/preferences/projects", data=sent)
    assert r.status_code == 422
    assert "at least one project" in r.text
    assert "set-cookie" not in r.headers
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE))["projects"] == ["BR"]


def test_the_banner_save_is_a_same_origin_post_only(client, board):
    two_projects(board)
    r = client.post("/preferences/projects", data={"project": ["BR"]},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_the_preferences_page_still_saves_every_key(client, board):
    two_projects(board)
    client.post("/preferences/projects", data={"project": ["BR"]})
    client.post("/preferences", data={"project": ["MS"], "lane": ["ready"]})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE)) == {
        "projects": ["MS"], "lanes": ["ready"], "timezone": ""}


# --- the banner is "View", right of the toggles (MS-653) -------------------------


def controls_row(html):
    m = re.search(r'<div class="board-controls">(.*?)</details>\s*</div>', html, re.S)
    assert m, "no controls row"
    return m.group(0)


def test_the_toggles_and_the_view_banner_share_one_row_view_last(client, board):
    two_projects(board)
    html = client.get("/").text
    row = controls_row(html)
    assert banner(html) in row
    assert row.index("<summary>New issue</summary>") < row.index('class="filter-banner"')
    assert row.index(banner(html)) + len(banner(html)) == row.rindex("</details>") + len("</details>")
    assert html.index(row) < html.index('<div class="kanban">')


def test_the_view_banner_is_right_aligned_and_its_panel_anchored_right(client, board):
    html = client.get("/").text
    assert re.search(r"\.board-controls \{[^}]*display: flex", html)
    assert re.search(r"\.filter-banner \{[^}]*margin-left: auto", html)
    assert re.search(r"\.view-panel \{[^}]*position: absolute[^}]*right: 0", html)
    assert '<div class="view-panel">' in banner(html)
    # An opened toggle keeps the full row width its form had before MS-653.
    assert re.search(r"\.board-controls > details\[open\]:not\(\.filter-banner\) \{[^}]*flex: 1", html)


def test_the_view_panel_holds_the_project_form_and_show_buttons(client, board):
    two_projects(board)
    client.post("/preferences/lanes/hide", data={"lane": "done"})
    b = banner(client.get("/").text)
    panel = b[b.index('<div class="view-panel">'):]
    assert 'action="/preferences/projects"' in panel
    assert 'action="/preferences/lanes/show"' in panel


# --- a hide control on each swim lane (MS-651) ---------------------------------


ALL_LANES = list(core.TRANSITIONS)


def hide_buttons(html):
    """The lanes whose heading carries a hide control."""
    return re.findall(r'<form method="post" action="/preferences/lanes/hide">'
                      r'<input type="hidden" name="lane" value="([^"]+)">', html)


def saved_lanes(client):
    return web.read_prefs(client.cookies.get(web.PREFS_COOKIE))["lanes"]


def test_every_lane_heading_has_a_hide_control(client, board):
    html = client.get("/").text
    assert hide_buttons(html) == ALL_LANES
    assert 'title="Hide this lane"' in html


def test_hiding_a_lane_removes_exactly_that_lane_in_order(client, board):
    r = client.post("/preferences/lanes/hide", data={"lane": "processing"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    rest = [s for s in ALL_LANES if s != "processing"]
    assert saved_lanes(client) == rest
    assert lanes(client.get("/").text) == rest
    client.post("/preferences/lanes/hide", data={"lane": "backlog"})
    assert lanes(client.get("/").text) == rest[1:]


def test_hidden_lanes_show_only_inside_the_opened_view_banner(client, board):
    client.post("/preferences/lanes/hide", data={"lane": "cancelled"})
    client.post("/preferences/lanes/hide", data={"lane": "done"})
    html = client.get("/").text
    # The collapsed summary says nothing about hidden lanes (MS-653).
    assert banner_summary(html) == "View"
    assert html.count("Hidden lanes") == banner(html).count("Hidden lanes") == 1
    shows = re.findall(r'<form method="post" action="/preferences/lanes/show">'
                       r'<input type="hidden" name="lane" value="([^"]+)">', banner(html))
    assert shows == ["done", "cancelled"]


def test_no_hidden_lanes_leaves_the_summary_and_show_buttons_out(client, board):
    html = client.get("/").text
    assert "Hidden lanes" not in html and "/preferences/lanes/show" not in html


def test_showing_a_lane_restores_it_in_board_order(client, board):
    client.post("/preferences", data={"lane": ["done", "ready"]})
    r = client.post("/preferences/lanes/show", data={"lane": "backlog"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert lanes(client.get("/").text) == ["backlog", "ready", "done"]


def test_showing_the_last_hidden_lane_saves_all(client, board):
    client.post("/preferences/lanes/hide", data={"lane": "onhold"})
    client.post("/preferences/lanes/show", data={"lane": "onhold"})
    assert saved_lanes(client) == []
    assert lanes(client.get("/").text) == ALL_LANES


def test_hiding_down_to_one_lane_leaves_no_hide_button(client, board):
    client.post("/preferences", data={"lane": ["ready", "done"]})
    assert hide_buttons(client.get("/").text) == ["ready", "done"]
    client.post("/preferences/lanes/hide", data={"lane": "done"})
    html = client.get("/").text
    assert lanes(html) == ["ready"]
    assert hide_buttons(html) == []


def test_hiding_the_last_visible_lane_is_refused(client, board):
    client.post("/preferences", data={"lane": ["ready"]})
    r = client.post("/preferences/lanes/hide", data={"lane": "ready"})
    assert r.status_code == 422 and "set-cookie" not in r.headers
    assert saved_lanes(client) == ["ready"]


def test_a_lane_save_keeps_projects_and_timezone(client, board):
    two_projects(board)
    client.post("/preferences", data={"project": ["BR"], "timezone": "Asia/Tokyo"})
    client.post("/preferences/lanes/hide", data={"lane": "done"})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE)) == {
        "projects": ["BR"], "lanes": [s for s in ALL_LANES if s != "done"],
        "timezone": "Asia/Tokyo"}
    client.post("/preferences/lanes/show", data={"lane": "done"})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE)) == {
        "projects": ["BR"], "lanes": [], "timezone": "Asia/Tokyo"}


@pytest.mark.parametrize("action", ["hide", "show"])
@pytest.mark.parametrize("sent", [{}, {"lane": "nonsense"}])
def test_an_unknown_lane_is_refused(client, board, action, sent):
    client.post("/preferences/lanes/hide", data={"lane": "done"})
    r = client.post(f"/preferences/lanes/{action}", data=sent)
    assert r.status_code == 422 and "set-cookie" not in r.headers
    assert saved_lanes(client) == [s for s in ALL_LANES if s != "done"]


@pytest.mark.parametrize("action", ["hide", "show"])
def test_the_lane_controls_are_same_origin_posts_only(client, board, action):
    r = client.post(f"/preferences/lanes/{action}", data={"lane": "done"},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_the_hide_control_sits_outside_the_drag_lists(client, board):
    ready(board, "a card")
    html = client.get("/").text
    for cards in re.findall(r'<div class="cards"[^>]*>.*?</div>', html, re.S):
        assert "/preferences/lanes/hide" not in cards
    # Every remaining lane still carries a drop list after a hide.
    client.post("/preferences/lanes/hide", data={"lane": "onhold"})
    html = client.get("/").text
    drops = re.findall(r'<div class="cards" data-state="([^"]+)">', html)
    assert drops == lanes(html)


def test_the_localstorage_hide_closed_toggle_is_gone(client, board):
    html = client.get("/").text
    assert "hide-closed" not in html and "localStorage" not in html


def test_the_current_step_moves_past_a_done_step(client, board):
    wf = release(board, ["build", "ship"])
    build, ship = [st["id"] for st in wf["steps"]]
    claim = core.next(board, "w1")
    core.transition(board, build, "done", note="built", actor="w1", actor_kind=AGENT,
                    token=claim["lease_token"])
    c = wf_card(client.get("/").text, wf["id"])
    assert "current: ship" in " ".join(re.sub(r"<[^>]+>", " ", c).split())
    assert client.get(f"/workflows/{wf['id']}").headers["location"] == f"/issues/{ship}"


def test_ms637_the_hidden_attribute_beats_label_display_block(board, client):
    """`label { display: block }` outranks the UA's [hidden] rule, so JS hiding needs this."""
    html = client.get(f"/issues/{ready(board)}").text
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", html)


# --- history times in local time (MS-640) --------------------------------------


def history_times(html):
    return re.findall(r"<li>(\S+ \S+ \S+) &middot;", html)


def tz_client(board, monkeypatch, env=None):
    if env is not None:
        monkeypatch.setenv(web.TZ_ENV, env)
    return local_client(board)


def with_tz_cookie(client, value):
    client.cookies.set(web.PREFS_COOKIE, quote(json.dumps({"timezone": value}), safe=""))
    return client


def test_localtime_renders_minutes_and_the_zone_abbreviation():
    at = "2026-09-28T00:06:53.304775+00:00"
    assert web.localtime(at, ZoneInfo("Australia/Sydney")) == "2026-09-28 10:06 AEST"
    assert web.localtime(at, ZoneInfo("UTC")) == "2026-09-28 00:06 UTC"
    assert web.localtime(datetime.fromisoformat(at), ZoneInfo("Asia/Tokyo")) == "2026-09-28 09:06 JST"
    assert web.localtime(None, ZoneInfo("UTC")) == ""


def test_history_is_utc_when_neither_env_nor_cookie_names_a_zone(board, monkeypatch):
    iid = ready(board)
    times = history_times(tz_client(board, monkeypatch).get(f"/issues/{iid}").text)
    assert times and all(t.endswith(" UTC") for t in times)
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", t) for t in times)


def test_board_timezone_sets_the_default_zone(board, monkeypatch):
    iid = ready(board)
    times = history_times(tz_client(board, monkeypatch, "Asia/Tokyo").get(f"/issues/{iid}").text)
    assert times and all(t.endswith(" JST") for t in times)


def test_the_cookie_zone_overrides_the_env_default(board, monkeypatch):
    iid = ready(board)
    client = with_tz_cookie(tz_client(board, monkeypatch, "Asia/Tokyo"), "Asia/Kolkata")
    times = history_times(client.get(f"/issues/{iid}").text)
    assert times and all(t.endswith(" IST") for t in times)


def test_a_bad_cookie_zone_falls_back_to_the_env_default(board, monkeypatch):
    iid = ready(board)
    client = with_tz_cookie(tz_client(board, monkeypatch, "Asia/Tokyo"), "Mars/Olympus")
    times = history_times(client.get(f"/issues/{iid}").text)
    assert times and all(t.endswith(" JST") for t in times)


def test_a_bad_env_zone_is_logged_and_the_board_stays_up_in_utc(board, monkeypatch, caplog):
    iid = ready(board)
    with caplog.at_level("WARNING"):
        client = tz_client(board, monkeypatch, "Not/AZone")
    assert "Not/AZone" in caplog.text
    times = history_times(client.get(f"/issues/{iid}").text)
    assert times and all(t.endswith(" UTC") for t in times)


def test_the_preferences_form_saves_and_shows_a_timezone(client, board):
    r = client.post("/preferences", data={"timezone": " Asia/Tokyo "})
    assert r.status_code == 303
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE))["timezone"] == "Asia/Tokyo"
    html = client.get("/preferences").text
    assert re.search(r'<input[^>]*name="timezone"[^>]*value="Asia/Tokyo"', html)


def test_an_unknown_timezone_in_the_form_is_saved_as_blank(client, board):
    client.post("/preferences", data={"timezone": "Mars/Olympus"})
    assert web.read_prefs(client.cookies.get(web.PREFS_COOKIE))["timezone"] == ""


def test_lease_expiry_renders_in_the_chosen_zone(board, monkeypatch):
    iid = ready(board)
    core.next(board, "run-4")
    expires = core.show(board, iid)["lease_expires_at"]
    want = web.localtime(expires, ZoneInfo("Asia/Tokyo"))
    client = tz_client(board, monkeypatch, "Asia/Tokyo")
    assert f"Held by run-4 until {want}" in client.get(f"/issues/{iid}").text
    assert f"Held by run-4 until {want}" in card(client.get("/").text, iid)
    refused = client.post(f"/issues/{iid}/edit", data=edit_form(core.show(board, iid), body="x"),
                          headers=AS_CHAOS)
    assert refused.status_code == 409 and f"until {want}" in refused.text


# --- managing projects (MS-642) ----------------------------------------------


def project_row(html, key):
    m = re.search(rf'<tr data-key="{key}">.*?</tr>', html, re.S)
    assert m, f"no row for {key}"
    return m.group(0)


def test_the_projects_page_lists_key_name_and_issue_count(client, board):
    core.create_project(board, "BR", "brain")
    ready(board)
    html = client.get("/projects").text
    ms, br = project_row(html, "MS"), project_row(html, "BR")
    assert "memory-solution" in ms and "<td>1</td>" in ms
    assert "brain" in br and "<td>0</td>" in br
    assert html.index('data-key="BR"') < html.index('data-key="MS"')


def test_the_projects_page_offers_rename_of_the_name_only(client, board):
    row = project_row(client.get("/projects").text, "MS")
    assert 'action="/projects/MS/rename"' in row
    rename = re.search(r'<form[^>]*/rename".*?</form>', row, re.S).group(0)
    assert 'name="name"' in rename and 'name="key"' not in rename


def test_delete_is_offered_only_for_a_project_with_no_issues(client, board):
    core.create_project(board, "BR", "brain")
    ready(board)
    html = client.get("/projects").text
    assert 'action="/projects/BR/delete"' in project_row(html, "BR")
    assert "/delete" not in project_row(html, "MS")


def test_creating_a_project_from_the_page(client, board):
    r = client.post("/projects", data={"key": "BR", "name": "brain"}, headers=AS_CHAOS)
    assert r.status_code == 303 and r.headers["location"] == "/projects"
    assert {"key": "BR", "name": "brain", "issues": 0} in core.projects(board)


def test_a_taken_key_shows_the_error_page(client, board):
    r = client.post("/projects", data={"key": "MS", "name": "again"}, headers=AS_CHAOS)
    assert r.status_code == 409 and "already exists" in r.text
    assert core.projects(board)[0]["name"] == "memory-solution"


def test_renaming_a_project_from_the_page(client, board):
    iid = ready(board)
    r = client.post("/projects/MS/rename", data={"name": "memory"}, headers=AS_CHAOS)
    assert r.status_code == 303 and r.headers["location"] == "/projects"
    assert core.projects(board)[0]["name"] == "memory"
    assert core.show(board, iid)["project"] == "MS"


def test_deleting_a_project_from_the_page(client, board):
    core.create_project(board, "BR", "brain")
    r = client.post("/projects/BR/delete", headers=AS_CHAOS)
    assert r.status_code == 303
    assert [p["key"] for p in core.projects(board)] == ["MS"]


def test_deleting_a_project_with_issues_is_refused(client, board):
    ready(board)
    r = client.post("/projects/MS/delete", headers=AS_CHAOS)
    assert r.status_code == 422 and "1 issue" in r.text
    assert [p["key"] for p in core.projects(board)] == ["MS"]


@pytest.mark.parametrize("path,data", [("/projects", {"key": "BR", "name": "brain"}),
                                       ("/projects/MS/rename", {"name": "x"}),
                                       ("/projects/MS/delete", {})])
def test_project_writes_need_an_actor_and_the_same_origin(client, board, monkeypatch, path, data):
    monkeypatch.delenv(web.ACTOR_ENV, raising=False)
    core.create_project(board, "ZZ", "empty")
    path = path.replace("MS", "ZZ")
    assert client.post(path, data=data).status_code == 401
    r = client.post(path, data=data, headers=AS_CHAOS | {"origin": "https://evil.example"})
    assert r.status_code == 403
    assert core.projects(board) == [{"key": "MS", "name": "memory-solution", "issues": 0},
                                    {"key": "ZZ", "name": "empty", "issues": 0}]


# --- attachments (MS-643) -------------------------------------------------------


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


@pytest.fixture
def attach_dir(tmp_path, monkeypatch):
    d = tmp_path / "attachments"
    monkeypatch.setenv(web.ATTACH_DIR_ENV, str(d))
    monkeypatch.delenv(web.MAX_UPLOAD_ENV, raising=False)
    return d


def test_upload_on_create_stores_bytes_on_disk_by_hash(board, attach_dir, client):
    import hashlib
    r = client.post("/issues", data={"project": "MS", "title": "t", "state": "ready"},
                    files=[("files", ("notes.txt", b"hello", "text/plain"))],
                    headers=AS_CHAOS)
    assert r.status_code == 303
    [a] = core.show(board, "MS-1")["attachments"]
    assert (attach_dir / hashlib.sha256(b"hello").hexdigest()).read_bytes() == b"hello"
    got = client.get(f"/attachments/{a['id']}")
    assert got.status_code == 200 and got.content == b"hello"


def test_an_empty_file_field_attaches_nothing(board, attach_dir, client):
    r = client.post("/issues", data={"project": "MS", "title": "t"},
                    files=[("files", ("", b"", "application/octet-stream"))],
                    headers=AS_CHAOS)
    assert r.status_code == 303
    assert core.show(board, "MS-1")["attachments"] == []


def test_upload_on_edit(board, attach_dir, client):
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/edit", data=edit_form(issue),
                    files=[("files", ("spec.md", b"# spec", "text/markdown"))],
                    headers=AS_CHAOS)
    assert r.status_code == 303
    assert [a["filename"] for a in core.show(board, iid)["attachments"]] == ["spec.md"]


def test_a_notes_file_shows_under_that_note(board, attach_dir, client):
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data={"note": "the log"},
                    files=[("files", ("run.log", b"boom", "text/plain"))], headers=AS_CHAOS)
    assert r.status_code == 303
    issue = core.show(board, iid)
    [a] = issue["attachments"]
    html = client.get(f"/issues/{iid}").text
    history = html.split("<h2>History</h2>", 1)[1]
    item = re.search(r"<li>(?:(?!</li>).)*the log.*?</li>", history, re.S).group(0)
    assert f'href="/attachments/{a["id"]}"' in item and "run.log" in item


def test_the_forms_are_multipart_with_a_file_picker(board, attach_dir, client):
    iid = ready(board)
    for html in (client.get("/").text, client.get(f"/issues/{iid}").text):
        assert 'enctype="multipart/form-data"' in html
        assert '<input type="file" name="files" multiple>' in html
    page = client.get(f"/issues/{iid}").text
    assert page.count('enctype="multipart/form-data"') == 2


def test_an_over_cap_upload_is_refused_and_writes_nothing(board, attach_dir, client,
                                                          monkeypatch):
    monkeypatch.setenv(web.MAX_UPLOAD_ENV, "0.001")  # 1048 bytes
    r = client.post("/issues", data={"project": "MS", "title": "big"},
                    files=[("files", ("big.bin", b"x" * 2000, "application/octet-stream"))],
                    headers=AS_CHAOS)
    assert r.status_code == 413
    assert "big.bin" in unescape(r.text)
    assert core.overview(board)["issues"] == []
    assert not attach_dir.exists() or list(attach_dir.iterdir()) == []


def test_html_and_svg_download_rather_than_open(board, attach_dir, client):
    iid = ready(board)
    for name, ctype in (("x.html", "text/html"), ("x.svg", "image/svg+xml")):
        client.post(f"/issues/{iid}/note", data={"note": name},
                    files=[("files", (name, b"<script>alert(1)</script>", ctype))],
                    headers=AS_CHAOS)
    for a in core.show(board, iid)["attachments"]:
        r = client.get(f"/attachments/{a['id']}")
        assert r.headers["content-disposition"].startswith("attachment;")
        assert r.headers["content-type"] == "application/octet-stream"
        assert r.headers["x-content-type-options"] == "nosniff"


def test_a_png_shows_inline_with_its_type(board, attach_dir, client):
    iid = ready(board)
    client.post(f"/issues/{iid}/note", data={"note": "shot"},
                files=[("files", ("shot.png", PNG, "image/png"))], headers=AS_CHAOS)
    [a] = core.show(board, iid)["attachments"]
    r = client.get(f"/attachments/{a['id']}")
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert f'<img src="/attachments/{a["id"]}"' in client.get(f"/issues/{iid}").text


@pytest.mark.parametrize("raw, clean", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\me\\report.pdf", "report.pdf"),
    ("a\r\nb.txt", "ab.txt"),
    ("..", "file"),
    ("", "file"),
])
def test_filenames_lose_their_path_and_control_characters(raw, clean):
    assert web.safe_filename(raw) == clean


def test_an_upload_without_an_attachment_dir_is_refused(board, client, monkeypatch):
    monkeypatch.delenv(web.ATTACH_DIR_ENV, raising=False)
    r = client.post("/issues", data={"project": "MS", "title": "t"},
                    files=[("files", ("a.txt", b"a", "text/plain"))], headers=AS_CHAOS)
    assert r.status_code == 422
    assert core.overview(board)["issues"] == []


def test_an_unknown_attachment_is_404(board, attach_dir, client):
    assert client.get("/attachments/999").status_code == 404


def test_a_take_over_names_the_files_it_could_not_keep(board, attach_dir, client):
    """A file input cannot be refilled, so the take-over page says which files to attach again."""
    iid = ready(board)
    core.next(board, "run-2")
    issue = core.show(board, iid)
    refused = client.post(f"/issues/{iid}/edit", data=edit_form(issue, body="new spec"),
                          files=[("files", ("spec.md", b"# spec", "text/markdown")),
                                 ("files", ("<i>x.png", PNG, "image/png"))],
                          headers=AS_CHAOS)
    assert refused.status_code == 409
    text = unescape(refused.text)
    assert "spec.md" in text and "<i>x.png" in text and "attach" in text.lower()
    assert "<i>x.png" not in refused.text
    assert 'enctype="multipart/form-data"' in refused.text
    assert '<input type="file" name="files" multiple>' in refused.text
    assert core.show(board, iid)["attachments"] == []

    taken = client.post(f"/issues/{iid}/edit",
                        data=edit_form(issue, body="new spec", preempt="1"),
                        files=[("files", ("spec.md", b"# spec", "text/markdown"))],
                        headers=AS_CHAOS)
    assert taken.status_code == 303
    assert [a["filename"] for a in core.show(board, iid)["attachments"]] == ["spec.md"]


def test_a_take_over_without_files_says_nothing_about_them(board, attach_dir, client):
    iid = ready(board)
    core.next(board, "run-2")
    refused = client.post(f"/issues/{iid}/edit", data=edit_form(core.show(board, iid)),
                          headers=AS_CHAOS)
    assert refused.status_code == 409 and "were not attached" not in refused.text


def test_a_path_in_an_uploaded_filename_is_stripped_on_the_route(board, attach_dir, client):
    iid = ready(board)
    client.post(f"/issues/{iid}/note", data={"note": "n"},
                files=[("files", ("../../x", b"x", "text/plain"))], headers=AS_CHAOS)
    [a] = core.show(board, iid)["attachments"]
    assert a["filename"] == "x"
    assert not (attach_dir.parent.parent / "x").exists()


@pytest.mark.parametrize("raw", ["lots", "-1", "nan", "inf", "0"])
def test_a_bad_upload_cap_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv(web.MAX_UPLOAD_ENV, raw)
    assert web._max_upload_bytes() == web.DEFAULT_MAX_UPLOAD_MB * 1024 * 1024


# --- MS-644: plan an issue as a workflow ------------------------------------------


def test_a_loose_issue_page_offers_plan_as_workflow(board, client):
    iid = ready(board)
    html = client.get(f"/issues/{iid}").text
    assert f'action="/issues/{iid}/plan"' in html


def test_the_plan_form_makes_one_step_per_line(board, client):
    iid = ready(board, "big job")
    r = client.post(f"/issues/{iid}/plan", headers=AS_CHAOS,
                    data={"steps": "design\r\n\r\n  build  \ntest\n"})
    assert r.status_code == 303
    issue = core.show(board, iid)
    assert issue["state"] == "onhold"
    assert [s["title"] for s in issue["plan"]["steps"]] == ["design", "build", "test"]
    assert issue["events"][-1]["actor"] == CHAOS


def test_the_origin_page_shows_its_plan_and_no_second_form(board, client):
    iid = ready(board, "big job")
    view = core.plan(board, iid, [{"title": "a"}, {"title": "b"}], actor=CHAOS,
                     actor_kind=HUMAN)
    a, b = [s["id"] for s in view["plan"]["steps"]]
    html = client.get(f"/issues/{iid}").text
    section = workflow_section(html)
    steps = re.findall(r'<li[^>]*data-step="([^"]+)"', section)
    assert steps == [a, b]
    assert "mermaid" in html
    assert f'action="/issues/{iid}/plan"' not in html
    step_html = client.get(f"/issues/{a}").text
    assert f'href="/issues/{iid}"' in step_html
    assert f'action="/issues/{a}/plan"' not in step_html


def test_a_plan_with_no_steps_is_refused(board, client):
    iid = ready(board)
    r = client.post(f"/issues/{iid}/plan", headers=AS_CHAOS, data={"steps": " \n "})
    assert r.status_code >= 400
    assert core.show(board, iid)["plan"] is None


def test_a_plan_under_an_agents_lease_is_refused_409(board, client):
    iid = ready(board)
    core.next(board, "w1")
    r = client.post(f"/issues/{iid}/plan", headers=AS_CHAOS, data={"steps": "a"})
    assert r.status_code == 409
    assert core.show(board, iid)["state"] == "processing"


# --- MS-646: dependencies on the issue page and in the chart ------------------------


def depend(engine, iid, on):
    return core.depend(engine, iid, on, actor=CHAOS, actor_kind=HUMAN)


def dep_section(html, heading):
    m = re.search(rf'<section class="dependencies"[^>]*data-dep="{heading}"[^>]*>.*?</section>',
                  html, re.S)
    return m.group(0) if m else None


def test_the_issue_page_lists_what_it_waits_on_and_what_it_blocks(board, client):
    a, b, c = ready(board, "first thing"), ready(board, "second"), ready(board, "third")
    depend(board, b, a)
    depend(board, c, b)
    html = client.get(f"/issues/{b}").text
    waits, blocks = dep_section(html, "waits-on"), dep_section(html, "blocks")
    assert waits and "Waits on" in waits and f'href="/issues/{a}"' in waits
    assert "first thing" in waits and '<b class="state state-ready">ready</b>' in waits
    assert blocks and "Blocks" in blocks and f'href="/issues/{c}"' in blocks
    assert f'href="/issues/{a}"' not in blocks


def test_an_issue_without_dependencies_shows_neither_list(board, client):
    a = ready(board, "alone")
    html = client.get(f"/issues/{a}").text
    assert dep_section(html, "waits-on") is None and dep_section(html, "blocks") is None


def dag_wf(deps, done=()):
    """A fake workflow of steps MS-1.. whose `deps` maps a step number to what it waits on."""
    n = max([*deps, *(d for ds in deps.values() for d in ds)])
    return {"steps": [{"id": f"MS-{i}", "title": f"step {i}",
                       "state": "done" if i in done else "ready",
                       "depends_on": [f"MS-{d}" for d in deps.get(i, [])]}
                      for i in range(1, n + 1)]}


def dag(wf, here):
    """(source lines, edge set, gateway node ids) of a dependency chart."""
    src = [l.strip() for l in web.workflow_diagram(wf, here).splitlines()]
    edges = set()
    for l in src:
        if "-->" in l:
            parts = [p.strip() for p in l.split("-->")]
            assert len(parts) == 2, l
            edges.add(tuple(parts))
    gateways = {m.group(1) for l in src if (m := re.fullmatch(r'(g\w+)\{"#43;"\}', l))}
    return src, edges, gateways


def succ(edges, node):
    return {b for a, b in edges if a == node}


def pred(edges, node):
    return {a for a, b in edges if b == node}


def test_a_fork_and_a_join_each_get_a_parallel_gateway():
    # 1 -> {2, 3} -> 4
    src, edges, gws = dag(dag_wf({2: [1], 3: [1], 4: [2, 3]}), "MS-2")
    assert len(gws) == 2
    assert succ(edges, "ev_start") == {"s0"}
    (fork,) = succ(edges, "s0")
    assert fork in gws and succ(edges, fork) == {"s1", "s2"}
    (join,) = pred(edges, "s3")
    assert join in gws and pred(edges, join) == {"s1", "s2"}
    assert succ(edges, "s3") == {"ev_end"}
    assert "class s1 here" in src
    assert all(f"class {g} gateway" in src for g in gws)


def test_a_strict_workflow_draws_no_gateway():
    assert '{"#43;"}' not in web.workflow_diagram(fake_wf(4), "MS-2")
    wf = dag_wf({2: [1]})
    for st in wf["steps"]:
        st["depends_on"] = []
    assert '{"#43;"}' not in web.workflow_diagram(wf, "MS-2")


def test_parallel_starts_and_ends_meet_the_events_through_gateways():
    # Two independent chains: 1 -> 2 and 3 -> 4. Any declared dependency makes it a DAG.
    _, edges, gws = dag(dag_wf({2: [1], 4: [3]}), "MS-1")
    (fork,) = succ(edges, "ev_start")
    assert fork in gws and succ(edges, fork) == {"s0", "s2"}
    (join,) = pred(edges, "ev_end")
    assert join in gws and pred(edges, join) == {"s1", "s3"}


def test_a_dependency_outside_the_workflow_is_not_drawn_but_frees_the_steps():
    wf = {"steps": [{"id": "MS-1", "title": "a", "state": "ready", "depends_on": ["MS-99"]},
                    {"id": "MS-2", "title": "b", "state": "ready", "depends_on": []}]}
    src, edges, gws = dag(wf, "MS-1")
    assert not any("MS-99" in l for l in src if "-->" in l)
    (fork,) = succ(edges, "ev_start")
    assert succ(edges, fork) == {"s0", "s1"}


def test_the_window_counts_dependency_layers_not_positions():
    # Twelve layers, each of two parallel steps: 1,2 | 3,4 | ... | 23,24.
    deps = {}
    for layer in range(1, 12):
        for k in (1, 2):
            deps[2 * layer + k] = [2 * layer - 1, 2 * layer]
    wf = dag_wf(deps)
    src, edges, _ = dag(wf, "MS-11")  # layer 5 (0-based)
    tasks = [re.match(r"(s\d+)\(", l).group(1) for l in src if re.match(r"s\d+\(", l)]
    # Layers 2..8 are drawn: fourteen steps, s4..s17.
    assert tasks == [f"s{n}" for n in range(4, 18)]
    more = {m.group(1): unescape_label(m.group(2)) for l in src
            if (m := re.fullmatch(r'(more_\w+)\["(.*)"\]', l))}
    assert more == {"more_before": "+4 earlier", "more_after": "+6 later"}
    assert succ(edges, "ev_start") == {"more_before"}
    assert pred(edges, "ev_end") == {"more_after"}


def test_at_the_first_layer_the_chart_stops_at_three_layers_after():
    deps = {}
    for layer in range(1, 12):
        for k in (1, 2):
            deps[2 * layer + k] = [2 * layer - 1, 2 * layer]
    src, edges, _ = dag(dag_wf(deps), "MS-1")  # layer 0
    tasks = [re.match(r"(s\d+)\(", l).group(1) for l in src if re.match(r"s\d+\(", l)]
    # Layers 0..3 are drawn: eight steps, s0..s7; nothing is hidden before.
    assert tasks == [f"s{n}" for n in range(8)]
    more = {m.group(1): unescape_label(m.group(2)) for l in src
            if (m := re.fullmatch(r'(more_\w+)\["(.*)"\]', l))}
    assert more == {"more_after": "+16 later"}


def test_the_issue_page_draws_declared_dependencies(board, client):
    core.create_template(board, "fan", "Fan", ["design", "left", "right", "join"])
    wf = core.instantiate(board, "fan", "MS", actor=CHAOS, actor_kind=HUMAN)
    design, left, right, join = [s["id"] for s in wf["steps"]]
    for step, on in ((left, design), (right, design), (join, left), (join, right)):
        depend(board, step, on)
    src = mermaid_source(client.get(f"/issues/{left}").text)
    assert src.count('{"#43;"}') == 2


# --- project colour (MS-652) -----------------------------------------------------


def hue(colour):
    return int(re.search(r"hsl\((\d+)", colour).group(1))


def lightness(colour):
    return int(re.search(r"hsl\(\d+ \d+% (\d+)%", colour).group(1))


def test_a_project_colour_comes_from_its_bucket_and_key():
    c = web.project_colour("MS", 3)
    assert c == web.project_colour("MS", 3)
    assert set(c) == {"bg", "border"}
    assert hue(c["bg"]) == hue(c["border"])
    # Within +-10 deg of the bucket's base hue, 36 deg apart.
    assert abs(hue(c["bg"]) - 3 * 36) <= 10
    assert 88 <= lightness(c["bg"]) <= 96 and 40 <= lightness(c["border"]) <= 50


def test_the_ten_buckets_have_distinct_hues():
    hues = [hue(web.project_colour("MS", b)["bg"]) for b in range(10)]
    assert len(set(hues)) == 10


def test_two_projects_sharing_a_bucket_differ_in_shade():
    assert web.project_colour("MS", 0) != web.project_colour("BR", 0)


def test_a_project_colour_is_the_same_in_a_fresh_process():
    # Python's hash() is salted per process; the colour must not be.
    import subprocess
    import sys
    out = subprocess.run(
        [sys.executable, "-c", "import json; from board import web; "
         "print(json.dumps(web.project_colour('MS', 4)))"],
        capture_output=True, text=True, check=True,
        env={"PYTHONHASHSEED": "12345", "PATH": ""}).stdout
    assert json.loads(out) == web.project_colour("MS", 4)


def colour(engine, key):
    return web.project_colour(key, core.colour_buckets(engine)[key])


def test_the_board_colours_by_the_stored_bucket_not_the_key_hash(board, client):
    # DR is the second project, so bucket 1; its key hash would say 4.
    core.create_project(board, "DR", "doc-review")
    assert core.colour_buckets(board)["DR"] == 1 != core.key_bucket("DR")
    issue_id = core.create(board, "DR", "x", actor=CHAOS, actor_kind=HUMAN, state="ready")["id"]
    c = card(client.get("/").text, issue_id)
    assert web.project_colour("DR", 1)["bg"] in c
    assert web.project_colour("DR", core.key_bucket("DR"))["bg"] not in c


def test_a_card_is_tinted_with_its_projects_colour(board, client):
    core.create_project(board, "DR", "doc-review")
    ms = ready(board)
    dr = core.create(board, "DR", "other", actor=CHAOS, actor_kind=HUMAN, state="ready")["id"]
    html = client.get("/").text
    for key, issue_id in (("MS", ms), ("DR", dr)):
        c = colour(board, key)
        assert c["bg"] in card(html, issue_id) and c["border"] in card(html, issue_id)


def test_a_leased_card_keeps_its_lease_class_beside_the_project_colour(board, client):
    live = ready(board)
    core.next(board, "run-4")
    c = card(client.get("/").text, live)
    assert "lease-live" in c and colour(board, "MS")["border"] in c


def test_search_results_carry_the_project_colour(board, client):
    issue_id = ready(board, "findme")
    html = client.get("/search", params={"q": "findme"}).text
    hit = re.search(rf'<li class="hit" data-id="{issue_id}"[^>]*>', html).group(0)
    assert colour(board, "MS")["bg"] in hit


def test_a_single_project_workflow_card_takes_that_projects_colour(board, client):
    wf = release(board)
    assert colour(board, "MS")["border"] in wf_card(client.get("/").text, wf["id"])


def test_a_mixed_project_workflow_card_keeps_the_workflow_green(board, client):
    core.create_project(board, "BR", "brain")
    wf = core.create_batch(board, [{"project": "MS", "title": "a"}, {"project": "BR", "title": "b"}],
                           actor=CHAOS, actor_kind=HUMAN, workflow_title="mixed")
    c = wf_card(client.get("/").text, wf["workflow_id"])
    assert "hsl(" not in c and "workflow-card" in c


def test_the_issue_page_and_projects_list_show_the_key_in_its_colour(board, client):
    issue_id = ready(board)
    border = colour(board, "MS")["border"]
    assert border in client.get(f"/issues/{issue_id}").text
    assert border in project_row(client.get("/projects").text, "MS")


# --- MS-661 review fixes ---------------------------------------------------------


@pytest.mark.parametrize("route", ["note", "edit"])
def test_ms661_an_unauthenticated_upload_writes_no_file(board, attach_dir, monkeypatch, route):
    monkeypatch.delenv(web.ACTOR_ENV, raising=False)
    client = local_client(board)
    iid = ready(board)
    issue = core.show(board, iid)
    data = note_form(issue, "x") if route == "note" else edit_form(issue)
    r = client.post(f"/issues/{iid}/{route}", data=data,
                    files=[("files", ("drop.txt", b"payload", "text/plain"))])
    assert r.status_code == 401
    assert not attach_dir.exists() or list(attach_dir.iterdir()) == []


def test_ms661_a_note_on_a_card_moved_meanwhile_is_kept(board, client):
    """The form showed ready and left the select alone; the card was claimed since."""
    iid = ready(board)
    issue = core.show(board, iid)
    core.next(board, "run-2")
    r = client.post(f"/issues/{iid}/note", data=note_form(issue, "just a note"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    after = core.show(board, iid)
    assert after["state"] == "processing"
    assert (after["events"][-1]["kind"], after["events"][-1]["note"]) == ("annotate",
                                                                           "just a note")


def test_ms661_a_state_changes_files_hang_off_the_transition(board, attach_dir, client):
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/note", data=note_form(issue, "parked", state="onhold"),
                    files=[("files", ("why.txt", b"because", "text/plain"))],
                    headers=AS_CHAOS)
    assert r.status_code == 303
    after = core.show(board, iid)
    assert [e["kind"] for e in after["events"]][-1] == "transition"
    assert not any(e["kind"] == "edit" for e in after["events"]), "no changed-attachments edit"
    assert [a["filename"] for a in after["events"][-1]["attachments"]] == ["why.txt"]


# --- MS-662: the issue opens in a right-half panel over the board ----------------

SITE_HEADER = '<header><a href="/">agent-board</a>'


def test_ms662_the_board_has_the_issue_panel_and_its_script(board, client):
    iid = ready(board)
    html = client.get("/").text
    m = re.search(r'<aside id="issue-panel"[^>]*>.*?</aside>', html, re.S)
    assert m, "the board carries one issue panel"
    assert " hidden" in m.group(0).split(">")[0], "closed until a card is clicked"
    assert '<iframe' in m.group(0) and "issue-panel-close" in m.group(0)
    assert "?embed=1" in html and "#issue=" not in html.split("<script")[0]
    assert "openPanel" in html and "Escape" in html
    # No JS, or a ctrl/cmd/middle click: the card's own link still goes full page.
    assert f'href="/issues/{iid}"' in card(html, iid)


def test_ms662_the_panel_is_half_the_viewport_and_full_width_when_narrow(client):
    html = client.get("/").text
    assert re.search(r"#issue-panel \{[^}]*position: fixed;[^}]*width: 50vw", html)
    assert re.search(r"@media \(max-width: 800px\) \{\s*#issue-panel \{ width: 100vw", html)


def test_ms662_an_embedded_issue_page_has_no_site_header_but_keeps_its_forms(board, client):
    iid = ready(board)
    full = client.get(f"/issues/{iid}").text
    embedded = client.get(f"/issues/{iid}?embed=1").text
    assert SITE_HEADER in full
    assert SITE_HEADER not in embedded
    assert f"<h1>{iid} item</h1>" in embedded
    actions = set(issue_forms(embedded))
    for route in ("edit", "note", "link"):
        assert f"/issues/{iid}/{route}?embed=1" in actions, route


def test_ms662_a_post_from_an_embedded_page_redirects_back_embedded(board, client):
    iid = ready(board)
    issue = core.show(board, iid)
    r = client.post(f"/issues/{iid}/note?embed=1", data=note_form(issue, "hi"),
                    headers=AS_CHAOS)
    assert r.status_code == 303
    assert r.headers["location"] == f"/issues/{iid}?embed=1"
    r = client.post(f"/issues/{iid}/edit?embed=1",
                    data=edit_form(core.show(board, iid), title="renamed"), headers=AS_CHAOS)
    assert r.headers["location"] == f"/issues/{iid}?embed=1"


def test_ms662_a_post_from_a_full_page_still_redirects_to_the_full_page(board, client):
    iid = ready(board)
    r = client.post(f"/issues/{iid}/note", data=note_form(core.show(board, iid), "hi"),
                    headers=AS_CHAOS)
    assert r.headers["location"] == f"/issues/{iid}"


def test_ms662_take_over_and_errors_stay_embedded(board, client):
    iid = ready(board)
    core.next(board, "run-2")
    issue = core.show(board, iid)
    refused = client.post(f"/issues/{iid}/edit?embed=1", data=edit_form(issue, body="x"),
                          headers=AS_CHAOS)
    assert refused.status_code == 409
    assert SITE_HEADER not in refused.text
    assert f'action="/issues/{iid}/edit?embed=1"' in refused.text
    stale = client.post(f"/issues/{iid}/edit?embed=1",
                        data=edit_form(issue, version="0", body="x", preempt="1"),
                        headers=AS_CHAOS)
    assert stale.status_code == 409 and SITE_HEADER not in stale.text


def test_ms662_links_inside_an_embedded_page_stay_embedded(board, client):
    """Issue links, including the diagram's, keep embed=1 by a click handler."""
    iid = ready(board)
    scripts = "".join(re.findall(r"<script>(.*?)</script>",
                                 client.get(f"/issues/{iid}?embed=1").text, re.S))
    assert "/issues/" in scripts and "embed=1" in scripts
    assert "embed=1" not in client.get(f"/issues/{iid}").text


# --- MS-662 review fixes ---------------------------------------------------------


def test_ms662_plan_link_and_release_redirect_back_embedded(board, client):
    other = ready(board)
    core.next(board, "run-2")
    iid = ready(board)
    html = client.get(f"/issues/{iid}?embed=1").text
    assert f"/issues/{iid}/plan?embed=1" in issue_forms(html)
    r = client.post(f"/issues/{iid}/link?embed=1", data={"kind": "url", "ref": "https://x"},
                    headers=AS_CHAOS)
    assert r.headers["location"] == f"/issues/{iid}?embed=1"
    r = client.post(f"/issues/{iid}/plan?embed=1", data={"steps": "a\nb"}, headers=AS_CHAOS)
    assert r.headers["location"] == f"/issues/{iid}?embed=1"
    take = core.show(board, other)
    client.post(f"/issues/{other}/edit", data=edit_form(take, preempt="1"), headers=AS_CHAOS)
    held = client.get(f"/issues/{other}?embed=1", headers=AS_CHAOS).text
    assert f"/issues/{other}/release?embed=1" in issue_forms(held)
    r = client.post(f"/issues/{other}/release?embed=1", headers=AS_CHAOS)
    assert r.headers["location"] == f"/issues/{other}?embed=1"


def test_ms662_an_embedded_errors_board_link_leaves_the_panel(board, client):
    r = client.post("/issues/MS-999/note?embed=1", data={"note": "x"}, headers=AS_CHAOS)
    assert r.status_code == 404
    assert '<a href="/" target="_top">Back to the board</a>' in r.text
    plain = client.post("/issues/MS-999/note", data={"note": "x"}, headers=AS_CHAOS)
    assert '<a href="/">Back to the board</a>' in plain.text


def test_ms662_pages_may_be_framed_only_by_the_board_itself(board, client):
    iid = ready(board)
    for path in ("/", f"/issues/{iid}", f"/issues/{iid}?embed=1"):
        assert client.get(path).headers["content-security-policy"] == "frame-ancestors 'self'"


# --- MS-662 round-2 review fixes: the panel's URL helpers, run in node ----------


def js_function(html, *names):
    """The source of each `function name(...) {...}` in a page's scripts."""
    return "\n".join(_js_function(html, name) for name in names)


def _js_function(html, name):
    start = html.index(f"function {name}(")
    depth, i = 0, html.index("{", start)
    while True:
        depth += {"{": 1, "}": -1}.get(html[i], 0)
        i += 1
        if depth == 0:
            return html[start:i]


def run_js(source, expr):
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run([node, "-e", f"{source}\nconsole.log(JSON.stringify({expr}));"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_ms662_only_an_issue_page_in_the_frame_names_the_panels_issue(client):
    """A take-over or error page at /issues/X/edit is not issue `X/edit`."""
    src = js_function(client.get("/").text, "segment", "panelId")
    cases = ["/issues/MS-1", "/issues/MS-1?embed=1", "/issues/MS-1/edit?embed=1",
             "/issues/MS-1/note", "/", "/search?q=x", "/issues/"]
    assert run_js(src, "[" + ",".join(f"panelId({json.dumps(c)})" for c in cases) + "]") == [
        "MS-1", "MS-1", None, None, None, None, None]


def test_ms662_the_hash_never_names_a_dot_segment(client):
    src = js_function(client.get("/").text, "segment", "hashId")
    cases = ["#issue=MS-1", "#issue=MS%2D1", "#issue=.", "#issue=..", "#issue=%2E%2E",
             "#issue=MS-1%2Fedit", "#issue=%E0", "", "#other"]
    assert run_js(src, "[" + ",".join(f"hashId({json.dumps(c)})" for c in cases) + "]") == [
        "MS-1", "MS-1", None, None, None, None, None, None, None]


def test_ms662_an_embedded_link_gets_embed_once_and_before_its_fragment(board, client):
    iid = ready(board)
    src = js_function(client.get(f"/issues/{iid}?embed=1").text, "embedHref")
    cases = {"/issues/MS-1": "/issues/MS-1?embed=1",
             "/issues/MS-1?embed=1": "/issues/MS-1?embed=1",
             "/issues/MS-1?a=1": "/issues/MS-1?a=1&embed=1",
             "/issues/MS-1?a=1&embed=1": "/issues/MS-1?a=1&embed=1",
             "/issues/MS-1#n3": "/issues/MS-1?embed=1#n3",
             "/issues/MS-1?a=1#n3": "/issues/MS-1?a=1&embed=1#n3",
             "/issues/MS-1?embed=1#n3": "/issues/MS-1?embed=1#n3"}
    got = run_js(src, "[" + ",".join(f"embedHref({json.dumps(c)})" for c in cases) + "]")
    assert got == list(cases.values())


def test_ms662_any_frame_load_after_the_first_marks_the_board_stale(client):
    """A save posted before the frame's load listener ran still redraws on close."""
    html = client.get("/").text
    assert "addEventListener(\"submit\"" not in html
    assert "loads > 1" in html or "++loads > 1" in html


def history(html):
    return html.split("<h2>History</h2>", 1)[1]


def test_ab2_history_lists_the_newest_event_first(board, client):
    iid = ready(board)
    core.annotate(board, iid, "first note", actor=CHAOS, actor_kind=HUMAN)
    core.annotate(board, iid, "second note", actor=CHAOS, actor_kind=HUMAN)
    events = history(client.get(f"/issues/{iid}").text)
    assert events.index("second note") < events.index("first note") < events.index("create")


@pytest.mark.parametrize("text", [
    "before <!-- zqxsecret remark --> after",
    "before\n\n<!-- zqxsecret\nremark -->\n\nafter",
])
def test_ab3_an_html_comment_in_markdown_is_not_shown(board, client, text):
    iid = ready(board, body=text)
    core.annotate(board, iid, text, actor=CHAOS, actor_kind=HUMAN)
    page = client.get(f"/issues/{iid}").text
    assert "&lt;!-- zqxsecret" in page, "the edit box keeps the source"
    html = re.sub(r"<textarea[^>]*>.*?</textarea>", "", page, flags=re.S)
    assert "zqxsecret" not in html and "remark" not in html
    assert "&lt;!--" not in html
    assert html.count("before") == 2 and html.count("after") == 2


def test_ab3_a_comment_inside_code_stays_visible(board, client):
    iid = ready(board, body="`<!-- kept -->`\n\n    <!-- also kept -->")
    html = client.get(f"/issues/{iid}").text
    assert "<code>&lt;!-- kept --&gt;</code>" in html
    assert "&lt;!-- also kept --&gt;" in html


def test_ab3_other_raw_html_is_still_escaped_not_rendered(board, client):
    iid = ready(board, body="<div onclick=\"x()\">hi</div>\n\ntext <b>bold</b>")
    html = client.get(f"/issues/{iid}").text
    assert '<div onclick' not in html and "<b>bold</b>" not in html
    assert "&lt;b&gt;bold&lt;/b&gt;" in html


def test_ab5_an_open_panel_narrows_the_board_instead_of_covering_it(client):
    html = client.get("/").text
    assert re.search(r"body\.issue-panel-open main \{[^}]*margin-right: 50vw", html)
    # Narrow, the panel is full width anyway, so the board keeps its width beneath.
    assert re.search(r"@media \(max-width: 800px\) \{(?:[^{}]*\{[^}]*\})*?[^{}]*"
                     r"body\.issue-panel-open main \{ margin-right: 0", html)
    assert 'classList.toggle("issue-panel-open"' in html
