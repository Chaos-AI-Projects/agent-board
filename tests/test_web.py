"""The web UI against design section 10 row 7, plus the take-over round trip.

Row 7 is an HTML assertion: a card whose lease has expired renders
differently from one whose lease is live. The round trip is section 6 in a
browser: an edit under a live lease is refused and offers a take-over, the
take-over leaves the card held by the human with a Release button, and
Release hands it back to the queue.
"""

import json
import re
from datetime import timedelta
from html import unescape

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
            "labels": ", ".join(issue["labels"]), "state": issue["state"]} | fields


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
    nodes = [l.strip() for l in src if re.match(r"\s*s\d+\[", l)]
    assert len(nodes) == len(ids)
    # One node per step in position order, labelled id, title and state.
    for n, (node, st) in enumerate(zip(nodes, wf["steps"])):
        assert node.startswith(f's{n}["')
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


HOSTILE = 'a "quoted" [bracket] --> x;\nclick s0 "javascript:alert(1)" %%{init}%% <b>'


def test_a_hostile_step_title_cannot_break_or_inject_into_the_diagram(board, client):
    # Step titles come from the template, which an agent may write.
    wf = release(board, [HOSTILE, "ship"])
    build = wf["steps"][0]["id"]
    src = mermaid_source(client.get(f"/issues/{build}").text)
    lines = [l.strip() for l in src.splitlines()]
    # Still exactly two nodes, one edge, two clicks: nothing the title said became syntax.
    assert len([l for l in lines if re.match(r"s\d+\[", l)]) == 2
    assert [l for l in lines if l.startswith("click ")] == [
        f'click s0 "/issues/{build}"', f'click s1 "/issues/{wf["steps"][1]["id"]}"']
    assert "javascript" not in "".join(l for l in lines if l.startswith("click "))
    node = next(l for l in lines if l.startswith("s0["))
    label = re.fullmatch(r's0\["(.*)"\]', node).group(1)
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
    r = client.post(f"/issues/{iid}/edit", data=edit_form(issue, state="done"),
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
    for field in ("title", "body", "rank", "labels", "state"):
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


def test_ms637_the_state_change_note_shows_only_when_the_state_changes(board, client):
    """Visible in the HTML for no-JS users; JS hides it until the state select moves."""
    iid = ready(board)
    html = client.get(f"/issues/{iid}").text
    _, inside, _ = edit_control(html)
    note = re.search(r"<label([^>]*)>[^<]*<textarea name=\"note\"", inside)
    assert note and "data-state-note" in note.group(1)
    assert "hidden" not in note.group(1), "the server does not hide it; JS does"
    assert re.search(r'<select name="state"[^>]*data-current="ready"', inside)
    script = re.search(r"<script>(.*?)</script>", html, re.S)
    assert script and "data-state-note" in script.group(1) and "data-current" in script.group(1)


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
    assert '<textarea name="note"' in forms[f"/issues/{iid}/edit"]


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
    return re.findall(r'<li class="hit"><a href="/issues/([^"]+)">', html)


def test_the_board_has_no_search_and_ignores_filter_params(client, board):
    iid = ready(board, "Lease expiry")
    html = client.get("/", params={"q": "zzz", "project": "XX"}).text
    assert f'data-id="{iid}"' in html
    assert not re.search(r'<input[^>]*name="q"', html)
    assert not re.search(r'<select name="label"', html)


def test_every_page_header_links_to_search_and_preferences(client, board):
    iid = ready(board)
    for path in ("/", f"/issues/{iid}", "/search", "/preferences"):
        header = re.search(r"<header>.*?</header>", client.get(path).text, re.S).group(0)
        assert 'href="/search"' in header and 'href="/preferences"' in header


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
        "projects": ["BR"], "lanes": ["ready", "done"]}


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
        "projects": ["BR"], "lanes": ["ready", "done"]}


def test_preferences_are_a_same_origin_post_only(client, board):
    r = client.post("/preferences", data={"lane": ["ready"]},
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403


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
