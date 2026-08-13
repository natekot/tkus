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

**Commits with no AI usage** add no ledger entry and change nothing.

**Amend, squash, rebase** all work without special handling. An amended commit
keeps its entry — the entry is already in the index — and adds only new usage. A
squash merges entries like any file.

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
| `tkus rollup [--by branch\|identity\|date]` | Totals from the tracked ledger |
| `tkus reprice` | Re-price the ledger with the current rate table |
| `tkus show [<commit>]` | Per-commit detail from the local `.git/` ledger |

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
- **Unknown models are flagged, never priced at zero.**

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
- **Per-commit mapping is best-effort.** Entries record the parent SHA, which
  identifies a commit on linear history but not after a squash — where per-commit
  attribution genuinely no longer exists, since the commits do not.
- **Long gaps.** A commit made after days of uncommitted work absorbs all of it.
- **Interleaved branches** share one repository-level cursor, so usage lands on
  whichever branch commits first.
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
python3 -m unittest discover -s tests -t .      # 156 tests
```

The suite includes regressions pinned to redacted snapshots of real agent data
for both providers, and exercises squash, amend, abandonment, and branch
switching against real git operations rather than reasoning about them.
