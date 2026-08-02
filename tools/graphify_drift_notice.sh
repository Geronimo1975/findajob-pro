#!/bin/sh
# SessionStart hook payload: surface graphify semantic drift at the top of a session.
#
# The post-commit hook writes <dir>/graphify-out/.needs_update when a source
# document no longer hashes to what semantic.json was written against. That file
# is worthless if nobody reads it, and the whole failure mode being guarded here
# is "nobody noticed" — auth.html landed in web/ on 2026-08-01 and the graph went
# on answering questions as though it had not.
#
# So the notice is pushed, not pulled: into the model's context via
# additionalContext, and onto the user's screen via systemMessage.
#
# Silent when clean. Never fails a session start.

set -u

DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$DIR" ] || exit 0

BODY=""
for target in web prototype; do
    marker="$DIR/$target/graphify-out/.needs_update"
    [ -f "$marker" ] || continue
    BODY="$BODY
--- $target ---
$(cat "$marker" 2>/dev/null)"
done

[ -n "$BODY" ] || exit 0

# jq builds the JSON so a stray quote or newline in a filename cannot produce a
# malformed payload that the harness silently discards.
printf '%s' "$BODY" | jq -Rs '{
  systemMessage: "graphify: the knowledge graph has stale semantic nodes — see .needs_update",
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: ("The graphify knowledge graph for this repo has drifted. Some semantic nodes in graph.json are stamped `stale: true` and their claims describe versions of documents that no longer exist. Treat them as unverified until refreshed.\n" + .)
  }
}' 2>/dev/null || exit 0
