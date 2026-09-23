"""Tests for ``git_sleep.cli``: exit codes, and how options reach the client and the orchestrator."""

from json import dumps
from pathlib import Path
from typing import Any

from pytest import CaptureFixture, MonkeyPatch, fixture

from git_sleep.cli import EXIT_ALL_MERGED, EXIT_NOT_ALL_MERGED, EXIT_USAGE, main
from git_sleep.config import Plan

PLAN = {"poll_interval": 60, "pulls": ["https://github.com/acme/lib/pull/1"]}


class FakeOrchestrator:
    """Records how it was built and returns ``result`` from ``run``."""

    seen: dict[str, Any] = {}
    result = True

    def __init__(self, plan: Plan, client: object, *, dry_run: bool) -> None:
        FakeOrchestrator.seen = {"plan": plan, "client": client, "dry_run": dry_run}

    def run(self) -> bool:
        return FakeOrchestrator.result


@fixture
def plan_file(tmp_path: Path) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(dumps(PLAN))
    return path


@fixture(autouse=True)
def fake_run(monkeypatch: MonkeyPatch) -> None:
    FakeOrchestrator.seen = {}
    FakeOrchestrator.result = True
    monkeypatch.setattr("git_sleep.cli.Orchestrator", FakeOrchestrator)
    monkeypatch.setattr("git_sleep.cli.GitHubClient", lambda token, api_url: ("client", token, api_url))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


def test_invalid_plan_is_a_usage_error(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "missing.json"), "--token", "t"]) == EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_missing_token_is_a_usage_error(plan_file: Path, capsys: CaptureFixture[str]) -> None:
    assert main([str(plan_file)]) == EXIT_USAGE
    assert "token" in capsys.readouterr().err


def test_non_positive_poll_interval_is_a_usage_error(plan_file: Path) -> None:
    assert main([str(plan_file), "--token", "t", "--poll-interval", "0"]) == EXIT_USAGE


def test_runs_orchestrator_with_options(plan_file: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "env-token")

    code = main([str(plan_file), "--dry-run", "--poll-interval", "5", "--api-url", "https://ghe.example/api/v3"])

    assert code == EXIT_ALL_MERGED
    assert FakeOrchestrator.seen["plan"].poll_interval == 5
    assert FakeOrchestrator.seen["dry_run"] is True
    assert FakeOrchestrator.seen["client"] == ("client", "env-token", "https://ghe.example/api/v3")


def test_not_everything_merged(plan_file: Path) -> None:
    FakeOrchestrator.result = False

    assert main([str(plan_file), "--token", "t"]) == EXIT_NOT_ALL_MERGED
