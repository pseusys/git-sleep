"""Tests for ``git_sleep.schema``: the committed JSON schema matches the models and agrees with them on real plans."""

from json import loads
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pytest import CaptureFixture, mark, raises

from git_sleep.cli import main
from git_sleep.config import parse_plan
from git_sleep.schema import plan_schema, schema_json

ROOT = Path(__file__).parents[1]
SCHEMA_FILE = ROOT / "plan.schema.json"
LIB = "https://github.com/acme/lib/pull/12"


def validator() -> Draft202012Validator:
    return Draft202012Validator(plan_schema())


def test_committed_schema_is_up_to_date() -> None:
    assert SCHEMA_FILE.read_text() == schema_json(), "regenerate with: poetry run git-sleep --schema > plan.schema.json"


def test_schema_is_a_valid_json_schema() -> None:
    Draft202012Validator.check_schema(plan_schema())


@mark.parametrize(
    "plan",
    [
        loads((ROOT / "examples" / "plan.json").read_text()),
        [LIB],
        {"$schema": "./plan.schema.json", "pulls": [LIB + "/", {"url": "https://ghe.example/acme/app/pull/3", "depends_on": LIB}]},
        {"defaults": {"retry": {"max_attempts": 0, "delay": 0.5}}, "pulls": [{"url": LIB, "id": "lib", "checks": "required"}]},
    ],
    ids=["example", "bare-list", "shorthands", "settings"],
)
def test_plans_accepted_by_both(plan: Any) -> None:
    validator().validate(plan)
    parse_plan(plan)


@mark.parametrize(
    "plan",
    [
        {"pulls": []},
        {"pulls": [{"url": LIB, "colour": "red"}]},
        {"pulls": ["https://github.com/acme/lib/issues/1"]},
        {"pulls": [{"url": LIB, "merge_method": "octopus"}]},
        {"pulls": [{"url": LIB, "checks": "some"}]},
        {"pulls": [{"url": LIB, "retry": {"strategy": "random"}}]},
        {"pulls": [{"url": LIB, "retry": {"factor": 0.5}}]},
        {"pulls": [{"url": LIB, "update_branch": "yes"}]},
        {"poll_interval": True, "pulls": [LIB]},
        {"poll_interval": 0, "pulls": [LIB]},
        "nonsense",
    ],
    ids=["no-pulls", "unknown-key", "not-a-pull-url", "merge-method", "checks", "strategy", "factor", "update-branch", "bool-interval", "zero-interval", "string"],
)
def test_plans_rejected_by_both(plan: Any) -> None:
    assert not validator().is_valid(plan)
    with raises(ValueError):
        parse_plan(plan)


def test_cli_prints_schema(capsys: CaptureFixture[str]) -> None:
    with raises(SystemExit) as exit_info:
        main(["--schema"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == schema_json()
