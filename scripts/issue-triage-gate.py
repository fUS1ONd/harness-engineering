#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LOCAL_REPOS = {
    "rumap/rumap": "/root/rumap",
    "rumap/rumap-android": "/root/rumap-android",
    "rumap/rumap-core": "/root/rumap-core",
}

MARKER = "<!-- benny-triage -->"
MARKER_RE = re.compile(r"<!--\s*benny-triage\s*-->")

AREA_LABELS = ("area:ui", "area:android", "area:backend", "area:core", "area:vpn")
TARGET_STATUS_LABELS = ("status:ready-for-agent", "status:needs-owner")
BUG_OPT_IN_LABELS = {"bug", "status:needs-triage"}
NON_BUG_LABELS = {
    "enhancement",
    "question",
    "type:epic",
    "documentation",
    "duplicate",
    "invalid",
    "wontfix",
    "area:roadmap",
}
NON_BUG_LABEL_PREFIXES = ("wayfinder:",)
NON_BUG_TITLE_RE = re.compile(r"^\s*(spec|map)\s*:", re.IGNORECASE)
HARD_STOP_LABELS = {
    "status:in-progress",
    "status:review",
    "status:blocked",
    "contributor",
    "human-only",
} | NON_BUG_LABELS
POST_TRIAGE_STATUS_LABELS = set(TARGET_STATUS_LABELS)
STOP_LABELS = HARD_STOP_LABELS | POST_TRIAGE_STATUS_LABELS

RETRIGGER_CMD_RE = re.compile(r"^\s*/(?:benny|triage)\b", re.IGNORECASE | re.MULTILINE)
ISSUE_HASH_RE = re.compile(r"(?<!\w)#(\d+)\b")
GH_URL_REF_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pull)/(\d+)"
)

LOCK_DIR = Path("/tmp/issue-triage-gate-locks")
LOCK_TTL_SECONDS = 900
DEFAULT_MAX_FIXERS = 2
MIN_MAX_FIXERS = 1
MAX_MAX_FIXERS = 2

NEEDS_TRIAGE_LABEL = "status:needs-triage"
IN_PROGRESS_LABEL = "status:in-progress"


def run(
    cmd: list[str],
    check: bool = True,
    *,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        text=True,
        input=input_text,
        capture_output=True,
        check=check,
    )


def gh_json(args: list[str]) -> dict | list:
    res = run(["gh", *args])
    return json.loads(res.stdout)


def gh_api_optional(endpoint: str, *, paginate: bool = False) -> dict | list | None:
    cmd = ["gh", "api"]
    if paginate:
        cmd.extend(["--paginate", "--slurp"])
    cmd.append(endpoint)
    res = run(cmd, check=False)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if paginate and isinstance(data, list) and data and all(isinstance(page, list) for page in data):
        return [item for page in data for item in page]
    return data


def resolve_lock_dir(cli_value: str | None = None) -> Path:
    if cli_value:
        return Path(cli_value)
    env_value = os.environ.get("ISSUE_TRIAGE_LOCK_DIR")
    if env_value:
        return Path(env_value)
    return LOCK_DIR


def resolve_max_fixers(cli_value: int | str | None = None) -> int:
    if cli_value is not None:
        value = int(cli_value)
        if not MIN_MAX_FIXERS <= value <= MAX_MAX_FIXERS:
            raise ValueError(f"max_fixers out of range: {value}")
        return value
    raw = os.environ.get("ISSUE_TRIAGE_MAX_FIXERS")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_MAX_FIXERS
        if MIN_MAX_FIXERS <= value <= MAX_MAX_FIXERS:
            return value
    return DEFAULT_MAX_FIXERS


def max_fixers_argument(value: str) -> int:
    try:
        return resolve_max_fixers(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"--max-fixers must be {MIN_MAX_FIXERS} or {MAX_MAX_FIXERS}"
        ) from exc


def _login(value) -> str:
    if isinstance(value, dict):
        return value.get("login") or ""
    return value or ""


def label_names(raw) -> list[str]:
    names: list[str] = []
    for label in raw or []:
        name = label.get("name") if isinstance(label, dict) else str(label)
        if name:
            names.append(name)
    return names


def _normalize_comment(raw: dict) -> dict:
    author = raw.get("author")
    if author is None:
        author = raw.get("user")
    return {
        "author": _login(author),
        "createdAt": raw.get("createdAt") or raw.get("created_at") or "",
        "body": raw.get("body") or "",
    }


def normalize_issue(raw: dict) -> dict:
    author = raw.get("author")
    if author is None:
        author = raw.get("user")
    return {
        "number": raw["number"],
        "title": raw.get("title") or "",
        "url": raw.get("url") or "",
        "state": raw.get("state") or "OPEN",
        "author": _login(author),
        "body": raw.get("body") or "",
        "labels": label_names(raw.get("labels")),
        "assignees": [login for login in (_login(a) for a in raw.get("assignees") or []) if login],
        "comments": [_normalize_comment(c) for c in raw.get("comments") or []],
    }


def fetch_issue_raw(repo: str, number: int) -> dict:
    return gh_json([
        "issue",
        "view",
        str(number),
        "--repo",
        repo,
        "--json",
        "number,title,url,state,author,body,labels,assignees,comments",
    ])


def fetch_issue(repo: str, number: int) -> dict:
    return normalize_issue(fetch_issue_raw(repo, number))


def hard_stop_reason(issue: dict) -> str | None:
    if issue["assignees"]:
        return "has_assignee"
    labels = issue["labels"]
    matched = sorted(
        {
            label
            for label in labels
            if label in HARD_STOP_LABELS or label.startswith(NON_BUG_LABEL_PREFIXES)
        }
    )
    if matched:
        return f"stop_label:{matched[0]}"
    if not (set(labels) & BUG_OPT_IN_LABELS):
        title_match = NON_BUG_TITLE_RE.match(issue["title"])
        if title_match:
            return f"non_bug_title:{title_match.group(1).lower()}"
    return None


def post_triage_stop_reason(issue: dict) -> str | None:
    matched = sorted(set(issue["labels"]) & POST_TRIAGE_STATUS_LABELS)
    if matched:
        return f"stop_label:{matched[0]}"
    return None


def last_marker_created_at(issue: dict) -> str | None:
    last: str | None = None
    for comment in issue["comments"]:
        if MARKER_RE.search(comment["body"]):
            if last is None or comment["createdAt"] > last:
                last = comment["createdAt"]
    return last


def comment_retrigger(issue: dict, since_ts: str | None) -> dict | None:
    hits = [
        comment
        for comment in issue["comments"]
        if RETRIGGER_CMD_RE.search(comment["body"])
        and (not since_ts or comment["createdAt"] > since_ts)
    ]
    if not hits:
        return None
    comment = max(hits, key=lambda c: c["createdAt"])
    match = RETRIGGER_CMD_RE.search(comment["body"])
    command = match.group(0).strip().lower() if match else ""
    return {
        "kind": "comment_command",
        "command": command,
        "createdAt": comment["createdAt"],
        "author": comment["author"],
    }


def label_retrigger(repo: str, issue: dict, since_ts: str | None) -> dict | None:
    if NEEDS_TRIAGE_LABEL not in issue["labels"]:
        return None
    events = gh_api_optional(
        f"repos/{repo}/issues/{issue['number']}/events?per_page=100",
        paginate=True,
    )
    if not isinstance(events, list):
        return None
    latest: str | None = None
    for event in events:
        if event.get("event") != "labeled":
            continue
        label = (event.get("label") or {}).get("name") or event.get("label_name") or ""
        if label != NEEDS_TRIAGE_LABEL:
            continue
        created_at = event.get("created_at") or event.get("createdAt") or ""
        if since_ts and created_at <= since_ts:
            continue
        if latest is None or created_at > latest:
            latest = created_at
    if latest is None:
        return None
    return {"kind": "label_relabel", "label": NEEDS_TRIAGE_LABEL, "createdAt": latest}


def retrigger_reason(
    repo: str, issue: dict, marker_ts: str | None
) -> tuple[str | None, dict | None]:
    comment = comment_retrigger(issue, marker_ts)
    if comment:
        return f"comment:{comment['command']}@{comment['createdAt']}", comment
    label = label_retrigger(repo, issue, marker_ts)
    if label:
        return f"label:{label['label']}@{label['createdAt']}", label
    return None, None


def collect_referenced_items(repo: str, issue: dict) -> list[dict]:
    texts = [issue["body"]]
    for comment in issue["comments"]:
        texts.append(comment["body"])
    combined = "\n".join(texts)

    seen: set[tuple[str, int]] = {(repo, issue["number"])}
    refs: list[tuple[str, str, int]] = []
    for match in GH_URL_REF_RE.finditer(combined):
        ref_repo, kind, number = match.group(1), match.group(2), int(match.group(3))
        if (ref_repo, number) not in seen:
            seen.add((ref_repo, number))
            refs.append((ref_repo, kind, number))
    for match in ISSUE_HASH_RE.finditer(combined):
        number = int(match.group(1))
        if (repo, number) not in seen:
            seen.add((repo, number))
            refs.append((repo, "issues", number))

    results: list[dict] = []
    for ref_repo, kind, number in refs[:10]:
        if kind == "pull":
            try:
                ref_pr = gh_json([
                    "pr",
                    "view",
                    str(number),
                    "--repo",
                    ref_repo,
                    "--json",
                    "number,title,state,url,body",
                ])
            except subprocess.CalledProcessError:
                pass
            else:
                results.append({
                    "type": "pr",
                    "repo": ref_repo,
                    "number": ref_pr["number"],
                    "title": ref_pr.get("title", ""),
                    "state": ref_pr.get("state", ""),
                    "url": ref_pr.get("url", ""),
                    "body": ref_pr.get("body", ""),
                })
                continue
        try:
            ref_issue = gh_json([
                "issue",
                "view",
                str(number),
                "--repo",
                ref_repo,
                "--json",
                "number,title,state,url,body,comments",
            ])
        except subprocess.CalledProcessError:
            continue
        results.append({
            "type": "issue",
            "repo": ref_repo,
            "number": ref_issue["number"],
            "title": ref_issue.get("title", ""),
            "state": ref_issue.get("state", ""),
            "url": ref_issue.get("url", ""),
            "body": ref_issue.get("body", ""),
            "comments": [
                _normalize_comment(c) for c in (ref_issue.get("comments") or [])
            ],
        })
    return results


def lock_path_for(lock_dir: Path | str, repo: str, issue_number: int) -> Path:
    safe_repo = repo.replace("/", "__")
    return Path(lock_dir) / f"{safe_repo}__issue{issue_number}.lock"


def _lock_guard_path_for(lock_dir: Path | str, repo: str, issue_number: int) -> Path:
    safe_repo = repo.replace("/", "__")
    return Path(lock_dir) / f"{safe_repo}__issue{issue_number}.guard"


@contextlib.contextmanager
def _lock_guard(lock_dir: Path | str, repo: str, issue_number: int):
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    guard_path = _lock_guard_path_for(lock_dir, repo, issue_number)
    fd = os.open(guard_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def acquire_lock(lock_dir: Path | str, repo: str, issue_number: int) -> bool:
    path = lock_path_for(lock_dir, repo, issue_number)
    with _lock_guard(lock_dir, repo, issue_number):
        now = time.time()
        try:
            st = path.stat()
        except FileNotFoundError:
            st = None
        if st is not None:
            if (now - st.st_mtime) <= LOCK_TTL_SECONDS:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            os.write(fd, f"{os.getpid()} {int(now)}\n".encode("utf-8"))
        finally:
            os.close(fd)
        return True


def release_lock(lock_dir: Path | str, repo: str, issue_number: int) -> None:
    if not Path(lock_dir).exists():
        return
    path = lock_path_for(lock_dir, repo, issue_number)
    with _lock_guard(lock_dir, repo, issue_number):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def count_active_fixers(repos: list[str]) -> int:
    total = 0
    for repo in repos:
        issues = gh_json([
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            IN_PROGRESS_LABEL,
            "--json",
            "number",
        ])
        total += len(issues)
    return total


def with_concurrency(item: dict, active_fixers: int, max_fixers: int) -> dict:
    enriched = dict(item)
    enriched["active_fixers"] = active_fixers
    enriched["max_fixers"] = max_fixers
    enriched["auto_fix_slot_available"] = active_fixers < max_fixers
    return enriched


def evaluate_issue(
    repo: str,
    raw_issue: dict,
    *,
    take_lock: bool = False,
    lock_dir: Path | str = LOCK_DIR,
) -> dict:
    issue = normalize_issue(raw_issue)
    base = {"repo": repo, "issue": issue["number"]}

    if issue["state"].upper() != "OPEN":
        return {**base, "action": "skip", "reason": f"state={issue['state']}"}

    reason = hard_stop_reason(issue)
    if reason:
        return {**base, "action": "skip", "reason": reason}

    post_triage = post_triage_stop_reason(issue)
    marker_ts = last_marker_created_at(issue)
    retrigger: str | None = None
    retrigger_info: dict | None = None
    if marker_ts is not None or post_triage is not None:
        retrigger, retrigger_info = retrigger_reason(repo, issue, marker_ts)
    if retrigger is None:
        if post_triage is not None:
            return {**base, "action": "skip", "reason": post_triage}
        if marker_ts is not None:
            return {**base, "action": "skip", "reason": "already_triaged"}

    locked = False
    if take_lock:
        if not acquire_lock(lock_dir, repo, issue["number"]):
            return {**base, "action": "skip", "reason": "triage_in_progress_locked"}
        locked = True

    try:
        referenced_items = collect_referenced_items(repo, issue)
    except Exception:
        if locked:
            release_lock(lock_dir, repo, issue["number"])
        raise

    return {
        **base,
        "action": "triage",
        "url": issue["url"],
        "title": issue["title"],
        "author": issue["author"],
        "body": issue["body"],
        "labels": issue["labels"],
        "comments": issue["comments"],
        "referenced_items": referenced_items,
        "local_repo_path": LOCAL_REPOS[repo],
        "retrigger_reason": retrigger,
        "retrigger": retrigger_info,
    }


def inspect_issue(
    repo: str,
    number: int,
    *,
    take_lock: bool = False,
    lock_dir: Path | str = LOCK_DIR,
) -> dict:
    raw = fetch_issue_raw(repo, number)
    return evaluate_issue(repo, raw, take_lock=take_lock, lock_dir=lock_dir)


def list_open_issues(repo: str) -> list[dict]:
    return gh_json([
        "issue",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,url,state,author,body,labels,assignees,comments",
    ])


def is_pull_request(raw_issue: dict) -> bool:
    if raw_issue.get("pull_request") or raw_issue.get("pullRequest"):
        return True
    return "/pull/" in (raw_issue.get("url") or "")


def validate_repos(repos: list[str]) -> str | None:
    unknown = [repo for repo in repos if repo not in LOCAL_REPOS]
    if unknown:
        return f"unknown_repo:{unknown[0]}"
    return None


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def emit_error(message: str) -> int:
    print(json.dumps({"result": "error", "error": message}, ensure_ascii=False, indent=2))
    return 2


def cmd_scan(args: argparse.Namespace) -> int:
    repos = list(dict.fromkeys(args.repos or list(LOCAL_REPOS.keys())))
    error = validate_repos(repos)
    if error:
        return emit_error(error)

    lock_dir = resolve_lock_dir(args.lock_dir)
    max_fixers = resolve_max_fixers(args.max_fixers)

    open_issues_by_repo: dict[str, list[dict]] = {}
    scanned_active_fixers = 0
    for repo in repos:
        issues = [raw for raw in list_open_issues(repo) if not is_pull_request(raw)]
        open_issues_by_repo[repo] = issues
        scanned_active_fixers += sum(
            1 for raw in issues if IN_PROGRESS_LABEL in label_names(raw.get("labels"))
        )

    remaining_repos = [repo for repo in LOCAL_REPOS if repo not in open_issues_by_repo]
    active_fixers = scanned_active_fixers + count_active_fixers(remaining_repos)

    items: list[dict] = []
    skipped: list[dict] = []
    acquired_locks: list[tuple[str, int]] = []
    try:
        for repo, open_issues in open_issues_by_repo.items():
            for raw_issue in open_issues:
                info = evaluate_issue(
                    repo,
                    raw_issue,
                    take_lock=args.lock,
                    lock_dir=lock_dir,
                )
                if info["action"] == "skip":
                    skipped.append(info)
                else:
                    if args.lock:
                        acquired_locks.append((repo, info["issue"]))
                    items.append(with_concurrency(info, active_fixers, max_fixers))
    except Exception:
        for locked_repo, locked_issue in acquired_locks:
            release_lock(lock_dir, locked_repo, locked_issue)
        raise

    return emit({
        "items": items,
        "skipped": skipped,
        "active_fixers": active_fixers,
        "max_fixers": max_fixers,
        "auto_fix_slot_available": active_fixers < max_fixers,
    })


def cmd_inspect(args: argparse.Namespace) -> int:
    if args.repo not in LOCAL_REPOS:
        return emit_error(f"unknown_repo:{args.repo}")
    lock_dir = resolve_lock_dir(args.lock_dir)
    max_fixers = resolve_max_fixers(args.max_fixers)

    info = inspect_issue(
        args.repo,
        args.issue,
        take_lock=args.lock,
        lock_dir=lock_dir,
    )
    if info["action"] == "skip":
        return emit(info)

    try:
        active_fixers = count_active_fixers(list(LOCAL_REPOS.keys()))
        return emit(with_concurrency(info, active_fixers, max_fixers))
    except Exception:
        if args.lock:
            release_lock(lock_dir, args.repo, args.issue)
        raise


def plan_comment_body(raw_body: str) -> str:
    body = raw_body.strip()
    if MARKER_RE.search(body):
        return body
    if not body:
        return MARKER
    return f"{body}\n\n{MARKER}"


def cmd_act(args: argparse.Namespace) -> int:
    lock_dir = resolve_lock_dir(args.lock_dir)

    if args.repo not in LOCAL_REPOS:
        return emit_error(f"unknown_repo:{args.repo}")

    areas: list[str] = []
    for area in args.area:
        if area not in AREA_LABELS:
            return emit_error(f"unknown_area_label:{area}")
        if area not in areas:
            areas.append(area)
    if not areas:
        return emit_error("area_label_required")

    if args.claim_autofix and args.status != "status:ready-for-agent":
        return emit_error("claim_autofix_requires_ready_for_agent")

    if not args.body_file or not Path(args.body_file).is_file():
        return emit_error(f"body_file_not_found:{args.body_file}")

    comment_body = plan_comment_body(Path(args.body_file).read_text(encoding="utf-8"))
    max_fixers = resolve_max_fixers(args.max_fixers)

    try:
        current_issue = fetch_issue(args.repo, args.issue)
        current_labels = current_issue["labels"]

        autofix_claimed = False
        claim_skipped_reason: str | None = None
        effective_status = args.status
        add_assignees: list[str] = []
        active_fixers: int | None = None

        if args.claim_autofix:
            active_fixers = count_active_fixers(list(LOCAL_REPOS.keys()))
            if current_issue["assignees"] or IN_PROGRESS_LABEL in current_labels:
                claim_skipped_reason = "already_claimed"
            elif active_fixers < max_fixers:
                autofix_claimed = True
                effective_status = IN_PROGRESS_LABEL
                add_assignees = ["@me"]
            else:
                claim_skipped_reason = "max_fixers_reached"

        add_labels = [*areas, effective_status]
        requested_removals = [NEEDS_TRIAGE_LABEL, *TARGET_STATUS_LABELS, *args.remove_label]
        remove_labels: list[str] = []
        for label in requested_removals:
            if label in current_labels and label not in add_labels and label not in remove_labels:
                remove_labels.append(label)

        claim_fields: dict = {}
        if args.claim_autofix:
            claim_fields = {
                "autofix_claimed": autofix_claimed,
                "active_fixers": active_fixers,
                "max_fixers": max_fixers,
                "add_assignees": add_assignees,
            }
            if claim_skipped_reason is not None:
                claim_fields["claim_skipped_reason"] = claim_skipped_reason

        if args.dry_run:
            return emit({
                "result": "dry_run",
                "repo": args.repo,
                "issue": args.issue,
                "add_labels": add_labels,
                "remove_labels": remove_labels,
                "comment_body": comment_body,
                "would_release_lock": True,
                **claim_fields,
            })

        if add_labels or remove_labels or add_assignees:
            edit_cmd = ["gh", "issue", "edit", str(args.issue), "--repo", args.repo]
            for label in add_labels:
                edit_cmd.extend(["--add-label", label])
            for label in remove_labels:
                edit_cmd.extend(["--remove-label", label])
            for assignee in add_assignees:
                edit_cmd.extend(["--add-assignee", assignee])
            run(edit_cmd)

        run(
            [
                "gh",
                "issue",
                "comment",
                str(args.issue),
                "--repo",
                args.repo,
                "--body-file",
                "-",
            ],
            input_text=comment_body,
        )

        return emit({
            "result": "acted",
            "repo": args.repo,
            "issue": args.issue,
            "comment_posted": True,
            "add_labels": add_labels,
            "remove_labels": remove_labels,
            "released_lock": True,
            **claim_fields,
        })
    finally:
        if not args.dry_run:
            release_lock(lock_dir, args.repo, args.issue)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic issue triage gate for rumap/* bugs")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--repos", nargs="*", help="Repos to scan (defaults to LOCAL_REPOS)")
    p_scan.add_argument("--lock", action="store_true", help="Acquire triage lock for eligible issues")
    p_scan.add_argument("--max-fixers", type=max_fixers_argument, default=None)
    p_scan.add_argument("--lock-dir")
    p_scan.set_defaults(func=cmd_scan)

    p_ins = sub.add_parser("inspect")
    p_ins.add_argument("repo", help="owner/name, e.g. rumap/rumap")
    p_ins.add_argument("issue", type=int)
    p_ins.add_argument("--lock", action="store_true", help="Acquire triage lock")
    p_ins.add_argument("--max-fixers", type=max_fixers_argument, default=None)
    p_ins.add_argument("--lock-dir")
    p_ins.set_defaults(func=cmd_inspect)

    p_act = sub.add_parser("act")
    p_act.add_argument("repo")
    p_act.add_argument("issue", type=int)
    p_act.add_argument("--body-file", required=True)
    p_act.add_argument("--area", action="append", required=True)
    p_act.add_argument("--status", choices=TARGET_STATUS_LABELS, required=True)
    p_act.add_argument("--remove-label", action="append", default=[])
    p_act.add_argument("--claim-autofix", action="store_true")
    p_act.add_argument("--max-fixers", type=max_fixers_argument, default=None)
    p_act.add_argument("--dry-run", action="store_true")
    p_act.add_argument("--lock-dir")
    p_act.set_defaults(func=cmd_act)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
