#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

OWNER_EXEMPT = {"fus1ond"}
LOCAL_REPOS = {
    "rumap/rumap": "/root/rumap",
    "rumap/rumap-core": "/root/rumap-core",
    "rumap/rumap-android": "/root/rumap-android",
}
LOCK_DIR = Path("/tmp/pr-review-gate-locks")
LOCK_TTL_SECONDS = 900

MARKER_RE = re.compile(
    r"<!--\s*review-sha:([0-9a-f]+)\s+status:(approved|changes_requested|missing_issue)\s*-->"
)
ISSUE_KEYWORD_RE = re.compile(r"(?:Closes|Fixes|Resolves|Part of|Ref)\s+#(\d+)", re.IGNORECASE)
ISSUE_HASH_RE = re.compile(r"(?<!\w)#(\d+)\b")
GH_URL_REF_RE = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/(\d+)")
BRANCH_ISSUE_RE = re.compile(r"(?:^|[/\-_])(\d{2,5})(?:[/\-_]|$)")
FORCE_REVIEW_RE = re.compile(r"^\s*/review\b", re.IGNORECASE)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def gh_json(args: list[str]) -> dict | list:
    res = run(["gh", *args])
    return json.loads(res.stdout)


def gh_api_optional(endpoint: str) -> dict | list | None:
    res = run(["gh", "api", endpoint], check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def summarize_checks(rollup: list[dict]) -> dict:
    pending, failed, passed, skipped = [], [], [], []
    for item in rollup or []:
        name = item.get("name") or item.get("context") or "unknown"
        status = (item.get("status") or "").upper()
        conclusion = (item.get("conclusion") or item.get("state") or "").upper()
        if status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"} or (
            not conclusion and status != "COMPLETED"
        ):
            pending.append(name)
        elif conclusion in {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"}:
            failed.append(name)
        elif conclusion in {"SKIPPED", "NEUTRAL", "CANCELLED"}:
            skipped.append(name)
        else:
            passed.append(name)
    if failed:
        state = "failed"
    elif pending:
        state = "pending"
    elif passed:
        state = "passed"
    else:
        state = "none"
    return {
        "state": state,
        "passed": len(passed),
        "pending": pending,
        "failed": failed,
        "skipped": len(skipped),
    }


def last_review_marker(pr: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (sha, status, timestamp, body) of the latest gate review marker."""
    entries: list[tuple[str, str]] = []
    for r in pr.get("reviews") or []:
        ts = r.get("submittedAt") or r.get("createdAt") or ""
        entries.append((ts, r.get("body") or ""))
    for c in pr.get("comments") or []:
        ts = c.get("createdAt") or ""
        entries.append((ts, c.get("body") or ""))
    entries.sort(key=lambda x: x[0])

    last_sha, last_status, last_ts, last_body = None, None, None, None
    for ts, text in entries:
        matches = list(MARKER_RE.finditer(text))
        if matches:
            m = matches[-1]
            last_sha, last_status, last_ts, last_body = m.group(1), m.group(2), ts, text
    return last_sha, last_status, last_ts, last_body


def has_force_review_after(pr: dict, since_ts: str | None) -> bool:
    for c in pr.get("comments") or []:
        ts = c.get("createdAt") or ""
        body = c.get("body") or ""
        if FORCE_REVIEW_RE.search(body):
            if not since_ts or ts > since_ts:
                return True
    return False


def has_other_changes_requested(pr: dict) -> bool:
    """Check if any reviewer currently has an active CHANGES_REQUESTED without our gate marker."""
    latest_by_author: dict[str, dict] = {}
    for r in pr.get("reviews") or []:
        state = (r.get("state") or "").upper()
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        author = ((r.get("author") or {}).get("login") or "").lower()
        if not author:
            continue
        latest_by_author[author] = r

    for author, r in latest_by_author.items():
        if (r.get("state") or "").upper() == "CHANGES_REQUESTED":
            body = r.get("body") or ""
            if not MARKER_RE.search(body):
                return True
    return False


def extract_issue_numbers(pr: dict, commits: list[str]) -> tuple[list[int], str]:
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    branch = pr.get("headRefName") or ""

    nums = sorted({int(m.group(1)) for m in ISSUE_KEYWORD_RE.finditer(body)})
    if nums:
        return nums, "pr_body_keyword"

    nums = sorted(
        {int(m.group(1)) for m in ISSUE_HASH_RE.finditer(f"{title}\n{body}")}
    )
    if nums:
        return nums, "pr_text_ref"

    commit_text = "\n".join(commits)
    nums = sorted({int(m.group(1)) for m in ISSUE_HASH_RE.finditer(commit_text)})
    if nums:
        return nums, "commit_ref"

    m = BRANCH_ISSUE_RE.search(branch)
    if m:
        return [int(m.group(1))], "branch_name"

    return [], "none"


def fetch_issue_with_hierarchy(repo: str, num: int) -> dict | None:
    try:
        issue = gh_json([
            "issue",
            "view",
            str(num),
            "--repo",
            repo,
            "--json",
            "number,title,state,body,labels,comments",
        ])
    except subprocess.CalledProcessError:
        return None

    issue["comments"] = [
        {
            "author": (c.get("author") or {}).get("login") or "",
            "createdAt": c.get("createdAt") or "",
            "body": c.get("body") or "",
        }
        for c in (issue.get("comments") or [])
    ]

    parent_raw = gh_api_optional(f"repos/{repo}/issues/{num}/parent")
    if isinstance(parent_raw, dict) and parent_raw.get("number"):
        parent_num = parent_raw["number"]
        parent_comments_raw = gh_api_optional(f"repos/{repo}/issues/{parent_num}/comments?per_page=100")
        parent_comments = (
            [
                {
                    "author": (c.get("user") or {}).get("login") or "",
                    "createdAt": c.get("created_at") or "",
                    "body": c.get("body") or "",
                }
                for c in parent_comments_raw
            ]
            if isinstance(parent_comments_raw, list)
            else []
        )
        issue["parent"] = {
            "number": parent_num,
            "title": parent_raw.get("title", ""),
            "body": parent_raw.get("body", ""),
            "comments": parent_comments,
        }
        siblings_raw = gh_api_optional(f"repos/{repo}/issues/{parent_num}/sub_issues")
        if isinstance(siblings_raw, list):
            issue["sibling_issues"] = [
                {
                    "number": s["number"],
                    "title": s.get("title", ""),
                    "state": s.get("state", ""),
                }
                for s in siblings_raw
                if s.get("number") != num
            ]

    subs_raw = gh_api_optional(f"repos/{repo}/issues/{num}/sub_issues")
    if isinstance(subs_raw, list) and subs_raw:
        issue["sub_issues"] = [
            {
                "number": s["number"],
                "title": s.get("title", ""),
                "state": s.get("state", ""),
            }
            for s in subs_raw
        ]

    return issue


def fetch_inline_review_comments(repo: str, pr_num: int) -> list[dict]:
    raw = gh_api_optional(f"repos/{repo}/pulls/{pr_num}/comments?per_page=100")
    if not isinstance(raw, list):
        return []
    return [
        {
            "id": c.get("id"),
            "author": (c.get("user") or {}).get("login") or "",
            "path": c.get("path") or "",
            "line": c.get("line") or c.get("original_line"),
            "createdAt": c.get("created_at") or "",
            "body": c.get("body") or "",
        }
        for c in raw
    ]


def collect_referenced_items(
    repo: str, pr_num: int, pr: dict, issues: list[dict]
) -> list[dict]:
    """Fetch cross-referenced issues/PRs mentioned in PR/issue bodies and comments."""
    texts: list[str] = [pr.get("body") or ""]
    for c in pr.get("comments") or []:
        texts.append(c.get("body") or "")
    for issue in issues:
        texts.append(issue.get("body") or "")
        for c in issue.get("comments") or []:
            texts.append(c.get("body") or "")
        parent = issue.get("parent")
        if isinstance(parent, dict):
            texts.append(parent.get("body") or "")
            for c in parent.get("comments") or []:
                texts.append(c.get("body") or "")

    seen: set[tuple[str, int]] = {(repo, pr_num)}
    for issue in issues:
        seen.add((repo, issue["number"]))
        parent = issue.get("parent")
        if isinstance(parent, dict) and parent.get("number"):
            seen.add((repo, parent["number"]))

    refs: list[tuple[str, str, int]] = []
    combined = "\n".join(texts)
    for m in GH_URL_REF_RE.finditer(combined):
        ref_repo, kind, num = m.group(1), m.group(2), int(m.group(3))
        if (ref_repo, num) not in seen:
            seen.add((ref_repo, num))
            refs.append((ref_repo, kind, num))
    for m in ISSUE_HASH_RE.finditer(combined):
        num = int(m.group(1))
        if (repo, num) not in seen:
            seen.add((repo, num))
            refs.append((repo, "issues", num))

    results: list[dict] = []
    for ref_repo, kind, num in refs[:10]:
        if kind == "pull":
            try:
                ref_pr = gh_json([
                    "pr",
                    "view",
                    str(num),
                    "--repo",
                    ref_repo,
                    "--json",
                    "number,title,state,url,body,mergedAt,files",
                ])
                results.append({
                    "type": "pr",
                    "repo": ref_repo,
                    "number": ref_pr["number"],
                    "title": ref_pr.get("title", ""),
                    "state": ref_pr.get("state", ""),
                    "url": ref_pr.get("url", ""),
                    "body": ref_pr.get("body", ""),
                    "files": [f.get("path", "") for f in (ref_pr.get("files") or [])],
                })
                continue
            except subprocess.CalledProcessError:
                pass
        try:
            ref_issue = gh_json([
                "issue",
                "view",
                str(num),
                "--repo",
                ref_repo,
                "--json",
                "number,title,state,url,body,comments",
            ])
            results.append({
                "type": "issue",
                "repo": ref_repo,
                "number": ref_issue["number"],
                "title": ref_issue.get("title", ""),
                "state": ref_issue.get("state", ""),
                "url": ref_issue.get("url", ""),
                "body": ref_issue.get("body", ""),
                "comments": [
                    {
                        "author": (c.get("author") or {}).get("login") or "",
                        "createdAt": c.get("createdAt") or "",
                        "body": c.get("body") or "",
                    }
                    for c in (ref_issue.get("comments") or [])
                ],
            })
        except subprocess.CalledProcessError:
            continue
    return results


def lock_path_for(repo: str, pr_num: int, head_sha: str) -> Path:
    safe_repo = repo.replace("/", "__")
    return LOCK_DIR / f"{safe_repo}__pr{pr_num}__{head_sha[:12]}.lock"


def is_locked(repo: str, pr_num: int, head_sha: str) -> bool:
    p = lock_path_for(repo, pr_num, head_sha)
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    if age > LOCK_TTL_SECONDS:
        try:
            p.unlink()
        except OSError:
            pass
        return False
    return True


def acquire_lock(repo: str, pr_num: int, head_sha: str) -> bool:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    p = lock_path_for(repo, pr_num, head_sha)
    if is_locked(repo, pr_num, head_sha):
        return False
    p.write_text(f"{os.getpid()} {int(time.time())}\n", encoding="utf-8")
    return True


def release_lock(repo: str, pr_num: int, head_sha: str) -> None:
    p = lock_path_for(repo, pr_num, head_sha)
    try:
        p.unlink()
    except OSError:
        pass


def inspect_pr(repo: str, pr_num: int, take_lock: bool = False) -> dict:
    pr = gh_json([
        "pr",
        "view",
        str(pr_num),
        "--repo",
        repo,
        "--json",
        "number,title,url,state,isDraft,author,baseRefName,headRefName,headRefOid,"
        "mergeable,mergeStateStatus,body,statusCheckRollup,reviews,comments",
    ])
    author = (pr.get("author") or {}).get("login") or ""
    head_sha = pr["headRefOid"]
    checks = summarize_checks(pr.get("statusCheckRollup") or [])
    prev_sha, prev_status, prev_ts, prev_body = last_review_marker(pr)
    force_review = has_force_review_after(pr, prev_ts)

    if pr.get("state") != "OPEN":
        return {"action": "skip", "repo": repo, "pr": pr_num, "reason": f"state={pr.get('state')}"}
    if author.lower() in OWNER_EXEMPT:
        return {"action": "skip", "repo": repo, "pr": pr_num, "reason": f"exempt_owner={author}"}
    if pr.get("isDraft"):
        return {"action": "skip", "repo": repo, "pr": pr_num, "reason": "draft"}
    if pr.get("mergeable") == "CONFLICTING":
        return {"action": "skip", "repo": repo, "pr": pr_num, "reason": "conflicting"}

    if prev_sha == head_sha and not force_review:
        if prev_status == "approved":
            if (
                checks["state"] == "passed"
                and pr.get("mergeable") == "MERGEABLE"
                and not has_other_changes_requested(pr)
            ):
                return {
                    "action": "merge_ready",
                    "repo": repo,
                    "pr": pr_num,
                    "head_sha": head_sha,
                    "base_ref": pr["baseRefName"],
                    "checks": checks,
                }
            return {
                "action": "skip",
                "repo": repo,
                "pr": pr_num,
                "reason": f"already_approved_waiting(ci={checks['state']},mergeable={pr.get('mergeable')})",
                "head_sha": head_sha,
                "checks": checks,
            }
        if prev_status == "changes_requested":
            return {
                "action": "skip",
                "repo": repo,
                "pr": pr_num,
                "reason": f"already_reviewed({prev_status})_ci({checks['state']})",
                "head_sha": head_sha,
                "checks": checks,
            }
        # prev_status == "missing_issue": allow re-check if issue was added

    repo_path = LOCAL_REPOS.get(repo)
    base_sha = None
    commits: list[str] = []
    if repo_path and Path(repo_path).is_dir():
        run(["git", "-C", repo_path, "fetch", "origin", "--prune"])
        ref = f"refs/remotes/origin/pr-{pr_num}"
        run(["git", "-C", repo_path, "fetch", "origin", f"pull/{pr_num}/head:{ref}"])
        base_ref = f"origin/{pr['baseRefName']}"
        base_sha = run(["git", "-C", repo_path, "merge-base", base_ref, ref]).stdout.strip()
        commits = [
            line
            for line in run(
                ["git", "-C", repo_path, "log", f"{base_sha}..{head_sha}", "--oneline"]
            ).stdout.splitlines()
            if line.strip()
        ]

    issue_nums, issue_source = extract_issue_numbers(pr, commits)
    issues = []
    for num in issue_nums:
        item = fetch_issue_with_hierarchy(repo, num)
        if item:
            issues.append(item)

    if not issues:
        if prev_sha == head_sha and prev_status == "missing_issue" and not force_review:
            return {
                "action": "skip",
                "repo": repo,
                "pr": pr_num,
                "reason": "already_requested_issue",
                "head_sha": head_sha,
            }
        return {
            "action": "missing_issue",
            "repo": repo,
            "pr": pr_num,
            "url": pr["url"],
            "title": pr["title"],
            "author": author,
            "head_sha": head_sha,
        }

    if take_lock and not acquire_lock(repo, pr_num, head_sha):
        return {
            "action": "skip",
            "repo": repo,
            "pr": pr_num,
            "reason": "review_in_progress_locked",
            "head_sha": head_sha,
        }

    pr_comments = [
        {
            "author": (c.get("author") or {}).get("login") or "",
            "createdAt": c.get("createdAt") or "",
            "body": c.get("body") or "",
        }
        for c in (pr.get("comments") or [])
    ]
    pr_reviews = [
        {
            "author": (r.get("author") or {}).get("login") or "",
            "state": r.get("state") or "",
            "submittedAt": r.get("submittedAt") or "",
            "body": r.get("body") or "",
        }
        for r in (pr.get("reviews") or [])
    ]
    inline_comments = fetch_inline_review_comments(repo, pr_num)
    referenced_items = collect_referenced_items(repo, pr_num, pr, issues)

    return {
        "action": "review",
        "repo": repo,
        "pr": pr_num,
        "url": pr["url"],
        "title": pr["title"],
        "author": author,
        "base_ref": pr["baseRefName"],
        "head_ref": pr["headRefName"],
        "base_sha": base_sha,
        "head_sha": head_sha,
        "repo_path": repo_path,
        "diff_cmd": (
            f"git -C {repo_path} diff {base_sha}...{head_sha}"
            if repo_path and base_sha
            else f"gh pr diff {pr_num} --repo {repo}"
        ),
        "pr_body": pr.get("body") or "",
        "pr_comments": pr_comments,
        "pr_reviews": pr_reviews,
        "inline_review_comments": inline_comments,
        "previous_gate_review": {
            "sha": prev_sha,
            "status": prev_status,
            "body": prev_body,
        }
        if prev_sha
        else None,
        "commits": commits,
        "issue_source": issue_source,
        "issues": issues,
        "referenced_items": referenced_items,
        "checks": checks,
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    result = inspect_pr(args.repo, args.pr, take_lock=args.lock)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    repos = args.repos or list(LOCAL_REPOS.keys())
    items = []
    skipped = []
    for repo in repos:
        prs = gh_json([
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,author,isDraft",
        ])
        for pr in prs:
            author = ((pr.get("author") or {}).get("login") or "").lower()
            if author in OWNER_EXEMPT or pr.get("isDraft"):
                continue
            info = inspect_pr(repo, pr["number"], take_lock=args.lock)
            if info["action"] == "skip":
                skipped.append(info)
            else:
                items.append(info)
    print(json.dumps({"items": items, "skipped": skipped}, ensure_ascii=False, indent=2))
    return 0


def cmd_act(args: argparse.Namespace) -> int:
    try:
        pr = gh_json([
            "pr",
            "view",
            str(args.pr),
            "--repo",
            args.repo,
            "--json",
            "headRefOid,statusCheckRollup,state,isDraft,author,mergeable,reviews",
        ])
        if pr.get("state") != "OPEN":
            print(json.dumps({"result": "skipped", "reason": "not_open"}))
            return 0
        if (pr.get("author") or {}).get("login", "").lower() in OWNER_EXEMPT:
            print(json.dumps({"result": "skipped", "reason": "exempt_owner"}))
            return 0
        if pr["headRefOid"] != args.sha:
            print(json.dumps({"result": "skipped", "reason": "head_changed", "current": pr["headRefOid"]}))
            return 0

        checks = summarize_checks(pr.get("statusCheckRollup") or [])

        if args.verdict == "missing_issue":
            marker = f"<!-- review-sha:{args.sha} status:missing_issue -->"
            body = (
                "Не найден связанный GitHub Issue для проверки соответствия спецификации.\n\n"
                "Добавьте ссылку на тикет в описание PR (например, `Closes #123`) — "
                "проверка запустится автоматически. Также можно запросить повторную проверку комментарием `/review`.\n\n"
                f"{marker}"
            )
            run([
                "gh",
                "pr",
                "review",
                str(args.pr),
                "--repo",
                args.repo,
                "--request-changes",
                "--body",
                body,
            ])
            print(json.dumps({"result": "missing_issue_requested", "sha": args.sha}))
            return 0

        if args.verdict == "merge":
            if (
                checks["state"] == "passed"
                and pr.get("mergeable") == "MERGEABLE"
                and not has_other_changes_requested(pr)
            ):
                run(["gh", "pr", "merge", str(args.pr), "--repo", args.repo, "--squash", "--delete-branch"])
                print(json.dumps({"result": "merged", "sha": args.sha, "checks": checks}))
            else:
                print(
                    json.dumps({
                        "result": "merge_blocked",
                        "sha": args.sha,
                        "checks": checks,
                        "mergeable": pr.get("mergeable"),
                    })
                )
            return 0

        if not args.body_file:
            print(json.dumps({"error": "--body-file required for approve/changes"}))
            return 2

        body = Path(args.body_file).read_text(encoding="utf-8").strip()
        status_label = "approved" if args.verdict == "approve" else "changes_requested"
        marker = f"<!-- review-sha:{args.sha} status:{status_label} -->"
        full_body = f"{body}\n\n{marker}"

        if args.verdict == "changes":
            run([
                "gh",
                "pr",
                "review",
                str(args.pr),
                "--repo",
                args.repo,
                "--request-changes",
                "--body",
                full_body,
            ])
            print(json.dumps({"result": "changes_requested", "sha": args.sha, "checks": checks}))
            return 0

        run([
            "gh",
            "pr",
            "review",
            str(args.pr),
            "--repo",
            args.repo,
            "--approve",
            "--body",
            full_body,
        ])
        if (
            checks["state"] == "passed"
            and pr.get("mergeable") == "MERGEABLE"
            and not has_other_changes_requested(pr)
        ):
            run(["gh", "pr", "merge", str(args.pr), "--repo", args.repo, "--squash", "--delete-branch"])
            print(json.dumps({"result": "merged", "sha": args.sha, "checks": checks}))
        else:
            print(
                json.dumps({
                    "result": "approved_waiting_ci",
                    "sha": args.sha,
                    "checks": checks,
                    "mergeable": pr.get("mergeable"),
                })
            )
        return 0
    finally:
        release_lock(args.repo, args.pr, args.sha)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic PR review gate for collaborator PRs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--repos", nargs="*", help="Repos to scan (defaults to LOCAL_REPOS)")
    p_scan.add_argument("--lock", action="store_true", help="Acquire review lock for items needing review")
    p_scan.set_defaults(func=cmd_scan)

    p_ins = sub.add_parser("inspect")
    p_ins.add_argument("repo", help="owner/name, e.g. rumap/rumap")
    p_ins.add_argument("pr", type=int)
    p_ins.add_argument("--lock", action="store_true", help="Acquire review lock")
    p_ins.set_defaults(func=cmd_inspect)

    p_act = sub.add_parser("act")
    p_act.add_argument("repo")
    p_act.add_argument("pr", type=int)
    p_act.add_argument("--sha", required=True)
    p_act.add_argument("--verdict", choices=["approve", "changes", "missing_issue", "merge"], required=True)
    p_act.add_argument("--body-file")
    p_act.set_defaults(func=cmd_act)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
