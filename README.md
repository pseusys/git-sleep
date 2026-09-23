# git-sleep

[![Python](https://github.com/pseusys/git-sleep/actions/workflows/python.yml/badge.svg)](https://github.com/pseusys/git-sleep/actions/workflows/python.yml)
[![Actions](https://github.com/pseusys/git-sleep/actions/workflows/actions.yml/badge.svg)](https://github.com/pseusys/git-sleep/actions/workflows/actions.yml)

A tool for automatic actions re-running and PR completion.

`git-sleep` takes a JSON plan of pull requests (in any number of repositories) and the dependencies between them, then:

1. watches every pull request that is not blocked by an unmerged dependency, polling once per `poll_interval` (60 s by default);
2. restarts failed GitHub Actions jobs according to a retry policy;
3. merges each pull request as soon as its checks pass and GitHub reports it mergeable;
4. starts watching the pull requests that were waiting on it;
5. exits once every pull request is merged, or once nothing left can be merged.

## Installation

```bash
poetry install            # or: pip install .
```

It needs Python 3.10 or later and depends on [githubkit](https://github.com/yanyongyu/githubkit) and [pydantic](https://docs.pydantic.dev/) v2.

## Usage

```bash
export GITHUB_TOKEN=ghp_...        # or GH_TOKEN, or --token
git-sleep examples/plan.json
git-sleep examples/plan.json --dry-run --poll-interval 10 -v
```

| Option | Meaning |
| --- | --- |
| `--token` | GitHub token (defaults to `$GITHUB_TOKEN`, then `$GH_TOKEN`) |
| `--api-url` | API base URL for GitHub Enterprise, e.g. `https://ghe.example/api/v3` (default `https://api.github.com`) |
| `--poll-interval SECONDS` | Override the plan's poll interval |
| `--dry-run` | Report only: no restarts, branch updates or merges (merges are simulated so dependents are exercised too) |
| `-v`, `--verbose` | Log every poll and HTTP request, not only status changes |
| `--schema` | Print the JSON schema of the plan file and exit |

The token needs read/write access to **pull requests**, **contents** (merging, updating branches) and **actions** (re-running workflows) in every repository in the plan.

Exit codes: `0` all merged, `1` some pull request failed or was skipped, `2` invalid plan or arguments, `130` interrupted.

## Plan format

```json
{
  "poll_interval": 60,
  "defaults": {
    "merge_method": "squash",
    "checks": "all",
    "update_branch": false,
    "retry": {"max_attempts": 3, "strategy": "exponential", "delay": 60, "factor": 2, "max_delay": 3600}
  },
  "pulls": [
    {"id": "lib", "url": "https://github.com/acme/lib/pull/12"},
    {"id": "sdk", "url": "https://github.com/acme/sdk/pull/7", "depends_on": ["lib"]},
    {"id": "app", "url": "https://github.com/acme/app/pull/34", "depends_on": ["lib", "sdk"],
     "merge_method": "merge", "checks": "required", "retry": {"strategy": "constant", "delay": 120}}
  ]
}
```

- `pulls[].url` (required): pull request URL.
- `pulls[].id`: name used in `depends_on`, defaults to the URL. `depends_on` accepts ids or URLs.
- `merge_method`: `merge` (default), `squash` or `rebase`.
- `checks`: `all` (default) waits for every check on the latest commit. `required` only waits for checks that branch protection or rulesets require, and ignores failures of optional checks.
- `update_branch`: when the branch is behind its base (and branch protection requires it to be up to date), update it from the base. The default is `false`.
- `retry`: `max_attempts` restarts per pull request and commit (default `3`); each restart re-runs the failed jobs of every failed workflow run at once. The delay before restart *n* (0-based) is `delay` for `constant`, `delay × (n+1)` for `linear`, and `delay × factor^n` for `exponential` (the default), capped at `max_delay`.

A JSON schema for plans is in [`plan.schema.json`](plan.schema.json), and `git-sleep --schema` prints the one matching your installed version.
Point a plan at it with a `"$schema"` key (as [`examples/plan.json`](examples/plan.json) does) to get autocompletion and validation in editors such as VS Code.

Anything set in `defaults` applies to every pull request, and each pull request can override it. Its `retry` object is merged key by key. Unknown keys, invalid values, unknown dependencies and dependency cycles are rejected before anything starts, with the location of each problem (e.g. `pulls.1.retry.strategy`). The plan can also be a plain list of URLs.

## How a pull request is handled

Each poll makes one GraphQL request per pull request to read its state, mergeability and checks. Then:

- **Merged** means done. **Closed without merging**, **merge conflicts**, or a **401/404** from the API marks it failed, and every pull request that depends on it is skipped. Independent pull requests carry on.
- The checks that count (all of them, or only required ones) decide what happens next:
  - Failed **GitHub Actions** checks are restarted together: once every other check has finished, and after the retry delay, the failed jobs of all failed workflow runs are re-run in one batch. Once the restarts are used up, the pull request fails. A new push gets a fresh budget.
  - A failed check from **another service** or a legacy commit status can't be restarted, so the pull request fails.
  - While checks are pending (including workflows waiting for approval), it waits.
- Once the counted checks pass, it waits while the pull request is a draft, blocked by required reviews, or GitHub is still computing mergeability. Then it is merged, with the merge pinned to the commit that was checked. Pull requests unblocked by the merge are polled right away.

Rate limits and server errors are retried by githubkit. Other transient errors are logged, and the poll is retried on the next cycle. Status is kept only in memory. If you restart the tool, pull requests that are already merged are recognised straight away.

## Development

```bash
poetry install
poetry run pytest
poetry run ruff check . && poetry run ruff format --check .
```

Coding rules and project notes for contributors (human or AI) are in [`AGENTS.md`](AGENTS.md).

Design notes live in [`docs/superpowers/specs`](docs/superpowers/specs), and the implementation plan in [`docs/superpowers/plans`](docs/superpowers/plans).
