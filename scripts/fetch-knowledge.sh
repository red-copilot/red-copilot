#!/usr/bin/env bash
# Fetch the bulk offensive-knowledge corpora into .harness/knowledge-cache/.
#
# The runtime never downloads knowledge: this script is an explicit pre-run
# step, revisions are pinned, and the result is gitignored. Mount the cache
# next to the in-repo catalogue, for example:
#
#   ./scripts/fetch-knowledge.sh
#   harness tsec --agent agents/examples/zc-offensive.yaml \
#     --knowledge-root .harness/knowledge-cache
#
# Corpora are point-in-time snapshots: bump the revision deliberately and note
# why, rather than tracking a moving branch.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="${KNOWLEDGE_CACHE_DIR:-$ROOT/.harness/knowledge-cache}"

# name|repository|pinned revision|licence note
CORPORA=(
  "PayloadsAllTheThings|https://github.com/swisskyrepo/PayloadsAllTheThings|3ac27901c711bdf3f5b65a7b1d1820a1f65bd09a|MIT"
  "HowToHunt|https://github.com/KathanP19/HowToHunt|4c8c23fce6c754e9495101d3894cc7228ee7c709|see upstream"
  "nuclei-templates|https://github.com/projectdiscovery/nuclei-templates|3de629d823bb6eb00aa3769edc365b6525134fb1|MIT"
)

mkdir -p "$CACHE"

for entry in "${CORPORA[@]}"; do
  IFS='|' read -r name repo revision licence <<<"$entry"
  target="$CACHE/$name"
  if [ -d "$target/.git" ]; then
    current="$(git -C "$target" rev-parse HEAD)"
    if [ "$current" = "$revision" ]; then
      echo "[skip] $name already at $revision"
      continue
    fi
    echo "[update] $name $current -> $revision"
    git -C "$target" fetch --depth 1 origin "$revision"
    git -C "$target" checkout --detach "$revision"
    continue
  fi
  echo "[fetch] $name @ $revision (licence: $licence)"
  git clone --depth 1 "$repo" "$target"
  git -C "$target" fetch --depth 1 origin "$revision"
  git -C "$target" checkout --detach "$revision"
done

echo "knowledge cache ready at $CACHE"
echo "licences: review the upstream LICENSE files before redistributing anything."
