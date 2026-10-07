# tkus

Attribute AI agent token usage and cost to your git history.

`tkus` reads the records that **Claude Code** and **GitHub Copilot CLI** already
write to your machine, and records the cost in a ledger tracked in your
repository — so you can answer *"what did this feature cost to build?"*

```sh
$ tkus rollup
branch                 entries       cost
------------------------------------------
main                        14      31.02
feature/cache-rewrite        6       8.49
------------------------------------------
TOTAL                                39.51  USD
```

**Commit messages are never modified.** The cost lives in `.tkus/`, a tracked
file, which is what lets it survive squash+merge — see [Why a
ledger](#why-a-ledger). There is no daemon, no CI, and no network access: both
agents already record their usage durably on disk, so the numbers are computed at
commit time from data that is already there.

---

## Contents

- [Supported agents](#supported-agents)
- [Requirements](#requirements)
- [Installation](#installation)
- [Why a ledger](#why-a-ledger)
- [What gets committed](#what-gets-committed)
- [Ledger format](#ledger-format)
- [Windows](#windows)
- [Deploying across many repositories](#deploying-across-many-repositories)
- [What to expect](#what-to-expect)
- [Commands](#commands)
- [Configuration](#configuration)
- [How attribution works](#how-attribution-works)
- [Cost accuracy](#cost-accuracy)
- [Privacy](#privacy)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Uninstalling](#uninstalling)
- [Development](#development)

---

## Supported agents

| Agent | Source read | Cost basis |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` | Token counts × bundled rate table |
| GitHub Copilot CLI | `~/.copilot/session-store.db` | AI Units the provider itself recorded |

Both are read **read-only** and need no configuration. If an agent isn't
installed it contributes nothing, and a machine with only one agent — or neither
— still commits normally.

Not supported: Copilot's **IDE/editor** integration (only the CLI writes the
database `tkus` reads), and OpenAI Codex (no adapter — see
[Other agents](#other-agents)).

---

## Requirements

- **Python 3.8 or newer.** No third-party runtime dependencies at all — a hook
  runs on every commit, so it must not depend on an import graph that can break
  independently.
- **git.** No special version required.
- **Windows, macOS, Linux, or WSL.** Hooks are `/bin/sh` scripts; on Windows they
  run under the `sh` that ships with Git for Windows. See [Windows](#windows).

---

## Installation

### 1. Install the package

`tkus` is **not published on PyPI**. Install it from the repository:

```sh
pip install git+https://github.com/natekot/tkus.git
```

> **Do not run `pip install tkus`.** That name is unclaimed on PyPI; if someone
> registers it later you would install a stranger's package.

Pin a version by appending `@<tag-or-commit>`, or install from a clone with
`pip install -e .` for development. Confirm it is on your PATH:

```sh
tkus --version
```

### 2. Enable it in a repository

```sh
cd your-repo
tkus install
```

Recording starts immediately — that is the point of the tool. By default the
cost is committed to `.tkus/` in the repository, which is what makes it survive
squash+merge and visible to your team. Read [What gets
committed](#what-gets-committed) first, because it publishes per-developer spend
to anyone with repository access.

If you would rather keep it to yourself, either of these works:

```sh
tkus install --local-only     # explicit
echo '.tkus/' >> .gitignore   # or just ignore it
```

Both record into `.git/` instead. Nothing is committed, but the data is lost with
`.git`, shared with nobody, and does not survive a squash. **Either way cost is
recorded** — the choice is only where.

> **`.gitignore` only works before the ledger is tracked.** Git ignores
> `.gitignore` for files it already tracks, so if you have already committed a
> ledger you also need `git rm -r --cached .tkus`. Until you do, entries keep
> being committed.

Two hooks are written:

| Hook | Role |
|---|---|
| `pre-commit` | Computes usage since the last commit, writes and stages the ledger |
| `post-commit` | Advances the watermark once the commit exists |

**Existing hooks are never clobbered.** A hook of the same name is renamed to
`<name>.local` and still runs first; if it fails, the commit aborts exactly as
before.

**One install covers every worktree.** Git runs hooks from the repository's
shared git directory, so a worktree made with `git worktree add` records from
its first commit. Each worktree keeps its own cursor. Running `tkus uninstall`
from any worktree removes the hooks from all of them.

### 3. Verify

```sh
tkus report      # usage not yet attributed to a commit
git commit -m "test"
tkus rollup      # totals from the ledger
```

---

## Why a ledger

An earlier version of `tkus` appended the cost to the commit message. That does
not survive the workflow most teams actually use.

**GitHub's squash+merge discards commit messages.** Its default for a
multi-commit PR keeps the PR title and a list of commit *subjects* — bodies are
dropped. And because the squash happens on GitHub's servers, no local hook runs
to compensate. The result was a `main` branch with no cost data at all.

**Squash preserves the tree.** It discards messages, not file changes. So a
tracked file survives squash, rebase, cherry-pick, and amend alike. That single
property is why the ledger is a file:

| Operation | Commit message | Tracked file |
|---|---|---|
| `git commit --amend` | Rewritten | Preserved |
| `git merge --squash` | Concatenated or dropped | Merged |
| GitHub squash+merge | **Discarded** | **Preserved** |
| `git rebase`, `cherry-pick` | Often rewritten | Preserved |

It also means `tkus` needs no amend detection, no squash special-casing, and no
GitHub Action — git's ordinary merge machinery does the work.

---

## What gets committed

One file per identity and branch:

```
.tkus/<identity>/<branch>.jsonl
```

Per branch so concurrent pull requests never touch the same file — that is what
avoids merge conflicts. Per identity so two people on similarly-named branches
stay separate. Identity comes from `tkus.identity` in git config, falling back to
`user.name`; **never `user.email`**, which would publish email addresses in
repository paths.

Each line is one commit's worth of usage: the window, per-model token counts,
cost, rate-table version, and the parent commit SHA.

> **This publishes spend.** Anyone with repository access can see per-developer
> cost, and in a public repository that means everyone. If that is not what you
> want, `tkus install --local-only` keeps it in `.git/` instead.

The file is rebuilt from `HEAD` on each commit rather than appended to, so a
commit you abandon in the editor leaves nothing behind to double-count.

Add this to `.gitattributes` to keep it out of review diffs:

```
.tkus/**/*.jsonl linguist-generated=true
```

---

## Ledger format

The `.tkus/` files are a stable interface. Tools that report on AI spend can
read them directly, from any clone or through a hosting API, without installing
`tkus`.

**Path.** `.tkus/<identity>/<branch>.jsonl`. `<identity>` is `tkus.identity`,
falling back to `user.name`, with slashes replaced by `-`. `<branch>` keeps its
slashes as nested directories, so `feature/x` is `.tkus/<identity>/feature/x.jsonl`.
A detached HEAD files under `detached`. In both names, characters that Windows
forbids in filenames (`<>:"\|?*` and control characters) become `-`, so a path
matches the branch name exactly unless it contained one of those.

**Tags.** Usage filed with [`tkus tag`](#work-that-belongs-to-no-branch) lives
at `.tkus/<identity>/.tags/<name>/<until>.jsonl`, one file per occurrence, so
reusing a name on two open pull requests never conflicts. `<until>` is the end
of its window, as `YYYYMMDDTHHMMSS.mmmZ`. Git rejects a branch name with a
component that starts with `.`, so a path with `.tags` as its third component
is always a tag, never a branch. Its entries are in the same format as a
branch's. A renamed
branch's entries move to its new file at the next commit; until then, or when
tkus cannot see the rename, they stay under the old name (see [Known
limitations](#known-limitations)).

**Entries.** One JSON object per line, one line per commit that had usage:

```json
{"at": "2026-09-08T19:04:26.645Z", "since": "2026-09-08T18:55:12.388Z",
 "until": "2026-09-08T19:04:26.645Z", "parent": "5dc98a31357c5e2300982b64c17948bcd3d90e7a",
 "currency": "USD", "usd": 4.721591, "rates_version": "2026-08",
 "providers": [{"provider": "claude-code", "model": "claude-opus-5", "reqs": 48,
                "in": 96, "out": 24875, "cr": 7279392, "cw1h": 45954, "usd": 4.721591}]}
```

| Field | Meaning |
|---|---|
| `at` | When the entry was written (UTC, ISO 8601) |
| `since`, `until` | The usage window claimed; `since` is `null` on the first entry after install, which claims everything before it |
| `parent` | SHA of `HEAD` when the commit was made, `null` for the root commit. A fallback only: the commit whose diff added the entry is authoritative — see [How entries find their commits](#how-entries-find-their-commits) |
| `currency`, `usd` | Total cost of the entry |
| `rates_version` | Version of the rate table that priced it |
| `providers[]` | One row per provider and model: `provider`, `model`, `usd`, and token counters |
| `providers[].filled` | `true` on a row priced after it was recorded, because its model had no rate then (see [Filling in a model priced late](#filling-in-a-model-priced-late)) |

The counters are `reqs` (requests), `in` (uncached input), `out`, `cr` (cache
reads), `cw1h` and `cw5m` (cache writes by TTL), `ws` (web searches), `reas`
(reasoning tokens) and `naiu` (Copilot nano AI Units). A counter that is zero is
omitted.

**Stability.** Fields may be added; none will be renamed or removed, or change
meaning. Readers should ignore fields they do not recognise, and treat a missing
counter as zero. A committed entry's content is never rewritten — a branch
rename moves it to another file unchanged — so `usd` is the price when it was
recorded, and `tkus reprice` only reports what the same tokens would cost
today. The one exception is a row recorded before its model had any rate: once
one exists, it is priced and marked `"filled": true`, and its entry's `usd`
grows to match. Nothing that already had a price is ever changed.

**Joining to pull requests.** Squash+merge keeps a branch's ledger file in the
default branch's tree, so after merging, `.tkus/*/<branch>.jsonl` still names
the PR's head branch. That is the key for cost per pull request. A branch name
reused for a later PR is disambiguated by entry timestamps.

---

## Windows

Windows is a supported platform. Install exactly as above, from PowerShell,
Command Prompt, or Git Bash. You need [Git for
Windows](https://git-scm.com/download/win), which supplies the `sh` that runs
hooks, and a normal Windows Python.

These differences are handled for you; each one produces wrong numbers rather
than an error if mishandled:

| Difference | Handling |
|---|---|
| `git rev-parse` reports `C:/git/x` while agents record `C:\git\x` | Separators normalised before comparison |
| Drive-letter case varies — real data contains both `C:\git\ctrl` and `c:\git\ctrl` | Comparison is case-insensitive on Windows only |
| Hook scripts written with CRLF fail under `sh` | Hooks are always written with LF |
| Interpreter paths contain spaces (`C:\Program Files\Python\python.exe`) | Quoted, with forward slashes |
| Files often carry no executable bit | A displaced `*.local` hook runs via `sh` rather than being skipped |
| SQLite cannot open `file:C:\...?mode=ro` | Converted to `file:///C:/...?mode=ro`, percent-encoded |
| Branch and identity names may contain characters illegal in filenames | Sanitised before use as paths |

**Verification status:** the Windows-specific rules are covered by tests that run
on any host, pinned to real path strings captured from a Windows machine. **They
have not been executed on Windows**, because no Windows machine was available.
Before a wide rollout, run the suite and a commit on one Windows box.

---

## Deploying across many repositories

The hooks and watermark live inside `.git`, so installation is per-repository.

**Every new clone, automatically.** Git copies a template directory into every
repository created by `git init` or `git clone`:

```sh
mkdir -p ~/.git-template/hooks
cd some-repo-with-tkus-installed
cp .git/hooks/pre-commit .git/hooks/post-commit ~/.git-template/hooks/
git config --global init.templateDir ~/.git-template
```

Verified working — a fresh `git init --template=…` records on its first commit.
Templates apply only at creation time, so existing repositories need a pass:

```sh
find ~/git -maxdepth 3 -type d -name .git -print0 |
  while IFS= read -r -d '' d; do
    ( cd "$(dirname "$d")" && tkus install >/dev/null && echo "ok: $PWD" )
  done
```

Re-running `tkus install` is safe and idempotent.

**If you use `core.hooksPath`,** git runs hooks only from there and ignores
`.git/hooks`. `tkus install` detects this and warns; copy the two generated hooks
into that directory, or chain to `tkus hook <name> "$@"` from your own.

---

## What to expect

**Your first commit** claims all previously unattributed usage for that
repository — potentially weeks of it, as one large entry. Check `tkus report`
first, or absorb it with `git commit --allow-empty -m "start tkus"`.

**Every commit after** claims only usage since the previous one. Every token is
counted exactly once.

**Commits with no AI usage** add no ledger entry and change nothing, except to
finish a pending branch rename.

**Amend, squash, rebase** all work without special handling. An amended commit
keeps its entry — the entry is already in the index — and adds only new usage. A
squash merges entries like any file.

**Renaming a branch** carries its ledger along: the first commit after `git
branch -m old new` moves the entries in `old.jsonl` into `new.jsonl` and removes
the old file.

That relies on the reflog, which misses a branch cut from the old one before
deleting it, or one renamed on GitHub or in another clone. For those, say so:

```sh
tkus rename            # list ledgers whose branch no longer exists
tkus rename old        # fold old.jsonl into the current branch's at the next commit
tkus rename old new    # ...or into new's, the next time you commit on it
```

It refuses while a branch named `old` still exists, since that file is still
that branch's. The move lands in your next commit, even one with no AI usage;
until then `tkus log` and `rollup` still show the split.

**Abandoning a commit** in the editor leaves nothing behind: the next commit
rebuilds the ledger from `HEAD`.

**`--no-verify` skips `pre-commit`**, so nothing is recorded for that commit.

**Performance.** The hook is unnoticeable — measured at 0.012 s for a typical
repository and 0.069 s scanning every project directory, against a 33 MB
transcript store.

---

## Commands

| Command | Purpose |
|---|---|
| `tkus install [--local-only]` | Install hooks; `--local-only` keeps cost out of the repo |
| `tkus uninstall` | Remove them, restoring any displaced hooks |
| `tkus report [--all]` | Usage not yet attributed to a commit |
| `tkus rollup [--by branch\|identity\|date] [--json]` | Totals from the tracked ledger |
| `tkus log [--branch N] [--total]` | Per-commit cost for one branch |
| `tkus rename [<old> [<new>]]` | Carry a branch's ledger over after its name changed |
| `tkus tag [<name>] [--undo]` | File the usage since the last commit under a name instead of a branch |
| `tkus reprice` | Re-price the ledger with the current rate table |
| `tkus reprice --fill [--yes]` | Price rows recorded before their model had a rate |
| `tkus show [<commit>]` | Per-commit detail from the local `.git/` ledger |
| `tkus rates [--at DATE] [--json]` | The rate table used for pricing |
| `tkus rates --check` | Compare against the installed Claude Code; exit 1 on drift |
| `tkus rates --update [--yes]` | Write refreshed rates to the global override |

### What a branch cost, commit by commit

`tkus rollup` gives a branch one row. `tkus log` breaks that row into the
commits it came from, reading the tracked ledger — so unlike `tkus show`, it
works in any clone, not only on the machine that made the commits.

```
$ tkus log
branch main
7 of 8 commits have recorded usage; 2 entries orphaned by rebase or amend

commit          cost  subject
--------------------------------------------------------------------------------
d260dd940      17.50  Fix the version so upgrades work, and stop report from...
73965dfd4       4.12  Price the eight models that were silently costing zero
e52cde168       3.96  Stop Sonnet 5 from jumping 50% on September 1
--              5.51  (orphaned: no commit on this branch claims these)
--------------------------------------------------------------------------------
TOTAL          43.38  USD
```

`--branch NAME` reports on a branch other than the current one, `--total` prints
just the number for scripting, and `--json` emits the same data structured.

The scope is the branch's **own** entries — the same figure as its row in `tkus
rollup`. Work that arrived through a merge stays attributed to the branch it was
written on, so summing every branch double-counts nothing.

#### How entries find their commits

An entry is written in `pre-commit`, when the new commit's SHA does not yet
exist. So `tkus log` credits each entry to the **commit whose diff added it**,
read from the history of the ledger files. That survives history rewriting: an
amended commit's diff adds every entry it carries, including the one written
during the amend, and each rebased commit re-adds exactly its own. It is plain
history, so every clone gives the same answer.

An entry no commit added — one only in the worktree, such as the staged ledger
of an abandoned commit — falls back to the **parent** SHA it recorded, and is
shown as orphaned if that no longer identifies a commit. Orphans are still real
money, so they are counted in `TOTAL` and shown on their own line rather than
dropped. **`TOTAL` therefore always agrees with the branch's `rollup` row**,
whatever git has done to the history.

The coverage line is how you judge the breakdown: it reports how many of the
branch's commits carry usage at all — commits made before `tkus install`, or
with no agent usage, are simply absent from the table — and how many entries
are orphaned.

### Work that belongs to no branch

Every commit claims the usage since the one before it, on whatever branch it
is on. So an hour spent planning on `main`, with nothing to commit there, is
billed to the first commit of whichever feature branch comes next. `tkus tag`
says what that work was instead:

```
$ tkus tag strategy
tagged 3.10 USD as strategy (2026-10-06 10:34 -> 15:24 UTC)
it lands in your next commit, on whichever branch that is
```

Nothing is committed yet, and nothing has to be committed on `main`. The tag is
a marker in `.git/`, and your next commit, **on any branch**, splits its window
there: the usage up to the tag goes to its own file, and only what came after
goes to the branch. The tag reaches the default branch when that commit's pull
request merges, and `tkus rollup` shows it as its own row:

```
branch                 entries       cost
------------------------------------------
feature/cache-rewrite        6       8.49
main                        14      31.02
tag:strategy                 2       3.10
------------------------------------------
TOTAL                                42.61  USD
```

Names are reusable buckets: tag `strategy` as often as you like and the row
sums them. Tagging the same name again before committing extends that tag
rather than adding a second. `tkus tag` with no name lists tags waiting for a
commit, `tkus tag --undo` drops the newest one, and `tkus report` shows pending
tags apart from the usage your next commit's branch will claim.

It refuses when there is nothing to tag, when the hooks are not installed (no
commit would ever record it), and in a `--local-only` repository, because tags
exist only in the tracked ledger. It has nothing to do with git tags.

### Seeing the rates

`tkus rates` prints the table every cost figure is derived from. The stored
table expresses cache prices as multipliers on the input rate; this resolves
them into money, because what matters when checking an invoice is that a cache
read costs $0.50/MTok, not that it is 0.1x something else.

Where that multiple does not hold, a price window says so outright and the
explicit figure wins:

```json
"claude-fable-5-1": {
  "standard": [{"from": null, "until": null, "input": 10.0, "output": 50.0,
                "cache": {"cache_read": 0.25}}]
}
```

Fable 5.1 and Mythos 5.1 are the models that need it: they price cache reads at
$0.25/MTok against a $10 input rate — 0.025x, where every other model is 0.1x.
Opus 5.5 does too, at $0.20/MTok against $4 input — 0.05x — and in fast mode at
$0.40 against $8. Deriving those would overstate cached reads **two- to
fourfold**, and cached reads dominate real agent usage. The `cache` key is per
field, so anything left out of it is still derived; `tkus rates --check` reports
a tier whose cache prices the table neither states nor derives correctly.

```
USD per 1M tokens
model             speed        input    output  cache-wr-1h  cache-wr-5m   cache-rd
-----------------------------------------------------------------------------------
claude-opus-5     standard      5.00     25.00        10.00         6.25       0.50
claude-opus-5     fast         10.00     50.00        20.00        12.50       1.00
```

It also reports the AI Unit conversion used for Copilot, any batch tier
discount, which files the rates came from, and whether an override is in
effect. Rates that change on a future date are listed under **Scheduled
changes**, so a price rise cannot arrive unnoticed; `--at YYYY-MM-DD` shows the
table as of any date, and `--json` emits the same data for scripts.

Like `tkus report`, it needs no installation — and it works outside a git
repository entirely, though a repository-level `.tkus.json` override obviously
cannot apply there.

### Keeping the rates current

`rates.json` is maintained by hand, so it rots silently — and a stale table
misstates real money when users are billed per token. `tkus rates --check`
compares it against the model catalog embedded in the **installed Claude Code
binary**:

```
$ tkus rates --check
compared against Claude Code 2.1.227

model           field        bundled -> claude-code
---------------------------------------------------
claude-sonnet-5 input           2.00 -> 3.00          [dated window ending 2026-08-31]
```

That source is deliberate. There is no rate-card API — `GET /v1/models` returns
capabilities and context sizes but no prices — and the community JSON that does
carry prices lists only Bedrock regional variants for current models, where
picking the wrong key overstates by 10% invisibly. Claude Code ships
first-party pricing in the same shape tkus stores, on every machine that already
has it, **with no network call**, so the tool keeps its promise of never
reaching out.

`--check` writes nothing and exits 1 on drift, which makes it usable in CI. A
machine without Claude Code exits 0: absence is not drift.

`tkus rates --update` writes the refreshed rates to
`~/.config/tkus/rates.json` — never to the bundled table — and is a dry run
unless you pass `--yes`. It refuses three things, because the catalog cannot
express them:

| Left alone | Why |
|---|---|
| A model you already override locally | Almost certainly a negotiated rate; a list price must not undo it |
| A dated window (e.g. introductory pricing) | The catalog carries no dates, so it cannot tell a price change from a promotion still running |
| `fast` pricing | The catalog has no speed dimension at all |

Cache pricing is *not* on that list. The catalog states it per tier, so a model
added on a tier where the usual multiple does not hold is written with an
explicit `cache` key rather than a derived — and wrong — one.

When a price does change, the old window is **closed** rather than rewritten, so
`tkus reprice` keeps historical commits at the rates that actually applied.

> This reads an undocumented internal of another program, which may change in
> any Claude Code release. It never raises: if the catalog cannot be read, tkus
> says so and keeps using the bundled table.

### Filling in a model priced late

A model released after your copy of the rate table is not priced at zero: the
commit's report says it is unpriced. But its ledger row is still committed,
with only its web searches in `usd`, so adding the rate later leaves that money
missing from `rollup` and `log`. `tkus reprice` says when that has happened,
and `--fill` puts it right:

```
$ tkus reprice --fill
.tkus/nate-kot/fix-amend-rename-attribution.jsonl
  2026-09-25  claude-opus-5-5      3,366,900 tokens   2.962351

adds 2.96 USD across 1 file
dry run. Re-run with --yes to write it.
```

With `--yes` it rewrites those lines and stages them, so they land in your
next commit. Every identity's files are filled, not only yours, because a row at
zero is wrong whoever recorded it. The rewrite is identical for anyone who runs
it, so two people filling the same line merge cleanly. Each filled row is
marked `"filled": true`, and it keeps its key, so `tkus log` still credits the
commit that recorded it.

Your next commit on a branch fills that branch's own file anyway. It has to:
the commit rebuilds the file from `HEAD`, where the row is still unpriced.

A ledger row records no speed, so a filled row is priced at the standard rate,
the same way `tkus reprice` prices everything. Fast-mode usage of a model that
had no rate is understated by the difference.

### Reading does not require installing

`tkus report` works in **any** git repository, whether or not `tkus install` has
been run there. It reads the agents' own transcripts and filters them by
repository path, so it needs no hooks and writes nothing — you can use it to see
what a repo has cost before deciding to record anything. When hooks are absent it
says so, because otherwise its "since the beginning" window looks like a backlog
awaiting attribution when in fact nothing is recording.

The other read commands do depend on installation, because they read what the
hooks wrote:

| Command | Reads | Needs `tkus install`? |
|---|---|---|
| `tkus report` | The agents' transcripts, live | No |
| `tkus rollup` | The tracked `.tkus/` ledger | Yes |
| `tkus log` | The tracked `.tkus/` ledger | Yes |
| `tkus show` | The local `.git/` ledger | Yes |

So `report` showing a large figure while `rollup` shows nothing is the expected
signal that a repository has AI usage but is not recording it.

---

## Configuration

Optional. Settings merge from the bundled defaults, then
`~/.config/tkus/rates.json` and `~/.config/tkus/config.json`, then `.tkus.json`
in the repository root.

```jsonc
{
  "repo_ledger": false,   // --local-only; default is true
  "models": {
    "claude-opus-5": {
      "standard": [{ "from": null, "until": null, "input": 4.0, "output": 20.0 }]
    }
  },
  "usd_per_aiu": 0.01
}
```

**List price is probably not your price** if you are on Amazon Bedrock, Google
Vertex, or a negotiated contract. Rate entries are date-ranged, so a scheduled
price change applies automatically to entries on either side of the boundary.
Reports say `override` rather than `list` only when a rate has actually been
changed — a config that merely sets `repo_ledger` still reports `list`.

| Variable | Effect |
|---|---|
| `CLAUDE_CONFIG_DIR` / `COPILOT_HOME` | Where each agent stores data |
| `TKUS_CLAUDE_PROJECTS` / `TKUS_COPILOT_HOME` | Override those outright |
| `TKUS_REPO_LEDGER` | Force committing the ledger on or off for one run |
| `XDG_CONFIG_HOME` | Where global config lives |

State lives in `.git/tkus/`: `cursor.json` (the watermark), `pending.json`
(in-flight), and `ledger.jsonl` (per-commit detail keyed by SHA, local only).
None of it is committed; deleting the directory resets attribution.

---

## How attribution works

A per-repository **watermark cursor** in `.git/tkus/cursor.json` records the end
of the last attributed window. Each commit claims usage since the cursor, then
advances it, so every token is attributed exactly once.

The cursor advances in `post-commit`, never in `pre-commit`, because the latter
runs before the commit is final and you might still abort.

The window comes from the cursor rather than from the ledger file itself. That
matters: a new branch has no ledger file of its own, so deriving the window from
it would reopen from the beginning and re-attribute work already recorded on
another branch.

**Matching usage to a repository** differs per agent. Claude Code records the
working directory, so sessions match by path, including sessions started in
subdirectories. Copilot matches on the `owner/name` of your `origin` remote,
because its recorded working directory can come from a different machine or
operating system entirely.

---

## Cost accuracy

The figure is designed to be reconcilable against a real invoice.

### Claude Code

- **Cache reads are priced.** On real data they were 58% of total cost; counting
  only input and output reports roughly a third of the true number.
- **Cache-write TTLs are distinguished** — 1-hour writes bill at 2× input,
  5-minute at 1.25×. Conflating them understates writes by up to 60%.
- **Per-model breakdown**, since one commit routinely spans models priced from
  $1/$5 to $10/$50 per MTok.
- **Fast mode and batch tier** are read from the transcript and priced accordingly.
- **Unknown models are flagged, never priced at zero.** Their rows are filled
  in once a rate exists — see [Filling in a model priced
  late](#filling-in-a-model-priced-late).

### GitHub Copilot

Copilot needs **no rate table**. Since usage-based billing arrived on 2026-06-01
it records the exact cost it charged per request — `total_nano_aiu`, in
billionths of a GitHub AI Unit. GitHub documents 1 AI Unit = $0.01, which is the
`usd_per_aiu` setting.

- **`input_tokens` already includes cached tokens**; the genuinely uncached input
  is `input_tokens − cache_read − cache_write`. Passing the raw column through
  while also counting cache reads would overstate the input line ~48×.
- **Self-hosted endpoints are free and reported as such** — token counts with zero
  dollars, labelled unbilled rather than flagged as a missing rate.

### What the figure excludes

Subscription billing. If your usage is covered by a subscription the number is
notional — what the same tokens would cost at the configured rates.

Because the ledger stores raw token counts rather than only a figure, history
stays re-priceable: `tkus reprice` recomputes past entries under the current
table.

---

## Privacy

Both agents' local stores contain your full prompts and the model's responses.
`tkus` reads **only** usage, model, timestamp, and repository/working-directory
fields. No prompt or response text is ever written anywhere.

For Copilot the database also holds `turns.user_message`,
`turns.assistant_response`, `sessions.summary`, and full-text search tables. The
adapter names every column it selects — never `SELECT *` — and a test asserts the
query touches nothing outside that allowlist. The connection is opened
**read-only**, so a running Copilot's database can never be written, locked, or
migrated.

The checked-in test fixtures are redactions of real data containing only those
fields, with identifiers and private repository names replaced, and tests assert
they hold no prose.

See also [What gets committed](#what-gets-committed) — the ledger itself makes
per-developer spend visible to everyone with repository access.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Nothing recorded | Hooks not installed, or the commit used `--no-verify` |
| No `.tkus/` file after a commit | `--local-only`, `.tkus` in `.gitignore`, `core.hooksPath` set, or `--no-verify`. Check `tkus show` — it is probably recorded locally |
| `tkus: not inside a git repository` | Run it from inside a working tree |
| First entry is huge | Expected — it absorbs previously unattributed usage |
| Claude usage missing | Check `~/.claude/projects` holds a directory for this repo's path |
| Copilot usage missing | Needs a CLI version that writes `assistant_usage_events`, and an `origin` remote matching the recorded repository |
| `N models unpriced` | A model has no rate entry; add one via config |
| A model shows zero cost | Correct for a self-hosted endpoint — it is not billed |
| Costs don't match my invoice | You are likely not on list price; configure an override |
| Still committed after adding to `.gitignore` | The file is already tracked; run `git rm -r --cached .tkus` |

---

## Known limitations

- **File sprawl.** One file per identity and branch accumulates over time. Files
  are tiny; a compaction command may come later.
- **Diff noise.** Every commit touches a JSON file. `linguist-generated` keeps it
  out of review diffs.
- **No cost in `git log`.** Reading it requires `tkus`. That is the price of
  leaving commit messages alone.
- **Per-commit mapping ends at a squash.** After a squash the individual
  commits no longer exist, so their entries are all credited to the squash
  commit.
- **Long gaps.** A commit made after days of uncommitted work absorbs all of it.
- **Interleaved branches** share one repository-level cursor, so usage lands on
  whichever branch commits first. For work that belongs to no branch at all,
  `tkus tag` files it separately.
- **A tag travels with the commit that carries it.** It reaches the default
  branch only when that commit's branch merges; a branch that is never merged
  takes its tags with it.
- **Branch renames are found through the local reflog.** The next commit
  after `git branch -m old new` folds `old.jsonl` into `new.jsonl`, unless a
  branch named `old` exists again. A rename the reflog can't see — made in
  another clone, or by cutting a new branch and deleting the old one — stays
  split, and `tkus rollup` shows two rows, until you run `tkus rename old` and
  commit.
- **Windows code paths are tested but have not been run on Windows.**

### Other agents

Claude Code and GitHub Copilot CLI are implemented behind a provider seam
(`tkus/providers/base.py`) that further adapters can slot into — one module plus a
`register()` call. Codex is not included: it isn't installed on any machine
available to this project, so an adapter would be written against a format that
could not be verified or tested.

---

## Uninstalling

```sh
tkus uninstall            # removes the hooks, restores any *.local ones
rm -rf .git/tkus          # optional: drop the watermark and local ledger
git rm -r --cached .tkus  # optional: stop tracking the ledger
pip uninstall tkus
```

Ledger entries already committed are ordinary files and stay in history.

---

## Development

```sh
git clone https://github.com/natekot/tkus.git
cd tkus
python3 -m unittest discover -s tests -t .      # 347 tests
```

The suite includes regressions pinned to redacted snapshots of real agent data
for both providers, and exercises squash, amend, abandonment, and branch
switching against real git operations rather than reasoning about them.
