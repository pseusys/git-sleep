"""Tests for ``git_sleep.engine``: the orchestrator driven by a fake client and a fake clock."""

from collections.abc import Callable
from typing import Any

from pytest import mark

from git_sleep.config import Plan, PullRef, parse_plan
from git_sleep.engine import Orchestrator, Status
from git_sleep.github import Check, CheckState, GitHubError, PullStatus

LIB = "https://github.com/acme/lib/pull/1"
APP = "https://github.com/acme/app/pull/2"


def pull(**changes: Any) -> PullStatus:
    base = PullStatus(state="open", draft=False, mergeable="MERGEABLE", merge_state="CLEAN", head_sha="sha1", url="", checks=())
    return base.model_copy(update=changes)


def check(id: str = "c1", state: CheckState = "success", required: bool = True, run: int | None = 10, workflow: int | None = 100, name: str = "build") -> Check:
    return Check(id=id, name=name, state=state, required=required, url="", workflow_run_id=run, workflow_id=workflow)


class FakeClient:
    """In-memory GitHub: statuses per PR, a log of calls, and switches for the failures a test wants."""

    def __init__(self, plan: Plan) -> None:
        self.pulls = {spec.ref: pull() for spec in plan.pulls}
        self.calls: list[tuple[Any, ...]] = []
        self.requeue = True
        self.merge_error: GitHubError | None = None
        self.status_error: GitHubError | None = None

    def get_status(self, ref: PullRef) -> PullStatus:
        self.calls.append(("get_status", str(ref)))
        if self.status_error:
            error, self.status_error = self.status_error, None
            raise error
        return self.pulls[ref]

    def set_checks(self, ref: PullRef, *checks: Check) -> None:
        self.pulls[ref] = self.pulls[ref].model_copy(update={"checks": checks})

    def rerun_failed_jobs(self, ref: PullRef, run_id: int) -> None:
        self.calls.append(("rerun", str(ref), run_id))
        if self.requeue:
            requeued = tuple(c.model_copy(update={"id": c.id + "'", "state": "pending"}) if c.workflow_run_id == run_id and c.state == "failure" else c for c in self.pulls[ref].checks)
            self.set_checks(ref, *requeued)

    def merge_pull(self, ref: PullRef, merge_method: str, head_sha: str) -> None:
        self.calls.append(("merge", str(ref), merge_method, head_sha))
        if self.merge_error:
            error, self.merge_error = self.merge_error, None
            raise error
        self.pulls[ref] = self.pulls[ref].model_copy(update={"state": "merged"})

    def update_branch(self, ref: PullRef, head_sha: str) -> None:
        self.calls.append(("update_branch", str(ref), head_sha))

    def called(self, name: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == name]


class Clock:
    """A clock that only moves when the orchestrator sleeps or a test sets ``now``."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def setup(raw: Any, dry_run: bool = False) -> tuple[Orchestrator, FakeClient, Clock, dict[str, PullRef]]:
    plan = parse_plan(raw)
    client = FakeClient(plan)
    clock = Clock()
    orchestrator = Orchestrator(plan, client, dry_run=dry_run, clock=clock, sleep=clock.sleep)
    refs = {spec.id: spec.ref for spec in plan.pulls}
    return orchestrator, client, clock, refs


def status(orchestrator: Orchestrator, pull_id: str) -> Status:
    return orchestrator.trackers[pull_id].status


def test_green_pull_is_merged() -> None:
    orchestrator, client, _, refs = setup({"defaults": {"merge_method": "squash"}, "pulls": [LIB]})
    client.set_checks(refs[LIB], check())

    assert orchestrator.run() is True
    assert client.called("merge") == [("merge", "acme/lib#1", "squash", "sha1")]


def test_dependent_waits_for_dependency_then_merges_in_same_tick() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"id": "lib", "url": LIB}, {"id": "app", "url": APP, "depends_on": ["lib"]}]})
    client.set_checks(refs["lib"], check(state="pending"))

    orchestrator.tick()

    assert status(orchestrator, "lib") is Status.ACTIVE
    assert status(orchestrator, "app") is Status.BLOCKED
    assert ("get_status", "acme/app#2") not in client.calls

    client.set_checks(refs["lib"], check())
    orchestrator.tick()

    assert status(orchestrator, "lib") is Status.MERGED
    assert status(orchestrator, "app") is Status.MERGED
    assert orchestrator.finished


def test_failed_check_is_restarted_after_delay_then_merged() -> None:
    orchestrator, client, clock, refs = setup({"pulls": [{"id": "lib", "url": LIB, "retry": {"strategy": "constant", "delay": 60}}]})
    client.set_checks(refs["lib"], check(state="failure"))

    orchestrator.tick()
    assert client.called("rerun") == []

    clock.now = 59
    orchestrator.tick()
    assert client.called("rerun") == []

    clock.now = 60
    orchestrator.tick()
    assert client.called("rerun") == [("rerun", "acme/lib#1", 10)]
    assert status(orchestrator, "lib") is Status.ACTIVE

    orchestrator.tick()
    assert status(orchestrator, "lib") is Status.ACTIVE  # restarted job still pending

    client.set_checks(refs["lib"], check(id="c1'"))
    orchestrator.tick()
    assert status(orchestrator, "lib") is Status.MERGED


def test_already_restarted_check_is_not_restarted_again() -> None:
    orchestrator, client, clock, refs = setup({"pulls": [{"id": "lib", "url": LIB, "retry": {"delay": 0}}]})
    client.requeue = False
    client.set_checks(refs["lib"], check(state="failure"))

    for _ in range(3):
        orchestrator.tick()
        clock.now += 1000

    assert len(client.called("rerun")) == 1
    assert status(orchestrator, "lib") is Status.ACTIVE


def test_all_failed_runs_are_restarted_together_once_each() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"id": "lib", "url": LIB, "retry": {"delay": 0}}]})
    client.set_checks(
        refs["lib"],
        check(id="a", name="lint", state="failure"),
        check(id="b", name="test", state="failure"),
        check(id="c", name="deploy", state="failure", run=11, workflow=101),
    )

    orchestrator.tick()

    assert sorted(call[2] for call in client.called("rerun")) == [10, 11]


def test_failures_wait_for_other_checks_to_finish_before_restart() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"id": "lib", "url": LIB, "retry": {"delay": 0}}]})
    client.set_checks(refs["lib"], check(id="a", state="failure"), check(id="b", state="pending", run=11))

    orchestrator.tick()
    assert client.called("rerun") == []

    client.set_checks(refs["lib"], check(id="a", state="failure"), check(id="b", state="failure", run=11))
    orchestrator.tick()
    assert sorted(call[2] for call in client.called("rerun")) == [10, 11]


def test_retry_budget_counts_batches_per_pull() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"id": "lib", "url": LIB, "retry": {"max_attempts": 1, "delay": 0}}]})
    client.set_checks(refs["lib"], check(id="a", state="failure"), check(id="b", state="failure", run=11))
    orchestrator.tick()
    assert len(client.called("rerun")) == 2
    assert status(orchestrator, "lib") is Status.ACTIVE

    client.set_checks(refs["lib"], check(id="a'", state="success"), check(id="b'", state="failure", run=11))
    orchestrator.tick()

    assert status(orchestrator, "lib") is Status.FAILED
    assert len(client.called("rerun")) == 2


def test_exhausted_retries_fail_pull_and_skip_dependents() -> None:
    orchestrator, client, _, refs = setup(
        {
            "defaults": {"retry": {"max_attempts": 1, "delay": 0}},
            "pulls": [{"id": "lib", "url": LIB}, {"id": "app", "url": APP, "depends_on": ["lib"]}],
        }
    )
    client.set_checks(refs["lib"], check(state="failure"))
    orchestrator.tick()
    client.set_checks(refs["lib"], check(id="c1'", state="failure"))

    assert orchestrator.run() is False
    assert status(orchestrator, "lib") is Status.FAILED
    assert status(orchestrator, "app") is Status.SKIPPED
    assert len(client.called("rerun")) == 1
    assert client.called("merge") == []


def test_new_commit_gets_fresh_retry_budget() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"id": "lib", "url": LIB, "retry": {"max_attempts": 1, "delay": 0}}]})
    client.set_checks(refs["lib"], check(state="failure"))
    orchestrator.tick()

    client.pulls[refs["lib"]] = pull(head_sha="sha2", checks=(check(id="new", run=12, state="failure"),))
    orchestrator.tick()

    assert status(orchestrator, "lib") is Status.ACTIVE
    assert [call[2] for call in client.called("rerun")] == [10, 12]


def test_failed_check_that_cannot_be_restarted_fails_pull() -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    client.set_checks(refs[LIB], check(state="failure", run=None, workflow=None, name="ci/external"))

    assert orchestrator.run() is False
    assert status(orchestrator, LIB) is Status.FAILED
    assert "ci/external" in orchestrator.trackers[LIB].reason
    assert client.called("rerun") == []


def test_required_mode_ignores_optional_checks() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"url": LIB, "checks": "required"}]})
    client.set_checks(
        refs[LIB],
        check(id="req"),
        check(id="opt1", required=False, state="failure", run=None, workflow=None),
        check(id="opt2", required=False, state="pending"),
    )
    client.pulls[refs[LIB]] = client.pulls[refs[LIB]].model_copy(update={"merge_state": "UNSTABLE"})

    assert orchestrator.run() is True
    assert client.called("rerun") == []


def test_all_mode_waits_for_optional_checks() -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    client.set_checks(refs[LIB], check(id="req"), check(id="opt", required=False, state="pending"))

    orchestrator.tick()

    assert status(orchestrator, LIB) is Status.ACTIVE
    assert client.called("merge") == []


def test_action_required_check_waits() -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    client.set_checks(refs[LIB], check(state="pending"))

    orchestrator.tick()

    assert status(orchestrator, LIB) is Status.ACTIVE


@mark.parametrize(
    "pr",
    [pull(state="closed"), pull(mergeable="CONFLICTING", merge_state="DIRTY"), pull(mergeable="CONFLICTING")],
    ids=["closed", "dirty", "conflicting"],
)
def test_unmergeable_pull_fails(pr: PullStatus) -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    client.pulls[refs[LIB]] = pr

    assert orchestrator.run() is False
    assert status(orchestrator, LIB) is Status.FAILED


def test_already_merged_pull_is_done() -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    client.pulls[refs[LIB]] = pull(state="merged")

    assert orchestrator.run() is True
    assert client.called("merge") == []


def test_waits_while_draft_blocked_or_unknown() -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    for pr in [pull(draft=True), pull(merge_state="BLOCKED"), pull(mergeable="UNKNOWN", merge_state="UNKNOWN")]:
        client.pulls[refs[LIB]] = pr
        orchestrator.tick()
        assert status(orchestrator, LIB) is Status.ACTIVE
    assert client.called("merge") == []


def test_behind_branch_is_updated_once_per_head_commit() -> None:
    orchestrator, client, _, refs = setup({"pulls": [{"url": LIB, "update_branch": True}]})
    client.pulls[refs[LIB]] = pull(merge_state="BEHIND")

    orchestrator.tick()
    orchestrator.tick()

    assert client.called("update_branch") == [("update_branch", "acme/lib#1", "sha1")]
    assert status(orchestrator, LIB) is Status.ACTIVE


def test_behind_branch_is_left_alone_without_update_branch() -> None:
    orchestrator, client, _, refs = setup({"pulls": [LIB]})
    client.pulls[refs[LIB]] = pull(merge_state="BEHIND")

    orchestrator.tick()

    assert client.called("update_branch") == []


def test_rejected_merge_is_retried_on_next_tick() -> None:
    orchestrator, client, _, _ = setup({"pulls": [LIB]})
    client.merge_error = GitHubError(405, "Base branch was modified")

    orchestrator.tick()
    assert status(orchestrator, LIB) is Status.ACTIVE

    orchestrator.tick()
    assert status(orchestrator, LIB) is Status.MERGED


def test_transient_api_errors_do_not_stop_polling() -> None:
    orchestrator, client, _, _ = setup({"pulls": [LIB]})
    client.status_error = GitHubError(502, "Bad Gateway")

    orchestrator.tick()
    assert status(orchestrator, LIB) is Status.ACTIVE

    orchestrator.tick()
    assert status(orchestrator, LIB) is Status.MERGED


@mark.parametrize("code", [401, 404])
def test_permanent_api_errors_fail_pull(code: int) -> None:
    orchestrator, client, _, _ = setup({"pulls": [LIB]})
    client.status_error = GitHubError(code, "nope")

    assert orchestrator.run() is False
    assert status(orchestrator, LIB) is Status.FAILED


def test_run_sleeps_poll_interval_between_ticks() -> None:
    orchestrator, client, clock, refs = setup({"poll_interval": 30, "pulls": [LIB]})
    client.pulls[refs[LIB]] = pull(mergeable="UNKNOWN", merge_state="UNKNOWN")
    ticks = []
    original_tick: Callable[[], None] = orchestrator.tick

    def tick() -> None:
        ticks.append(clock.now)
        if len(ticks) == 3:
            client.pulls[refs[LIB]] = pull()
        original_tick()

    orchestrator.tick = tick

    assert orchestrator.run() is True
    assert ticks == [0, 30, 60]


def test_dry_run_changes_nothing() -> None:
    orchestrator, client, _, refs = setup(
        {
            "defaults": {"update_branch": True, "retry": {"max_attempts": 2, "delay": 0}},
            "pulls": [{"id": "lib", "url": LIB}, {"id": "app", "url": APP, "depends_on": ["lib"]}],
        },
        dry_run=True,
    )
    client.pulls[refs["app"]] = pull(merge_state="BEHIND")
    orchestrator.tick()
    client.pulls[refs["app"]] = pull(checks=(check(state="failure"),))

    assert orchestrator.run() is False
    assert status(orchestrator, "lib") is Status.MERGED
    assert status(orchestrator, "app") is Status.FAILED
    assert [call[0] for call in client.calls if call[0] != "get_status"] == []
