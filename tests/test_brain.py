"""Read-only access to a brain checkout's KB and docs (AB-22).

Each test builds a small brain in tmp_path: a kb/wiki of pages and two doc
directories, plus a secret file outside them that nothing may reach.
"""

import os

import pytest

from board import brain
from board.brain import Brain, BrainError


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "brain"
    write(r / "kb/wiki/index.md", "# Index\n\nSee [[concept-leases]].\n")
    write(r / "kb/wiki/concept-leases.md",
          '---\ntitle: "Leases and Tokens"\ntype: concept\n---\n\n# Ignored heading\n\n'
          "A lease expires.\nThe lease token is secret.\nA third lease line.\n"
          "A fourth lease line.\n")
    write(r / "kb/wiki/notes.txt", "lease lease lease, but not markdown\n")
    write(r / "plans/fatigue.md", "# Fatigue plan\n\nlease once\n")
    write(r / "product-specs/tempo.md", "No heading here, one lease token.\n")
    write(r / "CLAUDE.md", "# Secret\n\nlease token outside every named directory\n")
    write(tmp_path / "outside.md", "# Outside\n\nlease token beyond the brain\n")
    return r


def make(root, **kw):
    kw.setdefault("doc_dirs", ["plans", "product-specs"])
    return Brain(root, **kw)


def paths(entries):
    return [e["path"] for e in entries]


# --- configuration ---

def test_unset_brain_dir_turns_the_feature_off():
    assert Brain.from_env({}) is None
    assert Brain.from_env({brain.BRAIN_DIR_ENV: "  "}) is None


def test_from_env_reads_the_kb_dir_default_and_the_doc_dir_list(root):
    b = Brain.from_env({brain.BRAIN_DIR_ENV: str(root),
                        brain.DOC_DIRS_ENV: "plans::product-specs"})
    assert paths(b.list("kb")) == ["kb/wiki/concept-leases.md", "kb/wiki/index.md"]
    assert paths(b.list("docs")) == ["plans/fatigue.md", "product-specs/tempo.md"]


def test_from_env_honours_another_kb_dir(root):
    b = Brain.from_env({brain.BRAIN_DIR_ENV: str(root), brain.KB_DIR_ENV: "plans"})
    assert paths(b.list("kb")) == ["plans/fatigue.md"]
    assert b.list("docs") == []


def test_a_missing_named_directory_is_an_error_not_an_empty_result(root):
    with pytest.raises(BrainError, match="kb/nope"):
        make(root, kb_dir="kb/nope").list("kb")


def test_an_unknown_scope_is_refused(root):
    with pytest.raises(BrainError, match="scope"):
        make(root).list("everything")


# --- list ---

def test_list_gives_markdown_files_only_with_their_titles(root):
    out = make(root).list("all")
    assert out == [
        {"path": "kb/wiki/concept-leases.md", "title": "Leases and Tokens"},
        {"path": "kb/wiki/index.md", "title": "Index"},
        {"path": "plans/fatigue.md", "title": "Fatigue plan"},
        {"path": "product-specs/tempo.md", "title": "tempo"},
    ]


def test_overlapping_directories_list_a_file_once(root):
    b = make(root, kb_dir="kb/wiki", doc_dirs=["kb"])
    assert paths(b.list("all")) == ["kb/wiki/concept-leases.md", "kb/wiki/index.md"]


def test_list_skips_a_symlink_that_leads_outside(root, tmp_path):
    os.symlink(tmp_path / "outside.md", root / "kb/wiki/escape.md")
    os.symlink(tmp_path, root / "kb/wiki/escape-dir")
    assert "kb/wiki/escape.md" not in paths(make(root).list("kb"))
    assert not any("escape-dir" in p for p in paths(make(root).list("kb")))


# --- read ---

def test_read_returns_the_page_text_and_title(root):
    out = make(root).read("kb/wiki/index.md")
    assert out == {"path": "kb/wiki/index.md", "title": "Index",
                   "text": "# Index\n\nSee [[concept-leases]].\n"}


def test_read_is_limited_to_the_scope_named(root):
    with pytest.raises(BrainError, match="outside"):
        make(root).read("plans/fatigue.md", scope="kb")
    assert make(root).read("plans/fatigue.md", scope="docs")["title"] == "Fatigue plan"


@pytest.mark.parametrize("path", [
    "CLAUDE.md",
    "kb/wiki/../../CLAUDE.md",
    "../outside.md",
    "kb/../../outside.md",
])
def test_read_refuses_a_path_outside_the_named_directories(root, path):
    with pytest.raises(BrainError, match="outside"):
        make(root).read(path)


def test_a_sibling_sharing_the_directory_name_prefix_is_outside(root):
    write(root / "kb/wiki2/secret.md", "# Secret\n")
    with pytest.raises(BrainError, match="outside"):
        make(root).read("kb/wiki2/secret.md", scope="kb")


def test_a_symlinked_brain_root_still_gives_paths_inside_it(root, tmp_path):
    os.symlink(root, tmp_path / "alias")
    assert paths(make(tmp_path / "alias").list("kb")) == [
        "kb/wiki/concept-leases.md", "kb/wiki/index.md"]


def test_read_refuses_an_absolute_path_outside(root, tmp_path):
    with pytest.raises(BrainError, match="outside"):
        make(root).read(str(tmp_path / "outside.md"))


def test_read_refuses_a_symlinked_file_that_leads_outside(root, tmp_path):
    os.symlink(tmp_path / "outside.md", root / "kb/wiki/escape.md")
    with pytest.raises(BrainError, match="outside"):
        make(root).read("kb/wiki/escape.md")


def test_read_refuses_a_path_through_a_symlinked_directory_that_leads_outside(root, tmp_path):
    os.symlink(tmp_path, root / "kb/wiki/escape-dir")
    with pytest.raises(BrainError, match="outside"):
        make(root).read("kb/wiki/escape-dir/outside.md")


def test_read_follows_a_symlink_that_stays_inside(root):
    os.symlink(root / "kb/wiki/index.md", root / "kb/wiki/alias.md")
    assert make(root).read("kb/wiki/alias.md")["title"] == "Index"


def test_read_refuses_a_file_that_is_not_markdown(root):
    with pytest.raises(BrainError, match=r"\.md"):
        make(root).read("kb/wiki/notes.txt")


def test_read_refuses_a_markdown_symlink_onto_a_non_markdown_file(root):
    os.symlink(root / "kb/wiki/notes.txt", root / "kb/wiki/notes.md")
    with pytest.raises(BrainError, match=r"\.md"):
        make(root).read("kb/wiki/notes.md")


def test_read_of_a_missing_page_is_an_error(root):
    with pytest.raises(BrainError, match="no such"):
        make(root).read("kb/wiki/absent.md")


# --- search ---

def test_search_needs_every_word_case_insensitively(root):
    out = make(root).search("LEASE Token")
    assert paths(out) == ["kb/wiki/concept-leases.md", "product-specs/tempo.md"]


def test_title_or_filename_hits_rank_before_hit_count(root):
    write(root / "plans/many.md", "# Many\n\n" + "fatigue\n" * 30)
    out = make(root).search("fatigue")
    assert paths(out) == ["plans/fatigue.md", "plans/many.md"]


def test_ties_on_title_rank_by_total_hit_count_then_path(root):
    write(root / "plans/more.md", "# More\n\nlease lease lease\n")
    out = make(root).search("lease", scope="docs")
    assert paths(out) == ["plans/more.md", "plans/fatigue.md", "product-specs/tempo.md"]


def test_a_result_carries_title_and_up_to_three_matching_lines(root):
    hit = make(root).search("lease", scope="kb")[0]
    assert hit["path"] == "kb/wiki/concept-leases.md"
    assert hit["title"] == "Leases and Tokens"
    assert hit["lines"] == [
        {"line": 2, "text": 'title: "Leases and Tokens"'},
        {"line": 8, "text": "A lease expires."},
        {"line": 9, "text": "The lease token is secret."},
    ]


def test_search_returns_at_most_twenty(root):
    for i in range(25):
        write(root / f"kb/wiki/page-{i:02}.md", f"# Page {i}\n\nneedle\n")
    assert len(make(root).search("needle")) == brain.MAX_RESULTS == 20


def test_search_never_reaches_outside_files(root, tmp_path):
    os.symlink(tmp_path / "outside.md", root / "kb/wiki/escape.md")
    assert "beyond" not in str(make(root).search("beyond"))
    assert make(root).search("outside every named") == []


def test_an_empty_query_is_refused(root):
    with pytest.raises(BrainError, match="query"):
        make(root).search("   ")
