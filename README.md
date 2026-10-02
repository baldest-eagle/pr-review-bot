# AI PR Review

Manually-triggered AI code review for GitHub pull requests. You choose the PR,
the bot fetches the diff, sends it to a model, and posts the result back as a
review with inline comments on the exact changed lines.

Two providers are wired up:

| Provider | Default model | Notes |
|---|---|---|
| `gemini` | `gemini-3.8-flash` | Falls back through `gemini-3.6-flash` → `gemini-flash-latest` → `gemini-2.5-flash` on 429/503/timeout |
| `nous` | `meituan/longcat-2.5-preview:free` | Falls back to `poolside/laguna-s-2.1:free`. Reasoning model — slow, allow 2–5 minutes. |

Nous free-tier availability as of this writing:

| Model | Status |
|---|---|
| `meituan/longcat-2.5-preview:free` | works — best findings, but slow and reasoning-heavy |
| `poolside/laguna-s-2.1:free` | works intermittently — either times out on large diffs or returns empty content with `finish_reason=length` |
| `meituan/longcat-2.0:free` | no longer free (404) |
| `stepfun/step-3.7-flash:free` | 400, provider rejected the request |
| `inclusionai/ling-3.0-flash-*:free` | 404 / 400 |
| `poolside/laguna-xs-2.1:free` | returns empty content |

Note that `upstage/solar-pro4` is **not** free on a standard Nous key — the `:free` suffix is what matters.

## Usage

1. Go to **Actions → AI PR Review → Run workflow**.
2. Fill in:
   - **repo** — `owner/name` of the repository holding the PR (any repo your token can read)
   - **pr_number** — the PR to review
   - **provider** — `gemini` or `nous`
   - **model** — optional, blank uses the provider default
3. The review appears on the PR as a comment with inline findings.

Nothing is triggered automatically. No PR is ever reviewed unless you dispatch it.

## Required secrets

Set these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `GH_PAT` | GitHub token with `repo` scope on the target repo. The default `GITHUB_TOKEN` only reaches the repo the workflow lives in, so a PAT is needed to review PRs elsewhere. |
| `GEMINI_API_KEY` | Google AI Studio key. Needs quota for a Flash-tier model — Pro-tier free quota is 0. |
| `NOUS_PORTAL_API_KEY` | Nous Portal key (`sk-nous-...`). |

## How it works

`review.py` does the following:

1. Fetches PR metadata and the unified diff via the GitHub REST API.
2. Drops noise files (lockfiles, binaries, `dist/`, `node_modules/`) and enforces
   a 120k-character diff budget so large PRs don't blow the context window.
3. Parses the diff to build the set of line numbers that are *commentable* on the
   new side of each file.
4. **Runs a static symbol check.** Every `obj->method(` / `obj.method(` added by
   the PR is searched for across the repo at the PR head. Any method name that
   appears *only* as a call site — never as a declaration — is fed to the model
   as a strong hint that the API was invented. This targets the LLM failure mode
   of hallucinating methods that do not exist.
5. Asks the model for a JSON verdict: `{summary, verdict, comments[]}`.
6. Discards any finding that doesn't anchor to a changed line — this is what stops
   the model inventing line numbers or commenting on untouched code.
7. Posts a `COMMENT` review with the inline findings, capped at 15 per run.

Because the review event is always `COMMENT`, the bot never approves or blocks a
PR by itself. The verdict is reported in the summary text for you to act on.

## Catching hallucinated APIs

This was built after a real case on `baldest-eagle/tdesktop` PR #6, where an
agent-authored PR called:

```cpp
const auto track = _members->lookupTrack(endpoint);   // never declared anywhere
_members->trackSizeValue(endpoint),                    // only exists on VideoTile
```

Neither method exists on the `Members` class. `lookupTrack` appeared exactly once
in the entire repo — at that call site. The static check reports these under a
`STATIC CHECK` heading in the prompt so the model can confirm and flag them.

The check is a heuristic. A symbol from an external library (Qt, std) will also
show no declaration and get listed, so the prompt asks the model to distinguish
"hallucinated API" from "external library method" rather than treating every hit
as a bug.

## Local testing

Dry run against a real PR without posting anything:

```bash
export GH_PAT="$(gh auth token)"
export TARGET_REPO=owner/name PR_NUMBER=123 PROVIDER=gemini DRY_RUN=1
python review.py
```

`DRY_RUN=1` prints the rendered review and inline comments to stdout instead of
calling the review API.

## Limitations

- Findings are model output. Verify anything you act on — the reviewer has been
  observed making correct catches and confident wrong ones on the same PR.
- Diffs over 120k characters are partially skipped; the summary notes which files.
- Reviews are per-PR, not per-commit. Re-running re-reviews the whole diff.
