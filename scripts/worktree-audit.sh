#!/usr/bin/env bash
# Аудит неубранных временных worktree и локальных веток t3/* в целевом репозитории:
# находит предков origin/dev или origin/main, detached HEAD, патчи, эквивалентные
# целевой ветке (git cherry), и влитые squash-мержем PR (gh pr list).
#
# Использование: scripts/worktree-audit.sh [путь_к_репозиторию]
set -euo pipefail

repo="${1:-.}"
cd "$repo"

merged_prs=""
if command -v gh >/dev/null 2>&1; then
	merged_prs="$(gh pr list --state merged --limit 300 --json number,headRefName,headRefOid \
		--jq '.[] | "\(.headRefName) \(.headRefOid) #\(.number)"' 2>/dev/null || true)"
fi

base_branch="origin/main"
if git show-ref --verify --quiet refs/remotes/origin/dev; then
	base_branch="origin/dev"
fi

check_ref() {
	local sha="$1" br="$2" ch hit
	if git show-ref --verify --quiet refs/remotes/origin/dev && git merge-base --is-ancestor "$sha" origin/dev 2>/dev/null; then
		echo "ancestor of origin/dev"
		return 0
	fi
	if git show-ref --verify --quiet refs/remotes/origin/main && git merge-base --is-ancestor "$sha" origin/main 2>/dev/null; then
		echo "ancestor of origin/main"
		return 0
	fi
	ch="$(git cherry "$base_branch" "$sha" 2>/dev/null || true)"
	if [ -n "$ch" ] && ! printf '%s\n' "$ch" | grep -q '^+'; then
		echo "patch-merged into $base_branch"
		return 0
	fi
	if [ -n "$merged_prs" ] && [ -n "$br" ] && [ "$br" != "DETACHED" ]; then
		hit="$(printf '%s\n' "$merged_prs" | awk -v b="$br" -v s="$sha" '$1==b && $2==s {print $3; exit}')"
		if [ -n "$hit" ]; then
			echo "squash-merged PR $hit"
			return 0
		fi
	fi
	return 1
}

audit_header="origin/main"
if [ "$base_branch" != "origin/main" ]; then
	audit_header="$base_branch or origin/main"
fi
echo "=== Worktrees merged into $audit_header ==="
git worktree list --porcelain | awk \
	'/^worktree /{if (wt!="") print n, wt, sha, br; n++; wt=$2; sha=""; br="DETACHED"}
	 /^HEAD /{sha=$2}
	 /^branch /{br=$2; sub(/^refs\/heads\//,"",br)}
	 END{if (wt!="") print n, wt, sha, br}' |
	while read -r idx wt sha br; do
		[ "$idx" = "1" ] && continue
		if reason="$(check_ref "$sha" "$br")"; then
			echo "  $wt ($br @ ${sha:0:8} — $reason)"
		fi
	done

echo "=== Merged t3/* branches without worktree ==="
git for-each-ref --format='%(refname:short) %(objectname) %(worktreepath)' refs/heads/t3/ |
	while read -r br sha wt; do
		if [ -z "$wt" ] && reason="$(check_ref "$sha" "$br")"; then
			echo "  $br ($reason)"
		fi
	done
