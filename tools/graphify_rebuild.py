#!/usr/bin/env python3
"""Rebuild the findajob knowledge graphs from changed files.

Called by .git/hooks/post-commit. Safe to run by hand:

    tools/graphify_rebuild.py                # rebuild both graphs
    tools/graphify_rebuild.py web/app.html   # rebuild only what these files touch

Why this exists instead of `graphify update <dir>`
--------------------------------------------------
`graphify update` is unsafe in this repo, verified by running it on 2026-08-02:

1. The CLI has no exclude support, so it walks `web/vendor/` and turns the
   minified React bundles into 271 nodes named `vendor_react_dom_production_min_aa`
   and similar. Node count went 162 -> 433 and the graph became unreadable.
2. It re-derives the corpus and drops the hand-written semantic nodes for
   documents. `app_api_gate`, `app_dashboard_shell` and `404_error_page` were all
   lost. Those describe the .html screens, which is most of what this repo IS.

So this script keeps the two halves separate and explicit:

  AST layer       regenerated from source every run, deterministic, no LLM.
  semantic layer  read verbatim from <dir>/graphify-out/semantic.json, never
                  regenerated here. It is hand-written and describes the .html
                  documents, the design tokens and the design rationale.

A code change is therefore fully automatic. A change to a .html/.md document
CANNOT be: nothing here can read prose. Those get recorded in
`<dir>/graphify-out/.needs_update` so the next session knows a semantic pass is
owed, which is exactly the failure that let auth.html land invisibly on
2026-08-01.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Each graphed directory and what to keep out of it. vendor/ is the self-hosted
# React UMD build and fonts/ is binary; design_handoff_findjob_pro/ is the stale
# snapshot CLAUDE.md forbids reading.
TARGETS = {
    "web": ["vendor", "fonts"],
    "prototype": ["design_handoff_findjob_pro"],
}

# Community names are ours, not the clusterer's. Keyed by directory; the graph
# keeps whatever ids Louvain hands out, so these are re-applied by position only
# when the count still matches — otherwise the run falls back to placeholders and
# says so rather than mislabelling.
LABELS_FILE = "graphify-out/.graphify_labels.json"


def log(msg: str) -> None:
    print(f"[graphify] {msg}", flush=True)


def targets_for(changed: list[str]) -> list[str]:
    """Which graphed directories does this changeset touch?"""
    if not changed:
        return list(TARGETS)
    hit = []
    for name, excludes in TARGETS.items():
        for f in changed:
            parts = Path(f).parts
            if not parts or parts[0] != name:
                continue
            # A change inside an excluded subtree is not a change to the graph.
            if len(parts) > 1 and parts[1] in excludes:
                continue
            if "graphify-out" in parts:
                continue
            hit.append(name)
            break
    return hit


def resolve_labels(name: str, out: Path, comms: dict) -> dict:
    """Map community ids to our names by anchor membership, not by number.

    Louvain hands out community ids in an order that is not stable across runs —
    the same cluster can be C3 today and C7 tomorrow. Keying names on the number
    therefore silently permutes every label in the report, and a wrong-but-
    plausible label is worse than no label. So community-labels.json stores, per
    name, a handful of member node ids; each community takes the name whose
    anchors it actually contains.
    """
    anchors_path = out / "community-labels.json"
    fallback = {c: f"Community {c}" for c in comms}
    if not anchors_path.exists():
        return fallback

    anchors = json.loads(anchors_path.read_text(encoding="utf-8"))
    labels, claimed = {}, set()
    for cid, members in comms.items():
        member_set = set(members)
        best, best_hits = None, 0
        for label, ids in anchors.items():
            if label in claimed:
                continue
            hits = len(member_set & set(ids))
            if hits > best_hits:
                best, best_hits = label, hits
        if best is not None:
            labels[cid] = best
            claimed.add(best)
        else:
            labels[cid] = f"Community {cid}"

    unnamed = sum(1 for v in labels.values() if v.startswith("Community "))
    if unnamed:
        log(f"{name}: {unnamed} community/communities unnamed — re-label with /graphify")
    return labels


def rebuild(name: str, changed: list[str]) -> bool:
    from graphify.analyze import god_nodes, suggest_questions, surprising_connections
    from graphify.build import build_from_json
    from graphify.cluster import cluster, score_all
    from graphify.detect import detect, save_manifest
    from graphify.export import to_json
    from graphify.extract import extract
    from graphify.report import generate

    root = REPO / name
    out = root / "graphify-out"
    semantic_path = out / "semantic.json"
    if not semantic_path.exists():
        log(f"{name}: no semantic.json — refusing to rebuild without it")
        return False

    os.chdir(root)

    det = detect(Path("."), extra_excludes=TARGETS[name])
    ast = extract([Path(f) for f in det["files"]["code"]], cache_root=Path("."))
    sem = json.loads(semantic_path.read_text(encoding="utf-8"))

    seen = {n["id"] for n in ast["nodes"]}
    nodes = list(ast["nodes"])
    for n in sem["nodes"]:
        if n["id"] not in seen:
            nodes.append(n)
            seen.add(n["id"])

    edges = ast["edges"] + sem["edges"]
    # A semantic edge can point at a code symbol that has since been renamed or
    # deleted. Dropping it loudly beats building a graph with dangling endpoints
    # that every downstream query then silently walks off.
    live = [e for e in edges if e["source"] in seen and e["target"] in seen]
    if len(live) != len(edges):
        stale = [
            (e["source"], e["target"])
            for e in edges
            if e["source"] not in seen or e["target"] not in seen
        ]
        log(f"{name}: dropped {len(stale)} edge(s) with missing endpoints: {stale[:5]}")

    extraction = {
        "nodes": nodes,
        "edges": live,
        "hyperedges": sem.get("hyperedges", []),
        "input_tokens": 0,
        "output_tokens": 0,
    }

    G = build_from_json(extraction, root=".", directed=False)
    if G.number_of_nodes() == 0:
        log(f"{name}: extraction produced no nodes — aborting, graph left untouched")
        return False

    comms = cluster(G)
    coh = score_all(G, comms)
    if not to_json(G, comms, "graphify-out/graph.json"):
        log(f"{name}: refused to shrink graph.json — run with GRAPHIFY_FORCE=1 if intended")
        return False

    labels = resolve_labels(name, out, comms)

    questions = suggest_questions(G, comms, labels)
    report = generate(
        G, comms, coh, labels, god_nodes(G), surprising_connections(G, comms),
        det, {"input": 0, "output": 0}, ".", suggested_questions=questions,
    )
    (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")

    from graphify.cli import _stamped_manifest_files

    save_manifest(
        _stamped_manifest_files(det["files"], extraction, Path(".")),
        root=".",
        scan_corpus={f for fl in det["files"].values() for f in fl},
    )

    log(f"{name}: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, {len(comms)} communities")

    # Documents carry the meaning here and nothing in this script can read prose.
    # Record them so the debt is visible instead of silently accruing.
    doc_exts = {".html", ".md", ".txt", ".svg"}
    docs = [
        f for f in changed
        if Path(f).parts and Path(f).parts[0] == name and Path(f).suffix.lower() in doc_exts
    ]
    marker = out / ".needs_update"
    if docs:
        marker.write_text(
            "Semantic layer is stale. These documents changed but semantic.json was not\n"
            "regenerated — run /graphify in Claude Code to refresh it.\n\n"
            + "\n".join(sorted(set(docs)))
            + "\n",
            encoding="utf-8",
        )
        log(f"{name}: {len(docs)} document(s) changed — semantic pass owed, see .needs_update")
    return True


def main() -> int:
    changed = [a for a in sys.argv[1:] if a.strip()]
    if not changed:
        env = os.environ.get("GRAPHIFY_CHANGED", "")
        changed = [line.strip() for line in env.splitlines() if line.strip()]

    names = targets_for(changed)
    if not names:
        return 0

    rebuilt = [n for n in names if rebuild(n, changed)]
    if not rebuilt:
        return 1

    # The root graph is a side-by-side merge of the two, not a build of its own.
    # merge-graphs namespaces ids (web::x / prototype::x) so the halves stay
    # disjoint; it exists so one query can reach both. Shelled out rather than
    # imported: there is no graphify.merge module, the logic lives in cli.
    import subprocess  # noqa: PLC0415

    os.chdir(REPO)
    (REPO / "graphify-out").mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "graphify", "merge-graphs",
         *(str(REPO / n / "graphify-out" / "graph.json") for n in TARGETS),
         "--out", str(REPO / "graphify-out" / "graph.json")],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        log(f"merge failed, root graph left as-is: {proc.stderr.strip()[:200]}")
        return 1
    log("merged root graph updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
