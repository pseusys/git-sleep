"""Orchestration: poll unblocked PRs, restart failed workflow runs, merge, then unblock dependents."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from logging import DEBUG, ERROR, INFO, WARNING, getLogger
from time import monotonic, sleep
from typing import Protocol

from git_sleep.config import Plan, PullRef, PullSpec
from git_sleep.github import Check, GitHubError, PullStatus

MERGEABLE_STATES = frozenset({"CLEAN", "HAS_HOOKS", "UNSTABLE"})
# Bad credentials or a PR/repository that does not exist (or is invisible to the token) will not fix themselves.
PERMANENT_ERRORS = frozenset({401, 404})
# Merge refused because the PR changed or is not mergeable after all; both clear up on a later poll.
MERGE_REJECTED_ERRORS = frozenset({405, 409})
WAITING_HINTS = {
    "BLOCKED": "merge is blocked by branch protection (required reviews or checks)",
    "UNKNOWN": "GitHub is still computing mergeability",
    "DRAFT": "pull request is a draft",
}

logger = getLogger("git_sleep")


class Client(Protocol):
    """The GitHub operations the orchestrator needs; ``GitHubClient`` in production, a fake in tests."""

    def get_status(self, ref: PullRef) -> PullStatus: ...
    def rerun_failed_jobs(self, ref: PullRef, run_id: int) -> None: ...
    def merge_pull(self, ref: PullRef, merge_method: str, head_sha: str) -> None: ...
    def update_branch(self, ref: PullRef, head_sha: str) -> None: ...


class Status(Enum):
    """Lifecycle of a pull request: blocked, then active, then one of the three final states."""

    BLOCKED = "blocked"
    ACTIVE = "active"
    MERGED = "merged"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class RetryState:
    """Batch restarts done so far for one head commit, and when the next one is due."""

    attempts: int = 0
    next_at: float | None = None


@dataclass
class Tracker:
    """Everything the orchestrator remembers about one pull request between polls."""

    spec: PullSpec
    status: Status = Status.BLOCKED
    reason: str = ""
    # Keyed by head commit: every batch restart of failed runs counts once, a new push resets the budget.
    retries: dict[str, RetryState] = field(default_factory=dict)
    # Ids of failed checks already restarted; until GitHub replaces them they still show as failed.
    restarted_check_ids: set[str] = field(default_factory=set)
    updated_from_sha: str | None = None
    last_status: str | None = None


class Orchestrator:
    """Drives every pull request of a plan to merged, failed or skipped; see the spec in docs/superpowers/specs."""

    def __init__(
        self,
        plan: Plan,
        client: Client,
        *,
        dry_run: bool = False,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], None] = sleep,
    ) -> None:
        self.plan = plan
        self.client = client
        self.dry_run = dry_run
        self._clock = clock
        self._sleep = sleep
        self.trackers = {spec.id: Tracker(spec) for spec in plan.pulls}

    @property
    def finished(self) -> bool:
        """Whether no pull request is blocked or active any more."""
        return all(t.status not in (Status.BLOCKED, Status.ACTIVE) for t in self.trackers.values())

    def run(self) -> bool:
        """Poll until every PR is merged or can no longer be merged; return whether all were merged."""
        while True:
            self.tick()
            if self.finished:
                break
            self._sleep(self.plan.poll_interval)
        self._summarize()
        return all(t.status is Status.MERGED for t in self.trackers.values())

    def tick(self) -> None:
        """Poll every active PR once; PRs unblocked by merges during this tick are polled right away."""
        polled: set[str] = set()
        while True:
            self._unblock()
            batch = [t for t in self.trackers.values() if t.status is Status.ACTIVE and t.spec.id not in polled]
            if not batch:
                return
            for tracker in batch:
                polled.add(tracker.spec.id)
                try:
                    self._poll(tracker)
                except GitHubError as e:
                    if e.status in PERMANENT_ERRORS:
                        self._finish(tracker, Status.FAILED, f"GitHub API error: {e}")
                    else:
                        self._event(tracker, WARNING, f"GitHub API error, will retry: {e}")

    def _unblock(self) -> None:
        """Activate PRs whose dependencies all merged, skip those with a failed or skipped dependency."""
        changed = True
        while changed:
            changed = False
            for tracker in self.trackers.values():
                if tracker.status is not Status.BLOCKED:
                    continue
                deps = [self.trackers[dep] for dep in tracker.spec.depends_on]
                broken = [dep.spec.id for dep in deps if dep.status in (Status.FAILED, Status.SKIPPED)]
                if broken:
                    self._finish(tracker, Status.SKIPPED, f"skipped, dependency not merged: {', '.join(broken)}")
                    changed = True
                elif all(dep.status is Status.MERGED for dep in deps):
                    tracker.status = Status.ACTIVE
                    self._event(tracker, INFO, f"watching {tracker.spec.url}")
                    changed = True

    def _poll(self, tracker: Tracker) -> None:
        """Read one PR and take the next step for it: wait, restart failed checks, merge or give up."""
        status = self.client.get_status(tracker.spec.ref)
        if status.state == "merged":
            self._finish(tracker, Status.MERGED, "merged")
            return
        if status.state == "closed":
            self._finish(tracker, Status.FAILED, "closed without merging")
            return
        if status.draft:
            self._status(tracker, WAITING_HINTS["DRAFT"])
            return

        counted = [c for c in status.checks if tracker.spec.checks == "all" or c.required]
        failed = [c for c in counted if c.state == "failure" and c.id not in tracker.restarted_check_ids]
        for check in failed:
            if check.workflow_run_id is None:
                self._finish(tracker, Status.FAILED, f"check {check.name!r} failed and cannot be restarted {check.url}".rstrip())
                return
        # Failed runs are restarted together, once every other counted check has finished.
        pending = sorted({c.name for c in counted if c.state != "success" and c not in failed})
        if pending:
            suffix = f" ({len(failed)} failed, will restart together)" if failed else ""
            self._status(tracker, f"waiting for check(s): {', '.join(pending)}{suffix}")
            return
        if failed:
            self._restart_failed(tracker, status.head_sha, failed)
            return
        self._try_merge(tracker, status)

    def _restart_failed(self, tracker: Tracker, head_sha: str, failed: list[Check]) -> None:
        """Restart every failed workflow run of the PR in one batch, once the retry delay has passed."""
        policy = tracker.spec.retry
        now = self._clock()
        state = tracker.retries.setdefault(head_sha, RetryState())
        names = ", ".join(sorted({c.name for c in failed}))
        if state.attempts >= policy.max_attempts:
            self._finish(tracker, Status.FAILED, f"check(s) {names} still failing after {state.attempts} restart(s)")
            return
        if state.next_at is None:
            state.next_at = now + policy.delay_for(state.attempts)
            self._event(
                tracker,
                WARNING,
                f"check(s) {names} failed, restart {state.attempts + 1}/{policy.max_attempts} in {state.next_at - now:.0f}s",
            )
        if now < state.next_at:
            self._status(tracker, "waiting to restart failed checks")
            return

        # Count the attempt up front, so an API error halfway through the batch cannot restart runs endlessly.
        state.attempts += 1
        state.next_at = None
        by_run: dict[int, list[Check]] = {}
        for check in failed:
            assert check.workflow_run_id is not None
            by_run.setdefault(check.workflow_run_id, []).append(check)
        for run_id, checks in sorted(by_run.items()):
            if not self.dry_run:
                self.client.rerun_failed_jobs(tracker.spec.ref, run_id)
                tracker.restarted_check_ids.update(c.id for c in checks)
        verb = "dry run: would restart" if self.dry_run else "restarted"
        self._event(tracker, INFO, f"{verb} failed jobs of {len(by_run)} workflow run(s): {names}")

    def _try_merge(self, tracker: Tracker, status: PullStatus) -> None:
        """Merge a PR whose checks passed, or update its branch, or wait, or fail on conflicts."""
        spec = tracker.spec
        state = status.merge_state
        if status.mergeable == "CONFLICTING" or state == "DIRTY":
            self._finish(tracker, Status.FAILED, "has merge conflicts")
            return
        if state == "BEHIND":
            if not spec.update_branch:
                self._status(tracker, "branch is behind its base (set update_branch to update it automatically)")
            elif tracker.updated_from_sha == status.head_sha:
                self._status(tracker, "waiting for the branch update to land")
            else:
                if not self.dry_run:
                    self.client.update_branch(spec.ref, status.head_sha)
                tracker.updated_from_sha = status.head_sha
                self._event(tracker, INFO, f"{'dry run: would update' if self.dry_run else 'updated'} branch from its base")
            return
        if status.mergeable != "MERGEABLE" or state not in MERGEABLE_STATES:
            self._status(tracker, WAITING_HINTS.get(state, f"not mergeable yet (state: {state})"))
            return

        if self.dry_run:
            self._finish(tracker, Status.MERGED, f"dry run: would merge ({spec.merge_method})")
            return
        try:
            self.client.merge_pull(spec.ref, spec.merge_method, status.head_sha)
        except GitHubError as e:
            if e.status not in MERGE_REJECTED_ERRORS:
                raise
            self._status(tracker, f"merge rejected, will retry: {e.message}")
            return
        self._finish(tracker, Status.MERGED, f"merged ({spec.merge_method})")

    def _finish(self, tracker: Tracker, status: Status, reason: str) -> None:
        """Put the PR in a final state and log why."""
        tracker.status = status
        tracker.reason = reason
        self._event(tracker, INFO if status is Status.MERGED else ERROR, reason)

    def _event(self, tracker: Tracker, level: int, message: str) -> None:
        """Log a message about one PR."""
        logger.log(level, "[%s] %s", tracker.spec.ref, message)

    def _status(self, tracker: Tracker, message: str) -> None:
        """Log a waiting status when it changes; repeats only show up at debug level."""
        level = DEBUG if message == tracker.last_status else INFO
        tracker.last_status = message
        self._event(tracker, level, message)

    def _summarize(self) -> None:
        """Log the final state of every PR."""
        for tracker in self.trackers.values():
            level = INFO if tracker.status is Status.MERGED else ERROR
            logger.log(level, "%-7s %s  %s", tracker.status.value, tracker.spec.url, tracker.reason)
