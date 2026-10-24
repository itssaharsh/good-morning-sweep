#!/usr/bin/env python3
"""pb.py - deterministic helper for the prod-build skill (stdlib only, Python 3.8+; Playwright and boto3 optional).

It keeps the parts of a build that should not depend on an agent's memory or honesty:
the task graph, the evidence ledger, test-weakening checks, relay intake, project
memory records, the delivery-report check and, for events, the build window and
what a judge will open.

  init      create .prod-build/ and docs/memory/ (never overwrites)
  status    one-screen board: the builder's overdue steps first, tasks, next ready work, evidence, memory
  plan      check | waves | next         validate plan.json and compute parallel lanes
  task      <ID> start|done|skip|block|reopen [--evidence E0001,..] [--note ..]
  run       [--task ID] [--kind K] [--where local|preview|prod] [--expect-fail] -- <cmd>
  verify    <ID>                         run all of a task's verify commands through the ledger
  smoke     <base-url> [--routes / /x]   HTTP + asset check of a running or deployed app
  evidence  add | list | redact          record an observation, list the ledger, scrub a leaked secret
  diffcheck [--base REF | --task ID]     find skipped/deleted tests, loosened configs, secrets
  intake    <ID> [--base REF]            check a relay agent's work before accepting it
  mem       add | find | check | index | mark     project memory in docs/memory/
  report    <file>                       every verification claim must cite passing evidence
  clock     [set --start I --end I ...]  the event's build window: phase, time left, what is allowed now
  window    [--start I --end I]          the repository's history against the build window
  submit    check [--description ..] [--video ..] [--online] [--published ..] [--page-profile ..] [--headings ..]
                                         what a judge opens: card, README first screen, links, history, video,
                                         the live page against its draft, a DEV post through devto_check.py
  page      capture <url> [--out docs/published.md]   the live page as a signed-out browser draws it (Playwright)
  preflight aws [--region R] [--model ID]          read-only account checks on day 0, recorded (boto3)

Run from anywhere inside the project; the root is the nearest folder holding .prod-build/
or .git (override with --root). Exit codes: 0 ok, 1 check failed (or, for page capture and
preflight, a tool they need is missing: SKIP), 2 usage error. Playwright and boto3 are optional and
imported only by the two commands that use them.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

VERSION = "0.2.1"
ARGV_TEXT = [""]  # the command line as typed, for the ledger
MODES = ("hackathon", "product", "hybrid")
CHANNELS = ("direct", "relay", "chat")
STATUSES = ("todo", "doing", "done", "skip", "blocked")
KINDS = ("build", "test", "red", "lint", "typecheck", "e2e", "smoke", "security", "deploy",
         "eval", "perf", "migration", "review", "manual", "other")
MEM_TYPES = {  # type -> (id prefix, folder)
    "decision": ("ADR", "decisions"),
    "failure": ("F", "failures"),
    "lesson": ("L", "lessons"),
    "assumption": ("A", "assumptions"),
    "feedback": ("U", "feedback"),
    "pattern": ("P", "patterns"),
}
MEM_STATUSES = ("active", "superseded", "archived", "invalidated")
MEM_REQUIRED = ("id", "type", "title", "status", "scope", "components", "evidence", "verified_at")
CAPS = {"index_lines": 150, "record_lines": 60, "active": 150}
HOT_FILES = re.compile(
    r"(^|/)(package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?|"
    r"pyproject\.toml|poetry\.lock|uv\.lock|requirements[^/]*\.txt|go\.mod|go\.sum|Cargo\.(toml|lock)|"
    r"schema\.prisma|drizzle\.config\.[^/]+|tsconfig[^/]*\.json|next\.config\.[^/]+|vite\.config\.[^/]+|"
    r"docker-compose[^/]*\.ya?ml|\.env[^/]*)$|(^|/)(migrations|alembic|supabase/migrations)/|(^|/)\.github/workflows/")
TEST_FILE = re.compile(
    r"(^|/)(tests?|__tests__|spec|e2e)/|\.(test|spec)\.[cm]?[jt]sx?$|_test\.go$|(^|/)test_[^/]+\.py$|_test\.py$|_spec\.rb$")
CONFIG_FILE = re.compile(
    r"(^|/)(\.eslintrc[^/]*|eslint\.config\.[cm]?[jt]s|tsconfig[^/]*\.json|jest\.config\.[^/]+|"
    r"vitest\.config\.[^/]+|playwright\.config\.[^/]+|pytest\.ini|setup\.cfg|tox\.ini|\.coveragerc|"
    r"\.golangci\.ya?ml|biome\.jsonc?|\.prettierrc[^/]*|knip\.jsonc?|\.semgrep[^/]*|\.gitleaks\.toml)$"
    r"|(^|/)\.github/workflows/|(^|/)\.pre-commit-config\.yaml$"
    r"|(^|/)(AGENTS|CLAUDE|GEMINI)\.md$|(^|/)\.claude/(settings[^/]*\.json|agents/|hooks/)|(^|/)\.gemini/settings\.json$")
WEAKENING = [  # (regex on an added line, severity, message)
    (r"\b(describe|it|test|context|suite)\.(skip|only)\s*\(", "error", "focused/skipped JS test"),
    (r"\b(xit|xdescribe|fit|fdescribe|xtest)\s*\(", "error", "focused/skipped JS test"),
    (r"\btest\.fixme\s*\(|\btest\.fail\s*\(", "warn", "Playwright test marked fixme/fail"),
    (r"[{,]\s*skip\s*:\s*(true|[\"'])", "error", "test skipped via options ({ skip: … })"),
    (r"[{,]\s*todo\s*:\s*true", "warn", "test marked todo"),
    (r"@pytest\.mark\.(skip|xfail)|pytest\.skip\s*\(|@unittest\.skip", "error", "skipped Python test"),
    (r"\bt\.Skip(Now|f)?\s*\(", "error", "skipped Go test"),
    (r"#\[ignore\]", "error", "ignored Rust test"),
    (r"@ts-ignore|@ts-nocheck|@ts-expect-error", "warn", "type check suppressed"),
    (r"eslint-disable", "warn", "lint rule disabled"),
    (r"#\s*type:\s*ignore|#\s*noqa|//\s*nolint", "warn", "check suppressed"),
    (r"--no-verify", "error", "git hooks bypassed"),
    (r"\bdebugger\s*;", "warn", "debugger statement"),
    (r"--passWithNoTests|--pass-with-no-tests", "warn", "suite allowed to pass with no tests"),
]
SECRET_PATTERNS = [
    re.compile(r"\b(sk|rk)_(live|test)_[0-9A-Za-z]{16,}"),
    re.compile(r"\bwhsec_[0-9A-Za-z]{16,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]{20,}"),
    re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
SECRET_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(api[_-]?key|secret|token|passw(or)?d|client[_-]?secret))\b(\s*[:=]\s*[\"']?)([^\s\"'`]{8,})")
DB_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^:/\s@]+:)([^@\s]{3,})(@)")
URL_SECRET_PARAM = re.compile(r"(?i)([?&#](?:key|token|secret|password|pass|sig|signature|access_token|api_key|apikey|auth)=)[^&\s#\"']+")
ENV_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|CLIENT_SECRET|WEBHOOK)[A-Z0-9_]*)\s*=\s*[\"']?([^\s\"'#]{8,})")
SECRET_LITERAL = re.compile(
    r"(?i)(api[_-]?key|secret|token|passw(or)?d|client[_-]?secret|private[_-]?key)\w*[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_\-+/=.]{16,})[\"']")
PLACEHOLDER = re.compile(r"(?i)(example|placeholder|dummy|sample|your[_-]|xxxx|changeme|replace[_-]?me|redacted|<[^>]+>|\$\{)")
EVIDENCE_ID = re.compile(r"\bE\d{4,}\b")
NOT_RUN = re.compile(r"(?i)(\bnot run\b|\bnot performed\b|\bnot applicable\b|\bn/a\b|\bnot deployed\b|\bnot verified\b)")
REPORT_SECTIONS = ("built", "architecture", "verification", "deployment", "bugs", "known limitations",
                   "cleanup", "optimization", "memory", "next steps")
CLAIM_SECTIONS = ("verification", "deployment", "bugs")


# ----------------------------------------------------------------------------- utilities

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today() -> str:
    return _dt.date.today().isoformat()


def die(msg: str, code: int = 2) -> None:
    print(f"pb: {msg}", file=sys.stderr)
    sys.exit(code)


def find_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    here = Path.cwd().resolve()
    for p in [here, *here.parents]:
        if (p / ".prod-build").is_dir():
            return p
    for p in [here, *here.parents]:
        if (p / ".git").exists():
            return p
    return here


def git(root: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=check)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "git not found")


def git_info(root: Path) -> dict:
    head = git(root, "rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        return {"sha": None, "dirty": None}
    dirty = git(root, "status", "--porcelain", "--", ".", ":(exclude).prod-build")
    return {"sha": head.stdout.strip(), "dirty": bool(dirty.stdout.strip())}


def tree_hash(root: Path):
    """Content fingerprint of the working tree (tracked + untracked, minus ignored files and .prod-build/).
    Stored with each evidence entry so `task done` can refuse evidence gathered on different code."""
    if git(root, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return None
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(td, "index"))

        def g(*a):
            return subprocess.run(["git", *a], cwd=root, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        g("read-tree", "HEAD")
        g("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".prod-build")
        if g("add", "-A", "--", ".", ":(exclude).prod-build").returncode != 0:
            return None
        w = g("write-tree")
        return w.stdout.strip()[:12] if w.returncode == 0 else None


def redact(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    text = SECRET_ASSIGN.sub(lambda m: m.group(1) + m.group(4) + "[REDACTED]", text)
    text = DB_URL_PASSWORD.sub(lambda m: m.group(1) + "[REDACTED]" + m.group(3), text)
    text = URL_SECRET_PARAM.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


def looks_like_secret(line: str) -> bool:
    """High-precision check used for findings (redact() is deliberately greedy and used for logs)."""
    if PLACEHOLDER.search(line):
        return False
    if any(p.search(line) for p in SECRET_PATTERNS):
        return True
    m = SECRET_LITERAL.search(line)
    if m and len(set(m.group(3))) >= 8:
        return True
    m = ENV_ASSIGN.search(line)
    if m and len(set(m.group(2))) >= 6 and not m.group(2).startswith(("process.env", "os.environ", "$")):
        return True
    m = DB_URL_PASSWORD.search(line)
    return bool(m and not re.search(r"(?i)^(pass(word)?|postgres|root|admin|user)$", m.group(2)))


def norm_path(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def has_glob(p: str) -> bool:
    return bool(re.search(r"[*?\[]", p))


def static_prefix(p: str) -> str:
    m = re.search(r"[*?\[]", p)
    if not m:
        return p
    pre = p[: m.start()]
    return pre[: pre.rfind("/") + 1] if "/" in pre else ""


def glob_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append({"*": "[^/]*", "?": "[^/]"}.get(c, re.escape(c)))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def paths_overlap(a: str, b: str) -> bool:
    """Conservative: True when two write surfaces could touch the same file."""
    a, b = norm_path(a), norm_path(b)
    if not a or not b:
        return False
    if a == b:
        return True
    ga, gb = has_glob(a), has_glob(b)

    def contains(d: str, p: str) -> bool:
        d = d.rstrip("/")
        return p.startswith(d + "/")

    if not ga and not gb:
        return contains(a, b) or contains(b, a)
    if ga and gb:
        pa, pb_ = static_prefix(a), static_prefix(b)
        return pa.startswith(pb_) or pb_.startswith(pa)
    pat, concrete = (a, b) if ga else (b, a)
    if glob_regex(pat).match(concrete):
        return True
    pre = static_prefix(pat)
    return contains(concrete, pre) or (pre != "" and concrete.rstrip("/") + "/" == pre)


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON: {e}", 1)


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def pb_dir(root: Path) -> Path:
    return root / ".prod-build"


def mem_dir(root: Path) -> Path:
    return root / "docs" / "memory"


# ----------------------------------------------------------------------------- init

CONTRACT_TMPL = """# Implementation contract - {name}
mode: {mode} | channel: {channel} | created: {date}
Upstream read: <idea package / UI-SPEC.md / DESIGN.md / product brief, or "none">

## 1. Intent
- User and moment of use:
- What they can do:
- What changes after they use it (before -> after):

## 2. Scope
MUST (core product, main workflow, judged/required):
SHOULD (materially improves the main workflow):
COULD (later; not in this build):
WON'T now (hypothetical-future infrastructure, explicitly deferred):

## 3. Core journeys (smallest set that constitutes the product)
J1: user action -> system response -> observable outcome

## 4. Surfaces (only the necessary ones)

## 5. States per journey
| Journey | initial | loading | success | error | empty | partial | retry | disabled | permission-denied | offline/degraded |

## 6. Data
| Entity | Key fields | Relationships | Owner | Persistence | Source of truth | PII/retention |

## 7. Interfaces
| Interface | Purpose | Contract file | Failure mode | Fallback |

## 8. Constraints
Security / privacy / budget / latency / platform / deploy target / deadline / sponsor rules (verbatim) / judging criteria (verbatim + weights):

## 9. Acceptance criteria
AC-1: WHEN <event> THE SYSTEM SHALL <result>. Verify: <command or check>. Priority: must

## 10. Assumptions
A-1 [safe|reversible|blocking]: <claim> - validate by <method>

## 11. Demo script (hackathon/hybrid) or launch checklist (product)
"""

CONTRACT_EVENT_TMPL = """
## 12. Event and judged path
(This file is committed with the repository, so a judge may read it. Quote the event's own public pages, with
the date each was read. Nothing about individual judges and no guesses about what they like.)
- Window: start / freeze / end as instants (UTC and local), the page they came from, and the rule on earlier work, verbatim:
- How an entry is judged: what a judge opens first, second and third (queue card -> video -> live link ->
  repository), minutes per project, and whether anyone runs the build; each as the organisers state it, or "not published":
- Criteria: the organisers' wording and weights, and where each was read (the judges' own form when it is public, else the event page):
- Criteria coverage: per criterion, the moment in the run that earns it and its state (met / partial / open):
- Capability coverage: each capability the prize text names -> where it happens in the planned run, on the
  sponsor's own surface when the sponsor asked to see it:
- Main result: the outcome a camera can see (physical / native state / number / artifact), on material we did
  not author, and the one staged failure the product holds through:
- Promise coverage: each noun in the title and tagline -> the data or feature behind it on the first screen:
- Before the clock: what was prepared before the start (planning, account checks) and the README sentence that says so:
"""

STATE_TMPL = """# Build state - {name}
Updated: {date} | mode: {mode} | channel: {channel} | phase: 0-intake
(Keep this under 60 lines. Overwrite it at every phase boundary and before stopping.)

## Now
- Phase / current task:
- Last good commit:            Last good deploy:

## What worked (with evidence IDs)

## What failed (memory IDs) - do not retry without a new root cause

## Not tried yet / open questions

## Changes that need care (migrations applied, deploys, destructive ops)

## Exact next step
"""

MEM_README = """# Project memory

Decisions, failures, lessons, assumptions, user feedback and working patterns that should change
what the next agent does. Records are hints until verified against the code they cite.
Generated index: INDEX.md. Tooling: `python3 .prod-build/pb.py mem find|add|check|mark`.
Never store secrets, transcripts, logs, or anything the code already says.
"""

AGENTS_BLOCK = """<!-- prod-build:memory-protocol -->
## Build memory protocol
- Before planning or changing a component: read `docs/memory/INDEX.md`; run
  `python3 .prod-build/pb.py mem find --paths <files> --terms <keywords>` and apply what fits.
- On any error: search failures by the error text first (`mem find --terms "<signature>"`); do not retry
  an approach a failure record says failed unless you have a new root cause.
- A record is a hint until its cited files still match (`mem check` flags stale ones).
- Run checks through `python3 .prod-build/pb.py run --task <ID> -- <command>` so results land in the
  evidence ledger; "done" means passing evidence exists, not that code was written.
- Task briefs: `.prod-build/briefs/<ID>.md`. Reports: `.prod-build/reports/<ID>.md`.
<!-- /prod-build:memory-protocol -->
"""


def cmd_init(args, root: Path) -> int:
    d = pb_dir(root)
    created = []
    event_old = load_event(root, repair=event_flags_given(args))
    event = build_event(args, event_old) if event_flags_given(args) else event_old  # validated before anything is created
    if args.mode_given and args.mode == "hackathon" and not has_clock(event):
        die("hackathon mode needs the event clock before anything is built:\n"
            "  pb.py init --mode hackathon --start <instant> --end <instant> --source <event page>\n"
            "or, where the event has no build window, --no-window '<the rule, quoted>'.\n"
            "Instants carry a zone (2026-03-14T09:00:00Z or 2026-03-14T14:30:00+05:30) and come from the event's "
            "own page, not from memory.")
    for sub in ("briefs", "reports", "logs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    if event and event != event_old:
        write_json(event_path(root), event)
        created.append(event_path(root))
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("logs/\nevidence.*.jsonl\n", encoding="utf-8")
        created.append(gi)
    name = args.name or root.name
    plan = d / "plan.json"
    if not plan.exists():
        write_json(plan, {"project": name, "mode": args.mode, "channel": args.channel,
                          "base": git_info(root)["sha"], "created": today(), "milestones": [], "tasks": []})
        created.append(plan)
    for fname, tmpl in (("contract.md", CONTRACT_TMPL), ("state.md", STATE_TMPL)):
        f = d / fname
        if not f.exists():
            body = tmpl.format(name=name, mode=args.mode, channel=args.channel, date=today())
            if fname == "contract.md" and (args.mode != "product" or event):
                body += CONTRACT_EVENT_TMPL
            f.write_text(body, encoding="utf-8")
            created.append(f)
    ev = d / "evidence.jsonl"
    if not ev.exists():
        ev.touch()
        created.append(ev)
    me = Path(__file__).resolve()
    target = d / "pb.py"
    if me != target.resolve() and (not target.exists() or target.read_bytes() != me.read_bytes()):
        shutil.copyfile(me, target)
        created.append(target)
    m = mem_dir(root)
    for _, (_, folder) in MEM_TYPES.items():
        (m / folder).mkdir(parents=True, exist_ok=True)
    (m / "archive").mkdir(parents=True, exist_ok=True)
    if not (m / "README.md").exists():
        (m / "README.md").write_text(MEM_README, encoding="utf-8")
        created.append(m / "README.md")
    write_index(root)
    agents = root / "AGENTS.md"
    text = agents.read_text(encoding="utf-8") if agents.exists() else ""
    if "prod-build:memory-protocol" not in text:
        sep = "\n" if text and not text.endswith("\n") else ""
        header = "" if text else f"# {name}\n\n"
        agents.write_text(text + sep + header + AGENTS_BLOCK, encoding="utf-8")
        created.append(agents)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("\n".join(GITIGNORE_BASE) + "\n", encoding="utf-8")
        created.append(gitignore)
    else:
        have = gitignore.read_text(encoding="utf-8", errors="replace")
        lacking = [w for w in ("node_modules", ".env") if w not in have]
        if lacking:
            print("note: .gitignore lacks " + ", ".join(lacking) + "; add them before the first `git add`")
    if event:
        for line in ensure_ignores(root):
            print(line)
    if args.checklists:
        refs = Path(__file__).resolve().parent.parent / "references"
        dest = d / "checklists"
        if refs.is_dir():
            dest.mkdir(exist_ok=True)
            for name in ("security.md", "verification.md", "deploy.md", "ai-products.md"):
                if (refs / name).exists() and not (dest / name).exists():
                    shutil.copyfile(refs / name, dest / name)
                    created.append(dest / name)
        else:
            print("note: --checklists needs the skill's references/ folder next to scripts/; run init from the skill copy")
    claude = root / "CLAUDE.md"
    note = ""
    if not claude.exists():
        claude.write_text("@AGENTS.md\n", encoding="utf-8")
        created.append(claude)
    elif "@AGENTS.md" not in claude.read_text(encoding="utf-8"):
        note = "CLAUDE.md exists without '@AGENTS.md': add that line so Claude Code also reads AGENTS.md."
    if args.checklists:
        asset = Path(__file__).resolve().parent.parent / "assets" / "smoke.spec.ts"
        dest = d / "checklists" / "smoke.spec.ts"
        if asset.exists() and (d / "checklists").is_dir() and not dest.exists():
            shutil.copyfile(asset, dest)
            created.append(dest)
    for c in created:
        print(f"created {c.relative_to(root)}")
    if not args.mode_given:
        print(f"note: mode defaulted to '{args.mode}'; after classifying (Step 1) set \"mode\" in .prod-build/plan.json")
    if not created:
        print("nothing to create; already initialised")
    if note:
        print("note: " + note)
    code = 0
    if event:
        for line in clock_lines(event):
            print(line)
        if event.get("start") and event.get("history", "strict") == "strict":
            res = window_findings(root, parse_instant(event["start"]), parse_instant(event["end"]),
                                  event.get("tz_min"))
            for f in res["fails"]:
                print(f"FAIL  {f}")
            if res["fails"]:
                print("      " + NOT_FIXABLE)
                code = 1
    elif args.mode in ("hackathon", "hybrid"):
        print("note: no event clock. If this is for an event with a build window, run `pb.py clock set --start .. --end ..` "
              "before any code or commit.")
    if has_clock(event) and event.get("start") and event_phase(event)["phase"] == "before":
        print("note: the window has not opened. These are planning files; leave them uncommitted until it does.")
    print("next: fill .prod-build/contract.md, then write tasks into .prod-build/plan.json and run `pb.py plan check`.")
    return code


# ----------------------------------------------------------------------------- plan

def load_plan(root: Path) -> dict:
    plan = read_json(pb_dir(root) / "plan.json")
    if plan is None:
        die("no .prod-build/plan.json (run `pb.py init`)", 1)
    return plan


def save_plan(root: Path, plan: dict) -> None:
    write_json(pb_dir(root) / "plan.json", plan)


def task_map(plan: dict) -> dict:
    return {t.get("id"): t for t in plan.get("tasks", []) if isinstance(t, dict)}


def find_cycle(tasks: dict):
    color, stack = {}, []

    def visit(n):
        color[n] = 1
        stack.append(n)
        for d in tasks[n].get("deps", []) or []:
            if d not in tasks:
                continue
            if color.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if color.get(d) is None:
                r = visit(d)
                if r:
                    return r
        stack.pop()
        color[n] = 2
        return None

    for n in tasks:
        if color.get(n) is None:
            r = visit(n)
            if r:
                return r
    return None


def layers(tasks: dict) -> list:
    active = {k: v for k, v in tasks.items() if v.get("status") != "skip"}
    indeg = {k: 0 for k in active}
    for k, v in active.items():
        for d in v.get("deps", []) or []:
            if d in active:
                indeg[k] += 1
    out, ready = [], sorted(k for k, n in indeg.items() if n == 0)
    seen = set()
    while ready:
        out.append(ready)
        seen.update(ready)
        nxt = []
        for k, v in active.items():
            if k in seen or k in nxt:
                continue
            if all(d in seen or d not in active for d in (v.get("deps", []) or [])):
                nxt.append(k)
        ready = sorted(nxt)
    return out


def conflict_reasons(a: dict, b: dict) -> list:
    reasons = []
    wa, wb = a.get("writes", []) or [], b.get("writes", []) or []
    for x in wa:
        for y in wb:
            if paths_overlap(x, y):
                reasons.append(f"both write {x if len(x) <= len(y) else y}")
                break
        if reasons:
            break
    hot_a = [x for x in wa if HOT_FILES.search(norm_path(x))]
    hot_b = [y for y in wb if HOT_FILES.search(norm_path(y))]
    if hot_a and hot_b and not reasons:
        reasons.append(f"both touch shared config/lockfile/migrations ({hot_a[0]}, {hot_b[0]})")
    ra, rb = a.get("runtime") or {}, b.get("runtime") or {}
    ports = set(ra.get("ports", []) or []) & set(rb.get("ports", []) or [])
    if ports:
        reasons.append(f"same port {sorted(ports)}")
    if ra.get("db") and ra.get("db") == rb.get("db") and ra.get("db") not in ("none", "in-memory"):
        reasons.append(f"same database '{ra.get('db')}'")
    if "migration" in (a.get("risk") or []) and "migration" in (b.get("risk") or []):
        reasons.append("both change the schema")
    return reasons


def lanes(ids: list, tasks: dict) -> list:
    """Greedy grouping of ids into lanes that may run in parallel with each other."""
    groups = []
    for i in ids:
        placed = False
        for g in groups:
            if not any(conflict_reasons(tasks[i], tasks[j]) for j in g):
                g.append(i)
                placed = True
                break
        if not placed:
            groups.append([i])
    return groups


def check_plan(plan: dict) -> tuple:
    errors, warns = [], []
    mode = plan.get("mode")
    if mode not in MODES:
        errors.append(f"mode must be one of {MODES}, got {mode!r}")
    tasks_list = plan.get("tasks")
    if not isinstance(tasks_list, list):
        return [*errors, "tasks must be a list"], warns
    seen = set()
    for t in tasks_list:
        if not isinstance(t, dict):
            errors.append("every task must be an object")
            continue
        tid = t.get("id", "?")
        if not re.match(r"^T\d{2,3}[a-z]?$", str(tid)):
            errors.append(f"{tid}: id must look like T01, T12 or T07a")
        if tid in seen:
            errors.append(f"{tid}: duplicate id")
        seen.add(tid)
        for field in ("title", "objective"):
            if not str(t.get(field, "")).strip():
                errors.append(f"{tid}: missing {field}")
        if t.get("status", "todo") not in STATUSES:
            errors.append(f"{tid}: status must be one of {STATUSES}")
        if not t.get("verify"):
            errors.append(f"{tid}: no verify commands (how will anyone know it works?)")
        if not t.get("acceptance"):
            errors.append(f"{tid}: no acceptance criteria")
        owner = str(t.get("owner") or "agent").strip().lower()
        if owner not in ("agent", "builder"):
            warns.append(f"{tid}: owner is \"builder\" (a step only the person can take) or \"agent\", not {owner!r}")
        if t.get("due") is not None:
            d = instant_or_none(str(t.get("due")))
            if d is None or d.tzinfo is None:
                errors.append(f"{tid}: due {t.get('due')!r} is not an instant with a zone (2026-10-29T12:00:00Z)")
        elif owner == "builder" and t.get("status", "todo") not in ("done", "skip"):
            warns.append(f"{tid}: a builder task with no \"due\" (a UTC instant): `pb.py status` cannot say when it "
                         "is late")
        if not t.get("writes"):
            if owner != "builder" and not re.search(r"(?i)review|verify|audit|deploy|smoke|qa|gate|record|submit",
                                                    f"{t.get('role', '')} {t.get('kind', '')}"):
                warns.append(f"{tid}: empty write surface (set \"kind\": \"review\"|\"deploy\"|\"qa\"|\"record\"|"
                             "\"submit\" if it writes no files)")
        elif len(t.get("writes")) > 15:
            warns.append(f"{tid}: writes {len(t['writes'])} paths; split it (one intent, <=15 files)")
        if not str(t.get("rollback", "")).strip():
            (errors if mode == "product" and "migration" in (t.get("risk") or []) else warns).append(
                f"{tid}: no rollback note")
        if t.get("size") and t.get("size") not in ("S", "M", "L"):
            warns.append(f"{tid}: size should be S, M or L")
        if t.get("size") == "L" and mode == "product":
            warns.append(f"{tid}: size L in product mode; consider splitting")
        if t.get("status") == "skip" and not str(t.get("notes", "")).strip():
            errors.append(f"{tid}: skipped without a reason in notes")
    tasks = task_map(plan)
    if tasks and not suite_items(plan):
        warns.append("no top-level \"suite\" (the full test command): `pb.py verify` can't run the whole suite per task")
    for tid, t in tasks.items():
        for d in t.get("deps", []) or []:
            if d not in tasks:
                errors.append(f"{tid}: depends on unknown task {d}")
    cyc = find_cycle(tasks)
    if cyc:
        errors.append("dependency cycle: " + " -> ".join(cyc))
    active = [t for t in tasks_list if isinstance(t, dict) and t.get("status") != "skip"]
    if len(active) > 12 and not plan.get("milestones"):
        warns.append(f"{len(active)} tasks and no milestones; group into milestones of 3-12 tasks")
    if not cyc and plan.get("parallel", True) is not False:
        for layer in layers(tasks):
            for i, a in enumerate(layer):
                for b in layer[i + 1:]:
                    r = conflict_reasons(tasks[a], tasks[b])
                    if r:
                        warns.append(f"{a} and {b} are unordered but conflict ({'; '.join(r)}): "
                                     "they will run one after the other unless isolated")
    return errors, warns


def check_briefs(root: Path, plan: dict) -> tuple:
    """Each brief must be the brief for ITS task: id + title, the verify path, the report block, a BASE line."""
    errors, warns = [], []
    bdir = pb_dir(root) / "briefs"
    if not bdir.is_dir():
        return errors, warns
    tasks = task_map(plan)
    bodies = {}
    for f in sorted(bdir.glob("*.md")):
        m = re.match(r"^(T\d{2,3}[a-z]?)\b", f.stem)
        if not m or m.group(1) not in tasks:
            continue
        tid, t = m.group(1), tasks[m.group(1)]
        text = f.read_text(encoding="utf-8")
        head = "\n".join(text.splitlines()[:3])
        if tid not in head:
            errors.append(f"briefs/{f.name}: heading doesn't name {tid}")
        if t.get("title") and words(t["title"]) and len(words(t["title"]) & words(text)) < min(2, len(words(t["title"]))):
            warns.append(f"briefs/{f.name}: doesn't mention its task title '{t['title']}'")
        runs_verify = re.search(rf"pb\.py\s+verify\s+{tid}\b", text)
        missing = [v for v in (t.get("verify") or []) if not v.lower().startswith(("observe:", "manual:"))
                   and norm_cmd(v) not in norm_cmd(text)]
        if not runs_verify and missing:
            errors.append(f"briefs/{f.name}: neither `pb.py verify {tid}` nor its verify command(s): " + "; ".join(missing[:3]))
        if "STATUS:" not in text:
            errors.append(f"briefs/{f.name}: no report block (STATUS: …)")
        if not re.search(r"(?i)\bBASE\b", text):
            warns.append(f"briefs/{f.name}: no BASE commit line")
        other = [o for o in re.findall(r"\bT\d{2,3}[a-z]?\b", text) if o != tid and o in tasks]
        if other.count(max(set(other), key=other.count) if other else "") > text.count(tid):
            warns.append(f"briefs/{f.name}: mentions {max(set(other), key=other.count)} more often than {tid}: copied brief?")
        body = re.sub(r"\bT\d{2,3}[a-z]?\b", "Txx", text)
        if body in bodies:
            errors.append(f"briefs/{f.name}: same content as briefs/{bodies[body]}")
        bodies[body] = f.name
    return errors, warns


def cmd_plan(args, root: Path) -> int:
    plan = load_plan(root)
    tasks = task_map(plan)
    if args.action == "check":
        errors, warns = check_plan(plan)
        be, bw = check_briefs(root, plan)
        errors, warns = errors + be, warns + bw
        ev = load_event(root)
        if plan.get("mode") == "hackathon" and not has_clock(ev):
            warns.append("hackathon plan with no event clock: `pb.py clock set --start .. --end .. --source ..` "
                         "(no task will start without it)")
        if ev and ev.get("provisional"):
            warns.append(f"the event clock is provisional ({ev['provisional']}): re-read the instants from the "
                         "event page and confirm them with `pb.py clock set --start .. --end .. --confirmed`")
        if ev and ev.get("start") and tasks:
            kinds = {str(t.get("kind") or "").lower() for t in tasks.values() if t.get("status") != "skip"}
            if "record" not in kinds:
                warns.append("no task of kind \"record\": schedule two, a backup take for the moment the end-to-end "
                             "path first works and the final take once the build is frozen")
            if "submit" not in kinds:
                warns.append("no task of kind \"submit\": the page, the form answers and `pb.py submit check`")
        if ev and ev.get("post_entry") and tasks:
            named = " ".join(f"{t.get('title', '')} {t.get('objective', '')}" for t in tasks.values())
            missing = [r for r in POST_ENTRY_TASKS if not re.search(rf"\b{r}\b", named)]
            if missing:
                warns.append("the post is this event's entry, and no task names " + ", ".join(missing)
                             + " (playbook R103-R112, scheduled in SKILL.md Step 4: put the rule's id in the task's "
                             "title; a rule that does not apply is a task skipped with its reason)")
        for w in warns:
            print(f"WARN  {w}")
        for e in errors:
            print(f"ERROR {e}")
        print(f"plan check: {len(errors)} error(s), {len(warns)} warning(s), {len(tasks)} task(s)")
        return 1 if errors or (args.strict and warns) else 0
    if find_cycle(tasks):
        die("plan has a dependency cycle; run `pb.py plan check`", 1)
    serial = plan.get("parallel", True) is False
    if args.action == "waves":
        for n, layer in enumerate(layers(tasks), 1):
            groups = [[i] for i in layer] if serial else lanes(layer, tasks)
            desc = "  then  ".join(" || ".join(g) for g in groups)
            print(f"wave {n}: {desc}")
        path = critical_path(tasks)
        if path:
            print("critical path: " + " -> ".join(path))
        return 0
    if args.action == "next":
        ready = [k for k, t in tasks.items() if t.get("status", "todo") == "todo"
                 and all(tasks.get(d, {}).get("status") in ("done", "skip") for d in (t.get("deps") or []))]
        doing = [k for k, t in tasks.items() if t.get("status") == "doing"]
        if doing:
            print("in progress (re-check exit criteria before trusting): " + ", ".join(sorted(doing)))
        if not ready:
            print("no ready tasks" + ("" if doing else " (all done, blocked, or waiting on deps)"))
            return 0
        groups = [[i] for i in sorted(ready)] if serial else lanes(sorted(ready), tasks)
        print("ready now. Tasks on one line may run in parallel; run the lines one after another:")
        for g in groups:
            print("  " + " || ".join(f"{i} {tasks[i].get('title', '')}" + (
                " (the builder's own step: remind him, do not do it)"
                if str(tasks[i].get("owner") or "").strip().lower() == "builder" else "") for i in g))
        return 0
    return 2


def critical_path(tasks: dict) -> list:
    weight = {"S": 1, "M": 2, "L": 3}
    order = [i for layer in layers(tasks) for i in layer]
    best, prev = {}, {}
    for i in order:
        w = weight.get(tasks[i].get("size"), 1)
        cands = [(best[d], d) for d in (tasks[i].get("deps") or []) if d in best]
        if cands:
            b, d = max(cands)
            best[i], prev[i] = b + w, d
        else:
            best[i] = w
    if not best:
        return []
    end = max(best, key=lambda k: best[k])
    path = [end]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    return list(reversed(path))


# ----------------------------------------------------------------------------- evidence ledger

def ledger_path(root: Path) -> Path:
    """The canonical ledger, or a local one (PB_LEDGER) for parallel lanes and relay clones."""
    alt = os.environ.get("PB_LEDGER")
    if alt:
        p = Path(alt)
        return p if p.is_absolute() else root / p
    return pb_dir(root) / "evidence.jsonl"


def read_ledger(root: Path) -> list:
    p = ledger_path(root)
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"id": f"?line{n}", "_corrupt": True})
    return out


def record_hash(rec: dict) -> str:
    body = {k: v for k, v in rec.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def verify_chain(entries: list) -> list:
    problems, prev = [], ""
    for e in entries:
        if e.get("_corrupt"):
            problems.append(f"{e['id']}: unparseable ledger line")
            continue
        if e.get("prev", "") != prev:
            problems.append(f"{e.get('id')}: chain broken (entry edited, removed or hand-written)")
        if record_hash(e) != e.get("hash"):
            problems.append(f"{e.get('id')}: hash mismatch (entry edited by hand)")
        prev = e.get("hash", "")
    return problems


def append_evidence(root: Path, rec: dict) -> dict:
    pb_dir(root).mkdir(parents=True, exist_ok=True)
    entries = read_ledger(root)
    nums = [int(e["id"][1:]) for e in entries if re.match(r"^E\d+$", str(e.get("id", "")))]
    rec["id"] = f"E{(max(nums) + 1 if nums else 1):04d}"
    rec["ts"] = now_iso()
    rec["agent"] = rec.get("agent") or os.environ.get("PB_AGENT", "unknown")
    rec["prev"] = entries[-1].get("hash", "") if entries else ""
    rec["hash"] = record_hash(rec)
    with ledger_path(root).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def shell_command(parts: list) -> list:
    cmd = parts[0] if len(parts) == 1 else (
        subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts))
    if os.name != "nt" and shutil.which("bash"):
        return ["bash", "-o", "pipefail", "-c", cmd], cmd
    return cmd, cmd


def cmd_run(args, root: Path) -> int:
    parts = list(args.command or [])
    if parts and parts[0] == "--":
        parts = parts[1:]
    if not parts:
        die("usage: pb.py run [--task ID] [--kind K] -- <command>")
    runnable, display = shell_command(parts)
    if os.name == "nt" and "|" in display:
        print("pb: warning: cmd.exe has no pipefail; a pipe can hide the real exit code", file=sys.stderr)
    logs = pb_dir(root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    start = time.time()
    try:
        proc = subprocess.run(runnable, cwd=root, shell=isinstance(runnable, str), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=args.timeout)
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        code = 124
        output = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")) \
            + f"\n[pb] timed out after {args.timeout}s"
    dur = round(time.time() - start, 2)
    clean = redact(output)
    tail = clean.splitlines()[-args.tail:] if args.tail > 0 else []
    expect = "fail" if args.expect_fail else "pass"
    ok = (code != 0) if args.expect_fail else (code == 0)
    kind = args.kind or ("red" if args.expect_fail else "other")
    rec = append_evidence(root, {
        "task": args.task, "kind": kind, "method": "ran", "cmd": redact(display),
        "cwd": ".", "exit": code, "expect": expect, "ok": ok, "duration_s": dur,
        "git": git_info(root), "tree": tree_hash(root), "where": args.where, "url": args.url, "tail": tail[-20:],
    })
    log = logs / f"{rec['id']}.log"
    log.write_text(f"$ {redact(display)}\n# exit {code} in {dur}s at {rec['ts']}\n\n{clean}", encoding="utf-8")
    for line in tail:
        print(line)
    verdict = "PASS" if ok else "FAIL"
    extra = " (expected failure)" if args.expect_fail else ""
    print(f"[{rec['id']}] {verdict}{extra} exit={code} {dur}s  {rec['cmd']}  -> .prod-build/logs/{log.name}")
    if args.expect_fail:
        return 0 if ok else 1  # RED recorded as expected -> success; an unexpected pass is the failure
    return code


def cmd_evidence(args, root: Path) -> int:
    if args.action == "redact":
        if not args.id or not args.pattern or not args.reason:
            die("usage: pb.py evidence redact --id E0012 --pattern '<regex>' --reason '<why>'")
        entries = read_ledger(root)
        broken = verify_chain(entries)
        if broken:
            die("ledger already fails its chain check (" + broken[0] + "); investigate before redacting", 1)
        rx = re.compile(args.pattern)
        target = next((e for e in entries if e.get("id") == args.id), None)
        if not target:
            die(f"unknown evidence id {args.id}", 1)
        old_hash = target.get("hash", "")
        before = json.dumps(target, sort_keys=True)
        for k in ("cmd", "summary", "url"):
            if isinstance(target.get(k), str):
                target[k] = rx.sub("[REDACTED]", target[k])
        if isinstance(target.get("tail"), list):
            target["tail"] = [rx.sub("[REDACTED]", x) for x in target["tail"]]
        if json.dumps(target, sort_keys=True) == before:
            die(f"pattern not found in {args.id}", 1)
        idx = entries.index(target)
        prev = entries[idx - 1]["hash"] if idx > 0 else ""
        for e in entries[idx:]:
            e["prev"] = prev
            e["hash"] = record_hash(e)
            prev = e["hash"]
        ledger_path(root).write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
        log = pb_dir(root) / "logs" / f"{args.id}.log"
        if log.exists():
            log.write_text(rx.sub("[REDACTED]", log.read_text(encoding="utf-8")), encoding="utf-8")
        rec = append_evidence(root, {"task": target.get("task"), "kind": "manual", "method": "observed",
                                     "cmd": None, "summary": f"redacted {args.id} (old hash {old_hash[:16]}): {args.reason}",
                                     "exit": None,
                                     "expect": "pass", "ok": True, "git": git_info(root), "where": "local"})
        print(f"redacted {args.id} and re-chained the ledger; audit entry {rec['id']}. Rotate the secret: "
              "history already committed elsewhere still has it.")
        return 0
    if args.action == "add":
        if not args.summary:
            die("evidence add needs --summary")
        rec = append_evidence(root, {
            "task": args.task, "kind": args.kind or "manual", "method": args.method, "cmd": None,
            "summary": redact(args.summary), "exit": None, "expect": "pass", "ok": not args.failed,
            "git": git_info(root), "tree": tree_hash(root), "where": args.where, "url": args.url, "files": args.file or [],
        })
        print(f"[{rec['id']}] recorded {args.method} evidence: {rec['summary']}")
        return 0
    entries = read_ledger(root)
    if args.task:
        entries = [e for e in entries if e.get("task") == args.task]
    if args.failed:
        entries = [e for e in entries if not e.get("ok")]
    for e in entries[-args.limit:]:
        what = e.get("cmd") or e.get("summary") or ""
        print(f"{e.get('id')} {('ok ' if e.get('ok') else 'FAIL'):4} {e.get('method', ''):8} "
              f"{str(e.get('task') or '-'):5} {e.get('kind', ''):9} {e.get('where', '')}  {what[:90]}")
    problems = verify_chain(read_ledger(root))
    for p in problems:
        print(f"LEDGER {p}")
    return 1 if problems else 0


# ----------------------------------------------------------------------------- smoke

class _Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("src"):
            self.urls.append(a["src"])
        elif tag == "link" and a.get("href") and any(
                r in (a.get("rel") or "").lower() for r in ("stylesheet", "icon", "preload", "modulepreload", "manifest")):
            self.urls.append(a["href"])
        elif tag == "img" and a.get("src") and not a["src"].startswith("data:"):
            self.urls.append(a["src"])


ERROR_MARKERS = ("Application error: a client-side exception", "Internal Server Error",
                 "This page could not be found", "An unexpected error has occurred",
                 "Traceback (most recent call last)", "FUNCTION_INVOCATION_FAILED", "DEPLOYMENT_NOT_FOUND")


def fetch(url: str, timeout: float, method: str = "GET"):
    req = urllib.request.Request(net_map(url), method=method, headers={"User-Agent": "prod-build-smoke/1.0"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(2_000_000) if method == "GET" else b""
            return r.status, body, round(time.time() - t, 2), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read(200_000) if method == "GET" else b"", round(time.time() - t, 2), ""
    except Exception as e:  # noqa: BLE001 - report any network failure as a finding
        return None, str(e).encode(), round(time.time() - t, 2), ""


def cmd_smoke(args, root: Path) -> int:
    base = args.url.rstrip("/")
    findings, lines = [], []
    for route in args.routes:
        url = base + (route if route.startswith("/") else "/" + route)
        status, body, secs, ctype = fetch(url, args.timeout)
        lines.append(f"{status} {secs:>5}s {url}")
        if status is None or status >= 400:
            findings.append(f"{url} -> {status or body.decode('utf-8', 'replace')[:120]}")
            continue
        if secs > args.slow:
            findings.append(f"{url} slow: {secs}s (cold start? warm it before a demo)")
        text = body.decode("utf-8", "replace")
        for marker in ERROR_MARKERS:
            if marker in text:
                findings.append(f"{url} page contains '{marker}'")
        if "html" in ctype or text.lstrip().lower().startswith("<!doctype html"):
            p = _Assets()
            try:
                p.feed(text)
            except Exception:  # noqa: BLE001
                pass
            for ref in dict.fromkeys(p.urls):
                aurl = urllib.parse.urljoin(url, ref)
                if urllib.parse.urlparse(aurl).netloc != urllib.parse.urlparse(base).netloc and not args.external:
                    continue
                st, _, _, _ = fetch(aurl, args.timeout, "HEAD")
                if st in (405, 501):
                    st, _, _, _ = fetch(aurl, args.timeout)
                if st is None or st >= 400:
                    findings.append(f"asset {aurl} -> {st}")
    for l in lines:
        print(l)
    for f in findings:
        print(f"FINDING {f}")
    ok = not findings
    rec = append_evidence(root, {
        "task": args.task, "kind": "smoke", "method": "ran",
        "cmd": f"pb.py smoke {args.url} --routes " + " ".join(args.routes),
        "exit": 0 if ok else 1, "expect": "pass", "ok": ok, "git": git_info(root), "tree": tree_hash(root), "where": args.where,
        "url": base, "tail": (lines + [f"FINDING {f}" for f in findings])[-20:],
    })
    print(f"[{rec['id']}] {'PASS' if ok else 'FAIL'} smoke {base} ({len(args.routes)} route(s), {len(findings)} finding(s)). "
          "Console errors need a browser check; record it with `pb.py evidence add --method observed`.")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- tasks

def norm_cmd(cmd: str) -> str:
    """Normalise a command for matching: squash spaces and unify the pb.py spelling. VAR=val prefixes are
    significant (a run that only passes with an extra variable is a different check), so they are kept."""
    c = re.sub(r"\s+", " ", redact(cmd or "")).strip()
    c = re.sub(r"(^|&&\s*|;\s*)(?:python3?|py)\s+(?:\./)?(?:\.prod-build/|\S*/)?pb\.py\b", r"\1pb.py", c)
    return c.strip("'\" ")


def pass_evidence(e: dict) -> bool:
    """Evidence that shows something WORKS: passed, expected to pass, and ran or was observed."""
    return bool(e.get("ok")) and e.get("expect", "pass") == "pass" and e.get("method") in ("ran", "observed")


def suite_items(plan: dict) -> list:
    s = plan.get("suite") or []
    return [s] if isinstance(s, str) else list(s)


def verify_items(t: dict, plan: dict) -> list:
    items = list(t.get("verify") or [])
    if t.get("suite", True) is not False:
        items += [x for x in suite_items(plan) if norm_cmd(x) not in {norm_cmd(i) for i in items}]
    return items


def evidence_num(e: dict) -> int:
    m = re.match(r"^E(\d+)$", str(e.get("id", "")))
    return int(m.group(1)) if m else 0


def words(text: str) -> set:
    stop = {"with", "that", "this", "from", "into", "have", "been", "were", "they", "then", "than", "when", "what"}
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 4 and w not in stop}


def match_observations(items: list, observed: list) -> dict:
    """Assign each observe: item its own observed entry (best word overlap, each entry used once)."""
    out, used = {}, set()
    for item in items:
        want = words(item.split(":", 1)[1] if ":" in item else item)
        best, best_score = None, -1
        for e in reversed(observed):
            if e["id"] in used:
                continue
            score = len(want & words(e.get("summary", "")))
            if score > best_score:
                best, best_score = e, score
        need = 1 if len(want) <= 2 else 2
        if best is not None and (best_score >= need or not want):
            out[item] = best
            used.add(best["id"])
    return out


def verify_status(t: dict, entries: list, plan: dict = None) -> tuple:
    """For each verify item: (item, state, latest_entry) with state pass | fail | missing, from the LATEST run
    recorded since the task was (re)started."""
    out = []
    since = int(t.get("started_after") or 0)
    mine = [e for e in entries if e.get("task") == t.get("id") and e.get("expect", "pass") == "pass"
            and evidence_num(e) > since]
    items = verify_items(t, plan or {})
    observe = [v for v in items if v.strip().lower().startswith(("observe:", "manual:"))]
    matched = match_observations(observe, [e for e in mine if e.get("method") == "observed"])
    for v in items:
        if v in observe:
            e = matched.get(v)
            out.append((v, "missing" if not e else ("pass" if e.get("ok") else "fail"), e))
            continue
        runs = [e for e in mine if e.get("cmd") and norm_cmd(e["cmd"]) == norm_cmd(v)]
        out.append((v, "missing" if not runs else ("pass" if runs[-1].get("ok") else "fail"), runs[-1] if runs else None))
    return out


def cmd_task(args, root: Path) -> int:
    plan = load_plan(root)
    tasks = task_map(plan)
    t = tasks.get(args.id)
    if not t:
        die(f"unknown task {args.id}", 1)
    note = (args.note or "").strip()
    stamp = f"{today()} {args.action}" + (" [test clock]" if fake_now() else "")
    if args.action in ("skip", "block", "reopen") and not note:
        die(f"`task {args.id} {args.action}` needs --note with the reason")
    if args.action == "start":
        blockers = [d for d in (t.get("deps") or []) if tasks.get(d, {}).get("status") not in ("done", "skip")]
        if blockers and not args.force:
            die(f"{args.id} depends on unfinished {', '.join(blockers)} (use --force to override)", 1)
        if load_event(root):
            for line in ensure_ignores(root):       # a scaffold may have replaced .gitignore since the clock was set
                print(line)
        gate, override = event_gate_for_start(root, plan, t, args.force, note)
        if gate:
            print(gate)
        if override:
            note = override + note
            append_evidence(root, {"task": args.id, "kind": "manual", "method": "reported", "summary": note[:300],
                                   "ok": True, "where": "local", "git": git_info(root), "tree": tree_hash(root)})
        gi = git_info(root)
        if gi["dirty"]:
            print("WARN  uncommitted changes outside .prod-build/: commit the plan/briefs first so BASE is meaningful")
        t["status"] = "doing"
        t["base"] = gi["sha"]
        if not plan.get("base"):
            plan["base"] = gi["sha"]
        t["started_after"] = max([evidence_num(e) for e in read_ledger(root)] or [0])
        print(f"{args.id} BASE {gi['sha']} (put this in the brief; intake diffs from here). "
              f"Only evidence after E{t['started_after']:04d} counts toward done.")
    elif args.action == "done":
        entries = read_ledger(root)
        ledger = {e.get("id"): e for e in entries}
        ids = [x.strip() for x in (args.evidence or "").split(",") if x.strip()]
        missing = [i for i in ids if i not in ledger]
        if missing:
            die(f"unknown evidence id(s): {', '.join(missing)}", 1)
        for_task = [e for e in entries if e.get("task") == args.id]
        pool = [ledger[i] for i in ids] or for_task
        passing = [e for e in pool if pass_evidence(e)]
        states = verify_status(t, entries, plan)
        failing = [v for v, st, _ in states if st == "fail"]
        unmatched = [v for v, st, _ in states if st == "missing"]
        now_tree = tree_hash(root)
        stale = [f"{v} ({e['id']})" for v, st, e in states
                 if st == "pass" and e and e.get("tree") and now_tree and e["tree"] != now_tree
                 and not v.strip().lower().startswith(("observe:", "manual:"))]
        problems = []
        if stale:
            problems.append("code changed since the passing run of: " + "; ".join(stale) + f" (re-run `pb.py verify {args.id}`)")
        if not passing and not [e for _, st, e in states if st == "pass"]:
            problems.append("no passing evidence (RED/expected-failure runs don't count)")
        if failing:
            problems.append("latest run FAILED for: " + "; ".join(failing))
        if unmatched:
            problems.append("never run: " + "; ".join(unmatched) + f" (`pb.py verify {args.id}` runs every item exactly as written)")
        if problems and not args.force:
            die(f"{args.id} not done: " + " | ".join(problems) + ". Fix and re-run, or --force --note '<why>'.", 1)
        if args.force and not note:
            die("--force needs --note explaining why the evidence is missing")
        latest = [e["id"] for _, st, e in states if st == "pass" and e]
        t["status"] = "done"
        t["evidence"] = sorted(set(latest + [ledger[i]["id"] for i in ids if pass_evidence(ledger[i])]))
        if any(e.get("method") == "reported" for e in pool):
            print("WARN  some evidence is only 'reported' by an agent; re-run the checks yourself before release")
    elif args.action == "skip":
        t["status"] = "skip"
    elif args.action == "block":
        t["status"] = "blocked"
    elif args.action == "reopen":
        t["status"] = "todo"
    if note or args.force:
        t["notes"] = (str(t.get("notes", "")).rstrip() + f"\n[{stamp}] {note}").strip()
    save_plan(root, plan)
    print(f"{args.id} -> {t['status']}")
    return 0


def cmd_verify(args, root: Path) -> int:
    """Run every verify command of a task through the ledger, exactly as written in the plan."""
    plan = load_plan(root)
    t = task_map(plan).get(args.id)
    if not t:
        die(f"unknown task {args.id}", 1)
    worst = 0
    if not suite_items(plan):
        print("note: plan.json has no \"suite\" (the full test command); only the task's own checks will run")
    for v in verify_items(t, plan):
        if v.strip().lower().startswith(("observe:", "manual:")):
            print(f"SKIP  {v}  (record it with `pb.py evidence add --task {args.id} --method observed --summary ...`)")
            continue
        ns = argparse.Namespace(command=[v], task=args.id, kind=None, where=args.where, url=None,
                                expect_fail=False, timeout=args.timeout, tail=args.tail)
        code = cmd_run(ns, root)
        worst = worst or (1 if code else 0)
        if code and not args.keep_going:
            print(f"stopped at the first failure (use --keep-going to run the rest)")
            break
    return worst


# ----------------------------------------------------------------------------- diffcheck / intake

SKIP_DIRS = re.compile(r"(^|/)(node_modules|\.venv|venv|env|dist|build|\.next|\.nuxt|\.svelte-kit|coverage|"
                       r"__pycache__|\.pytest_cache|target|vendor|\.turbo|\.cache|\.git)/")


def untracked(root: Path) -> list:
    files = [f for f in git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines() if f.strip()]
    return [f for f in files if not SKIP_DIRS.search(f)]


def changed_files(root: Path, base: str) -> list:
    out = git(root, "diff", "--name-status", "--no-renames", base)
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0][0], norm_path(parts[-1])))
    for f in untracked(root):
        if not f.startswith(".prod-build/logs/"):
            rows.append(("A", norm_path(f)))
    return rows


def added_lines(root: Path, base: str) -> list:
    out = git(root, "diff", "--no-color", "-U0", "--no-renames", base).stdout
    rows, cur, ln = [], None, 0
    for line in out.splitlines():
        if line.startswith("+++ "):
            cur = norm_path(line[6:]) if line[4:6] == "b/" else None
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            ln = int(m.group(1)) if m else 0
        elif line.startswith("+") and cur:
            rows.append((cur, ln, line[1:]))
            ln += 1
    for f in untracked(root)[:2000]:
        p = root / f
        if f.startswith(".prod-build/") or not p.is_file() or p.stat().st_size > 1_000_000:
            continue
        try:
            for n, text in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                rows.append((norm_path(f), n, text))
        except (UnicodeDecodeError, OSError):
            continue
    return rows


def removed_assertions(root: Path, base: str) -> dict:
    out = git(root, "diff", "--no-color", "-U0", "--no-renames", base).stdout
    per, cur = {}, None
    for line in out.splitlines():
        if line.startswith("--- "):
            cur = norm_path(line[6:]) if line[4:6] == "a/" else None
        elif line.startswith("+++ "):
            continue
        elif cur and TEST_FILE.search(cur) and re.search(r"\b(expect|assert\w*|should)\b", line[1:] if line[:1] in "+-" else ""):
            if line.startswith("-"):
                per[cur] = per.get(cur, 0) + 1
            elif line.startswith("+"):
                per[cur] = per.get(cur, 0) - 1
    return {k: v for k, v in per.items() if v > 0}


ENV_FILE = re.compile(r"(^|/)\.env(\.[A-Za-z0-9_-]+)?$")
ENV_TEMPLATE = re.compile(r"\.(example|sample|template|dist|defaults)$")


def diff_findings(root: Path, base: str) -> list:
    if git(root, "rev-parse", "--verify", base + "^{commit}").returncode != 0:
        die(f"unknown git ref {base!r}", 1)
    found = []
    if not (root / ".gitignore").exists() and (root / "node_modules").is_dir():
        found.append(("warn", ".gitignore", 0, "missing .gitignore while node_modules/ exists: it could get committed"))
    for status, path in changed_files(root, base):
        if path.startswith(".prod-build/"):
            continue
        if status == "D" and TEST_FILE.search(path):
            found.append(("error", path, 0, "test file deleted"))
        elif CONFIG_FILE.search(path):
            found.append(("warn", path, 0, "lint/test/type/CI/agent config changed: justify it in the report"))
        if status != "D" and ENV_FILE.search(path) and not ENV_TEMPLATE.search(path):
            tracked = git(root, "ls-files", "--error-unmatch", path).returncode == 0
            if tracked:
                found.append(("error", path, 0, "env file is tracked by git: untrack it, add it to .gitignore, rotate its secrets"))
    for path, ln, text in added_lines(root, base):
        if path.startswith(".prod-build/"):
            continue  # the helper, plan, briefs and ledger are orchestration files, not product code
        if not path.startswith("docs/memory/"):
            for rx, sev, msg in WEAKENING:
                if re.search(rx, text):
                    found.append((sev, path, ln, msg))
        if not path.endswith((".lock", "lock.json", "lock.yaml")) and looks_like_secret(text):
            in_tests = bool(TEST_FILE.search(path) or re.search(r"(^|/)(fixtures?|__fixtures__|testdata)/", path))
            found.append(("warn" if in_tests else "error", path, ln,
                          "possible secret added" + (" in test code (use an obvious fake)" if in_tests else
                                                     " (rotate it if it is real)")))
        if TEST_FILE.search(path) and re.search(r"\b(expect|assert)\w*\s*\(\s*(true|1)\s*\)", text):
            found.append(("warn", path, ln, "tautological assertion"))
    for path, n in removed_assertions(root, base).items():
        found.append(("warn", path, 0, f"{n} more assertion line(s) removed than added"))
    old_pkg = git(root, "show", f"{base}:package.json")
    if old_pkg.returncode == 0 and (root / "package.json").exists():
        try:
            before = json.loads(old_pkg.stdout).get("scripts") or {}
            after = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
            for k, v in before.items():
                if re.match(r"^(test|lint|typecheck|type-check|check|verify|build|e2e)(:|$)", k) and after.get(k) != v:
                    found.append(("error", "package.json", 0,
                                  f"script '{k}' changed ({v!r} -> {after.get(k)!r}): a changed check can fake a pass"))
        except (json.JSONDecodeError, AttributeError):
            pass
    old_plan = git(root, "show", f"{base}:.prod-build/plan.json")
    if old_plan.returncode == 0 and (pb_dir(root) / "plan.json").exists():
        try:
            before = task_map(json.loads(old_plan.stdout))
            after = task_map(load_plan(root))
            for tid, t in before.items():
                a = after.get(tid)
                if a is None:
                    found.append(("error", ".prod-build/plan.json", 0, f"{tid} removed from the plan"))
                    continue
                for field in ("acceptance", "verify", "writes"):
                    if t.get(field) != a.get(field):
                        found.append(("warn", ".prod-build/plan.json", 0,
                                      f"{tid}.{field} changed since {base[:10]} (executors may only change status/notes)"))
        except json.JSONDecodeError:
            pass
    return found


def report_base(root: Path, tid: str):
    report = pb_dir(root) / "reports" / f"{tid}.md"
    if report.exists():
        m = re.search(r"(?im)^\s*COMMITS:\s*([0-9a-f]{6,40})\s*\.\.", report.read_text(encoding="utf-8"))
        if m and git(root, "rev-parse", "--verify", m.group(1) + "^{commit}").returncode == 0:
            return m.group(1)
    return None


def pick_base(root: Path, plan: dict, tid, explicit):
    if explicit:
        return explicit
    t = task_map(plan).get(tid) if tid else None
    return (t or {}).get("base") or (report_base(root, tid) if tid else None) or plan.get("base") or "HEAD"


def cmd_diffcheck(args, root: Path) -> int:
    plan = read_json(pb_dir(root) / "plan.json") or {}
    base = pick_base(root, plan, args.task, args.base)
    found = diff_findings(root, base)
    for sev, path, ln, msg in found:
        loc = f"{path}:{ln}" if ln else path
        print(f"{sev.upper():5} {loc}  {msg}")
    errors = sum(1 for f in found if f[0] == "error")
    print(f"diffcheck vs {base}: {errors} error(s), {len(found) - errors} warning(s)")
    return 1 if errors else 0


def cmd_intake(args, root: Path) -> int:
    plan = load_plan(root)
    t = task_map(plan).get(args.id)
    if not t:
        die(f"unknown task {args.id}", 1)
    base = pick_base(root, plan, args.id, args.base)
    problems, notes = [], []
    if base == "HEAD":
        problems.append(f"no BASE recorded for {args.id}: run `pb.py task {args.id} start` before dispatching, "
                        "or pass --base <commit the work started from>")
    report = pb_dir(root) / "reports" / f"{args.id}.md"
    status = None
    if report.exists():
        m = re.search(r"(?im)^\s*STATUS:\s*([A-Z_]+)", report.read_text(encoding="utf-8"))
        status = m.group(1) if m else None
        if not status:
            problems.append(f"{report.relative_to(root)} has no 'STATUS:' line")
    else:
        problems.append(f"no report at .prod-build/reports/{args.id}.md")
    if status in ("BLOCKED", "NEEDS_CONTEXT"):
        notes.append(f"agent reports {status}: answer the blocker before anything else")
    anc = git(root, "merge-base", "--is-ancestor", base, "HEAD")
    if anc.returncode == 1:
        problems.append(f"base {base} is not an ancestor of HEAD (work started from the wrong commit)")
    found = diff_findings(root, base)
    for sev, path, ln, msg in found:
        (problems if sev == "error" else notes).append(f"{path}{':' + str(ln) if ln else ''} {msg}")
    writes = t.get("writes") or []
    if writes:
        allowed = writes + [".prod-build/**", "docs/memory/**"]
        stray = [p for _, p in changed_files(root, base) if not any(paths_overlap(p, w) for w in allowed)]
        if stray:
            notes.append("changed outside the task's write surface: " + ", ".join(stray[:12]))
    ledger = read_ledger(root)
    problems += [f"ledger: {c}" for c in verify_chain(ledger)]
    states = verify_status(t, ledger, plan)
    if not any(pass_evidence(e) for e in ledger if e.get("task") == args.id):
        problems.append(f"no passing evidence recorded for {args.id} (RED runs don't count)")
    for v, st, _ in states:
        if st != "pass":
            notes.append(f"verify '{v}': {st}")
    for p in problems:
        print(f"PROBLEM {p}")
    for n in notes:
        print(f"NOTE    {n}")
    verdict = "REJECT" if problems else "RE-VERIFY"
    print(f"intake {args.id} vs {base}: {verdict}. Re-run the checks yourself (`pb.py verify {args.id}`) "
          f"before `pb.py task {args.id} done`.")
    return 1 if problems else 0


# ----------------------------------------------------------------------------- memory

def parse_record(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 4:]
    meta["_path"] = path
    meta["_body"] = body
    meta["_lines"] = text.count("\n") + 1
    return meta


def split_list(v: str) -> list:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def all_records(root: Path, include_archive: bool = True) -> list:
    m = mem_dir(root)
    recs = []
    folders = [f for _, f in MEM_TYPES.values()] + (["archive"] if include_archive else [])
    for folder in folders:
        d = m / folder
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                recs.append(parse_record(p))
    return recs


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or "record"


BODIES = {
    "decision": "## Context\n\n## Decision\n\n## Alternatives rejected (and why)\n\n## Consequences\n\n## Revisit when\n",
    "failure": ("## Attempted\n\n## Failure (exact error signature)\n\n## Root cause (verified, not guessed)\n\n"
                "## Fix (commit / evidence IDs)\n\n## Lesson\nWhen <situation>, do <action>, because <reason>.\n\n"
                "## Early check\n<the cheapest check that would have caught this>\n"),
    "lesson": "When <situation>, do <action>, because <reason>.\n\nDon't apply when: <boundary>\n\nSource: <F-id or URL (date)>\n",
    "assumption": "## Claim\n\n## Risk if wrong\n\n## Validate by\n\n## Status\nopen\n",
    "feedback": "## What the user said (quote)\n\n## Context\n\n## Decision\naccepted | rejected | pending\n\n## Implication (incl. do-not-propose)\n",
    "pattern": "## Task type\n\n## Approach / agent / prompt shape\n\n## Outcome\n\n## Use when\n",
}


def cmd_mem(args, root: Path) -> int:
    act = args.action
    if act == "add":
        if args.type not in MEM_TYPES:
            die(f"type must be one of {', '.join(MEM_TYPES)}")
        if not args.title:
            die("mem add needs --title")
        prefix, folder = MEM_TYPES[args.type]
        recs = all_records(root)
        nums = [int(r["id"].split("-")[-1]) for r in recs if str(r.get("id", "")).startswith(prefix + "-")
                and r["id"].split("-")[-1].isdigit()]
        rid = f"{prefix}-{(max(nums) + 1 if nums else 1):04d}"
        similar = [r for r in recs if r.get("type") == args.type and r.get("status") == "active"
                   and difflib.SequenceMatcher(None, r.get("title", "").lower(), args.title.lower()).ratio() >= 0.8]
        if similar and not args.force:
            die(f"similar active record exists: {similar[0]['id']} '{similar[0].get('title')}'. "
                "Update it (or mark it superseded) instead; --force to add anyway", 1)
        gi = git_info(root)
        meta = {
            "id": rid, "type": args.type, "title": args.title.replace("\n", " "), "status": "active",
            "scope": args.scope, "components": ", ".join(split_list(args.components)),
            "triggers": ", ".join(split_list(args.triggers)), "evidence": ", ".join(split_list(args.evidence)),
            "verified_at": f"{today()}@{gi['sha'] or 'nogit'}", "relates": ", ".join(split_list(args.relates)),
            "supersedes": args.supersedes or "", "helpful": "0", "harmful": "0",
            "cite_hash": cite_hash(root, split_list(args.evidence)),
            "created": today(), "source": args.source or os.environ.get("PB_AGENT", "unknown"),
        }
        d = mem_dir(root) / folder
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{rid}-{slugify(args.title)}.md"
        body = args.body.replace("\\n", "\n") if args.body else BODIES[args.type]
        path.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n\n" + body,
                        encoding="utf-8")
        if args.supersedes:
            mark(root, args.supersedes, "superseded", by=rid)
        write_index(root)
        print(f"created {path.relative_to(root)} ({rid}); fill in the body, keep it under {CAPS['record_lines']} lines")
        if not meta["evidence"]:
            print("WARN  no --evidence: cite a file (path#L10-40), commit:sha or test:cmd so it can be re-verified")
        return 0
    if act == "find":
        return mem_find(root, args)
    if act == "check":
        return mem_check(root)
    if act == "index":
        write_index(root)
        print("wrote docs/memory/INDEX.md")
        return 0
    if act == "mark":
        return mark(root, args.id, args.how, by=args.by)
    return 2


def citations(rec_or_list) -> list:
    """File citations as (path, anchor) pairs; skips commit:, test:, url:, evidence ids and the ledger itself."""
    raw = split_list(rec_or_list.get("evidence", "")) if isinstance(rec_or_list, dict) else rec_or_list
    out = []
    for c in raw:
        if re.match(r"^(commit|test|url|http|https):", c) or re.match(r"^E\d{4,}$", c):
            continue
        path, _, anchor = c.partition("#")
        path = norm_path(path)
        if path and not path.startswith(".prod-build/"):
            out.append((path, anchor))
    return out


def cited_paths(rec: dict) -> list:
    return [p for p, _ in citations(rec)]


def md_section(text: str, anchor: str):
    """Text of the markdown section whose heading matches the anchor (by text or slug), or None."""
    want = slugify(anchor)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and (slugify(m.group(2)) == want or m.group(2).strip().lower() == anchor.strip().lower()):
            level, body = len(m.group(1)), []
            for nxt in lines[i + 1:]:
                n = re.match(r"^(#{1,6})\s", nxt)
                if n and len(n.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body)
    return None


def cite_hash(root: Path, rec_or_list) -> str:
    h = hashlib.sha256()
    for path, anchor in citations(rec_or_list):
        f = root / path
        if not f.is_file():
            h.update(f"missing:{path}".encode())
            continue
        data = f.read_bytes()
        if anchor and path.lower().endswith(".md") and not re.match(r"^L\d", anchor):
            sec = md_section(data.decode("utf-8", "replace"), anchor)
            data = (sec if sec is not None else f"missing-section:{anchor}").encode()
        h.update(path.encode() + b"\0" + data)
    return h.hexdigest()[:16]


def staleness(root: Path, rec: dict) -> str:
    cites = citations(rec)
    if not cites:
        return "uncited"
    if any(not (root / p).exists() for p, _ in cites):
        return "dead-citation"
    for p, a in cites:
        if a and p.lower().endswith(".md") and not re.match(r"^L\d", a):
            if md_section((root / p).read_text(encoding="utf-8", errors="replace"), a) is None:
                return "dead-citation"
    if rec.get("cite_hash") and rec.get("cite_hash") != cite_hash(root, rec):
        return "needs-verify"
    return "fresh"


def mem_find(root: Path, args) -> int:
    paths = [norm_path(p) for p in split_list(args.paths)]
    terms = [t.lower() for t in split_list(args.terms)]
    segs = {s.lower() for p in paths for s in re.split(r"[/._\-]", p) if len(s) > 2}
    scored = []
    for r in all_records(root, include_archive=False):
        if r.get("status") != "active" or (args.type and r.get("type") != args.type):
            continue
        comps = {c.lower() for c in split_list(r.get("components", ""))}
        trig = " ".join(split_list(r.get("triggers", ""))).lower()
        text = (r.get("title", "") + " " + r["_body"]).lower()
        score = 3 * len(comps & segs)
        score += sum(3 for c in comps if any(c in p.lower() for p in paths))
        score += sum(2 for t in terms if t in trig)
        score += sum(min(text.count(t), 3) for t in terms)
        score += sum(2 for cp in cited_paths(r) for p in paths if paths_overlap(cp, p) or cp.startswith(p))
        if score >= args.min_score or (not paths and not terms):
            h, hm = int(r.get("helpful", "0") or 0), int(r.get("harmful", "0") or 0)
            scored.append((score + h - 2 * hm, r))
    scored.sort(key=lambda x: (-x[0], x[1].get("id", "")))
    if not scored:
        print("no matching memory")
        return 0
    for score, r in scored[: args.limit]:
        gist = next((l.strip() for l in r["_body"].splitlines()
                     if l.strip() and not l.startswith("#") and not l.startswith("<")), "")
        st = staleness(root, r)
        flag = "" if st == "fresh" else f" [{st}]"
        print(f"{r.get('id')} ({r.get('type')}, score {score}){flag} {r.get('title')}\n    {gist[:160]}\n    "
              f"{r['_path'].relative_to(root)}")
    return 0


def mem_check(root: Path) -> int:
    errors, warns = [], []
    recs = all_records(root)
    ids = {}
    for r in recs:
        rid = r.get("id") or r["_path"].name
        missing = [k for k in MEM_REQUIRED if k not in r]
        if missing:
            errors.append(f"{rid}: missing field(s) {', '.join(missing)}")
        if rid in ids:
            errors.append(f"{rid}: duplicate id ({ids[rid].name}, {r['_path'].name})")
        ids[rid] = r["_path"]
        if r.get("type") not in MEM_TYPES:
            errors.append(f"{rid}: unknown type {r.get('type')!r}")
        if r.get("status") not in MEM_STATUSES:
            errors.append(f"{rid}: unknown status {r.get('status')!r}")
        if r["_lines"] > CAPS["record_lines"]:
            warns.append(f"{rid}: {r['_lines']} lines (cap {CAPS['record_lines']}); condense it")
        if any(looks_like_secret(l) for l in r["_body"].splitlines()):
            errors.append(f"{rid}: looks like it contains a secret")
        if r.get("status") == "active":
            st = staleness(root, r)
            if st != "fresh":
                warns.append(f"{rid}: {st}" + (f" (re-read the cited code, then `pb.py mem mark {rid} verified`)"
                                                 if st == "needs-verify" else
                                                 (" (cite a file, path#L10-40 or ARCHITECTURE.md#Section)"
                                                  if st == "uncited" else "")))
            if int(r.get("harmful", "0") or 0) >= 2 and int(r.get("harmful", "0") or 0) > int(r.get("helpful", "0") or 0):
                warns.append(f"{rid}: marked harmful more than helpful; archive or rewrite it")
            if not r.get("cite_hash") and cited_paths(r):
                warns.append(f"{rid}: no content hash yet; after checking it, run `pb.py mem mark {rid} verified`")
            if "<situation>" in r["_body"] or "<reason>" in r["_body"]:
                warns.append(f"{rid}: template placeholders still in the body")
    for r in recs:
        for rel in split_list(r.get("relates", "")) + split_list(r.get("supersedes", "")):
            if rel not in ids:
                warns.append(f"{r.get('id')}: refers to unknown record {rel}")
    active = [r for r in recs if r.get("status") == "active"]
    if len(active) > CAPS["active"]:
        warns.append(f"{len(active)} active records (cap {CAPS['active']}); archive stale or merge similar ones")
    by_type = {}
    for r in active:
        by_type.setdefault(r.get("type"), []).append(r)
    for rs in by_type.values():
        for i, a in enumerate(rs):
            for b in rs[i + 1:]:
                if difflib.SequenceMatcher(None, a.get("title", "").lower(), b.get("title", "").lower()).ratio() >= 0.8:
                    warns.append(f"{a.get('id')} and {b.get('id')} look like duplicates; merge them")
    idx = mem_dir(root) / "INDEX.md"
    if idx.exists() and idx.read_text(encoding="utf-8").count("\n") > CAPS["index_lines"]:
        warns.append(f"INDEX.md over {CAPS['index_lines']} lines")
    for w in warns:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"mem check: {len(recs)} record(s), {len(active)} active, {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


def mark(root: Path, rid: str, how: str, by: str | None = None) -> int:
    target = next((r for r in all_records(root) if r.get("id") == rid), None)
    if not target:
        die(f"unknown memory record {rid}", 1)
    path = target["_path"]
    text = path.read_text(encoding="utf-8")

    def setf(t, key, val):
        if re.search(rf"(?m)^{key}:.*$", t[: t.find('\n---', 3) + 1]):
            return re.sub(rf"(?m)^{key}:.*$", f"{key}: {val}", t, count=1)
        return t.replace("\n---", f"\n{key}: {val}\n---", 1)

    if how in ("helpful", "harmful"):
        text = setf(text, how, str(int(target.get(how, "0") or 0) + 1))
    elif how == "verified":
        text = setf(text, "verified_at", f"{today()}@{git_info(root)['sha'] or 'nogit'}")
        text = setf(text, "cite_hash", cite_hash(root, target))
    elif how in ("superseded", "archived", "invalidated"):
        text = setf(text, "status", how)
        if by:
            text = setf(text, "superseded_by", by)
    else:
        die(f"unknown mark {how!r}")
    path.write_text(text, encoding="utf-8")
    if how in ("superseded", "archived", "invalidated"):
        dest = mem_dir(root) / "archive" / path.name
        path.replace(dest)
    write_index(root)
    print(f"{rid}: {how}" + (f" by {by}" if by else ""))
    return 0


def write_index(root: Path) -> None:
    m = mem_dir(root)
    m.mkdir(parents=True, exist_ok=True)
    recs = [r for r in all_records(root, include_archive=False) if r.get("status") == "active"]
    order = {t: i for i, t in enumerate(MEM_TYPES)}
    recs.sort(key=lambda r: (order.get(r.get("type"), 9), r.get("id", "")))
    archived = len(all_records(root)) - len(all_records(root, include_archive=False))
    rows = recs[: CAPS["index_lines"] - 12]
    lines = ["# Memory index", "",
             "Generated by `pb.py mem index`; do not edit by hand. Records are hints until their cited code is re-checked.",
             f"Active: {len(recs)} | archived/superseded: {archived} | search: `python3 .prod-build/pb.py mem find --paths .. --terms ..`",
             "", "| ID | Type | Components | Title | File |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r.get('id')} | {r.get('type')} | {r.get('components', '')} | {r.get('title', '')} | "
                     f"{r['_path'].relative_to(m).as_posix()} |")
    if len(recs) > len(rows):
        lines.append(f"\n{len(recs) - len(rows)} more active records: use `mem find`.")
    (m / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------- report

def cmd_report(args, root: Path) -> int:
    path = Path(args.file)
    if not path.is_absolute():
        path = (Path.cwd() / path) if (Path.cwd() / path).exists() else root / path
    if not path.exists():
        die(f"no report at {path}", 1)
    text = path.read_text(encoding="utf-8")
    ledger = {e.get("id"): e for e in read_ledger(root)}
    errors, warns = [], []
    sections, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^#{2,3}\s+(.*)$", line)
        if m:
            cur = m.group(1).strip().lower().replace("optimisation", "optimization")
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    for req in REPORT_SECTIONS:
        if not any(k.startswith(req) or req in k for k in sections):
            errors.append(f"missing section '{req}'")
    for name, lines in sections.items():
        if not any(name.startswith(c) for c in CLAIM_SECTIONS):
            continue
        for line in lines:
            if not re.match(r"^\s*([-*]|\d+[.)])\s+\S", line):
                continue
            ids = EVIDENCE_ID.findall(line)
            if not ids and name.startswith("bugs") and re.search(r"(?i)\bnone\b", line):
                warns.append(f"[{name}] 'none' without evidence: cite the bug-hunt/review evidence id")
                continue
            if not ids and not NOT_RUN.search(line):
                errors.append(f"[{name}] claim without evidence id or 'not run': {line.strip()[:110]}")
            if name.startswith("deployment") and ids and not NOT_RUN.search(line):
                if not any((ledger.get(i) or {}).get("where") in ("preview", "prod") for i in ids):
                    errors.append(f"[{name}] deployment claim cites only local evidence: {line.strip()[:90]}")
            for i in ids:
                e = ledger.get(i)
                if e and e.get("expect") == "fail" and not re.search(r"(?i)\b(red|fail\w*|expected)\b", line):
                    errors.append(f"[{name}] cites {i}, a RED (expected-failure) run, as if it proved a pass")
                    continue
                if not e:
                    errors.append(f"[{name}] cites {i}, which is not in the ledger")
                elif not e.get("ok") and not re.search(r"(?i)\b(fail\w*|red|expected|broke\w*|regress\w*)\b", line):
                    errors.append(f"[{name}] cites {i}, which did not pass: {line.strip()[:90]}")
                elif e.get("method") == "reported":
                    warns.append(f"[{name}] {i} is an agent-reported result, not re-run by the orchestrator")
    mem_ids = re.findall(r"\b(ADR|F|L|A|U|P)-\d{4}\b", "\n".join(sections.get("memory", [])))
    known = {r.get("id") for r in all_records(root)}
    for prefix_num in re.findall(r"\b(?:ADR|F|L|A|U|P)-\d{4}\b", "\n".join(
            l for k, v in sections.items() if k.startswith("memory") for l in v)):
        if prefix_num not in known:
            warns.append(f"[memory] {prefix_num} not found in docs/memory")
    if not mem_ids and not any("none" in l.lower() for k, v in sections.items() if k.startswith("memory") for l in v):
        warns.append("[memory] no memory IDs listed (write 'none' if nothing was worth keeping)")
    chain = verify_chain(list(read_ledger(root)))
    errors += [f"ledger: {c}" for c in chain]
    for w in warns:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"report check: {len(errors)} error(s), {len(warns)} warning(s)")
    return 1 if errors else 0


# ----------------------------------------------------------------------------- event clock, window, submission

EVENT_KEYS = ("name", "start", "end", "freeze", "freeze_set", "source", "provisional", "history", "no_window",
              "tz_min", "video_cap_s", "card_chars", "sponsors", "judge_minutes", "ai_disclosure", "research",
              "post_entry", "set_at", "changes")
# Where the post is the entry (prod-idea's writeup track), these playbook rules are plan tasks (SKILL.md Step 4).
# R109 is a choice, and R111 and R112 (a check and publish-day sharing) live inside the post task, so none
# needs a task of its own.
POST_ENTRY_TASKS = ("R103", "R104", "R105", "R106", "R107", "R108", "R110")
FREEZE_KINDS = ("fix", "bugfix", "hotfix", "verify", "qa", "review", "deploy", "docs", "submit", "record", "smoke",
                "test", "e2e", "lint", "typecheck", "security", "manual")
_NAME_DOC = r"\.(md|markdown|mdx|txt|pdf|docx?|rtf)"
_DIR_DOC = r"\.(md|markdown|mdx|txt|json|pdf|docx?|rtf|html?)"
INTERNAL_PATHS = re.compile(
    r"(?i)(^|/)(judging[-_ ]?(intel|notes?)[^/]*|idea[-_ ]?package[^/]*|judges?[-_ ]?(notes?|profiles?|intel)[^/]*)"
    + _NAME_DOC + r"$|(^|/)(notes/)?(hackathon|prod)[-_ ]?ideas?/([^/]+/)*[^/]+" + _DIR_DOC + r"$")
DRAFT_PATHS = re.compile(r"(?i)(^|/)(SHOOT|SCRIPT|VIDEO[-_ ]?SCRIPT|SUBMISSION|PITCH([-_ ]?NOTES?)?|DEMO[-_ ]?SCRIPT)"
                         r"\.(md|txt)$")
# A post draft at the root (devto-challenge's examples call it post.md). Only the root: docs/post.md may be content.
POST_DRAFT_PATHS = re.compile(r"(?i)^(devto[-_ ]?)?post([-_ ]?draft)?\.(md|markdown|txt)$")
# A first user's own material (their chats, recordings, screenshots): a root first-user/ folder, or one inside the
# idea package. Only those two places, so an app route such as app/first-user/ is not mistaken for it.
FIRST_USER_PATHS = re.compile(r"(?i)^((notes/)?(hackathon|prod)[-_ ]?ideas?/([^/]+/)*)?first[-_ ]?users?/")
INTERNAL_PHRASES = re.compile(
    r"(?i)\bjudging[- ]intel(ligence)?\b|\bworth (one|a) message\b|\bwhat (the )?judges (want|reward|look for)\b|"
    r"\bjudge (psychology|profile)s?\b")
GITIGNORE_BASE = ["node_modules/", ".env", ".env.*", "!.env.example", ".prod-build/logs/", "dist/", ".next/",
                  "__pycache__/", ".venv/", "coverage/", ".claude/worktrees/"]
IGNORE_BLOCK = """# prod-build: planning notes stay out of a judged repository
hackathon-idea/
prod-idea/
notes/hackathon-idea/
judging-intel*.md
judging-notes*.md
judge-notes*.md
idea-package*.md
idea_package*.md
SUBMISSION.md
submission.md
SHOOT.md
SCRIPT.md
PITCH.md
PITCH-NOTES.md
DEMO-SCRIPT.md
VIDEO-SCRIPT.md
video-script.md
# browser session files hold live sign-in cookies
storage-state*.json
storage_state*.json
*.storage-state.json
"""
POST_IGNORE_MARK = "prod-build: a post draft and a first user's material"
POST_IGNORE_BLOCK = f"""# {POST_IGNORE_MARK} stay out of the repository
/post.md
/post-draft.md
/devto-post.md
/first-user/
"""
SESSION_FILES = re.compile(r"(?i)(^|/)(storage[-_]?state[^/]*|[^/]*[.\-_]storage[-_]?state)\.json$")
MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*\bsrc=", re.I)
MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*([^)\s]*)[^)]*\)")
MD_LINK_FULL = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
BARE_URL = re.compile(r"https?://[^\s)>\]\"'`]+")
VIDEO_HOST = re.compile(r"(?i)(youtube\.com|youtu\.be|loom\.com|vimeo\.com|drive\.google\.com/file|\.mp4\b)")
CODE_HOST = re.compile(r"(?i)(github\.com|gitlab\.com|bitbucket\.org|shields\.io|badge|npmjs\.com|pypi\.org|"
                       r"localhost|127\.0\.0\.1|example\.(com|org|net)|creativecommons\.org|opensource\.org|"
                       r"wikipedia\.org|docs\.|/docs/|developer\.)")

# Unfinished text a judge would see. Every pattern runs on prose only: fenced blocks, indented code and
# `inline code` are removed first, because a curl sample or an env template is not a placeholder.
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
_LABEL = r"(?:demo\s+video|video|demo|live(?:\s+(?:demo|link|site|app))?|link|screenshots?|deck|slides|site|url)s?"
PLACEHOLDER_TARGET = re.compile(
    r"(?i)^(add[_ -]?(link|url)|link|url|todo|tbd|tba|insert[_ -]?(link|url)|link[-_ ]?here|"
    r"your[-_ ]?[a-z_ -]*(link|url|video|id)|video[-_ ]?(id|url|link)|demo[-_ ]?(url|link))$")
PLACEHOLDER_URL = re.compile(
    r"(?i)^https?://\S*?(\bexample\.(com|org|net)\b|\.example(?=[/:?#]|$)|\byour[-_]?(app|project|site|video|link|domain|"
    r"username|name)\b|x{5,}|VIDEO[_-]?ID|<[^>]*>|\{[^}]*\})")
EXAMPLE_URL = re.compile(r"(?i)https?://(www\.)?example\.(com|org|net)\b")
LOCAL_URL = re.compile(r"(?i)^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)\b")
LINKISH_TEXT = re.compile(r"(?i)\b(video|demo|live|watch|try|deck|slides|site|app|link|here|launch|open|blog|post)\b")
LIVE_TEXT = re.compile(r"(?i)\b(live|demo|try|deployed|production|launch)\b")
RX_PROMISED = re.compile(
    r"(?i)^[\s>*_\-+#|]*" + _LABEL + r"[\W_]{0,12}(link\s+)?(to follow|coming soon|to be added|to come|tbd|tba|"
    r"goes here|will be (added|uploaded|linked|recorded)|not yet (uploaded|recorded))\b")
RX_PROMISED_ANY = re.compile(
    r"(?i)\b(?:" + _LABEL + r"|recording|walkthrough)\b[^.\n]{0,40}\b(to follow|coming soon|to be added|to come|"
    r"will be (added|uploaded|linked|recorded)|not yet (uploaded|recorded))\b"
    r"|\b(links?|videos?|urls?)\s+(to follow|coming soon|to be added)\b")
RX_TOKEN = re.compile(r"\b(ADD_LINK|ADD_URL|INSERT_[A-Z_]+|REPLACE_ME|YOUR_[A-Z_]*(LINK|URL|VIDEO)[A-Z_]*|VIDEO_ID|"
                      r"VIDEO_URL|DEMO_URL|DEMO_LINK)\b|(?i:lorem ipsum)")
RX_BRACKET = re.compile(r"(?i:\[(link|url|tbd|tba|todo|video link|demo link|add link|insert link)\](?!\())|"
                        r"(?i:<\s*(add|insert|your|paste)[^>\n]{0,30}(link|url)\s*>)|<[A-Z0-9_ ]*(LINK|URL|_ID)>")
RX_SELFNOTE = re.compile(
    r"(?i)\bnote to self\b|\b(replace|remove|delete) this (line|section|text|paragraph)\b|"
    r"\b(todo\s*:|fixme\b|tbd\b|tba\b)[^\n]{0,60}\b(link|video|demo|url|screenshot)s?\b|"
    r"\b(link|video|demo|url|screenshot)s?\b[^\n]{0,40}\b(todo\s*:|fixme\b|tbd\b|tba\b)")
RX_COMMENT = re.compile(r"(?s)<!--(.{0,400}?)-->")
RX_COMMENT_NOTE = re.compile(
    r"(?i:\b(replace this|fill (this )?in|before submitting|note to self|add (the |a )?(link|video|screenshot|url)|"
    r"remove (this|before))\b)|\bTODO\b|\bFIXME\b|(?i:\btodo\s*:)")
RX_LABELLED_URL = re.compile(r"(?i)^[\s>*_\-+#|]*" + _LABEL + r"[\W_]{0,12}(https?://\S+)")
HTML_TAIL = re.compile(r"(?i)(</?(br|p|div|span|a|li|td|th|b|i|strong|em|sub|sup|center)\b[^>]*>)+$")
SOFT_PLACEHOLDERS = re.compile(r"\b(TODO|TBD|FIXME|WIP|YOUR_[A-Z_]+)\b|(?i:\bcoming soon\b)")
# Brackets and sentences left for the author ("[add the link once uploaded]", "[FILL FROM THE RUN: …]", "Delete the
# rows for stages that were not reached."). The same shapes as anti-ai-writing's gate: a bracket counts only when it
# opens with a fill verb or marker AND reads as an instruction (a colon, a placeholder noun after the verb, or several
# words in capitals), so "[Add to cart]", "click [Update Results]", "[Update, Oct 7: fixed]", "Press [ENTER]", "[3]",
# "[ ]", "[!NOTE]", "[^1]" and links never match.
FILL_START = re.compile(
    r"(add|insert|fill(\s+(in|out))?|paste|replace|update|include|link|attach|upload|embed|write"
    r"|enter|specify|confirm|verify|describe|provide|supply|cite|need|needs|todo|to\s?do|tbd|tba"
    r"|tbc|tk|fixme|xxx|placeholder|query|your)\b", re.I)
PURE_MARKER = re.compile(r"(todo|to\s?do|tbd|tba|tbc|tk|fixme|xxx|placeholder|link|url|link here)", re.I)
READER_LABEL = re.compile(r"(update[sd]?|note|n\.?\s?b\.?|edit(ed)?|editor'?s note|correction)\s*"
                          r"(\d[^:]*)?[:,]\s*", re.I)
FILL_NOUN = re.compile(
    r"\b(links?|urls?|video|screenshots?|image|numbers?|figures?|costs?|results?|run|account"
    r"|date|name|here|later|once|from|demo|slides|deck|presentation|repo|values?|amount|price"
    r"|totals?|metrics?|stats?|quote|citation|source|email|address|chart|logo|when|after"
    r"|until|before)\b", re.I)
FILL_BRACKET = re.compile(r"(?<![\]\\])\[(?![\^!])([^\[\]\n]{1,200})\](?![(\[])")
QUOTE_CHARS = "\"'“”‘’"
UI_BEFORE = re.compile(r"\b(click|clicking|clicked|press|pressing|tap|tapping|select|choose|hit)"
                       r"\s+(on\s+)?(the\s+)?[*_]{0,2}$", re.I)
UI_AFTER = re.compile(r"[*_]{0,2}\s+(button|menu|tab|icon|option|control|link)\b", re.I)
EDIT_VERB = (r"(delete|remove|replace|fill(\s+(this|these|it|them))?\s+(in|out)|update|add|insert|paste|complete|"
             r"cut|drop)\b")
SENTENCE_START = re.compile(r"(^[ \t]*(?:(?:[-*+>]|\d+[.)])[ \t]+)?|(?<=[.!?:])[ \t]+|(?<=\|)[ \t]*)"
                            r"(?=" + EDIT_VERB + ")", re.I)
AUTHOR_CONDITION = re.compile(
    r"\b(that|which|who|if|where|when)\s+(were|was|are|is|did|do|does|have|has|had)\s*(n[o']t|never)\s+"
    r"(been\s+|yet\s+)?(reached|attempted|measured|recorded|filled)\b"
    r"|\b(delete|remove)\s+(this|these)\s+(notes?|instructions?|placeholders?)\b"
    r"|\b(this|these)\s+(rows?|sections?|lines?|paragraphs?|columns?|cells?)\b.*\b(if|when)\s+"
    r"(not\s+applicable|n/?a|unused|irrelevant)\b"
    r"|\bfill\s+(this|these|it|them)?\s*(in|out)\s+(from|with|once|after|when)\s+(the\s+)?"
    r"(run|account|results|numbers|real|actual|final)\b", re.I)


def reads_as_fill(inner: str) -> bool:
    s = inner.strip()
    label = READER_LABEL.match(s)
    if label:
        return reads_as_fill(s[label.end():])
    if PURE_MARKER.fullmatch(s) and (s.isupper() or s.islower()):
        return True                      # [TBD], [todo], [link]; not a "[Todo]" tab label
    first = FILL_START.match(s)
    if not first:
        return False
    letters = [c for c in s if c.isalpha()]
    return (":" in s or bool(FILL_NOUN.search(s[first.end():]))
            or (len(s.split()) >= 2 and len(letters) >= 2 and all(c.isupper() for c in letters)))


def fill_bracket(line: str, defined=frozenset()):
    """The first bracket in a (code-free) line that a judge would read as a note to the author, or None."""
    for mo in FILL_BRACKET.finditer(line):
        inner, before = mo.group(1), line[:mo.start()]
        if line.startswith(":", mo.end()) and not before.strip():
            continue                     # a reference-link definition
        if inner.strip().lower() in defined:
            continue                     # a shortcut reference link
        if UI_BEFORE.search(before) or UI_AFTER.match(line, mo.end()):
            continue                     # "click [Add link]", "the [Add link] button"
        if (before and before[-1] in QUOTE_CHARS) or re.match(r"[.,;:!?…]?[" + QUOTE_CHARS + "]",
                                                              line[mo.end():mo.end() + 2]):
            continue                     # quoted as an example
        if reads_as_fill(inner):
            return mo.group(0)
    return None


def author_sentence(line: str):
    """A sentence in the line that tells the author to finish the document, or None."""
    for mo in SENTENCE_START.finditer(line):
        rest = line[mo.end():mo.end() + 300]
        end = re.search(r"[.!?](\s|$)|\|", rest)
        sentence = rest[:end.start() + 1] if end else rest
        if AUTHOR_CONDITION.search(sentence):
            return sentence.strip()
    return None
AI_TOOLS = re.compile(r"(?i)\b(claude|copilot|cursor|chatgpt|codex|gemini|windsurf|aider|kiro|amazon q|"
                      r"ai (coding )?(tools?|assistants?|agents?)|co-authored-by)\b")
LIMITS_HEAD = re.compile(r"(?im)^\s*(#{1,6}\s+|\*\*)[^\n]{0,60}\b(limits?|limitations?|not built|not yet|honest|"
                         r"scope|simulated|mocked|what is real|what's real|known issues?|what it does(n'| no)t)\b")
# sections a reader meets as "what this does not show": limits, refusals, corrections, an honesty essay
CAVEAT_HEAD = re.compile(r"(?i)\b(limits?|limitations?|caveats?|refus\w*|(does|do|can)(n'?t| not) (claim|show|prove|settle)|"
                         r"not (claim|built)|threats? to validity|corrections?|errata|honest\w*|what (it|this|we) "
                         r"(can(no|')t|does(n'| no)t|won'?t)|what (failed|went wrong|broke|did ?n[o']t work)|"
                         r"failures?|known (issues?|bugs?|defects?|problems?)|post-?mortem|mistakes)\b")
# "How it catches and recovers from failures" answers a rubric line; it is not a caveat section
RUBRIC_ANSWER = re.compile(r"(?i)\b(catch\w*|caught|recover\w*|detect\w*|handl\w*|prevent\w*|toleran\w*|resilien\w*)\b")
CHANGELOG_HEAD = re.compile(r"(?i)\b(changelog|change log|revisions?|corrections?|errata)\b")
REPRO = re.compile(r"(?m)^\s*(\$\s*)?(make|python3?|uv|npm|pnpm|node|bash|sh|cargo|go run|docker|just)\b[^\n]*$")
WINDOW_LINE = re.compile(r"(?i)(first commit|built (during|inside|within|between)\b|built in (the )?(\d+|a |one |two |"
                         r"three )|build window|hackathon window|hacking began|before the (clock|start|window)\b|"
                         r"(after|when) the (clock|window|hackathon) (started|opened))")
README_NAME = re.compile(r"(?i)^readme(\.(md|markdown|mdown|mkd|mdx|rst|txt|org|adoc|asciidoc|textile))?$")


def instant_or_none(s: str):
    """ISO-8601 text -> datetime (aware when the text carried a zone), or None when it does not parse."""
    if not isinstance(s, str):
        return None
    txt = re.sub(r"[zZ]$", "+00:00", s.strip())
    txt = re.sub(r"^(\d{4}-\d{2}-\d{2})[ T]", r"\1T", txt)
    txt = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", txt)
    try:
        return _dt.datetime.fromisoformat(txt)
    except ValueError:
        return None


def parse_instant(s: str) -> _dt.datetime:
    """ISO-8601 instant with an explicit zone, returned in UTC. A zone-less time is refused: that is how a
    start gets read as local when the page meant something else."""
    d = instant_or_none(s)
    if d is None:
        die(f"not an ISO-8601 instant: {s!r} (write 2026-03-14T09:00:00Z or 2026-03-14T14:30:00+05:30)")
    if d.tzinfo is None:
        die(f"{s!r} has no time zone: add Z or an offset such as +05:30, copied from the event page")
    return d.astimezone(_dt.timezone.utc)


def git_instant(s: str):
    d = instant_or_none(s)
    return d.astimezone(_dt.timezone.utc) if d is not None and d.tzinfo is not None else None


def iso_z(d: _dt.datetime) -> str:
    return d.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def offset_minutes(s: str):
    """Offset of an instant string or of '+05:30' / '-0700' / 'Z', in minutes; None when there is none."""
    s = (s or "").strip()
    if re.search(r"[zZ]$", s):
        return 0
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
    if not m:
        return None
    return (1 if m.group(1) == "+" else -1) * (int(m.group(2)) * 60 + int(m.group(3)))


_FAKE_SAID = []


def fake_now():
    return os.environ.get("PB_FAKE_NOW") or None


def now_utc() -> _dt.datetime:
    fake = fake_now()
    if fake:
        if not _FAKE_SAID:
            _FAKE_SAID.append(1)
            print(f"TEST CLOCK: PB_FAKE_NOW={fake} (unset it for a real check)")
        return parse_instant(fake)
    return _dt.datetime.now(_dt.timezone.utc)


def span(td: _dt.timedelta) -> str:
    secs = int(round(abs(td.total_seconds()) / 60.0)) * 60
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    return f"{h}h {m}m" if h else f"{m}m"


def local_label(d: _dt.datetime, tz_min) -> str:
    if tz_min is None:
        return ""
    tz = _dt.timezone(_dt.timedelta(minutes=tz_min))
    sign = "+" if tz_min >= 0 else "-"
    return d.astimezone(tz).strftime("%a %d %b %H:%M ") + f"{sign}{abs(tz_min) // 60:02d}:{abs(tz_min) % 60:02d}"


def event_path(root: Path) -> Path:
    return pb_dir(root) / "event.json"


def event_problems(ev: dict) -> dict:
    """Keys of a stored event record that cannot be used, each with the reason. {} when the record is sound."""
    bad = {}
    for k in ("start", "end", "freeze"):
        v = ev.get(k)
        if v in (None, ""):
            continue
        d = instant_or_none(v)
        if d is None or d.tzinfo is None:
            bad[k] = f"'{k}' is not an instant with a zone ({v!r})"
    if not bad:
        if bool(ev.get("start")) != bool(ev.get("end")):
            k = "start" if ev.get("start") else "end"
            bad[k] = "'start' and 'end' must both be set"
        elif ev.get("start") and parse_instant(ev["end"]) <= parse_instant(ev["start"]):
            bad["end"] = "'end' is not after 'start'"
        elif ev.get("start") and ev.get("freeze") and not (
                parse_instant(ev["start"]) < parse_instant(ev["freeze"]) <= parse_instant(ev["end"])):
            bad["freeze"] = f"'freeze' is outside the window ({ev['freeze']!r})"
    tz = ev.get("tz_min")
    if tz is not None and not (isinstance(tz, int) and not isinstance(tz, bool) and abs(tz) <= 14 * 60):
        bad["tz_min"] = f"'tz_min' must be whole minutes between -840 and 840 ({tz!r})"
    if ev.get("history") not in (None, "strict", "none"):
        bad["history"] = f"'history' must be \"strict\" or \"none\" ({ev.get('history')!r})"
    if "sponsors" in ev and not (isinstance(ev["sponsors"], list) and all(isinstance(s, str) for s in ev["sponsors"])):
        bad["sponsors"] = "'sponsors' must be a list of names"
    for k in ("card_chars", "video_cap_s", "judge_minutes"):
        v = ev.get(k)
        if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0):
            bad[k] = f"'{k}' must be a positive number ({v!r})"
    for k in ("no_window", "name", "source", "provisional"):
        if ev.get(k) is not None and not isinstance(ev[k], str):
            bad[k] = f"'{k}' must be text"
    return bad


def load_event(root: Path, repair: bool = False):
    """The stored event record, or None. A record edited by hand into something unusable stops every command
    with one line naming the key, because a clock that silently reads as 'no clock' gates nothing. With
    repair=True (only `clock set`) the unusable keys are dropped so new flags can replace them."""
    try:
        raw = event_path(root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError as e:
        if repair:
            return None             # `clock set` writes a fresh record over a file that cannot be read
        die(f".prod-build/event.json is not valid JSON ({e}). Set the clock again: `pb.py clock set --start <instant> "
            "--end <instant> --source <event page>`. Nothing is gated until it is.", 1)
    if not isinstance(ev, dict):
        if repair:
            return None
        die(".prod-build/event.json is not a JSON object. Set the clock again: `pb.py clock set --start <instant> "
            "--end <instant> --source <event page>`", 1)
    bad = event_problems(ev)
    if bad and not repair:
        die(".prod-build/event.json cannot be used: " + "; ".join(bad.values()) + ". Set it again with `pb.py clock "
            "set --start <instant> --end <instant>`. Nothing is gated until it is.", 1)
    for k in bad:
        ev.pop(k, None)
    if bad and ("start" in bad or "end" in bad):
        for k in ("start", "end", "freeze", "freeze_set"):
            ev.pop(k, None)
    return ev


def has_clock(ev) -> bool:
    """True when the record settles the question: a window with both instants, or a quoted 'no window' rule."""
    return bool(ev and (ev.get("no_window") or (ev.get("start") and ev.get("end"))))


def default_freeze(start: _dt.datetime, end: _dt.datetime) -> _dt.datetime:
    """The last 12% of the window, at least two hours and at most a day, and never more than a quarter of the
    window: time for the take, the page and a signed-out check of every link, not for building."""
    window = end - start
    hold = max(_dt.timedelta(hours=2), min(_dt.timedelta(hours=24), window * 0.12))
    return end - min(hold, window / 4)


def build_event(args, old=None) -> dict:
    """Event record from CLI flags, layered over an existing record."""
    ev = dict(old or {})
    start_s, end_s = getattr(args, "start", None), getattr(args, "end", None)
    no_window = (getattr(args, "no_window", None) or "").strip()
    hist_flag = getattr(args, "history", None)
    if no_window and (start_s or end_s):
        die("--no-window and --start/--end contradict each other: the event has a build window or it does not")
    if no_window:
        if len(re.findall(r"\w+", no_window)) < 4:
            die("--no-window takes the rule that says so, quoted from the event's own page")
        ev.update({"no_window": no_window, "history": "none"})
        for k in ("start", "end", "freeze", "freeze_set", "provisional"):
            ev.pop(k, None)
    if start_s or end_s:
        if not (start_s or ev.get("start")) or not (end_s or ev.get("end")):
            die("the event clock needs both --start and --end")
        start = parse_instant(start_s or ev["start"])
        end = parse_instant(end_s or ev["end"])
        if end <= start:
            die(f"--end ({iso_z(end)}) is not after --start ({iso_z(start)})")
        was_none = ev.pop("no_window", None)
        ev["start"], ev["end"] = iso_z(start), iso_z(end)
        if not getattr(args, "freeze", None):
            kept = None
            if ev.get("freeze_set") and ev.get("freeze"):
                fr = parse_instant(ev["freeze"])
                kept = fr if start < fr <= end else None
            if kept is not None:
                print(f"freeze kept at {iso_z(kept)} (it was set by hand; pass --freeze to move it)")
            else:
                ev["freeze"] = iso_z(default_freeze(start, end))
                ev.pop("freeze_set", None)
        off = offset_minutes(start_s or "")
        if off is not None and ("tz_min" not in ev or off != 0):
            ev["tz_min"] = off  # show local times in the zone the start was written in
        if was_none and not hist_flag:
            ev["history"] = "strict"  # a window replaces "no window": its default rule is the strict one
        else:
            ev.setdefault("history", "strict")
    if getattr(args, "freeze", None):
        if not ev.get("start"):
            die("--freeze needs --start and --end")
        fr = parse_instant(args.freeze)
        if not (parse_instant(ev["start"]) < fr <= parse_instant(ev["end"])):
            die(f"--freeze ({iso_z(fr)}) must fall inside the window, after the start")
        ev["freeze"], ev["freeze_set"] = iso_z(fr), True
    if getattr(args, "tz", None) and ev.get("start"):
        tz = offset_minutes(args.tz)
        if tz is None or abs(tz) > 14 * 60:
            die("--tz takes an offset such as +05:30 or -07:00")
        ev["tz_min"] = tz
    for flag, key in (("event", "name"), ("source", "source"), ("history", "history"),
                      ("video_cap", "video_cap_s"), ("card_chars", "card_chars"),
                      ("judge_minutes", "judge_minutes")):
        v = getattr(args, flag, None)
        if v not in (None, ""):
            if key in ("video_cap_s", "card_chars", "judge_minutes") and not (
                    isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0):
                die(f"--{flag.replace('_', '-')} must be a positive number ({v!r})")
            ev[key] = v
    if (getattr(args, "provisional", None) or "").strip():
        ev["provisional"] = args.provisional.strip()
    elif getattr(args, "confirmed", False):
        ev.pop("provisional", None)
    if getattr(args, "sponsor", None):
        ev["sponsors"] = [s.strip() for s in args.sponsor if s.strip()]
    if getattr(args, "ai_disclosure", False):
        ev["ai_disclosure"] = True
    if getattr(args, "research", False):
        ev["research"] = True
    if getattr(args, "post_entry", False):
        ev["post_entry"] = True
    if old:
        was = {k: old[k] for k in ("start", "end", "freeze", "history", "no_window")
               if old.get(k) is not None and old.get(k) != ev.get(k)}
        if was:
            ev["changes"] = (list(old.get("changes") or []) + [{"at": iso_z(now_utc()), "was": was}])[-10:]
    ev["set_at"] = iso_z(now_utc())
    return {k: ev[k] for k in EVENT_KEYS if k in ev}


def event_flags_given(args) -> bool:
    return any(getattr(args, k, None) for k in ("start", "end", "freeze", "no_window", "event", "source", "history",
                                                "video_cap", "card_chars", "judge_minutes", "sponsor", "tz",
                                                "ai_disclosure", "provisional", "confirmed", "research",
                                                "post_entry"))


def event_phase(ev, now: _dt.datetime = None) -> dict:
    """phase: none | before | building | freeze | closed, with the instants as datetimes."""
    if not ev or not ev.get("start") or not ev.get("end"):
        return {"phase": "none"}
    start, end = parse_instant(ev["start"]), parse_instant(ev["end"])
    freeze = parse_instant(ev["freeze"]) if ev.get("freeze") else default_freeze(start, end)
    now = now or now_utc()
    phase = "before" if now < start else "closed" if now > end else "freeze" if now >= freeze else "building"
    return {"phase": phase, "start": start, "end": end, "freeze": freeze, "now": now}


PREP_ALLOWED = ("Allowed before the start: reading the rules, docs and past winners; the idea package, UI spec, "
                "contract, plan and briefs, written but not committed; account, quota and deploy checks with one "
                "real call each; installing tools. Not before the start: project code, seed scripts or demo data "
                "that will ship, the repository (git init, a remote), a commit, a push, or anything you would have "
                "to hide. A prototype of the project is the project: practise on something else.")
FREEZE_MEANS = ("freeze: stop building. Fix only what the rehearsed judge path trips over, then record, write the "
                "page and open every link signed out.")
NO_CLOCK = ("no event clock (hackathon builds need one: `pb.py clock set --start <instant> --end <instant> "
            "--source <event page>`, or `--no-window '<the rule, quoted>'` where the event has none)")


def phase_text(ph: dict) -> str:
    now = ph["now"]
    return {"before": f"BEFORE: starts in {span(ph['start'] - now)}",
            "building": f"BUILDING: freeze in {span(ph['freeze'] - now)}, ends in {span(ph['end'] - now)}",
            "freeze": f"FREEZE: ends in {span(ph['end'] - now)}",
            "closed": f"CLOSED: ended {span(now - ph['end'])} ago"}[ph["phase"]]


def clock_summary(ev) -> str:
    """One line for the status board."""
    if not has_clock(ev):
        return NO_CLOCK
    if ev.get("no_window"):
        return f"event: {ev.get('name') or '(unnamed)'} | no build window: {ev['no_window']}"
    ph = event_phase(ev)
    return (f"clock: now {iso_z(ph['now'])}  {local_label(ph['now'], ev.get('tz_min'))}".rstrip()
            + f"  phase {phase_text(ph)}" + ("  (PROVISIONAL)" if ev.get("provisional") else ""))


def clock_lines(ev) -> list:
    if not ev:
        return [NO_CLOCK]
    if ev.get("no_window"):
        return [f"event: {ev.get('name') or '(unnamed)'} | no build window: {ev['no_window']}"]
    ph = event_phase(ev)
    if ph["phase"] == "none":
        return ["event clock is incomplete (no start or end): run `pb.py clock set --start <instant> --end <instant>`"]
    tz = ev.get("tz_min")
    out = [f"event: {ev.get('name') or '(unnamed)'} | source: {ev.get('source') or 'NOT RECORDED'}"]
    if ev.get("provisional"):
        out.append(f"  PROVISIONAL: {ev['provisional']}. Re-read the instants from the event's own page before "
                   "building, then `pb.py clock set --start .. --end .. --confirmed`.")
    for label, key in (("start ", "start"), ("freeze", "freeze"), ("end   ", "end")):
        d = ph[key]
        out.append(f"  {label} {iso_z(d)}  {local_label(d, tz)}".rstrip())
    out.append(f"  now    {iso_z(ph['now'])}  {local_label(ph['now'], tz)}".rstrip() + f"  phase {phase_text(ph)}")
    strict = ev.get("history", "strict") == "strict"
    out.append("  history rule: " + ("strict: the first commit is at or after the start, and no code written before "
                                     "it is copied in" if strict else
                                     "earlier work is allowed by this event's rules; still say what existed before"))
    if ev.get("post_entry"):
        out.append("  the post is the entry: playbook R103-R112 are plan tasks, and the post is checked before it is "
                   "published (SKILL.md Step 12)")
    if ph["phase"] == "before" and strict:
        out.append("  " + PREP_ALLOWED)
    if ph["phase"] == "freeze":
        out.append("  " + FREEZE_MEANS)
        out.append("  task kinds that may start now: " + ", ".join(FREEZE_KINDS))
    return out


def ensure_ignores(root: Path) -> list:
    """Planning notes and session files are ignored before the first `git add` can pick them up."""
    said = []
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("\n".join(GITIGNORE_BASE) + "\n" + IGNORE_BLOCK + POST_IGNORE_BLOCK, encoding="utf-8")
        said.append("created .gitignore (dependencies, env files, and planning notes that stay out of a judged repository)")
        return said
    if "prod-build: planning notes" not in gitignore.read_text(encoding="utf-8", errors="replace"):
        have = gitignore.read_text(encoding="utf-8", errors="replace")
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(("" if have.endswith("\n") or not have else "\n") + IGNORE_BLOCK)
        said.append("added to .gitignore: idea packages, judging notes, filming scripts and browser session files "
                    "(they stay out of a judged repository)")
    if POST_IGNORE_MARK not in gitignore.read_text(encoding="utf-8", errors="replace"):
        have = gitignore.read_text(encoding="utf-8", errors="replace")
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(("" if have.endswith("\n") or not have else "\n") + POST_IGNORE_BLOCK)
        said.append("added to .gitignore: a post draft at the root (post.md, post-draft.md, devto-post.md) and "
                    "first-user/ (a first user's own material stays out of the repository)")
    return said


def cmd_clock(args, root: Path) -> int:
    if args.action == "set":
        if not event_flags_given(args):
            die("usage: pb.py clock set --start <instant> --end <instant> [--freeze <instant>] [--event NAME] "
                "[--source URL] [--history strict|none] [--provisional '<why>' | --confirmed] [--video-cap SECONDS] "
                "[--card-chars N] [--sponsor NAME ...] [--ai-disclosure] | --no-window '<the rule, quoted>'")
        ev = build_event(args, load_event(root, repair=True))
        if not has_clock(ev):
            die("the event clock needs --start and --end (or --no-window '<the rule, quoted>')")
        pb_dir(root).mkdir(parents=True, exist_ok=True)
        before = load_event(root, repair=True) or {}
        write_json(event_path(root), ev)
        for line in ensure_ignores(root):
            print(line)
        moved = {k: before.get(k) for k in ("start", "end", "freeze", "history", "no_window")
                 if before.get(k) is not None and before.get(k) != ev.get(k)}
        if moved and (pb_dir(root) / "plan.json").exists():
            was = ", ".join(f"{k} was {v}" for k, v in moved.items())
            print(f"WARN  the clock changed after the plan was made ({was}): this moves the start, freeze and "
                  "deadline gates. Change it only when the event's own page changed, quote the change in contract "
                  "§12, and list it with the overrides in the delivery report")
            rec = append_evidence(root, {
                "task": None, "kind": "review", "method": "reported", "cmd": ("pb.py " + ARGV_TEXT[0])[:400],
                "exit": 0, "expect": "pass", "ok": True, "git": git_info(root), "tree": tree_hash(root),
                "where": "local", "tail": [f"EVENT CLOCK CHANGED ({iso_z(now_utc())}): {was}"]})
            print(f"[{rec['id']}] recorded")
        if not ev.get("source") and not ev.get("no_window"):
            print("WARN  no --source: record the event page (and the quoted line) these instants came from")
        if load_rules(root) is None:
            print("NEXT  copy the event's hard rules into the ledger now, before any build work: `pb.py rules add "
                  "--id ID --quote '<the rule>' --source <page> [--dq] (--file PATH | --section FILE#HEADING | "
                  "--attest)`. `submit check` fails without it (H54)")
        if ev.get("start"):
            hours = (parse_instant(ev["end"]) - parse_instant(ev["start"])).total_seconds() / 3600
            if hours < 2 or hours > 24 * 60:
                print(f"WARN  the window is {hours:.1f} hours long; check the instants against the event page")
    ev = load_event(root)
    for line in clock_lines(ev):
        print(line)
    return 0 if has_clock(ev) else 1


def commit_times(root: Path, *refs: str) -> list:
    """[(sha, earliest_of_author_and_committer, latest_of_both, subject)] oldest first; [] without history."""
    out = git(root, "log", "--format=%H%x09%aI%x09%cI%x09%s", *(refs or ("HEAD",)))
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 3:
            continue
        a, c = git_instant(parts[1]), git_instant(parts[2])
        if a is None or c is None:
            continue
        rows.append((parts[0], min(a, c), max(a, c), parts[3] if len(parts) > 3 else ""))
    rows.sort(key=lambda r: r[1])
    return rows


def repo_state(root: Path) -> dict:
    """Whether `root` is its own repository: own | nested (inside a larger one) | none."""
    top = git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0 or not top.stdout.strip():
        return {"kind": "none", "top": None}
    try:
        same = Path(top.stdout.strip()).resolve() == root.resolve()
    except OSError:
        same = True
    return {"kind": "own" if same else "nested", "top": top.stdout.strip()}


def window_findings(root: Path, start: _dt.datetime, end: _dt.datetime, tz_min=None, now: _dt.datetime = None,
                    provisional: bool = False) -> dict:
    """Compare the repository's history with the event window. FAIL = commits dated before the start, or a
    history that cannot be seen whole."""
    fails, warns, info = [], [], []
    repo = repo_state(root)
    if repo["kind"] == "nested":
        info.append(f"this folder is inside the repository at {repo['top']}, so it has no history of its own to "
                    "check. A judged entry needs its own repository: create it in this folder after the start")
        return {"fails": fails, "warns": warns, "info": info, "rows": [], "certified": False,
                "unchecked": "no repository of its own"}
    rows = commit_times(root)
    if not rows:
        info.append("no commits yet: create the repository and make the first commit after the start")
        return {"fails": fails, "warns": warns, "info": info, "rows": rows, "certified": False,
                "unchecked": "no commits yet"}
    first, last = rows[0], max(rows, key=lambda r: r[2])
    info.append(f"commits on HEAD: {len(rows)} | first {first[0][:7]} {iso_z(first[1])} | "
                f"last {last[0][:7]} {iso_z(last[2])}")
    shallow = git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    if shallow:
        fails.append("this is a shallow clone, so commits older than the ones fetched are not visible here; run "
                     "`git fetch --unshallow` and check again")
    early = [r for r in rows if r[1] < start]
    late = [r for r in rows if r[2] > end]
    if early:
        fails.append(f"{len(early)} of {len(rows)} commits are dated before the start; the earliest is "
                     f"{early[0][0][:7]} at {iso_z(early[0][1])}, {span(start - early[0][1])} early "
                     f"(\"{early[0][3][:60]}\")")
    if late:
        worst = max(late, key=lambda r: r[2])
        warns.append(f"{len(late)} commit(s) are dated after the end (latest {worst[0][:7]} at {iso_z(worst[2])}): "
                     "judges may discount them; after the deadline touch only a dead link, and say so")
    on_head = {r[0] for r in rows}
    pushed = [r for r in commit_times(root, "--remotes") if r[0] not in on_head and r[1] < start]
    local = [r for r in commit_times(root, "--branches", "--tags") if r[0] not in on_head and r[1] < start
             and r[0] not in {p[0] for p in pushed}]
    if pushed:
        fails.append(f"{len(pushed)} commit(s) on pushed branches other than this one are dated before the start "
                     f"(earliest {pushed[0][0][:7]} at {iso_z(pushed[0][1])}): a pushed branch is part of the public "
                     "history")
    if local:
        warns.append(f"{len(local)} commit(s) on local branches or tags are dated before the start (earliest "
                     f"{local[0][0][:7]}): do not push them to the judged repository")
    certified = not (early or shallow or pushed or local)
    if certified and provisional:
        info.append("no statement to print yet: the start is provisional. Re-read it from the event's page, then "
                    "`pb.py clock set --start .. --end .. --confirmed`, and run this again")
    elif certified:
        when = local_label(first[1], tz_min) or iso_z(first[1])
        began = local_label(start, tz_min) or iso_z(start)
        info.append("true statement for the README and the form: \"The first commit "
                    f"({first[0][:7]}) is timestamped {when} ({iso_z(first[1])}), after the build opened at {began}.\"")
    return {"fails": fails, "warns": warns, "info": info, "rows": rows, "certified": certified}


NOT_FIXABLE = ("This is not fixable by editing dates: pushes and the repository's creation are public records. "
               "Say exactly what existed before the start in the README and the form, ask the organisers in their "
               "public channel, or do not confirm that it was built during the event. The choice is the builder's.")


def record_check(root: Path, cmd: str, bad: bool, tail: list, task=None) -> None:
    """Event checks leave a ledger entry when a plan exists; without one they are still valid, just unrecorded."""
    if not (pb_dir(root) / "plan.json").exists():
        print("(not recorded: no .prod-build/plan.json here)")
        return
    rec = append_evidence(root, {
        "task": task, "kind": "review", "method": "ran", "cmd": cmd[:400],
        "exit": 1 if bad else 0, "expect": "pass", "ok": not bad, "git": git_info(root), "tree": tree_hash(root),
        "where": "local", "tail": tail[-20:]})
    print(f"[{rec['id']}] recorded")


def cmd_window(args, root: Path) -> int:
    ev = load_event(root) or {}
    if event_flags_given(args):
        ev = build_event(args, ev)
    if ev.get("no_window"):
        print(f"no build window recorded for this event ({ev['no_window']}); nothing to check")
        return 0
    if not ev.get("start"):
        die("no event clock: pass --start and --end, or run `pb.py clock set` first", 1)
    start, end = parse_instant(ev["start"]), parse_instant(ev["end"])
    print(f"window {iso_z(start)} -> {iso_z(end)} ({span(end - start)})"
          + (f" | {ev['name']}" if ev.get("name") else "") + (" | PROVISIONAL" if ev.get("provisional") else ""))
    res = window_findings(root, start, end, ev.get("tz_min"), provisional=bool(ev.get("provisional")))
    strict = ev.get("history", "strict") == "strict"
    lines = []
    for i in res["info"]:
        print(f"      {i}")
    for w in res["warns"]:
        lines.append(f"WARN  {w}")
    for f in res["fails"]:
        lines.append(f"{'FAIL ' if strict else 'WARN '} {f}")
    for line in lines:
        print(line)
    if res["fails"] and strict:
        print("      " + NOT_FIXABLE)
    bad = bool(res["fails"]) and strict
    if res.get("unchecked"):
        print(f"window check: NOTHING TO CHECK ({res['unchecked']})")
        return 0
    print(f"window check: {'FAIL' if bad else 'PASS'}"
          + (" (against a provisional start: confirm the clock before relying on this)" if ev.get("provisional") else ""))
    record_check(root, "pb.py " + ARGV_TEXT[0], bad, lines)
    return 1 if bad else 0


def tracked_files(root: Path) -> list:
    out = git(root, "-c", "core.quotepath=off", "ls-files", "-z")
    if out.returncode == 0:
        return [norm_path(f) for f in out.stdout.split("\0") if f.strip()]
    files = []
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not SKIP_DIRS.search(rel + "/") and not rel.startswith(".git/"):
            files.append(rel)
    return files


def find_readme(root: Path, explicit=None):
    """The README a repository page shows: .github/ first, then the root, then docs/ (the host's own order)."""
    if explicit:
        p = Path(explicit)
        p = p if p.is_absolute() else root / p
        return p if p.is_file() else None
    for folder in (root / ".github", root, root / "docs"):
        try:
            names = sorted(p.name for p in folder.iterdir() if p.is_file() and README_NAME.match(p.name))
        except OSError:
            continue
        if names:
            md = [n for n in names if n.lower().endswith((".md", ".markdown", ".mdx"))]
            return folder / (md or names)[0]
    return None


def first_statement(lines: list):
    """The first plain sentence in the opening lines: not a heading, badge, image, link list or table."""
    for line in lines:
        t = line.strip()
        if not t or t.startswith(("#", "|", "<", "```", "---", "===", "[!")) or MD_IMAGE.search(t):
            continue
        t = re.sub(r"^[>\-*+\s]+", "", t)
        plain = MD_LINK.sub(lambda m: m.group(1), t)
        plain = re.sub(r"[*_`~]", "", plain).strip()
        if re.match(r"(?i)^(video|live|demo|repo(sitory)?|docs?|links?|site|try it|watch)\b[^.]{0,30}:", plain):
            continue  # a label and its link, not a sentence
        n = len(re.findall(r"[^\W_][\w'’\-]*", BARE_URL.sub("", plain)))
        if 6 <= n <= 60 and not re.match(r"^\s*(\[[^\]]*\]\([^)]*\)\s*[|·•,]?\s*)+$", t):
            return plain
    return None


def prose_lines(text: str) -> list:
    """[(line number, line with inline code removed)] for lines that are not code."""
    out, fenced, prev_blank = [], False, True
    for n, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            prev_blank = False
            continue
        if fenced:
            continue
        indented = bool(re.match(r"^( {4,}|\t)", line)) and not re.match(r"^\s*([-*+]|\d+[.)])\s", line)
        if indented and prev_blank:
            continue  # an indented code block
        prev_blank = not line.strip() or (indented and prev_blank)
        out.append((n, INLINE_CODE.sub(" ", line)))
    return out


def placeholder_findings(text: str, where: str) -> tuple:
    """(fails, warns) for unfinished text in a README or a description."""
    fails, warns, hit = [], [], set()

    def fail(n, what, line):
        if n not in hit:
            hit.add(n)
            fails.append(f"{where}:{n} has {what}: {line.strip()[:90]}")

    lines = prose_lines(text)
    defined = frozenset(d.lower() for d in re.findall(r"(?m)^ {0,3}\[([^\]\n]+)\]:", text))
    for n, line in lines:
        for m in MD_LINK_FULL.finditer(line):
            label, whole = m.group(1), m.group(2).strip().strip("<>").strip()
            target = whole.split()[0] if whole else ""
            if target in ("", "#"):
                # an empty target is a broken link whatever its text says; "#" is one only on a link a judge would press
                if not label.startswith("!") and (target == "" or LINKISH_TEXT.search(label)):
                    fail(n, "a link with no target", line)
            elif PLACEHOLDER_TARGET.match(whole) or PLACEHOLDER_TARGET.match(target):
                fail(n, "a link whose target is a placeholder", line)
            elif PLACEHOLDER_URL.match(target):
                fail(n, "a link that points at a placeholder address", line)
            elif LOCAL_URL.match(target) and LIVE_TEXT.search(label):
                fail(n, "a live link that only opens on your own machine", line)
        if RX_PROMISED.search(line) or RX_PROMISED_ANY.search(line):
            fail(n, "a link or asset that is promised but not there", line)
        if RX_TOKEN.search(line):
            fail(n, "placeholder text", line)
        if RX_BRACKET.search(line):
            fail(n, "a bracket where a link should be", line)
        if RX_SELFNOTE.search(line):
            fail(n, "a note-to-self line", line)
        fb = fill_bracket(line, defined)
        if fb:
            fail(n, f"a bracket left for the author to fill ({fb[:60]})", line)
        if author_sentence(line):
            fail(n, "a sentence telling the author to finish the document", line)
        m = RX_LABELLED_URL.search(line)
        bare = HTML_TAIL.sub("", m.group(1)) if m else ""
        if bare and PLACEHOLDER_URL.match(bare):
            fail(n, "a link that points at a placeholder address", line)
        elif bare and LOCAL_URL.match(bare) and LIVE_TEXT.search(line):
            fail(n, "a live link that only opens on your own machine", line)
        if n in hit:
            continue
        if EXAMPLE_URL.search(line):
            warns.append(f"{where}:{n} has an example.com address outside a code sample: {line.strip()[:90]}")
        elif SOFT_PLACEHOLDERS.search(line):
            warns.append(f"{where}:{n} reads as unfinished: {line.strip()[:90]}")
    kept = {n for n, _ in lines}
    numbered = text.splitlines()
    prose = "\n".join(l if i + 1 in kept else "" for i, l in enumerate(numbered))
    for m in RX_COMMENT.finditer(prose):
        if RX_COMMENT_NOTE.search(m.group(1)):
            n = prose[: m.start()].count("\n") + 1
            fail(n, "a note-to-self comment", m.group(0).replace("\n", " "))
    return fails, warns


def probe_video(path: Path):
    if not shutil.which("ffprobe"):
        return None
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                            str(path)], capture_output=True, text=True, timeout=120)
        data = json.loads(p.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        return None

    def rate(s):
        try:
            a, b = (s or "0/1").split("/")
            return float(a) / float(b) if float(b) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    nominal, avg = rate(v.get("r_frame_rate")), rate(v.get("avg_frame_rate"))
    return {"duration": num((data.get("format") or {}).get("duration")) or num(v.get("duration")),
            "fps": nominal or avg, "avg_fps": avg or nominal,
            "width": int(num(v.get("width"))), "height": int(num(v.get("height"))),
            "audio": any(s.get("codec_type") == "audio" for s in data.get("streams", []))}


def md_sections(text: str) -> list:
    """(heading, body) pairs for a markdown document; the text before the first heading has heading ''."""
    out, head, buf = [], "", []
    fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else (re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
                                or re.match(r"^\s{0,3}\*\*([^*]{2,80})\*\*:?\s*$", line))
        if m:
            out.append((head, "\n".join(buf)))
            head, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    out.append((head, "\n".join(buf)))
    return out


def caveat_words(text: str) -> int:
    """Words under caveat headings, counting a subsection under its parent's: "What failed" with its "Before the run"
    and "In the run" below it is one block of failures, whatever the subsections are called."""
    words, fence, stack, cav = 0, False, [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
            continue
        m = None if fence else re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", line)
        b = None if fence or m else re.match(r"^\s{0,3}\*\*([^*]{2,80})\*\*:?\s*$", line)
        if m or b:
            level = len(m.group(1)) if m else 7   # a bold line heads a block inside the section it sits in
            title = (m.group(2) if m else b.group(1)).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, bool(CAVEAT_HEAD.search(title)) and not RUBRIC_ANSWER.search(title)))
            cav = any(c for _, c in stack)
            continue
        if cav and not fence:
            words += len(re.findall(r"\S+", line))
    return words


def writeup_findings(text: str, where: str, research: bool, scored: bool = False) -> tuple:
    """How a write-up spends its words (H51): a research entry leads with the answer and a number, carries a table
    or figure and one command that rebuilds it, and keeps its caveats to the ones that change the headline."""
    fails, warns, notes, oks = [], [], [], []
    plain = re.sub(r"(?s)```.*?```", " ", text)
    total = len(re.findall(r"\S+", plain)) or 1
    secs = md_sections(text)
    share = caveat_words(text) / total
    dashes = text.count("\u2014")
    if dashes:
        warns.append(f"{where}: {dashes} em dash(es) in judge-facing prose; run it through anti-ai-writing where it is "
                     "installed (an organiser on a review stream: \"See, this text is heavily AI generated\")")
    if research:
        paras = [p.strip() for _, body in secs for p in re.split(r"\n\s*\n", body) if p.strip()
                 and not p.strip().startswith(("!", "[", "|", "<", ">", "```", "---"))]
        first = paras[0] if paras else ""
        first_flat = re.sub(r"\s+", " ", first)[:100]
        bare = re.sub(r"\b(19|20)\d\d(-\d\d(-\d\d)?)?\b", " ", re.sub(r"https?://\S+|\]\([^)]*\)", " ", first))
        if not re.search(r"\d", bare):
            warns.append(f"{where}: the first paragraph states no number. Lead with the answer, the number, the "
                         "sample and the data (field notes H51): four of five placed entries at one research event "
                         "led with a question or an answer about the event's subject")
        else:
            oks.append(f"{where}: the first paragraph carries a number: \"{first_flat}\"")
        if not (re.search(r"(?m)^\s{0,3}\|[^\n]+\|", text) or MD_IMAGE.search(text)):
            warns.append(f"{where}: no table or figure; a finding is read off one (H51)")
        code_spans = re.findall(r"(?s)```.*?```", text) + re.findall(r"`[^`\n]+`", text)
        if not any(REPRO.search(c) for c in code_spans):
            warns.append(f"{where}: no command that rebuilds the main number (H30, H51)")
        if share >= 0.4:
            fails.append(f"{where}: {round(100 * share)}% of the words sit under limits, refusals, corrections or "
                         "honesty headings. Keep the caveats that change how the headline reads (about five bullets), "
                         "move corrections to a dated changelog at the end, and spend the rest on the result (H51: one entry filed "
                         "even its results under such headings, and did not place)")
        elif share >= 0.25:
            warns.append(f"{where}: {round(100 * share)}% of the words sit under limits, refusals, corrections or "
                         "honesty headings; keep the ones that change how the headline reads (H51)")
        heads = [h for h, _ in secs if h]
        if any(CHANGELOG_HEAD.search(h) for h in heads[:-2]) and not CHANGELOG_HEAD.search(" ".join(heads[-2:])):
            notes.append(f"{where}: corrections sit before the end; a dated changelog after the result reads as care, "
                         "before it as the story")
    elif scored and share >= 0.4:
        fails.append(f"{where}: {round(100 * share)}% of the words sit under failure, limits or correction headings. "
                     "This is the document judges score: answer the rubric's lines in order with numbers, keep the "
                     "caveats that change a claim, and leave the build diary out (H62: one entry spent 45% of its "
                     "scored FACTORY.md this way and did not place; a research entry spent 55% and did not place)")
    elif share >= (0.3 if scored else 0.4):
        warns.append(f"{where}: {round(100 * share)}% of the words sit under limits, refusals, failures or honesty "
                     "headings; say what is real and what is not built plainly (H26), then spend the words on what works")
    return fails, warns, notes, oks


# ----------------------------------------------------------------------------- referenced files

MAP_TOKEN = re.compile(r"`([^`\s]+)`")
MAP_HEADER = re.compile(r"(?i)\b(paths?|files?|folders?|director(y|ies)|dirs?|locations?|where)\b")
TEMPLATE_TOKEN = re.compile(r"[<>{}*$]|\.\.\.|…|(^|[-_/.])[A-Z]([-_/.]|$)|^~|^\.\./")
PATHLIKE = re.compile(r"^[\w.@-]+(/[\w.@-]+)*/?$")
FILE_EXT = re.compile(r"(?i)(\.(jsonl?|mdx?|markdown|txt|rst|py|ipynb|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|c|h|cc|cpp|"
                      r"hpp|rb|php|swift|sh|bash|ps1|ya?ml|toml|ini|cfg|conf|env|csv|tsv|parquet|sqlite|db|sql|html?|"
                      r"css|scss|svg|png|jpe?g|gif|webp|pdf|mp4|mov|webm|mp3|wav|zip|tar|gz|lock|log|xml|proto)|"
                      r"(^|/)(Dockerfile|Makefile|Procfile|LICENSE))$")


def path_exists(root: Path, rel: str, files: list, base: Path = None) -> bool:
    rel = rel.split("#", 1)[0].split("?", 1)[0].strip()
    if not rel:
        return True
    cands = []
    if rel.startswith("/"):
        cands.append(rel.lstrip("/"))
    else:
        if base is not None:
            try:
                cands.append((base / rel).resolve().relative_to(root.resolve()).as_posix())
            except ValueError:
                return True  # points outside the repository; not this check's business
        cands.append(rel[2:] if rel.startswith("./") else rel)
    tracked = set(files)
    for c in cands:
        c = c.rstrip("/")
        if c in ("", "."):
            return True   # the repository root, or the document's own folder inside it
        if c in tracked or any(f.startswith(c + "/") for f in files):
            return True
        if not files and (root / c).exists():
            return True
    return False


def path_findings(root: Path, text: str, where: str, base: Path, files: list) -> tuple:
    """Links to files that are not in the repository, and the repository map's rows that name a missing path. A
    judge who clicks a link or opens a file the map promises meets a 404 (H63: the map listed `room.json`, the
    room export the rules required, and it was not there)."""
    fails, warns, seen = [], [], set()
    for n, line in prose_lines(text):
        for m in MD_LINK_FULL.finditer(line):
            raw_t = m.group(2).strip()
            if raw_t.startswith("<") and ">" in raw_t:
                target = raw_t[1:raw_t.index(">")]
            else:
                target = raw_t.split()[0] if raw_t else ""
            target = urllib.parse.unquote(target)
            if not target or re.match(r"(?i)^([a-z][a-z0-9+.-]*:|#|//)", target) or TEMPLATE_TOKEN.search(target):
                continue
            if not path_exists(root, target, files, base) and target not in seen:
                seen.add(target)
                what = "an image" if m.start() > 0 and line[m.start() - 1] == "!" else "a link"
                fails.append(f"{where}:{n} has {what} to {target}, which is not in the repository")
    raw = text.splitlines()
    fence, is_map = False, False
    for n, line in enumerate(raw, 1):
        if FENCE.match(line):
            fence = not fence
        if fence or not line.lstrip().startswith("|"):
            is_map = False
            continue
        if re.match(r"^\s*\|[\s:|-]+\|?\s*$", line):
            head = raw[n - 2] if n >= 2 else ""
            is_map = bool(MAP_HEADER.search(head.strip().strip("|").split("|")[0]))
            continue
        if not is_map:
            continue
        first = line.strip().strip("|").split("|")[0]
        for tok in MAP_TOKEN.findall(first):
            if TEMPLATE_TOKEN.search(tok) or not PATHLIKE.match(tok) or tok in seen:
                continue
            if not ("/" in tok or FILE_EXT.search(tok)):
                continue
            seen.add(tok)
            if path_exists(root, tok, files, root):
                continue
            if "/" not in tok.rstrip("/") and any(f.rsplit("/", 1)[-1] == tok for f in files):
                continue
            if git(root, "check-ignore", "-q", "--", tok.rstrip("/")).returncode == 0:
                continue   # ignored on purpose (.env, build output): the map documents it, the repository should not hold it
            fails.append(f"{where}:{n} lists `{tok}` in its map of the repository, and it is not there")
    return fails, warns


# ----------------------------------------------------------------------------- the event's hard rules (H54)

RULE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
RULE_KINDS = ("file", "section", "attest")


def rules_path(root: Path) -> Path:
    return pb_dir(root) / "rules.json"


def has_event(ev: dict) -> bool:
    return bool(ev and (ev.get("start") or ev.get("no_window")))


def load_rules(root: Path):
    p = rules_path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f".prod-build/rules.json is not valid JSON ({e}); fix it or add the rules again", 1)
    if not isinstance(data, dict) or not isinstance(data.get("rules", []), list):
        die(".prod-build/rules.json must be an object with a \"rules\" list", 1)
    return data


def section_bodies(text: str, heading: str) -> list:
    """[(title, body)] for every heading whose title contains `heading` (any case), each body with its subsections;
    an exact title comes first."""
    want = re.sub(r"\s+", " ", heading).strip().lower()
    lines, fence, heads = text.splitlines(), False, []
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    out = []
    for k, (i, level, title) in enumerate(heads):
        flat = re.sub(r"\s+", " ", title).lower()
        if want not in flat:
            continue
        end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(lines))
        out.append((title, "\n".join(lines[i + 1:end]), flat == want))
    out.sort(key=lambda x: not x[2])
    return [(t, b) for t, b, _ in out]


# A rule asking for an approved open-source licence (the Strands rules: "The code repository must be public with an
# approved OSI Open Source License"): the repository's root must hold one, by its text or its SPDX id.
OSI_RULE = re.compile(r"(?i)\bOSI\b|open[- ]source licen[cs]e")
LICENCE_FILE = re.compile(r"(?i)^(licen[cs]e|copying)([-._][\w.-]*)?$")
OSI_IDS = ("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "GPL-2.0", "GPL-3.0", "LGPL-2.1", "LGPL-3.0",
           "MPL-2.0", "AGPL-3.0", "ISC", "EPL-2.0", "0BSD", "Unlicense")
OSI_TEXT = (   # (SPDX id, what only that licence's text says), the more specific first
    ("AGPL-3.0", r"gnu affero general public license,? version 3"),
    ("LGPL-3.0", r"gnu lesser general public license,? version 3"),
    ("LGPL-2.1", r"gnu lesser general public license,? version 2\.1"),
    ("GPL-3.0", r"gnu general public license,? version 3"),
    ("GPL-2.0", r"gnu general public license,? version 2"),
    ("Apache-2.0", r"apache license,? version 2\.0"),
    ("MPL-2.0", r"mozilla public license,? (version|v\.?) ?2\.0"),
    ("EPL-2.0", r"eclipse public license -? ?v(ersion)? ?2\.0"),
    ("BSD-3-Clause", r"redistribution and use in source and binary forms.*neither the name of"),
    ("BSD-2-Clause", r"redistribution and use in source and binary forms"),
    ("ISC", r"permission to use, copy, modify, and/or distribute this software for any purpose with or without fee "
            r"is hereby granted, provided that"),
    ("0BSD", r"permission to use, copy, modify, and/or distribute this software for any purpose with or without fee "
             r"is hereby granted"),
    ("MIT", r"permission is hereby granted, free of charge, to any person obtaining a copy"),
    ("Unlicense", r"this is free and unencumbered software released into the public domain"),
    ("MIT", r"^(the )?mit license\b"), ("Apache-2.0", r"^apache license\b"), ("ISC", r"^isc license\b"),
    ("BSD-3-Clause", r"^bsd 3-clause\b"), ("BSD-2-Clause", r"^bsd 2-clause\b"), ("Unlicense", r"^the unlicense\b"),
)
NOT_OSI = re.compile(r"(?i)\b(commons clause|business source license|server side public license|elastic license|"
                     r"creative commons|polyform|functional source license|source[- ]available)\b")
# words an OSI text can hold itself: GPL-2.0's "noncommercial distribution", the Unlicense's "commercial or
# non-commercial", a BSD file's "All rights reserved." They decide unless the file opens as an OSI licence (the FSL
# names Apache-2.0 only far down, as its future licence)
NOT_OSI_ALONE = re.compile(r"(?i)\b(non-?commercial|all rights reserved)\b")
# A step the rules want done inside the window, outside the build (the Strands workshop "within the Submission Period")
IN_WINDOW_RULE = re.compile(r"(?i)\b(within|during|inside|in)\s+the\s+(submission|hackathon|event|build|challenge|"
                            r"contest)\s+(period|window)\b")
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"), 1)}
EVIDENCE_DATE = re.compile(r"(?i)\b(20\d\d)-(\d\d)-(\d\d)"
                           r"(?:[T ](\d\d):(\d\d)(?::\d\d(?:\.\d+)?)?\s*(Z|[+-]\d\d:?\d\d)?)?"
                           r"|\b(\d{1,2}) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (20\d\d)\b"
                           r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2}),? (20\d\d)\b")


def licence_problems(root: Path, files: list) -> list:
    """What keeps the repository's root from holding an OSI-approved licence; [] when it does."""
    names = [f for f in files if "/" not in f and LICENCE_FILE.match(f)] if files else \
        sorted(p.name for p in root.iterdir() if p.is_file() and LICENCE_FILE.match(p.name))
    if not names:
        return ["no LICENSE or COPYING file at the repository's root, and the rule asks for an approved OSI "
                "open-source licence (MIT or Apache-2.0 are the usual): add one and commit it"]
    seen = []
    for n in names:
        text = (root / n).read_text(encoding="utf-8", errors="replace") if (root / n).is_file() else ""
        flat = re.sub(r"\s+", " ", re.sub(r"[*#=_`>]", " ", text)).strip().lower()
        spdx = re.search(r"(?i)spdx-license-identifier:\s*([\w.+-]+)", text)
        sid = re.sub(r"(?i)-(only|or-later)$|\+$", "", spdx.group(1)) if spdx else ""
        hit = next((i for i in OSI_IDS if i.lower() == sid.lower()), None)
        opens = bool(hit)
        for i, rx in ([] if hit else OSI_TEXT):
            m = re.search(rx, flat[:400] if rx.startswith("^") else flat)
            if m:
                hit, opens = i, m.start() < 600
                break
        bad = NOT_OSI.search(flat) or (None if opens else NOT_OSI_ALONE.search(flat))
        if hit and not bad:
            return []
        seen.append(f"{n} reads as {bad.group(1)}" if bad else f"{n} is none of them")
    return [f"the root's licence is not one this check knows as OSI-approved ({', '.join(OSI_IDS)}): "
            + "; ".join(seen)]


def evidence_dates(text: str) -> list:
    """[(datetime, has a time)] for each date written in a confirmation."""
    out = []
    for m in EVIDENCE_DATE.finditer(text or ""):
        try:
            if m.group(1):
                d = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0),
                                 int(m.group(5) or 0))
                z = offset_minutes(m.group(6) or "") if m.group(6) else None
                out.append((d.replace(tzinfo=_dt.timezone(_dt.timedelta(minutes=z))) if z is not None else d,
                            z is not None))
            elif m.group(7):
                out.append((_dt.datetime(int(m.group(9)), MONTHS[m.group(8)[:3].lower()], int(m.group(7))), False))
            else:
                out.append((_dt.datetime(int(m.group(12)), MONTHS[m.group(10)[:3].lower()], int(m.group(11))), False))
        except (ValueError, KeyError):
            continue
    return out


def window_date_problems(ev: dict, evidence: str) -> tuple:
    """(problems, note) for a step the rules want done inside the window: each date its confirmation carries."""
    if not (ev and ev.get("start") and ev.get("end")):
        return [], "no event clock to compare its date with"
    start, end = parse_instant(ev["start"]), parse_instant(ev["end"])
    tz = _dt.timezone(_dt.timedelta(minutes=ev.get("tz_min") or 0))
    dates = evidence_dates(evidence)
    if not dates:
        return [], "its confirmation carries no date: say when it was done, so the check can hold it to the window"
    out = []
    for d, timed in dates:
        if timed:
            inside = start <= d.astimezone(_dt.timezone.utc) <= end
            shown = iso_z(d)
        else:
            inside = start.astimezone(tz).date() <= d.date() <= end.astimezone(tz).date()
            shown = d.date().isoformat()
        if not inside:
            out.append(f"its confirmation is dated {shown}, outside the window ({iso_z(start)} to {iso_z(end)}): "
                       "a step the rules want inside the window, done outside it, disqualifies")
    return out, ""


def rule_problems(root: Path, r: dict, files: list) -> list:
    """What keeps one recorded rule from being met; [] when it is."""
    kind, out = r.get("kind"), []
    if kind == "file":
        for pat in r.get("paths") or []:
            if any(ch in pat for ch in "*?["):
                import fnmatch
                hit = [f for f in files if fnmatch.fnmatch(f, pat)] if files else \
                      [q.relative_to(root).as_posix() for q in root.glob(pat) if q.is_file()]
            elif pat.endswith("/"):
                hit = [f for f in files if f.startswith(pat)] if files else \
                      ([pat] if (root / pat).is_dir() and any((root / pat).iterdir()) else [])
            else:
                hit = [pat] if (pat in files if files else (root / pat).is_file()) else []
                if not hit and (any(f.startswith(pat.rstrip("/") + "/") for f in files) if files
                                else (root / pat).is_dir()):
                    hit = [f for f in files if f.startswith(pat.rstrip("/") + "/")] or [pat]
            if not hit:
                out.append(f"{pat} is not in the repository" + ("" if files else " (no git history: read from disk)"))
                continue
            for f in hit[:50]:
                fp = root / f
                if fp.is_file() and fp.stat().st_size == 0:
                    out.append(f"{f} is empty")
                elif fp.suffix.lower() == ".json" and fp.is_file():
                    try:
                        json.loads(fp.read_text(encoding="utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        out.append(f"{f} is not valid JSON")
                elif fp.suffix.lower() in (".md", ".markdown", ".txt") and fp.is_file():
                    pf, _ = placeholder_findings(fp.read_text(encoding="utf-8", errors="replace"), f)
                    out += pf[:3]
    elif kind == "section":
        f, head = r.get("file", ""), r.get("heading", "")
        draft = is_draft(f)   # a page's draft (R114) stays out of the repository (R81), so it is read from disk
        fp = (on_disk(root, f) if draft else None) or root / f
        if draft and not fp.is_file():
            out.append(f"{f} is not there (a draft is read from disk, since drafts stay out of the repository)")
        elif not draft and ((files and f not in files) or not fp.is_file()):
            out.append(f"{f} is not in the repository")
        else:
            found = section_bodies(fp.read_text(encoding="utf-8", errors="replace"), head)
            if not found:
                out.append(f"{f} has no heading containing \"{head}\"")
            tried = []
            for title, body in found:
                probs = []
                prose = re.sub(r"(?s)```.*?```", " ", body)
                if len(re.findall(r"\S+", prose)) < 10:
                    probs.append(f"{f} \"{title}\" has under ten words")
                pf, _ = placeholder_findings(body, f"{f} \"{title}\"")
                probs += pf[:3]
                bare = re.sub(r"\b(19|20)\d\d(-\d\d(-\d\d)?)?\b|`[^`]*`|https?://\S+", " ", prose)
                if r.get("number") and not re.search(r"\d", bare):
                    probs.append(f"{f} \"{title}\" states no number, and the rule asks for a measured one")
                if not probs:
                    tried = []
                    break
                tried.append(probs)
            if tried:
                out += tried[0]
    elif kind == "attest":
        c = r.get("confirmed") or {}
        if len(re.findall(r"\S+", str(c.get("evidence", "")))) < 6:
            out.append("not confirmed: a person checks it and records what shows it with `pb.py rules confirm "
                       f"{r.get('id')} --evidence '<where it shows, e.g. the video at 0:12 to 1:40>'`")
        elif in_window(r):
            out += window_date_problems(load_event(root) or {}, str(c.get("evidence", "")))[0]
    else:
        out.append(f"unknown kind {kind!r}")
    if OSI_RULE.search(str(r.get("quote", ""))):
        out += licence_problems(root, files)
    return out


def in_window(r: dict) -> bool:
    """The rule wants its step done inside the build window (--in-window, or its quote says so)."""
    return bool(r.get("in_window") or IN_WINDOW_RULE.search(str(r.get("quote", ""))))


def rule_notes(root: Path, r: dict) -> list:
    """What a met rule still leaves unchecked: a step to be done inside the window whose confirmation has no date."""
    c = r.get("confirmed") or {}
    if r.get("kind") == "attest" and in_window(r) and c.get("evidence"):
        note = window_date_problems(load_event(root) or {}, str(c["evidence"]))[1]
        return [note] if note else []
    return []


def rules_findings(root: Path, data: dict, files: list) -> tuple:
    fails, warns, oks = [], [], []
    rules = data.get("rules") or []
    if data.get("none") and not rules:
        oks.append(f"rules ledger: none recorded ({str(data['none'].get('why', ''))[:80]})")
        return fails, warns, oks
    if not rules:
        fails.append("rules ledger is empty: add the event's hard rules with `pb.py rules add`, or record `pb.py rules "
                     "none --source <page> --why '<what the page says>'`")
    for r in rules:
        probs = rule_problems(root, r, files)
        label = f"rule {r.get('id')} (\"{str(r.get('quote', ''))[:70]}\")"
        if probs:
            fails.append(("DISQUALIFYING: " if r.get("dq") else "") + label + ": " + "; ".join(probs[:4]))
        else:
            oks.append(f"{label} is met" + (" (confirmed by a person)" if r.get("kind") == "attest" else ""))
            warns += [f"{label}: {n}" for n in rule_notes(root, r)]
    return fails, warns, oks


def cmd_rules(args, root: Path) -> int:
    data = load_rules(root) or {"rules": []}
    rules = data.setdefault("rules", [])
    if args.action == "add":
        rid = (args.id or "").strip()
        if not RULE_ID.match(rid):
            die("--id: a short slug (lowercase letters, digits, hyphens), e.g. room-export")
        if len(re.findall(r"\S+", args.quote or "")) < 4:
            die("--quote: the rule in the event's own words, four words or more, copied from its page")
        if not (args.source or "").strip():
            die("--source: where the rule is written (the URL, or the kickoff file and its heading)")
        kinds = [k for k, v in (("file", args.file), ("section", args.section), ("attest", args.attest)) if v]
        if len(kinds) != 1:
            die("say how the rule is checked, one of: --file PATH [PATH ...] (a file or glob that must be in the "
                "repository), --section FILE#HEADING [--number] (a section that must be there and filled), or "
                "--attest (only a person can check it: what the video shows, how many messages the run had)")
        r = {"id": rid, "quote": args.quote.strip(), "source": args.source.strip(), "dq": bool(args.dq),
             "kind": kinds[0], "added": iso_z(now_utc())}
        if args.in_window:
            if kinds[0] != "attest":
                die("--in-window goes with --attest: a step done outside the build, confirmed with its date")
            r["in_window"] = True
        if args.file:
            r["paths"] = [x.strip() for x in args.file if x.strip()]
        if args.section:
            if "#" not in args.section:
                die("--section takes FILE#HEADING, e.g. FACTORY.md#What it cost")
            r["file"], r["heading"] = [x.strip() for x in args.section.split("#", 1)]
            r["number"] = bool(args.number)
        old = next((x for x in rules if x.get("id") == rid), None)
        if old:
            rules.remove(old)
            print(f"replaced rule {rid}" + (" (its confirmation was dropped: the rule changed)"
                                            if old.get("confirmed") else ""))
        rules.append(r)
        data.pop("none", None)
        pb_dir(root).mkdir(parents=True, exist_ok=True)
        write_json(rules_path(root), data)
        print(f"rule {rid} recorded ({r['kind']}{', disqualifying' if r['dq'] else ''})")
        return 0
    if args.action == "none":
        if not (args.source or "").strip() or len(re.findall(r"\S+", args.why or "")) < 6:
            die("usage: pb.py rules none --source <the rules page> --why '<what it says, six words or more>'")
        if rules:
            die("the ledger already holds rules; `none` is only for an event whose pages state none")
        data["none"] = {"source": args.source.strip(), "why": args.why.strip(), "at": iso_z(now_utc())}
        pb_dir(root).mkdir(parents=True, exist_ok=True)
        write_json(rules_path(root), data)
        print("recorded: this event's pages state no hard rules beyond the form")
        return 0
    if args.action == "confirm":
        r = next((x for x in rules if x.get("id") == args.id), None)
        if not r:
            die(f"no rule {args.id!r}; `pb.py rules list` shows the ids")
        if r.get("kind") != "attest":
            die(f"rule {args.id} is checked from the repository ({r.get('kind')}); only --attest rules are confirmed")
        if len(re.findall(r"\S+", args.evidence or "")) < 6:
            die("--evidence: what shows the rule is met and where, six words or more (e.g. 'the video shows the "
                "BAND room from 0:12 to 1:40, unedited')")
        r["confirmed"] = {"evidence": args.evidence.strip(), "at": iso_z(now_utc())}
        write_json(rules_path(root), data)
        print(f"rule {args.id} confirmed")
        if in_window(r):
            probs, note = window_date_problems(load_event(root) or {}, args.evidence)
            for x in probs + ([note] if note else []):
                print(f"WARN  {x}")
        return 0
    files = tracked_files(root) if repo_state(root)["kind"] != "none" else []
    if not rules and not data.get("none"):
        print("no rules recorded: `pb.py rules add --id ID --quote '<the rule>' --source <page> [--dq] "
              "(--file PATH | --section FILE#HEADING [--number] | --attest)`")
        return 1 if args.action == "check" else 0
    bad = 0
    for r in rules:
        probs = rule_problems(root, r, files)
        bad += bool(probs)
        tag = "ok   " if not probs else ("FAIL " if args.action == "check" else "open ")
        print(f"{tag} {r['id']:<22} {r['kind']:<7} {'DQ ' if r.get('dq') else '   '} {r['quote'][:80]}")
        for pr in probs[:4] + ([] if probs else [f"warn: {n}" for n in rule_notes(root, r)]):
            print(f"        {pr}")
    if data.get("none"):
        print(f"none: {data['none'].get('why', '')[:100]}")
    print(f"rules: {len(rules) - bad} met, {bad} open")
    return 1 if (bad and args.action == "check") else 0


# ----------------------------------------------------------------------------- the page as published (R114)

FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?\n)?(?:---|\.\.\.)[ \t]*(?:\r?\n|\Z)", re.S)
# what a page drawn by JavaScript returns to a plain HTTP fetch: Builder Center's says "Loading project..."
SHELL_TEXT = re.compile(r"(?im)^(?:[^\w\n]|_)*loading\b|\b(enable|turn on) javascript\b|\bjavascript (is )?(required|"
                        r"disabled|needed|turned off)\b|\brequires? javascript\b")
NUMBERING = re.compile(r"^\s*\d{1,3}[.)]\s+")
# How much of the draft reached the page: the share of the draft's runs of four words (reading order, lower case,
# no markup, code or addresses) found anywhere in the capture. A site's menus, dialogs and footer hold none of them.
# The share of word counts it replaced read Calltree's page at 36% to 109% by capture method (R114).
RUN = 4
PAGE_MIN_SHARE = 0.85
LIQUID = re.compile(r"\{%.*?%\}")   # DEV's {% embed … %} tags show as cards, not as their words
MD_TITLE_TAIL = re.compile(r"""\s+("[^"]*"|'[^']*')\s*$""")   # [text](url "title")
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 "
              "Safari/537.36")
# Builder Center's project page (--page-profile builder-center): what Zero to Shipped's rules asked of the page and
# its five winners carried (field notes H96 to H100). The entry's text runs from the page's title to the disclaimer.
BC_END = re.compile(r"(?i)^\W*(any opinions in this (project|post|article) are those of|comments \(\d+\))")
BC_META = re.compile(r"(?i)^\W*((published|last modified|updated|edited)\b.{0,60}|\d+|-{3,}|\*{3,}|_{3,})\W*$")
PROOF_HEAD = re.compile(r"(?i)\b(coding[- ]agents?|agent (proof|connection)|proof|ship[- ]gate|development process|"
                        r"built with|how (i|we|it) (was )?built)\b")
# what only AWS writes (H99): a CloudTrail eventID, invokedBy, the agent's userAgent marker (app/kiro-ide), the MCP host
AWS_PROOF = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\binvokedBy\b|"
                       r"aws-mcp\.amazonaws\.com")
# the agent's user-agent marker counts on a line that says it is one ("userAgent: aws-sdk-js/3.6 app/claude-code",
# ClinicLine's "AWS_SDK_UA_APP_ID=kiro-ide ... stamped app/kiro-ide"); a Next.js path (app/page.tsx) is not one
UA_MARKER = re.compile(r"(?<![\w/.-])app/[a-z][\w.#-]*")
UA_CONTEXT = re.compile(r"(?i)user[- ]?agent|sdk_ua_app_id|\baws-sdk-|\baws-cli/|\bbotocore/|\bboto3/")
COMPARED = re.compile(r"(?i)\b(vs\.?|versus|against|baseline)(?!\w)")
EXPECTED_HEAD = re.compile(r"(?i)should happen|expected")
MONEY = re.compile(r"[₹$€£¥]\s?\d|\b\d[\d,.]*\s?(?:usd|eur|gbp|inr|rupees?|dollars?|euros?|cents?)\b|"
                   r"\b(?:usd|eur|gbp|inr|rs\.?)\s?\d", re.I)
BUSINESS_LINE = re.compile(r"(?i)\b(subscriptions?|per[- ](seat|user)|pilots?|adoption)\b")
PAGE_PROFILES = ("builder-center",)


def is_draft(rel: str) -> bool:
    """A filming or submission draft by name (R81): it stays out of the repository, so it is read from disk."""
    return bool(DRAFT_PATHS.search(rel or "") or POST_DRAFT_PATHS.search(rel or ""))


def on_disk(root: Path, rel: str):
    """root/rel, or the same name in another case (SUBMISSION.md for submission.md); None when neither is there."""
    p = root / rel
    if p.is_file():
        return p
    try:
        hits = sorted(q for q in p.parent.iterdir() if q.is_file() and q.name.lower() == p.name.lower())
    except OSError:
        return None
    return hits[0] if hits else None


def front_matter(text: str) -> tuple:
    """(the front matter's text, the rest of the document)."""
    m = FRONT_MATTER.match(text)
    return ((m.group(1) or ""), text[m.end():]) if m else ("", text)


def front_tags(text: str) -> list:
    """A post's tags from its front matter: `tags: a, b`, `tags: [a, b]` or a YAML list under `tags:`."""
    m = re.search(r"(?im)^tags[ \t]*:(.*(?:\n[ \t]*-[^\n]*)*)", front_matter(text)[0])
    return [t.lower() for t in re.findall(r"[A-Za-z0-9][\w-]*", m.group(1))] if m else []


def reading_lines(text: str) -> list:
    """The lines a reader meets: front matter, fenced code and HTML comments left out."""
    body = re.sub(r"(?s)<!--.*?-->", " ", front_matter(text)[1])
    out, fenced = [], False
    for line in body.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return out


def plain_line(line: str) -> str:
    """A line without its markup: images dropped, links reduced to their text, HTML tags removed."""
    line = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", line)
    line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    return re.sub(r"<[^>\n]*>", " ", line)


def count_words(lines: list) -> int:
    """Words a reader sees. A token counts when it holds a letter or a digit, so bullets, rules and table pipes do
    not, and a markdown capture and a text capture of one page count the same."""
    return sum(1 for line in lines for w in plain_line(line).split() if re.search(r"[^\W_]", w))


def norm_line(line: str) -> str:
    """A line's words in lower case, without markup, numbering or punctuation."""
    return " ".join(re.findall(r"[^\W_]+", plain_line(NUMBERING.sub("", line)).lower()))


def draft_headings(lines: list) -> list:
    """The draft's ## and ### headings (a line underlined with --- too), in order, once each."""
    out, prev = [], ""
    for line in lines:
        m = re.match(r"^\s{0,3}#{2,3}\s+(.*?)(?:\s+#+)?\s*$", line)
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
        elif (re.match(r"^\s{0,3}-{2,}\s*$", line) and prev.strip()
              and not re.match(r"^\s{0,3}([#>|]|[-*+]\s|\d+[.)]\s)", prev)):
            out.append(prev.strip())
        prev = line
    return list(dict.fromkeys(h for h in out if norm_line(h)))


def heading_lines(lines: list) -> list:
    """A capture's heading lines, # or underlined."""
    out, prev = [], ""
    for line in lines:
        if re.match(r"^\s{0,3}#{1,6}\s+\S", line):
            out.append(line)
        elif re.match(r"^\s{0,3}(-{2,}|={2,})\s*$", line) and prev.strip():
            out.append(prev)
        prev = line
    return out


def page_urls(lines: list) -> list:
    """The http(s) addresses in a document, in order, once each; placeholders and local ones left out."""
    out = []
    for line in lines:
        for u in BARE_URL.findall(line):
            u = u.rstrip(".,;:!?*_")
            if u and not PLACEHOLDER_URL.match(u) and not LOCAL_URL.match(u):
                out.append(u)
    return list(dict.fromkeys(out))


def local_file(root: Path, name):
    """A file named on the command line: as given, else under the project root; None when neither exists."""
    if not name:
        return None
    p = Path(name).expanduser()
    if p.is_file():
        return p
    if not p.is_absolute() and (root / name).is_file():
        return root / name
    return None


def rel_name(root: Path, p: Path) -> str:
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p)


def text_runs(text: str, reader: bool) -> list:
    """A document's words in reading order, lower case, without markup or addresses. reader: only what a reader of the
    draft meets (no front matter, fenced code or comments); a capture is read whole."""
    lines = reading_lines(text) if reader else text.splitlines()
    flat = html.unescape(" ".join(plain_line(NUMBERING.sub("", l)) for l in lines))
    flat = BARE_URL.sub(" ", LIQUID.sub(" ", flat))
    return re.findall(r"[^\W_]+", flat.lower())


def draft_on_page(draft_text: str, page_text: str) -> float:
    """The share of the draft's runs of RUN words that the capture holds anywhere (playbook R114)."""
    d, p = text_runs(draft_text, True), text_runs(page_text, False)
    if len(d) < RUN:
        return 1.0 if set(d) <= set(p) else 0.0
    have = {tuple(p[i:i + RUN]) for i in range(len(p) - RUN + 1)}
    runs = [tuple(d[i:i + RUN]) for i in range(len(d) - RUN + 1)]
    return sum(r in have for r in runs) / len(runs)


URL_STRIPPED = "".join(map(chr, range(0x21)))   # what a browser strips from an href's ends: C0 controls, space


def link_spaces(raw: str, exact: bool = False) -> tuple:
    """('FAIL' | 'WARN' | '', how) for a link as stored. A space inside its host breaks it. Spaces before or after the
    address only warn: browsers strip them (Calltree's Demo link held two trailing spaces, its a.href ended
    '.amplifyapp.com/' and a click opened the app), and some clients do not."""
    # exact: the href as stored (page capture's record); else as a markdown capture wrote it, a space shown as %20
    s = raw if exact else re.sub(r"(?i)%20", " ", raw).replace("\u00a0", " ")
    core = s.strip(URL_STRIPPED) if exact else s.strip()
    m = re.match(r"(?i)^[a-z][a-z0-9+.-]*://([^/?#]*)", core)
    if m and re.search(r"\s", m.group(1)):
        return "FAIL", ("has a space inside its host, so it does not open: re-enter it in the platform's form "
                        "without the space")
    if core and core != s:
        where = " and ".join(w for w, hit in (("before", s[:1].isspace()), ("after", s[-1:].isspace())) if hit)
        return "WARN", (f"has spaces {where} its address. Browsers strip them and it opens (Calltree's Demo link "
                        "did), but some clients do not: re-enter it in the platform's form without spaces")
    if exact and re.search(r"(?i)^(%20|\u00a0)|(%20|\u00a0)$", core):
        return "WARN", ("ends in %20 or a no-break space, which a browser keeps, so it opens another address: re-enter "
                        "it in the platform's form without them")
    return "", ""


def host_problem(host: str) -> str:
    """Why a link's host cannot be looked up in DNS, or ''."""
    h = re.sub(r":\d*$", "", host.rsplit("@", 1)[-1]).rstrip(".")
    if h.startswith("[") or re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h):
        return ""
    if not h:
        return "it has no host"
    try:
        a = h.encode("idna").decode("ascii")
    except UnicodeError:
        return f"{h} is not a host name DNS can hold"
    bad = next((x for x in a.split(".") if not re.fullmatch(r"(?!-)[A-Za-z0-9_-]{1,63}(?<!-)", x)), None)
    return f"\"{bad}\" in {h} is not a DNS label" if bad is not None or len(a) > 253 else ""


def stored_links(text: str) -> list:
    """The web addresses a markdown capture links, as written (spaces kept): link targets, then bare addresses."""
    out = []
    for m in MD_LINK_FULL.finditer(text):
        t = MD_TITLE_TAIL.sub("", m.group(2))
        if m.start() and text[m.start() - 1] == "!":
            continue
        if re.match(r"(?i)^https?://", re.sub(r"(?i)^(\s|%20)+", "", t)):
            out.append(t)
    rest = MD_LINK_FULL.sub(" ", text)
    out += [u.rstrip(".,;:!?*_") for u in BARE_URL.findall(rest)]
    return list(dict.fromkeys(out))


def net_map(url: str) -> str:
    """PB_NET_MAP, for tests only: a JSON object of address prefixes and their stand-ins, so no test reaches the
    network. The longest prefix wins."""
    try:
        m = json.loads(os.environ.get("PB_NET_MAP") or "{}")
    except ValueError:
        return url
    hit = max((k for k in m if isinstance(k, str) and url.startswith(k)), key=len, default=None)
    return m[hit] + url[len(hit):] if hit else url


def open_like_a_browser(url: str, timeout: float = 20) -> tuple:
    """(status or None, why) for an address requested the way a browser would: HEAD, then GET when HEAD is refused."""
    st, why = None, ""
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(net_map(url), method=method, headers={"User-Agent": BROWSER_UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            st, why = e.code, f"answers {e.code}"
        except Exception as e:  # noqa: BLE001 - any network failure is the finding
            st, why = None, str(getattr(e, "reason", e))[:80]
    return st, why


def capture_record(pub: Path) -> tuple:
    """(published.json written by `pb.py page capture` with this capture, or None; a note when one was set aside)."""
    js = pub.with_suffix(".json")
    if not js.is_file():
        return None, ""
    try:
        data = json.loads(js.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, f"{js.name} is not valid JSON, so the links were read from {pub.name}"
    if not isinstance(data, dict) or not isinstance(data.get("links", []), list):
        return None, f"{js.name} is not a capture record, so the links were read from {pub.name}"
    if pub.stat().st_mtime > js.stat().st_mtime + 60:
        return None, f"{js.name} is older than {pub.name}, so it describes another capture and was not used"
    return data, ""


def bc_body(lines: list):
    """The entry's own lines on a Builder Center page: after its title (the first # heading or line underlined with
    ===, else its "Published …" line) and the platform's date lines, up to the disclaimer. None without a title."""
    start = next((i + 1 for i, l in enumerate(lines) if re.match(r"^\s{0,3}#\s+\S", l)
                  or (i and re.match(r"^\s{0,3}={2,}\s*$", l) and lines[i - 1].strip())), None)
    if start is None:
        start = next((i + 1 for i, l in enumerate(lines) if re.match(r"(?i)^\W*published\b.{0,60}$", l)), None)
    if start is None:
        return None
    end = next((j for j in range(start, len(lines)) if BC_END.search(lines[j])), len(lines))
    body = lines[start:end]
    while body and (not body[0].strip() or BC_META.match(body[0])):
        body = body[1:]
    return body


def md_tables(lines: list) -> list:
    """[(header row, [rows])] for each markdown table: a | row over a |---| row."""
    out = []
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith("|") and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append((lines[i], lines[i + 2:j]))
    return out


def profile_findings(lines: list, args, pwhere: str) -> dict:
    """--page-profile builder-center: what Zero to Shipped's rules asked of the page and its winners carried."""
    out = {"fails": [], "warns": [], "oks": []}
    tag = "page profile builder-center"
    body = bc_body(lines)
    if body is None:
        out["fails"].append(f"{tag}: {pwhere} has no page title (a # heading) or \"Published …\" line to find the "
                            "entry's text by: capture the page with `pb.py page capture <url>` (R114)")
        return out
    textish = not any(re.search(r"\]\(|^\s{0,3}#{1,6}\s", l) for l in body)
    hint = " (a text capture keeps a link's words, not its address: capture as markdown)" if textish else ""
    n = count_words(body)
    if n < 1000:
        out["fails"].append(f"{tag}: the entry's text on the page is {n} words. Exactly 100 of Zero to Shipped's 326 "
                            "entries reached 1,000, the size of the cut to Gate 2, and its five winners ran 1,511 to "
                            "3,450 (field notes H96)")
    elif n < 1500:
        out["warns"].append(f"{tag}: the entry's text on the page is {n} words; Zero to Shipped's five winners ran "
                            "1,511 to 3,450 (field notes H96)")
    shown = [l for l in body if l.strip() and not re.match(r"^\s*([-=*_]{3,}|\|?[\s:|-]*-[\s:|-]*\|?)\s*$", l)][:2]
    if not any(BARE_URL.search(re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", l)) for l in shown):
        out["fails"].append(f"{tag}: neither of the entry's first two lines holds a link ("
                            + " / ".join(f"\"{l.strip()[:50]}\"" for l in shown) + "): put the live URL there "
                            "(R117; field notes H100)" + hint)
    tables = md_tables(body)
    if not any(EXPECTED_HEAD.search(h) for h, _ in tables):
        out["fails"].append(f"{tag}: no table whose header says what should happen: give the judge a \"Do this | "
                            "What should happen\" table with exact inputs (R117; four of five winners had a zero-setup "
                            "path, field notes H100)" + hint)
    heads = []
    for i, l in enumerate(body):
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$", l)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
        elif i and re.match(r"^\s{0,3}(-{2,}|={2,})\s*$", l) and body[i - 1].strip():
            heads.append((i - 1, 1 if "=" in l else 2, body[i - 1].strip()))
    proofs = []
    for k, (i, level, title) in enumerate(heads):
        if PROOF_HEAD.search(title):
            end = next((j for j, lv, _ in heads[k + 1:] if lv <= level), len(body))
            sec = "\n".join(body[i + 1:end])
            plain = BARE_URL.sub(" ", re.sub(r"\]\([^)]*\)", "]", sec))
            proofs.append((title, bool(AWS_PROOF.search(plain) or "aws-mcp.amazonaws.com" in sec.lower()
                                       or any(UA_MARKER.search(x) and UA_CONTEXT.search(x)
                                              for x in plain.splitlines()))))
    if not proofs:
        out["fails"].append(f"{tag}: no section on the coding-agent proof (a heading naming the coding agent, the "
                            "proof or the development process): the rules required it and all five winners had one "
                            "(R115; field notes H99)")
    elif not any(ok for _, ok in proofs):
        out["fails"].append(f"{tag}: the section \"{proofs[0][0][:60]}\" holds no evidence AWS wrote: a CloudTrail "
                            "eventID, invokedBy, the agent's userAgent marker (app/kiro-ide) or aws-mcp.amazonaws.com. "
                            "A chat transcript is not evidence; four of five winners cited CloudTrail (R115; field "
                            "notes H99)")
    units = ["\n".join([h] + rows) for h, rows in tables] + [l for l in body if not l.lstrip().startswith("|")]
    compared = any(re.search(r"\d", plain_line(u)) and COMPARED.search(plain_line(u)) for u in units)
    if not compared:
        out["warns"].append(f"{tag}: no number stands against a baseline (vs, against, baseline): one reproducible "
                            "number against a baseline, with the command that made it (R116; field notes H100)")
    lane = str(getattr(args, "lane", None) or "").strip().lower()
    flat = " ".join(plain_line(l) for l in body)
    if lane.startswith("startup") and not (MONEY.search(flat) or BUSINESS_LINE.search(flat)):
        out["fails"].append(f"{tag}, startups lane: no price in currency and no subscription, per-seat, pilot or "
                            "adoption line: say who buys, who uses, and the price or the adoption path (R118; field "
                            "notes H98: three of four startup-lane winners gave one)")
    if not out["fails"] and not out["warns"]:
        out["oks"].append(f"{tag}: {n} words, a link in the first two lines, an expected-results table, proof AWS "
                          "wrote and a number against a baseline")
    return out


def record_findings(root: Path, data: dict, pub: Path, draft: Path, args) -> dict:
    """The links and the time `pb.py page capture` recorded in published.json beside the capture (R114)."""
    out = {"fails": [], "warns": [], "oks": [], "notes": []}
    jwhere = rel_name(root, pub.with_suffix(".json"))
    seen = set()
    for l in data.get("links") or []:
        raw = l.get("href_raw") if isinstance(l, dict) else None
        if not isinstance(raw, str) or raw in seen:
            continue
        seen.add(raw)
        level, how = link_spaces(raw, exact=True)
        core = raw.strip(URL_STRIPPED)
        m = re.match(r"(?i)^https?://([^/?#]*)", core)
        bad = host_problem(m.group(1)) if m and level != "FAIL" else ""
        text = str(l.get("text") or "").strip()[:40]
        label = f"the link \"{raw[:90]}\"" + (f" ({text})" if text else "")
        if level == "FAIL":
            out["fails"].append(f"published page: {label} {how}")
        elif bad:
            out["fails"].append(f"published page: {label} cannot open: {bad}")
        elif level == "WARN":
            out["warns"].append(f"published page: {label} {how}")
    fetched = instant_or_none(str(data.get("fetched_at") or ""))
    changed = _dt.datetime.fromtimestamp(draft.stat().st_mtime, _dt.timezone.utc)
    if fetched is None or fetched.tzinfo is None:
        out["warns"].append(f"published page: {jwhere} has no fetched_at instant, so its age is unknown")
    elif fetched + _dt.timedelta(seconds=1) < changed:
        again = f"pb.py page capture {data.get('url') or '<url>'}"
        out["fails"].append(f"published page: {jwhere} was captured {iso_z(fetched)}, before the draft "
                            f"{rel_name(root, draft)} last changed ({iso_z(changed)}): the capture is stale. Paste the "
                            f"draft into the page again if it changed, then `{again}`")
    if getattr(args, "online", False):
        page_host = urllib.parse.urlsplit(str(data.get("final_url") or data.get("url") or "")).hostname or ""
        todo = []
        for l in data.get("links") or []:
            u = l.get("href_resolved") if isinstance(l, dict) else None
            host = urllib.parse.urlsplit(u).hostname if isinstance(u, str) else None
            if host and u.startswith(("http://", "https://")) and host != page_host and u not in todo:
                todo.append(u)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(open_like_a_browser, todo[:40]))
        for u, (st, why) in zip(todo, answers):
            if st in (429, 999):
                out["warns"].append(f"published page: the link {u} answered {st} (rate-limited or refused to a "
                                    "script); open it signed out")
            elif st is None or st >= 400:
                out["fails"].append(f"published page: the link {u} does not open from here ({why or st})")
        out["oks"].append(f"published page: {len(todo[:40])} link(s) off the platform requested as a browser would")
    return out


def published_findings(root: Path, args) -> dict:
    """The live page, captured after publishing, against the draft that was pasted into it (R114). Calltree's page
    was 353 words of a 1,000-word draft: the coding-agent proof the rules required never reached it."""
    out = {"fails": [], "warns": [], "notes": [], "oks": [], "skipped": []}
    name = args.published
    pub = local_file(root, name)
    if pub is None:
        out["fails"].append(f"published page: {name} is not there. After publishing, capture the live page into it "
                            "with `pb.py page capture <url>`, or another browser that runs JavaScript, as markdown "
                            "(R114)")
        return out
    if getattr(args, "draft", None):
        draft = local_file(root, args.draft)
        dname = args.draft
    else:   # the R114 draft, in either case of its name, else the write-up being checked
        draft = on_disk(root, "docs/submission.md") or local_file(root, getattr(args, "writeup", None))
        dname = "docs/submission.md"
    if draft is None:
        out["fails"].append(f"published page: no draft to compare it with: {dname} is not there (pass --draft <the "
                            "text you pasted into the page>)")
        return out
    pwhere, dwhere = rel_name(root, pub), rel_name(root, draft)
    pub_text = pub.read_text(encoding="utf-8", errors="replace")
    draft_text = draft.read_text(encoding="utf-8", errors="replace")
    plines = reading_lines(pub_text)
    dlines = reading_lines(draft_text)
    pw, dw = count_words(plines), count_words(dlines)
    if not dw:
        out["fails"].append(f"published page: the draft {dwhere} has no words to compare with")
        return out
    share = draft_on_page(draft_text, pub_text)
    pct = round(100 * share)
    heads = draft_headings(dlines)
    on_page = {norm_line(l) for l in plines}
    page_heads = [norm_line(h) for h in heading_lines(plines)]
    # a heading counts as there when a line of the capture reads as it (a text capture keeps it as a plain line),
    # or a heading of the capture holds it whole ("3. Architecture")
    missing = [h for h in heads if norm_line(h) not in on_page
               and not any(f" {norm_line(h)} " in f" {x} " for x in page_heads)]
    out["notes"].append(f"published page: {pwhere} {pw} words, draft {dwhere} {dw} words; {pct}% of the draft's text "
                        f"is on the page (its runs of four words); {len(missing)} of the draft's {len(heads)} ## and "
                        "### headings missing" + (": " + "; ".join(missing) if missing else ""))
    loading = next((l.strip(" #*_>") for l in plines if SHELL_TEXT.search(l)), None)
    if pw < 80 or (loading and (pw < 200 or share < 0.1)):
        why = (f", \"{loading[:40]}\"). A plain HTTP fetch of a page drawn by JavaScript returns only this" if loading
               else "). If the page shows more, the capture missed it")
        out["fails"].append(f"published page: {pwhere} looks like an app shell or an empty page, not the entry ({pw} "
                            f"words{why}. Capture the live page again with `pb.py page capture <url>`, or another "
                            "browser that runs JavaScript, as markdown (R114)")
        return out
    if [l.rstrip() for l in plines if l.strip()] == [l.rstrip() for l in dlines if l.strip()]:
        out["warns"].append(f"published page: {pwhere} holds the draft's own text line for line. It must be a capture "
                            "of the live page made after publishing, or this compares the draft with itself (R114)")
    if share < PAGE_MIN_SHARE:
        out["fails"].append(f"published page: {pct}% of the draft {dwhere} is on {pwhere} (its runs of four words; "
                            f"under {round(100 * PAGE_MIN_SHARE)}%): part of the draft never reached the page. Paste "
                            "the rest, then capture the page again (R114; Calltree's page held 36% of its draft)")
    for h in missing:
        out["fails"].append(f"published page: the draft's heading \"{h}\" is not on the page")
    raw = pub_text.replace("&amp;", "&")
    live = getattr(args, "live", None)
    if not live:   # the first address on a line that calls it live, a demo or the app
        live = next((u for line in dlines if LIVE_TEXT.search(plain_line(line)) for u in page_urls([line])
                     if not VIDEO_HOST.search(u) and not CODE_HOST.search(u)), None)
    if live and live.rstrip("/") not in raw:
        out["warns"].append(f"published page: the live URL {live} is not on the page; put it in the first two lines "
                            "(R117)")
    textish = not re.search(r"\]\(|^\s{0,3}#{1,6}\s|^\s{0,3}(-{3,}|={3,})\s*$", pub_text, re.M)
    # a picture's address is left out: DEV serves each picture from its own uploads or CDN, so the draft's address
    # is not on the page even when the picture is
    for u in page_urls([re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", l) for l in dlines]):
        video = YOUTUBE_ID.match(u)   # a YouTube link the page shows as an embed is the same video
        if u.rstrip("/") not in raw and (not live or u.rstrip("/") != live.rstrip("/")) and not (
                video and re.search(r"(?i)youtu(\.be|be(-nocookie)?\.com)/[^\s)]*" + re.escape(video.group(1)), raw)):
            out["warns"].append(f"published page: the draft's link {u} is not in the capture"
                                + (" (a text capture keeps a link's words, not its address: capture as markdown)"
                                   if textish else ""))
    record, why = capture_record(pub)
    if why:
        out["notes"].append(f"published page: {why}")
    if record is not None:
        res = record_findings(root, record, pub, draft, args)
        for k in ("fails", "warns", "oks", "notes"):
            out[k] += res[k]
    else:   # a capture made another way: the links as the markdown wrote them
        for u in stored_links(raw):
            level, how = link_spaces(u)
            if level:
                out["fails" if level == "FAIL" else "warns"].append(f"published page: the link \"{u[:90]}\" {how}")
    if not missing and share >= PAGE_MIN_SHARE:
        out["oks"].append(f"published page: {pwhere} carries every ## and ### heading of {dwhere} and {pct}% of its "
                          "text")
    profile = getattr(args, "page_profile", None)
    if profile:
        res = profile_findings(pub_text.splitlines(), args, pwhere)
        for k in ("fails", "warns", "oks"):
            out[k] += res[k]
    pfails, pwarns = placeholder_findings(pub_text, pwhere)
    out["fails"] += pfails
    out["warns"] += pwarns
    return out


# ----------------------------------------------------------------------------- capturing the live page (R114)

# Run in the page: what a signed-out reader sees, as markdown. Headings as #, paragraphs, list items, simple tables,
# images as ![alt](src), and links as [text](href) with the href exactly as stored (spaces kept), each link also listed
# with the address the browser resolves it to. Hidden elements, scripts and buttons are left out.
PAGE_WALKER = r"""() => {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "SVG", "CANVAS", "OBJECT",
                        "EMBED", "SELECT", "OPTION", "TEXTAREA", "INPUT", "BUTTON"]);
  const BLOCK = new RegExp("^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|BODY|CAPTION|DD|DETAILS|DIALOG|DIV|DL|DT|FIELDSET|" +
                           "FIGCAPTION|FIGURE|FOOTER|FORM|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|SUMMARY|" +
                           "TABLE|TBODY|TD|TFOOT|TH|THEAD|TR|UL)$");
  const KEEP = "\uE000";   // a space inside an href, kept through the squashing of whitespace
  const links = [];
  const shown = el => { const s = getComputedStyle(el); return s.display !== "none" && s.visibility !== "hidden"; };
  const squash = s => s.replace(/[\s\u00a0]+/g, " ");
  const hasBlock = el => Array.from(el.querySelectorAll("*")).some(c => BLOCK.test(c.tagName));
  function inline(node) {
    if (node.nodeType === 3) return squash(node.nodeValue);
    const tag = node.nodeType === 1 ? node.tagName.toUpperCase() : "";   // an svg's own tags are lower case
    if (node.nodeType !== 1 || SKIP.has(tag) || !shown(node)) return "";
    if (tag === "BR") return " ";
    if (tag === "IMG") {
      const alt = squash(node.getAttribute("alt") || "").trim();
      return ` ![${alt}](${node.getAttribute("src") || node.currentSrc || ""}) `;
    }
    if (tag === "IFRAME") { const src = node.getAttribute("src"); return src ? ` [embedded frame](${src}) ` : ""; }
    const inner = Array.from(node.childNodes).map(inline).join("");
    if (tag === "A" && node.hasAttribute("href")) {
      const raw = node.getAttribute("href"), text = squash(inner).trim();
      if (!text && raw.startsWith("#")) return "";   // a heading's empty anchor: nothing a reader sees
      links.push({text: text, href_raw: raw, href_resolved: node.href});
      return `[${text}](${raw.replace(/\s/g, KEEP)})`;
    }
    if (tag === "CODE" && inner.trim() && !inner.includes("`")) return "`" + inner.trim() + "`";
    return inner;
  }
  function lines(node, out) {
    let cur = [];
    const flush = () => { const t = squash(cur.join("")).trim(); if (t) out.push(t); cur = []; };
    const gap = () => { flush(); if (out.length && out[out.length - 1] !== "") out.push(""); };
    for (const c of Array.from(node.childNodes)) {
      if (c.nodeType === 3) { cur.push(c.nodeValue); continue; }
      const tag = c.nodeType === 1 ? c.tagName.toUpperCase() : "";
      if (c.nodeType !== 1 || SKIP.has(tag) || !shown(c)) continue;
      if (/^H[1-6]$/.test(tag)) {
        gap(); const t = squash(inline(c)).trim(); if (t) out.push("#".repeat(+tag[1]) + " " + t); gap();
      } else if (tag === "A" && c.hasAttribute("href") && hasBlock(c)) {
        flush(); const t = squash(inline(c)).trim(); if (t) out.push(t);
      } else if (tag === "PRE") {
        gap(); out.push("```");
        for (const l of (c.innerText || c.textContent).replace(/\n$/, "").split("\n")) out.push(l);
        out.push("```"); gap();
      } else if (tag === "TABLE") {
        gap();
        Array.from(c.querySelectorAll("tr")).filter(shown).forEach((r, i) => {
          const cells = Array.from(r.children).filter(x => /^T[DH]$/.test(x.tagName));
          out.push("| " + cells.map(x => squash(inline(x)).trim().replace(/\|/g, "\\|")).join(" | ") + " |");
          if (i === 0) out.push("|" + cells.map(() => " --- |").join(""));
        });
        gap();
      } else if (tag === "UL" || tag === "OL") {
        gap(); let n = 0;
        for (const li of Array.from(c.children)) {
          if (li.tagName !== "LI" || !shown(li)) continue;
          n += 1;
          const body = lines(li, []).filter(l => l !== "");
          if (!body.length) continue;
          out.push((tag === "OL" ? `${n}. ` : "- ") + body[0]);
          for (const l of body.slice(1)) out.push("  " + l);
        }
        gap();
      } else if (tag === "BLOCKQUOTE") {
        gap(); for (const l of lines(c, [])) out.push(l ? "> " + l : ">"); gap();
      } else if (tag === "HR") {
        gap(); out.push("---"); gap();
      } else if (BLOCK.test(tag)) {
        flush(); const before = out.length; lines(c, out);
        const text = Array.from(c.childNodes).some(x => x.nodeType === 3 && x.nodeValue.trim());
        if (tag === "P" || (out.length > before && text)) gap();
      } else {
        cur.push(inline(c));
      }
    }
    flush();
    return out;
  }
  const out = lines(document.body, []);
  while (out.length && out[out.length - 1] === "") out.pop();
  return {markdown: out.join("\n").split(KEEP).join(" ") + "\n", title: document.title, links: links};
}"""


def cmd_page(args, root: Path) -> int:
    """`page capture <url>`: the live page as a signed-out browser draws it, into docs/published.md and a record of its
    links and the time into docs/published.json, for `submit check --published` (R114)."""
    out = Path(args.out) if Path(args.out).is_absolute() else root / args.out
    rec_path = out.with_suffix(".json")
    try:
        from playwright.sync_api import sync_playwright, Error as PwError
    except ImportError:
        print("SKIP  page capture: playwright-python is not installed here (pip install playwright, then python3 -m "
              "playwright install chromium). Nothing was captured, so a published.md already there is an old one. "
              "Capture the page another way, as markdown, with a browser that runs JavaScript (the scrapling MCP's "
              "fetch with extraction_type markdown and network_idle true), into " + rel_name(root, out))
        return 1
    status, final, waited = None, args.url, ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context().new_page()   # a fresh context: signed out, no cookies
                try:
                    resp = page.goto(args.url, wait_until="networkidle", timeout=args.timeout * 1000)
                    status = resp.status if resp else None
                except PwError as e:
                    if "Timeout" not in type(e).__name__ and "timeout" not in str(e).lower():
                        raise
                    waited = f"the network did not go quiet in {args.timeout} s; this is what was drawn by then"
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(500)
                final = page.url
                data = page.evaluate(PAGE_WALKER)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - a browser that will not start, or a page that will not load
        msg = str(e).strip().splitlines()[0][:200] if str(e).strip() else type(e).__name__
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            print(f"SKIP  page capture: Chromium for Playwright is not installed (python3 -m playwright install "
                  f"chromium). Nothing was captured ({msg})")
        else:
            print(f"FAIL  page capture: {args.url} did not load ({msg}). Nothing was captured")
        return 1
    md = data.get("markdown") or ""
    words = count_words(reading_lines(md))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    write_json(rec_path, {"url": args.url, "final_url": final, "status": status, "fetched_at": iso_z(now_utc()),
                          "title": data.get("title") or "", "words": words, "links": data.get("links") or []})
    print(f"captured {args.url} (status {status}, {words} words, {len(data.get('links') or [])} links) into "
          f"{rel_name(root, out)} and {rel_name(root, rec_path)}")
    if waited:
        print(f"WARN  {waited}")
    if status is not None and status >= 400:
        print(f"FAIL  the page answered {status}: a judge opening it signed out sees that")
        return 1
    print(f"next: pb.py submit check --published {rel_name(root, out)}")
    return 0


# ----------------------------------------------------------------------------- the cloud account on day 0 (R113)

def cmd_preflight(args, root: Path) -> int:
    """`preflight aws`: read-only checks of the account the build will use, recorded in the ledger. Who the
    credentials are, the Bedrock models the region lists and, with --model, whether the account may call it. Each call
    is free; none calls a model, so R113's one real call (and its cost) stays a separate, deliberate step."""
    try:
        import boto3
    except ImportError:
        print("SKIP  preflight aws: boto3 is not installed here (pip install boto3). Nothing was checked. By hand: "
              "`aws sts get-caller-identity` and `aws bedrock list-foundation-models --region <region>`, each recorded "
              "with `pb.py evidence add --method observed`")
        return 1
    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    lines, ok = [], True
    try:
        session = boto3.session.Session(region_name=region)
        who = session.client("sts").get_caller_identity()
        lines.append(f"identity: account {who.get('Account')}, {who.get('Arn')}")
        bedrock = session.client("bedrock", region_name=region)
        ids = [m.get("modelId") for m in bedrock.list_foundation_models().get("modelSummaries") or []]
        lines.append(f"bedrock {region}: {len(ids)} foundation models listed")
        if args.model:
            if args.model not in ids:
                ok = False
                lines.append(f"FAIL model {args.model} is not listed in {region}: pick a region that lists it, or an "
                             "inference profile")
            else:
                a = bedrock.get_foundation_model_availability(modelId=args.model)
                agree = (a.get("agreementAvailability") or {}).get("status")
                lines.append(f"model {args.model}: authorization {a.get('authorizationStatus')}, entitlement "
                             f"{a.get('entitlementAvailability')}, region {a.get('regionAvailability')}, agreement "
                             f"{agree}")
                if a.get("authorizationStatus") != "AUTHORIZED" or a.get("entitlementAvailability") != "AVAILABLE":
                    ok = False
                    lines.append("FAIL the account may not call it yet: open the model's access or the quota case "
                                 "now (R113), and plan the product to hold without it by the end of day 1")
    except Exception as e:  # noqa: BLE001 - no credentials, access denied, no network: each is the finding
        ok = False
        lines.append(f"FAIL {type(e).__name__}: {str(e)[:200]}")
    for line in lines:
        print(line)
    rec = append_evidence(root, {
        "task": args.task, "kind": "other", "method": "ran", "cmd": f"pb.py preflight aws --region {region}"
        + (f" --model {args.model}" if args.model else ""), "exit": 0 if ok else 1, "expect": "pass", "ok": ok,
        "git": git_info(root), "where": "local", "tail": [redact(x) for x in lines]})
    print(f"[{rec['id']}] {'PASS' if ok else 'FAIL'} preflight aws ({region}). Listing is not calling: a model can be "
          "listed and still refuse (\"Operation not allowed\"), so R113's one real call is still to be made and logged")
    return 0 if ok else 1


# ----------------------------------------------------------------------------- a DEV post (R110)

DEVTO_BY_HAND = ("python3 <devto-challenge skill folder>/scripts/devto_check.py {post} --headings \"<the round's "
                 "required headings>\"{money}")


def plugin_version(script: Path) -> tuple:
    """The version in the plugin.json above a script, () when there is none."""
    for up in list(script.parents)[:5]:
        pj = up / ".claude-plugin" / "plugin.json"
        if pj.is_file():
            try:
                v = json.loads(pj.read_text(encoding="utf-8")).get("version", "")
            except (OSError, ValueError, AttributeError):
                return ()
            return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])
    return ()


def find_devto_check() -> tuple:
    """(devto_check.py to run or None, where it was looked for). DEVTO_CHECK names it outright. Otherwise beside this
    plugin, then the installed plugins and skills (R110). Older copies stay beside the live one, so of several the
    highest plugin version wins."""
    env = os.environ.get("DEVTO_CHECK", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        return (p if p.is_file() else None), f"DEVTO_CHECK={env}"
    found = []
    skill = Path(__file__).resolve().parent.parent
    if (skill / "SKILL.md").is_file():   # not when run as a project's .prod-build/pb.py
        found = list(skill.parents[2].glob("devto-challenge*/skills/devto-challenge/scripts/devto_check.py"))
    if not found:
        home = Path.home() / ".claude"
        for base in (home / "plugins", home / "skills"):
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in (".trash", ".git", "node_modules", "__pycache__")]
                if "devto_check.py" in filenames and "devto-challenge" in dirpath:
                    found.append(Path(dirpath) / "devto_check.py")
    where = "DEVTO_CHECK, beside this plugin, ~/.claude/plugins and ~/.claude/skills"
    if not found:
        return None, where
    return max(found, key=lambda p: (plugin_version(p), p.as_posix())), where


def dev_post(ev: dict, post) -> tuple:
    """(why the entry is a DEV post or '', the event page's host)."""
    src = str(ev.get("source") or "").strip()
    m = re.match(r"(?i)^[a-z][a-z0-9+.-]*://([^/\s?#:@]+)", src)
    host = m.group(1).lower() if m else ("dev.to" if re.match(r"(?i)^(www\.)?dev\.to(/|$)", src) else "")
    if post is not None and "devchallenge" in front_tags(post.read_text(encoding="utf-8", errors="replace")):
        return "its front matter is tagged devchallenge", host
    if host == "dev.to" or host.endswith(".dev.to"):
        return "the event's page is on dev.to", host
    if ev.get("post_entry") and not host:
        return "--post-entry", host
    return "", host


def devto_findings(root: Path, args, ev: dict, post) -> dict:
    """A DEV post is checked by devto-challenge's devto_check.py, and its FAILs block like submit check's (R110)."""
    out = {"fails": [], "warns": [], "notes": [], "skipped": []}
    why, host = dev_post(ev, post)
    headings, money = getattr(args, "headings", None), bool(getattr(args, "money_theme", False))
    effort = bool(getattr(args, "effort_theme", False))
    if not why:
        if ev.get("post_entry") and post is None:
            out["skipped"].append(f"the post itself, which is this event's entry: its page is off DEV ({host}), so "
                                  "pass it as --writeup and check it against the event's own template (devto_check.py "
                                  "is DEV's; playbook R110)")
        if headings is not None or money:
            out["notes"].append("--headings and --money-theme go to devto_check.py, which runs only on a DEV post "
                                "(front matter tagged devchallenge, an event page on dev.to, or --post-entry)")
        return out
    by_hand = DEVTO_BY_HAND.format(post=rel_name(root, post) if post else "<post.md>",
                                   money=" --money-theme" if money else "")
    if post is None:
        out["skipped"].append(f"the post itself, which is this event's entry ({why}): pass it as --writeup <post.md> "
                              "and submit check runs devto-challenge's devto_check.py on it with the round's "
                              "--headings (playbook R110)")
        return out
    script, where = find_devto_check()
    if script is None:
        out["skipped"].append(f"the post itself, which is this event's entry ({why}): devto_check.py was not found "
                              f"({where}). Run it by hand: {by_hand}; or, without devto-challenge, do playbook R110's "
                              "checks by hand and say so in the report")
        return out
    cmd = [sys.executable, str(script), str(post.resolve())]
    if headings is not None:
        cmd += ["--headings", headings]
    if money:
        cmd.append("--money-theme")
    if effort:
        cmd.append("--effort-theme")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(root))
    except (OSError, subprocess.SubprocessError) as e:
        out["fails"].append(f"devto_check: {script} did not run ({e}); run it by hand: {by_hand}")
        return out
    said = 0
    for line in p.stdout.splitlines():
        m = re.match(r"^(FAIL|WARN)\s+(.*\S)", line)
        if m:
            (out["fails"] if m.group(1) == "FAIL" else out["warns"]).append("devto_check: " + m.group(2))
            said += m.group(1) == "FAIL"
    if p.returncode != 0 and not said:
        last = (p.stderr.strip().splitlines() or p.stdout.strip().splitlines() or ["no output"])[-1]
        out["fails"].append(f"devto_check: exited {p.returncode} without a verdict ({last[:120]}); run it by hand: "
                            f"{by_hand}")
    if headings is None:
        out["warns"].append("devto_check ran without the round's --headings, so a missing template heading only warns: "
                            "pass --headings \"<the round's required headings, word for word>\" (playbook R110)")
    out["notes"].append(f"devto_check: {script} on {rel_name(root, post)} ({why})")
    return out


# A rule that wants the video public (the Strands rules: "uploaded to and made publicly visible on YouTube, Vimeo,
# Facebook Video, or Youku"). An unlisted video is not public: Calltree's demo video was unlisted [its watch page].
VIDEO_RULE = re.compile(r"(?i)\b(videos?|youtube|vimeo|demo|film|recording)\b")
YOUTUBE_ID = re.compile(r"(?i)^\s*(?:https?://)?(?:www\.|m\.)?"
                        r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)([\w-]{11})")


def youtube_visibility(vid: str) -> tuple:
    """('public' | 'unlisted' | 'private' | 'unavailable' | 'unknown', why) from the video's watch page, signed out."""
    req = urllib.request.Request(net_map(f"https://www.youtube.com/watch?v={vid}&hl=en"),
                                 headers={"User-Agent": BROWSER_UA, "Accept-Language": "en",
                                          "Cookie": "SOCS=CAI; CONSENT=YES+"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(4_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return ("unavailable" if e.code in (404, 410) else "unknown"), f"the watch page answers {e.code}"
    except Exception as e:  # noqa: BLE001 - any network failure leaves the visibility unknown
        return "unknown", str(getattr(e, "reason", e))[:80]
    m = re.search(r'"playabilityStatus":\{"status":"([A-Z_]+)"(?:,"reason":"((?:[^"\\]|\\.)*)")?', body)
    status, reason = (m.group(1), m.group(2) or "") if m else ("", "")
    if '"isUnlisted":true' in body:
        return "unlisted", "its watch page says isUnlisted"
    if '"isPrivate":true' in body or (status == "LOGIN_REQUIRED" and "private" in reason.lower()):
        return "private", reason or "its watch page says it is private"
    if status in ("ERROR", "UNPLAYABLE"):
        return "unavailable", reason or status
    if status == "OK" and '"isUnlisted":false' in body:
        return "public", ""
    return "unknown", f"the watch page did not say (status {status or 'none'}{': ' + reason if reason else ''})"


def submit_findings(root: Path, args, ev: dict) -> dict:
    """Everything a judge opens, checked the way a judge meets it: card, README first screen, links, history,
    video file. FAIL = a judge would see something broken, false or embarrassing."""
    fails, warns, notes, oks, skipped = [], [], [], [], []
    card = int(getattr(args, "card_chars", None) or ev.get("card_chars") or 180)
    sponsors = [str(s) for s in (getattr(args, "sponsor", None) or ev.get("sponsors") or [])]
    if not sponsors:
        skipped.append("sponsor names on the page (none set: `pb.py clock set --sponsor <names>` or --sponsor here)")

    # --- README
    readme = find_readme(root, getattr(args, "readme", None))
    text = ""
    if not readme:
        fails.append("no README: the repository page is the first thing a judge reads after the video")
    else:
        text = readme.read_text(encoding="utf-8", errors="replace")
        try:
            where = readme.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            where = readme.name
        lines = text.splitlines()
        nonempty = [l for l in lines if l.strip()]
        head = nonempty[:40]
        head_text = "\n".join(head)
        stmt = first_statement(nonempty[:14])
        if stmt:
            oks.append(f"README opens with a statement: \"{stmt[:110]}\"")
        else:
            warns.append("README first screen has no plain one-sentence statement of what the product does "
                         "(one sentence a judge could repeat, before any badge wall or architecture)")
        if MD_IMAGE.search(head_text):
            oks.append("README has a screenshot or image in its first screen")
        else:
            warns.append("no screenshot in the README's first 40 lines (organisers ask for one at the top)")
        links = [m.group(2) for m in MD_LINK.finditer(text)] + BARE_URL.findall(text)
        head_links = [m.group(2) for m in MD_LINK.finditer(head_text)] + BARE_URL.findall(head_text)
        want_video, want_live = getattr(args, "video_url", None), getattr(args, "live", None)
        vids = [u for u in links if VIDEO_HOST.search(u)]
        if want_video and want_video.rstrip("/") not in text:
            warns.append(f"README does not link the submitted video ({want_video}); put it in the first screen")
        elif not vids:
            warns.append("README has no demo video link; put it in the first screen, with its length")
        elif not any(VIDEO_HOST.search(u) for u in head_links):
            warns.append("the demo video link is below the README's first screen; move it to the top")
        else:
            oks.append("demo video link is in the README's first screen (not opened from here)")
        live = [u for u in links if u.startswith("http") and not VIDEO_HOST.search(u) and not CODE_HOST.search(u)
                and not PLACEHOLDER_URL.match(u)]
        if want_live and want_live.rstrip("/") not in text:
            fails.append(f"README does not link the submitted live URL ({want_live})")
        elif want_live and want_live.rstrip("/") not in head_text:
            warns.append("the live link is below the README's first screen; move it to the top")
        elif not live:
            warns.append("README has no live link (at one event 78% of the shortlisted 50 had one against 51% of the "
                         "rest; at another 70% of all entries had one and it did not separate winners)")
        elif not any(u in head_links for u in live):
            warns.append("the live link is below the README's first screen; move it to the top")
        else:
            oks.append("live link is in the README's first screen (not opened from here)")
        pf, pw = placeholder_findings(text, where)
        fails += pf
        warns += pw
        words_n = len(re.findall(r"\S+", text))
        if words_n > 3000:
            notes.append(f"README is {words_n} words: a judge has minutes, so the statement, the picture and the "
                         "links have to come before everything else")
        if LIMITS_HEAD.search(text):
            oks.append("README says what is real, simulated or not built")
        else:
            notes.append("README has no section on limits (what is simulated, what is not built, what the data "
                         "cannot see)")
        if AI_TOOLS.search(text):
            oks.append("README names the AI tools used")
        else:
            (warns if ev.get("ai_disclosure") else notes).append(
                "README does not say which AI coding tools were used" +
                (" (this event's rules ask for the list; it also goes in the form)" if ev.get("ai_disclosure")
                 else " (many events ask for the list)"))
        for s in sponsors:
            n = len(re.findall(re.escape(s), text, flags=re.I))
            if n == 0:
                fails.append(f"README never names {s}, which this event requires or rewards")
            elif n < 3 or not re.search(re.escape(s), head_text, flags=re.I):
                warns.append(f"README names {s} {n} time(s)" + ("" if re.search(re.escape(s), head_text, flags=re.I)
                             else " and not in its first screen") + ": say which step it performs, where a judge "
                             "sees it. Counting the name is all this check does; whether it is really used is yours "
                             "to show")
            else:
                oks.append(f"README names {s} {n} times, starting in its first screen (the name, not the use, "
                           "is what was counted)")
        if ev.get("start") and ev.get("history", "strict") == "strict" and not WINDOW_LINE.search(text):
            notes.append("README does not say when it was built: when the history is clean, `pb.py window` prints a "
                         "first-commit line you can state truthfully")

    # --- how the write-up spends its words (README, and the results write-up when it is a separate file)
    research = bool(ev.get("research") or getattr(args, "research", False))
    wu = getattr(args, "writeup", None)
    if wu:
        wp = Path(wu)
        if not wp.is_file() and not wp.is_absolute() and (root / wu).is_file():
            wp = root / wu
        if not wp.is_file():
            fails.append(f"write-up not found: {wu}")
        else:
            f_, w_, n_, o_ = writeup_findings(wp.read_text(encoding="utf-8", errors="replace"), wp.name, research,
                                              scored=True)
            fails += f_
            warns += w_
            notes += n_
            oks += o_
    if text:
        f_, w_, n_, o_ = writeup_findings(text, "README", research and not wu)
        fails += f_
        warns += w_
        notes += n_
        oks += o_
    if research and not wu:
        notes.append("research event: the README was read as the write-up; pass --writeup <file> when the results "
                     "write-up is a separate file")

    # --- files that should not be in a judged repository
    if repo_state(root)["kind"] == "none":
        files = []
        skipped.append("files in the repository (this folder is not a git repository yet)")
    else:
        files = tracked_files(root)

    # --- every judge-facing document, not only the README (H63): the write-up and each top-level .md file
    readme_rel = None
    if readme:
        try:
            readme_rel = readme.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            readme_rel = None
    docs = []
    if wu:
        wp2 = Path(wu) if Path(wu).is_absolute() else root / wu
        if wp2.is_file():
            docs.append(wp2)
    for f in files:
        if "/" in f or not f.lower().endswith((".md", ".mdx", ".markdown")) or f == readme_rel:
            continue
        if INTERNAL_PATHS.search(f) or re.match(r"(?i)^(changelog|license|licence|code_of_conduct|security|"
                                               r"contributing)\b", f):
            continue
        p = root / f
        if p.is_file() and all(p.resolve() != d.resolve() for d in docs):
            docs.append(p)
    for d in docs:
        try:
            dwhere = d.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            dwhere = d.name
        dtext = d.read_text(encoding="utf-8", errors="replace")
        pf, pw = placeholder_findings(dtext, dwhere)
        fails += pf
        warns += pw
        f_, w_ = path_findings(root, dtext, dwhere, d.parent, files)
        fails += f_
        warns += w_
    if text and readme:
        f_, w_ = path_findings(root, text, readme_rel or readme.name, readme.parent, files)
        fails += f_
        warns += w_

    # --- the event's own hard rules (H54): required files, scored sections, things only a person can confirm
    page_rules = False
    if has_event(ev):
        rl = load_rules(root)
        if rl is None:
            fails.append("no rules ledger: copy every hard rule from the event's pages into `pb.py rules add` "
                         "(required files, what the video must show, limits on human input, eligibility), or record "
                         "`pb.py rules none --source <page> --why '<what the page says>'`. At one event the room "
                         "export and the room recording were both required and neither was checked")
        else:
            rf, rw, ro = rules_findings(root, rl, files)
            fails += rf
            warns += rw
            oks += ro
            done = {Path(wu).name if wu else None, readme.name if readme else None}
            for f in dict.fromkeys(r.get("file") for r in rl.get("rules", []) if r.get("kind") == "section"):
                draft = bool(f) and is_draft(f)
                page_rules = page_rules or draft
                fp = on_disk(root, f) if draft else (root / f if f else None)
                if not fp or not fp.is_file() or fp.name in done or (files and f not in files and not draft):
                    continue
                ftext = fp.read_text(encoding="utf-8", errors="replace")
                f_, w_, n_, o_ = writeup_findings(ftext, fp.name, research, scored=True)
                fails += f_
                warns += w_
                notes += n_
                oks += o_
                if draft and all(fp.resolve() != d.resolve() for d in docs):
                    # the page's draft is what judges read once it is pasted: unfinished text fails here too
                    pf, pw = placeholder_findings(ftext, rel_name(root, fp))
                    fails += pf
                    warns += pw
    internal = [f for f in files if INTERNAL_PATHS.search(f) and not f.startswith(".prod-build/")]
    drafts = [f for f in files if (DRAFT_PATHS.search(f) or POST_DRAFT_PATHS.search(f))
              and not f.startswith(".prod-build/")]
    sessions = [f for f in files if SESSION_FILES.search(f)]
    first_user = [f for f in files if FIRST_USER_PATHS.search(f)]
    if first_user:
        fails.append("a first user's own material is in the repository: " + ", ".join(first_user[:4])
                     + (" …" if len(first_user) > 4 else "") + ". It is theirs (chats, recordings, other people's "
                     "names): remove it from the branch and from its history, and commit only a de-identified "
                     "excerpt (playbook R104)")
    if sessions:
        fails.append("a browser session file is in the repository: " + ", ".join(sessions[:4]) + ". It holds live "
                     "sign-in cookies: remove it from the repository and from its history, and sign that session out")
    if internal:
        fails.append(f"{len(internal)} internal planning file(s) are in the repository: " + ", ".join(internal[:6])
                     + (" …" if len(internal) > 6 else "") + " (idea packages and judging notes are not for judges; "
                     "take them off the judged branch)")
    elif files:
        oks.append("no idea-package or judging-notes files in the repository")
    if drafts:
        warns.append("filming or submission drafts are in the repository: " + ", ".join(drafts[:6])
                     + " (a judge can open them; check that every sentence in them is true, or take them out)")
    talk = []
    for f in files:
        if not f.lower().endswith((".md", ".mdx", ".txt")) or INTERNAL_PATHS.search(f) or SKIP_DIRS.search(f):
            continue
        p = root / f
        try:
            if p.is_file() and p.stat().st_size < 400_000 and INTERNAL_PHRASES.search(
                    p.read_text(encoding="utf-8", errors="replace")):
                talk.append(f)
        except OSError:
            continue
        if len(talk) >= 5:
            break
    if talk:
        warns.append("these files talk about working the judges: " + ", ".join(talk) + " (read them as a judge would)")
    own = [f for f in files if f.startswith(".prod-build/") and f.lower().endswith((".md", ".json"))]
    if own:
        notes.append(f"the build's own records are in the repository ({len(own)} files under .prod-build/: contract, "
                     "plan, briefs, reports). They are public with it: read the contract's event section as a judge "
                     "would")

    # --- history against the window
    if ev.get("start") and not ev.get("no_window"):
        start, end = parse_instant(ev["start"]), parse_instant(ev["end"])
        res = window_findings(root, start, end, ev.get("tz_min"), provisional=bool(ev.get("provisional")))
        strict = ev.get("history", "strict") == "strict"
        for f in res["fails"]:
            (fails if strict else warns).append("history: " + f + (". " + NOT_FIXABLE if strict and "dated before"
                                                                    in f else ""))
        warns += ["history: " + w for w in res["warns"]]
        rows = res["rows"]
        if not rows:
            skipped.append("history vs build window (" + res["info"][0] + ")")
        elif not res["fails"]:
            oks.append("history: no commit on HEAD is dated before the start"
                       + (" (the start is provisional: confirm the clock first)" if ev.get("provisional") else ""))
        if rows:
            last = max(r[2] for r in rows)
            window = end - start
            late_span = max(_dt.timedelta(hours=2), window * 0.05)
            late = [r for r in rows if end - late_span <= r[2] <= end]
            if end >= last >= end - _dt.timedelta(minutes=30):
                warns.append(f"history: the last commit landed {span(end - last)} before the deadline; whatever a "
                             "judge opens is then the least-tested state (no judge has said this cost a mark)")
            elif late:
                warns.append(f"history: {len(late)} commit(s) in the final {span(late_span)}; check that none "
                             "changed what the video shows")
    elif ev.get("no_window"):
        skipped.append("history vs build window (this event has none: " + str(ev["no_window"])[:80] + ")")
    else:
        skipped.append("history vs build window (no event clock: `pb.py clock set` or --start/--end)")

    # --- the description as the judge queue shows it
    desc = getattr(args, "description", None)
    if desc:
        try:
            p = Path(desc)
            if len(desc) < 400 and "\n" not in desc and p.is_file():
                desc = p.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
        desc = desc.strip()
        flat = re.sub(r"\s+", " ", desc)
        shown = flat[:card] + ("…" if len(flat) > card else "")
        notes.append(f"judge card as the queue shows it ({card} characters; read it, nothing here judges it): | {shown}")
        first = re.split(r"(?<=[.!?])\s+", flat, maxsplit=1)[0]
        if len(first) > card:
            warns.append(f"description: the first sentence is {len(first)} characters and the card cuts it at {card}")
        if len(re.findall(r"\S+", first)) < 5:
            warns.append("description: the first sentence is a fragment; say what the product is and does in it")
        mx = getattr(args, "desc_max", None)
        if mx and len(desc) > mx:
            fails.append(f"description is {len(desc)} characters; the form allows {mx}")
        for s in sponsors:
            if not re.search(re.escape(s), flat[: max(card, 300)], flags=re.I):
                warns.append(f"description: {s} is not named in the opening lines")
        pf, pw = placeholder_findings(desc, "description")
        fails += pf
        warns += pw
    else:
        skipped.append("submission description (--description <text or file>): the queue card was not checked")

    # --- the video file
    video = getattr(args, "video", None)
    cap = getattr(args, "video_cap", None) or ev.get("video_cap_s")
    if video:
        vp = Path(video)
        info = probe_video(vp) if vp.exists() else None
        if not vp.exists():
            fails.append(f"video file not found: {video}")
        elif info is None:
            skipped.append("video (ffprobe is missing, or the file has no picture it could read)")
        else:
            bad_v = False
            desc_v = f"{info['duration']:.0f} s, {info['width']}x{info['height']}, {info['fps']:.0f} fps"
            if not cap:
                skipped.append("video length limit (none set: `pb.py clock set --video-cap <seconds>` or --video-cap "
                               "here)")
            elif info["duration"] > float(cap):
                fails.append(f"video is {info['duration']:.0f} s; the limit is {cap} s")
                bad_v = True
            elif info["duration"] > float(cap) - 2:
                fails.append(f"video is {info['duration']:.1f} s against a limit of {cap} s: the host can show it a "
                             "second longer than the file, so leave two seconds")
                bad_v = True
            elif float(cap) >= 90 and info["duration"] < 0.85 * float(cap):
                warns.append(f"video is {info['duration']:.0f} s of an allowed {cap} s: when the video is all a judge "
                             "sees, a short one leaves the flow, the sponsor's part or the failure case unshown")
            if info["fps"] and round(info["fps"]) < 24:
                fails.append(f"video is {info['fps']:.0f} fps; deliver 30 (screen motion stutters below 24)")
                bad_v = True
            elif info["fps"] and info["avg_fps"] and info["avg_fps"] < 0.8 * info["fps"]:
                warns.append(f"video has a variable frame rate (nominal {info['fps']:.0f}, average "
                             f"{info['avg_fps']:.0f} fps): re-encode at a constant 30 so motion is even")
            if info["height"] and info["height"] < 720:
                fails.append(f"video is {info['width']}x{info['height']}; deliver at least 1280x720")
                bad_v = True
            if not info["audio"]:
                warns.append("video has no audio track: a silent video makes the judge read every claim")
            if not bad_v:
                oks.append("video file: " + desc_v)
    else:
        skipped.append("video file (--video <file>): length, frame rate and size were not checked")

    # --- links, only when asked: this touches the network
    urls = [u for u in (getattr(args, "live", None), getattr(args, "video_url", None)) if u]
    if getattr(args, "online", False):
        if text:
            seen = [u for u in dict.fromkeys(BARE_URL.findall(text)) if not re.search(r"localhost|127\.0\.0\.1", u)]
            urls += [u.rstrip(".,;") for u in seen[:20] if u.rstrip(".,;") not in urls]
        must = {u for u in (getattr(args, "live", None), getattr(args, "video_url", None)) if u}
        for u in urls:
            st, _, secs, _ = fetch(u, 20)
            if st is None or st >= 400:
                (fails if u in must else warns).append(f"link {u} -> {st or 'no response'}")
            elif secs > 8 and u in must:
                warns.append(f"link {u} took {secs}s: warm it before judging opens")
        oks.append(f"{len(urls)} link(s) requested from here; still open the video, the live URL and the repository "
                   "in a signed-out browser")
    else:
        skipped.append("links (--online): none were opened; check the video, live URL and repository signed out")

    # --- the video's visibility, where a recorded rule wants it public
    wants = [r for r in ((load_rules(root) or {}).get("rules") or [] if has_event(ev) else [])
             if re.search(r"(?i)\bpublic", str(r.get("quote", "")))
             and VIDEO_RULE.search(f"{r.get('quote', '')} {r.get('id', '')}")]
    vurl = getattr(args, "video_url", None)
    if wants:
        r0 = wants[0]
        label = (("DISQUALIFYING: " if r0.get("dq") else "")
                 + f"rule {r0.get('id')} (\"{str(r0.get('quote', ''))[:70]}\")")
        m = YOUTUBE_ID.match(vurl or "")
        if not vurl:
            skipped.append(f"the video's visibility (--video-url <its link> --online): {label} wants it public, and "
                           "an unlisted video is not")
        elif not m:
            notes.append(f"the video's visibility is read on YouTube only: open {vurl} signed out and check that it "
                         f"is public, as {label} asks")
        elif not getattr(args, "online", False):
            skipped.append(f"the video's visibility (--online): {label} wants it public, and an unlisted video is not")
        else:
            state, why = youtube_visibility(m.group(1))
            if state == "public":
                oks.append(f"the video {vurl} is public on YouTube, as {label} asks")
            elif state in ("unlisted", "private", "unavailable"):
                fails.append(f"{label}: the video {vurl} is {state} on YouTube ({why}). Set it to Public in YouTube "
                             "Studio, then open it signed out")
            else:
                warns.append(f"the video's visibility could not be read ({why}): open {vurl} signed out and check "
                             f"that it is public, as {label} asks")

    # --- the page as published, against the draft pasted into it (R114)
    if getattr(args, "published", None):
        res = published_findings(root, args)
        fails += res["fails"]
        warns += res["warns"]
        notes += res["notes"]
        oks += res["oks"]
        skipped += res["skipped"]
        if not getattr(args, "page_profile", None) and re.search(r"(?i)^(https?://)?builder\.aws\.com\b",
                                                                 str(ev.get("source") or "")):
            notes.append("the event is on AWS Builder Center: add --page-profile builder-center (and --lane startups "
                         "in that lane) to check the page against what its winners carried (R114)")
    elif (has_event(ev) and (ev.get("post_entry") or page_rules)) or getattr(args, "draft", None):
        skipped.append("the page as published (--published <capture>): after publishing, capture the live page with "
                       "`pb.py page capture <url>` into docs/published.md and compare it with the draft (R114)")
    if getattr(args, "page_profile", None) and not getattr(args, "published", None):
        skipped.append(f"the page profile {args.page_profile} (it reads the capture of the live page: pass "
                       "--published docs/published.md)")

    # --- a DEV post: devto-challenge's checker, whose FAILs block like these (R110)
    res = devto_findings(root, args, ev, local_file(root, wu))
    fails += res["fails"]
    warns += res["warns"]
    notes += res["notes"]
    skipped += res["skipped"]
    return {"fails": fails, "warns": warns, "notes": notes, "oks": oks, "skipped": skipped}


def cmd_submit(args, root: Path) -> int:
    ev = load_event(root) or {}
    if event_flags_given(args):
        ev = build_event(args, ev)
    res = submit_findings(root, args, ev)
    for o in res["oks"]:
        print(f"ok    {o}")
    for s in res["skipped"]:
        print(f"SKIP  {s}")
    for n in res["notes"]:
        print(f"NOTE  {n}")
    for w in res["warns"]:
        print(f"WARN  {w}")
    for f in res["fails"]:
        print(f"FAIL  {f}")
    bad = bool(res["fails"]) or (args.strict and bool(res["warns"]))
    print(f"submit check: {len(res['fails'])} fail, {len(res['warns'])} warn, {len(res['skipped'])} not checked"
          f" -> {'NOT READY' if bad else 'ready, with the unchecked items still yours'}")
    record_check(root, "pb.py " + ARGV_TEXT[0], bad,
                 [f"FAIL {f}" for f in res["fails"]] + [f"WARN {w}" for w in res["warns"]]
                 + [f"SKIP {s}" for s in res["skipped"]], getattr(args, "task", None))
    return 1 if bad else 0


def event_gate_for_start(root: Path, plan: dict, t: dict, force: bool, note: str) -> tuple:
    """Refuse to start build work outside the event window. Returns (line to print or None, prefix for the
    task's note when the gate was overridden or None)."""
    mode = plan.get("mode")
    ev = load_event(root)
    if not has_clock(ev):
        if mode != "hackathon":
            return None, None
        phase = "no clock"
        why = ("this is a hackathon plan with no event clock. Set it before any work starts: `pb.py clock set "
               "--start <instant> --end <instant> --source <event page>`, or `--no-window '<the rule, quoted>'` where "
               "the event has none. If the event is over and this is now a product, set \"mode\": \"product\" in "
               ".prod-build/plan.json and say so")
    elif ev.get("no_window"):
        return None, None
    else:
        ph = event_phase(ev)
        phase, why = ph["phase"], None
        kind = str(t.get("kind") or "").strip().lower()
        if phase == "before" and ev.get("history", "strict") == "strict":
            why = (f"the build window opens in {span(ph['start'] - ph['now'])} ({iso_z(ph['start'])}). Project work "
                   "starts when the clock does. " + PREP_ALLOWED)
        elif phase == "closed" and mode == "hackathon":
            why = (f"the build window closed {span(ph['now'] - ph['end'])} ago. Nothing built now is part of the "
                   "submission; after the deadline touch only a dead link, and say so. To keep building this as a "
                   "product, set \"mode\": \"product\" in .prod-build/plan.json and stay off the judged branch until "
                   "judging closes")
        elif phase == "closed":
            return (f"NOTE  the event's build window closed {span(ph['now'] - ph['end'])} ago: keep this work off the "
                    "judged branch until judging closes"), None
        elif phase == "freeze" and kind not in FREEZE_KINDS:
            why = (f"{span(ph['end'] - ph['now'])} to the deadline. " + FREEZE_MEANS +
                   f" This task's kind is {kind or 'unset'}; freeze work is one of: {', '.join(FREEZE_KINDS)}")
        if not why:
            if ev.get("provisional") and phase in ("before", "building"):
                return (f"WARN  the event clock is provisional ({ev['provisional']}): re-read the instants from "
                        f"{ev.get('source') or 'the event page'}, then `pb.py clock set --start .. --end .. "
                        "--confirmed`"), None
            return None, None
    if force and note:
        return (f"WARN  EVENT GATE OVERRIDDEN ({phase}): {why}",
                f"EVENT GATE OVERRIDE ({phase}, {iso_z(now_utc())}{', test clock' if fake_now() else ''}): ")
    die(f"{t.get('id')} not started: {why}. Override only with --force --note '<why this is within the rules>'.", 1)


# ----------------------------------------------------------------------------- status

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def builder_lines(tasks: dict, now: _dt.datetime) -> tuple:
    """(overdue lines, the other open builder tasks): steps only the person can take (owner "builder"), by due time,
    each in UTC and in IST."""
    late, rest = [], []
    for tid, t in tasks.items():
        if str(t.get("owner") or "").strip().lower() != "builder" or t.get("status", "todo") in ("done", "skip"):
            continue
        d = instant_or_none(str(t.get("due") or ""))
        d = d.astimezone(_dt.timezone.utc) if d is not None and d.tzinfo is not None else None
        when = f"due {iso_z(d)} ({d.astimezone(IST).strftime('%a %d %b %H:%M')} IST)" if d else "no due time"
        if d and d < now:
            late.append((d, f"OVERDUE {tid} {t.get('title')}: {when}, {span(now - d)} ago (yours, not the agent's)"))
        else:
            rest.append((d or _dt.datetime.max.replace(tzinfo=_dt.timezone.utc),
                         f"  {tid} {t.get('title')}: {when}" + (f", in {span(d - now)}" if d else "")))
    return [x for _, x in sorted(late)], [x for _, x in sorted(rest)]


def cmd_status(args, root: Path) -> int:
    plan = read_json(pb_dir(root) / "plan.json")
    if plan is None:
        print("no .prod-build/plan.json here; run `pb.py init --mode <mode>`")
        return 1
    tasks = task_map(plan)
    late, mine = builder_lines(tasks, now_utc())
    for line in late:
        print(line)
    print(f"{plan.get('project')} | mode {plan.get('mode')} | channel {plan.get('channel')} | base {plan.get('base')}")
    ev = load_event(root)
    if ev or plan.get("mode") == "hackathon":
        print(clock_summary(ev))
    counts = {s: 0 for s in STATUSES}
    for t in tasks.values():
        counts[t.get("status", "todo")] = counts.get(t.get("status", "todo"), 0) + 1
    print("tasks: " + "  ".join(f"{k} {v}" for k, v in counts.items()))
    for t in tasks.values():
        if t.get("status") in ("doing", "blocked"):
            last = str(t.get("notes", "")).strip().splitlines()[-1:] or [""]
            print(f"  {t['id']} [{t['status']}] {t.get('title')}  {last[0][:80]}")
    if mine:
        print("the builder's own steps:")
        for line in mine:
            print(line)
    if not find_cycle(tasks):
        ready = [k for k, t in tasks.items() if t.get("status", "todo") == "todo"
                 and str(t.get("owner") or "").strip().lower() != "builder"
                 and all(tasks.get(d, {}).get("status") in ("done", "skip") for d in (t.get("deps") or []))]
        if ready:
            print("ready: " + ", ".join(sorted(ready)))
    ledger = read_ledger(root)
    fails = [e for e in ledger if not e.get("ok")]
    print(f"evidence: {len(ledger)} entries, {len(fails)} failing" + (f", last {ledger[-1].get('id')}" if ledger else ""))
    for e in ledger[-3:]:
        print(f"  {e.get('id')} {'ok' if e.get('ok') else 'FAIL'} {(e.get('cmd') or e.get('summary') or '')[:80]}")
    chain = verify_chain(ledger)
    if chain:
        print(f"LEDGER PROBLEM: {chain[0]}")
    recs = [r for r in all_records(root, include_archive=False) if r.get("status") == "active"]
    stale = [r.get("id") for r in recs if staleness(root, r) in ("needs-verify", "dead-citation")]
    by = {}
    for r in recs:
        by[r.get("type")] = by.get(r.get("type"), 0) + 1
    print("memory: " + (", ".join(f"{k} {v}" for k, v in by.items()) or "empty") +
          (f" | stale: {', '.join(stale[:8])}" if stale else ""))
    return 0


# ----------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pb.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--root", help="project root (default: nearest folder with .prod-build/ or .git)")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def event_flags(sp):
        sp.add_argument("--start", help="build window opens: ISO instant with a zone (2026-03-14T09:00:00Z)")
        sp.add_argument("--end", help="build window closes: ISO instant with a zone")
        sp.add_argument("--freeze", help="stop building at this instant (default: the last 12%% of the window, at "
                        "least 2 h, at most 24 h, never more than a quarter of it)")
        sp.add_argument("--event", help="event name")
        sp.add_argument("--source", help="the event page the instants and rules came from")
        sp.add_argument("--history", choices=("strict", "none"),
                        help="strict (default): nothing may predate the start; none: the rules allow earlier work")
        sp.add_argument("--provisional", help="the instants are not final yet: say why (the page says hours are "
                        "being finalised, or they were worked out from a relative time)")
        sp.add_argument("--confirmed", action="store_true", help="the instants were re-read from the event page; "
                        "clears --provisional")
        sp.add_argument("--no-window", dest="no_window", help="this event has no build window: quote the rule")
        sp.add_argument("--tz", help="offset for local times, e.g. +05:30 or -07:00 (default: the offset --start "
                        "was written in)")
        sp.add_argument("--video-cap", dest="video_cap", type=int, help="video limit in seconds")
        sp.add_argument("--card-chars", dest="card_chars", type=int,
                        help="characters of the description a judge sees before opening the entry (default 180)")
        sp.add_argument("--judge-minutes", dest="judge_minutes", type=float, help="minutes a judge has per project")
        sp.add_argument("--sponsor", nargs="+", help="names the event requires or rewards on the page and in the video")
        sp.add_argument("--ai-disclosure", dest="ai_disclosure", action="store_true",
                        help="the rules require listing the AI tools used")
        sp.add_argument("--research", action="store_true",
                        help="the event wants something found out (prod-idea's research track): the write-up leads "
                             "with the finding")
        sp.add_argument("--post-entry", dest="post_entry", action="store_true",
                        help="the post is the entry (prod-idea's writeup track, such as a DEV challenge): plan check "
                             "asks for the playbook's R103-R112 tasks and submit check lists the post check")

    s = sub.add_parser("init")
    s.add_argument("--mode", choices=MODES)
    s.add_argument("--channel", choices=CHANNELS, default="direct")
    s.add_argument("--name")
    s.add_argument("--checklists", action="store_true",
                   help="relay: copy the security/verification/deploy/AI checklists into .prod-build/checklists/")
    event_flags(s)

    s = sub.add_parser("clock", help="the event's build window: phase, time left, what is allowed now")
    s.add_argument("action", nargs="?", choices=("show", "set"), default="show")
    event_flags(s)

    s = sub.add_parser("window", help="check the repository's history against the build window")
    event_flags(s)

    s = sub.add_parser("rules", help="the event's hard rules: what must be there, what only a person can confirm")
    s.add_argument("action", nargs="?", choices=("list", "check", "add", "confirm", "none"), default="list")
    s.add_argument("id", nargs="?", help="with confirm: the rule's id")
    s.add_argument("--id", dest="id_flag")
    s.add_argument("--quote", help="the rule in the event's own words")
    s.add_argument("--source", help="where it is written")
    s.add_argument("--dq", action="store_true", help="breaking it disqualifies the entry")
    s.add_argument("--file", nargs="+", help="files, folders (ending in /) or globs that must be in the repository")
    s.add_argument("--section", help="FILE#HEADING: a section that must be there and filled")
    s.add_argument("--number", action="store_true", help="with --section: the section must state a number")
    s.add_argument("--attest", action="store_true", help="only a person can check it; confirm it with evidence")
    s.add_argument("--in-window", dest="in_window", action="store_true",
                   help="with --attest: a step the rules want done inside the build window (a workshop, a course); "
                        "its confirmation carries the date it was done, and a date outside the window fails")
    s.add_argument("--evidence", help="with confirm: what shows it is met, and where")
    s.add_argument("--why", help="with none: what the rules page says")

    s = sub.add_parser("submit", help="check what a judge will open")
    s.add_argument("action", choices=("check",))
    event_flags(s)
    s.add_argument("--readme")
    s.add_argument("--writeup", help="the write-up a judge reads (results write-up, report or submission text), "
                                    "when it is not the README")
    s.add_argument("--description", help="the submission text, or a file holding it")
    s.add_argument("--desc-max", dest="desc_max", type=int, help="the form's character limit")
    s.add_argument("--video", help="the local video file to be uploaded")
    s.add_argument("--video-url", dest="video_url")
    s.add_argument("--live", help="the live URL being submitted")
    s.add_argument("--online", action="store_true", help="also request the links (uses the network)")
    s.add_argument("--published", help="after publishing: the live page captured as markdown by a browser that runs "
                                      "JavaScript (docs/published.md), compared with the draft (R114)")
    s.add_argument("--draft", help="with --published: the text pasted into the page (default docs/submission.md in "
                                  "either case, else the --writeup file)")
    s.add_argument("--page-profile", dest="page_profile", choices=PAGE_PROFILES,
                   help="with --published: check the page against what the platform's winners carried "
                        "(builder-center: length, the live URL in the first two lines, an expected-results table, "
                        "proof AWS wrote, a number against a baseline)")
    s.add_argument("--lane", help="with --page-profile: the lane entered; startups also asks for a price or an "
                                  "adoption path")
    s.add_argument("--headings", help="a DEV post: the round's required headings, comma-separated, for devto_check.py")
    s.add_argument("--money-theme", dest="money_theme", action="store_true",
                   help="a DEV post whose theme is about earning or paying: passed to devto_check.py")
    s.add_argument("--effort-theme", dest="effort_theme", action="store_true",
                   help="a DEV post whose theme is about saving time or effort: passed to devto_check.py")
    s.add_argument("--strict", action="store_true", help="treat warnings as failures")
    s.add_argument("--task")

    s = sub.add_parser("page", help="capture the live page as a signed-out browser draws it, for submit check "
                                    "--published (R114)")
    s.add_argument("action", choices=("capture",))
    s.add_argument("url")
    s.add_argument("--out", default="docs/published.md", help="the markdown capture (default docs/published.md); "
                                                              "its links and the time go beside it as .json")
    s.add_argument("--timeout", type=int, default=60, help="seconds to wait for the network to go quiet")

    s = sub.add_parser("preflight", help="read-only checks of the cloud account on day 0, recorded (R113)")
    s.add_argument("action", choices=("aws",))
    s.add_argument("--region", help="default: AWS_REGION, AWS_DEFAULT_REGION, else us-east-1")
    s.add_argument("--model", help="a Bedrock model id: is it listed, and may this account call it")
    s.add_argument("--task")

    sub.add_parser("status")

    s = sub.add_parser("plan")
    s.add_argument("action", choices=("check", "waves", "next"))
    s.add_argument("--strict", action="store_true", help="treat warnings as failures")

    s = sub.add_parser("task")
    s.add_argument("id")
    s.add_argument("action", choices=("start", "done", "skip", "block", "reopen"))
    s.add_argument("--note")
    s.add_argument("--evidence", help="comma-separated evidence ids")
    s.add_argument("--force", action="store_true")
    s.add_argument("--strict", action="store_true", help="require evidence for every verify command")

    s = sub.add_parser("run")
    s.add_argument("--task")
    s.add_argument("--kind", choices=KINDS)
    s.add_argument("--where", default="local", choices=("local", "ci", "preview", "prod"))
    s.add_argument("--url")
    s.add_argument("--expect-fail", action="store_true", help="RED proof: the command is expected to fail")
    s.add_argument("--timeout", type=int, default=1800)
    s.add_argument("--tail", type=int, default=30, help="lines of output to print (full log is saved)")
    s.add_argument("command", nargs=argparse.REMAINDER)

    s = sub.add_parser("verify", help="run every verify command of a task through the ledger")
    s.add_argument("id")
    s.add_argument("--where", default="local", choices=("local", "ci", "preview", "prod"))
    s.add_argument("--timeout", type=int, default=1800)
    s.add_argument("--tail", type=int, default=15)
    s.add_argument("--keep-going", action="store_true", help="run the remaining commands after a failure")

    s = sub.add_parser("smoke")
    s.add_argument("url")
    s.add_argument("--routes", nargs="+", default=["/"])
    s.add_argument("--task")
    s.add_argument("--where", default="prod", choices=("local", "ci", "preview", "prod"))
    s.add_argument("--timeout", type=float, default=30)
    s.add_argument("--slow", type=float, default=8.0, help="seconds after which a page counts as slow")
    s.add_argument("--external", action="store_true", help="also check third-party assets")

    s = sub.add_parser("evidence")
    s.add_argument("action", choices=("add", "list", "redact"))
    s.add_argument("--id", help="redact: the evidence id")
    s.add_argument("--pattern", help="redact: regex of the text to remove")
    s.add_argument("--reason", help="redact: why (recorded in an audit entry)")
    s.add_argument("--task")
    s.add_argument("--kind", choices=KINDS)
    s.add_argument("--summary")
    s.add_argument("--method", default="observed", choices=("observed", "reported"))
    s.add_argument("--where", default="local", choices=("local", "ci", "preview", "prod"))
    s.add_argument("--url")
    s.add_argument("--file", action="append", help="screenshot or artifact path (repeatable)")
    s.add_argument("--failed", action="store_true", help="add: the observation found a problem; list: only failures")
    s.add_argument("--limit", type=int, default=40)

    s = sub.add_parser("diffcheck")
    s.add_argument("--base")
    s.add_argument("--task", help="use this task's BASE (recorded by `task start`)")

    s = sub.add_parser("intake")
    s.add_argument("id")
    s.add_argument("--base")

    s = sub.add_parser("mem")
    s.add_argument("action", choices=("add", "find", "check", "index", "mark"))
    s.add_argument("type", nargs="?", help="add: decision|failure|lesson|assumption|feedback|pattern; mark: record id")
    s.add_argument("extra", nargs="?", help="mark: helpful|harmful|verified|superseded|archived|invalidated")
    s.add_argument("--title")
    s.add_argument("--components", default="")
    s.add_argument("--triggers", default="")
    s.add_argument("--evidence", default="", help="path#L1-9, commit:sha, test:cmd, url:..")
    s.add_argument("--relates", default="")
    s.add_argument("--supersedes")
    s.add_argument("--scope", default="project", choices=("project", "global-candidate", "global"))
    s.add_argument("--source")
    s.add_argument("--body", help="record body (use \\n for newlines); default: the type's template")
    s.add_argument("--force", action="store_true")
    s.add_argument("--paths", default="")
    s.add_argument("--terms", default="")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--min-score", type=int, default=2, help="find: drop weaker matches")
    s.add_argument("--id", help="mark: record id")
    s.add_argument("--how", choices=("helpful", "harmful", "verified", "superseded", "archived", "invalidated"))
    s.add_argument("--by", help="mark superseded: the replacing record id")

    s = sub.add_parser("report")
    s.add_argument("file")
    return p


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv) - 1):  # argparse reads "--tz -07:00" as two flags
        if argv[i] == "--tz" and re.match(r"^[+-]\d{2}:?\d{2}$", argv[i + 1]):
            argv[i:i + 2] = [f"--tz={argv[i + 1]}"]
            break
    ARGV_TEXT[0] = " ".join(shlex.quote(a) for a in argv)
    args = build_parser().parse_args(argv)
    root = find_root(args.root)
    if args.cmd == "init":
        args.mode_given = args.mode is not None
        args.mode = args.mode or "hybrid"
    starts_event = (args.cmd == "clock" and args.action == "set") or (
        args.cmd == "init" and (args.mode == "hackathon" and args.mode_given or event_flags_given(args)))
    here = Path.cwd().resolve()
    nearer = None
    if not args.root and root != here:
        nearer = next((p for p in [here, *here.parents] if p != root and (p / ".git").exists()
                       and root in p.parents), None)
    if nearer and starts_event:
        die(f"{nearer} is a repository of its own, but the nearest .prod-build/ is in {root}. An event build is its "
            "own folder and its own repository: pass `--root .` to act on this one, or `--root` with the other path "
            "if that is the project.")
    if nearer:
        print(f"pb: note: acting on {root} (it holds .prod-build/). {nearer} is a repository of its own with no "
              ".prod-build/: pass `--root .` before the command to act on it.", file=sys.stderr)
    if starts_event and not args.root and root != here and not (root / ".prod-build").is_dir():
        die(f"this folder ({here}) has no .prod-build/ or .git of its own, and {root} does. An event build needs "
            "its own folder and its own repository. Pass `--root .` to make this folder the project, or run from "
            f"{root} if that is the project.")
    if args.cmd == "rules":
        args.id = args.id_flag or args.id
    if args.cmd == "mem" and args.action == "find" and args.type and args.type not in MEM_TYPES:
        die(f"--type for find must be one of {', '.join(MEM_TYPES)}")
    if args.cmd == "mem" and args.action == "mark":
        args.id = args.id or args.type
        args.how = args.how or args.extra
        if not args.id or args.how not in ("helpful", "harmful", "verified", "superseded", "archived", "invalidated"):
            die("usage: pb.py mem mark <ID> helpful|harmful|verified|superseded|archived|invalidated [--by ID]")
    handlers = {"init": cmd_init, "status": cmd_status, "plan": cmd_plan, "task": cmd_task, "run": cmd_run,
                "verify": cmd_verify,
                "smoke": cmd_smoke, "evidence": cmd_evidence, "diffcheck": cmd_diffcheck, "intake": cmd_intake,
                "mem": cmd_mem, "report": cmd_report, "clock": cmd_clock, "window": cmd_window,
                "submit": cmd_submit, "rules": cmd_rules, "page": cmd_page,
                "preflight": cmd_preflight}
    return handlers[args.cmd](args, root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head/less that closed early
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
