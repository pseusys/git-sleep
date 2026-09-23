"""Tests for ``git_sleep.config``: the file schema, how settings are layered and how dependencies are resolved."""

from json import dumps
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pytest import mark, raises

from git_sleep.config import DEFAULT_POLL_INTERVAL, ConfigError, PullRef, RetryPolicy, load_plan, parse_plan

LIB = "https://github.com/acme/lib/pull/12"
APP = "https://github.com/acme/app/pull/34"


def test_defaults_are_applied() -> None:
    plan = parse_plan({"pulls": [{"url": LIB}]})

    assert plan.poll_interval == DEFAULT_POLL_INTERVAL
    (pull,) = plan.pulls
    assert pull.id == LIB
    assert pull.ref == PullRef(owner="acme", repo="lib", number=12)
    assert str(pull.ref) == "acme/lib#12"
    assert pull.depends_on == ()
    assert pull.merge_method == "merge"
    assert pull.checks == "all"
    assert pull.update_branch is False
    assert pull.retry == RetryPolicy()


def test_pull_settings_override_defaults_field_by_field() -> None:
    plan = parse_plan(
        {
            "poll_interval": 30,
            "defaults": {"merge_method": "squash", "checks": "required", "retry": {"max_attempts": 5, "delay": 10}},
            "pulls": [
                {"id": "lib", "url": LIB},
                {
                    "id": "app",
                    "url": APP,
                    "merge_method": "rebase",
                    "checks": "all",
                    "update_branch": True,
                    "retry": {"delay": 1},
                },
            ],
        }
    )

    lib, app = plan.pulls
    assert plan.poll_interval == 30
    assert (lib.merge_method, lib.checks) == ("squash", "required")
    assert lib.retry == RetryPolicy(max_attempts=5, delay=10)
    assert (app.merge_method, app.checks, app.update_branch) == ("rebase", "all", True)
    assert app.retry == RetryPolicy(max_attempts=5, delay=1)


def test_dependencies_by_id_and_url_resolve_to_ids() -> None:
    plan = parse_plan(
        {
            "pulls": [
                {"id": "lib", "url": LIB},
                {"url": APP, "depends_on": [LIB + "/"]},
                {"id": "docs", "url": "https://github.com/acme/docs/pull/1", "depends_on": ["lib", APP]},
            ]
        }
    )

    assert plan.pulls[1].depends_on == ("lib",)
    assert plan.pulls[2].depends_on == ("lib", APP)


def test_bare_list_and_bare_urls_are_accepted() -> None:
    plan = parse_plan([LIB, {"url": APP + "/", "depends_on": LIB}])

    assert [p.id for p in plan.pulls] == [LIB, APP]
    assert plan.pulls[1].depends_on == (LIB,)


@mark.parametrize(
    ("raw", "message"),
    [
        ({"pulls": []}, "pulls: List should have at least 1 item"),
        ({"pulls": [{"url": "https://github.com/acme/lib/issues/1"}]}, "pulls.0.url: Value error, not a pull request URL"),
        ({"pulls": [{"url": LIB, "depends_on": ["nope"]}]}, "unknown pull request"),
        ({"pulls": [{"id": "a", "url": LIB, "depends_on": ["a"]}]}, "depends on itself"),
        ({"pulls": [{"id": "a", "url": LIB}, {"id": "a", "url": APP}]}, "duplicate"),
        ({"pulls": [LIB, LIB]}, "listed twice"),
        (
            {"pulls": [{"id": "a", "url": LIB, "depends_on": ["b"]}, {"id": "b", "url": APP, "depends_on": ["a"]}]},
            "cycle: a -> b -> a",
        ),
        ({"pulls": [{"url": LIB, "colour": "red"}]}, "pulls.0.colour: Extra inputs are not permitted"),
        ({"pulls": [{"url": LIB, "merge_method": "octopus"}]}, "pulls.0.merge_method"),
        ({"defaults": {"checks": "some"}, "pulls": [LIB]}, "defaults.checks"),
        ({"pulls": [{"url": LIB, "retry": {"strategy": "random"}}]}, "pulls.0.retry.strategy"),
        ({"pulls": [{"url": LIB, "retry": {"max_attempts": -1}}]}, "pulls.0.retry.max_attempts"),
        ({"pulls": [{"url": LIB, "retry": {"factor": 0.5}}]}, "pulls.0.retry.factor"),
        ({"poll_interval": 0, "pulls": [LIB]}, "poll_interval"),
        ({"poll_interval": True, "pulls": [LIB]}, "poll_interval"),
        ({"pulls": [{"url": LIB, "update_branch": "yes"}]}, "pulls.0.update_branch"),
        ("nonsense", "invalid plan"),
    ],
)
def test_invalid_plans_are_rejected(raw: Any, message: str) -> None:
    with raises(ConfigError, match=message.replace(".", r"\.")):
        parse_plan(raw)


@mark.parametrize(
    ("strategy", "expected"),
    [
        ("constant", [10, 10, 10, 10]),
        ("linear", [10, 20, 30, 40]),
        ("exponential", [10, 30, 45, 45]),
    ],
)
def test_retry_delays(strategy: str, expected: list[float]) -> None:
    policy = RetryPolicy(strategy=strategy, delay=10, factor=3, max_delay=45)

    assert [policy.delay_for(attempt) for attempt in range(4)] == expected


def test_runtime_models_are_frozen() -> None:
    pull = parse_plan([LIB]).pulls[0]

    with raises(ValidationError):
        pull.checks = "required"


def test_load_plan_reads_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(dumps({"pulls": [LIB]}))

    assert load_plan(path).pulls[0].url == LIB


def test_load_plan_reports_missing_file_and_bad_json(tmp_path: Path) -> None:
    with raises(ConfigError, match="cannot read"):
        load_plan(tmp_path / "missing.json")

    path = tmp_path / "plan.json"
    path.write_text("{")
    with raises(ConfigError, match="invalid JSON"):
        load_plan(path)
