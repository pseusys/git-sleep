"""Thin adapter over githubkit: PR status and checks through GraphQL, mutations through REST."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from githubkit import GitHub
from githubkit.exception import GitHubException, GraphQLFailed, RequestFailed
from httpx import BaseTransport, Response
from pydantic import BaseModel, ConfigDict

from git_sleep import __version__
from git_sleep.config import PullRef

DEFAULT_API_URL = "https://api.github.com"
HTTP_NOT_FOUND = 404

# The pull request state and every check of its head commit, one page of checks at a time.
STATUS_QUERY = Path(__file__).with_name("status.graphql").read_text()

SUCCESS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
PENDING_STATUS_STATES = frozenset({"PENDING", "EXPECTED"})

CheckState = Literal["pending", "success", "failure"]


class GitHubError(Exception):
    """Any failed GitHub call; ``status`` is the HTTP status, or ``None`` for network and GraphQL errors."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message


class Check(BaseModel):
    """One check on the head commit: an Actions or third-party check run, or a legacy commit status."""

    model_config = ConfigDict(frozen=True)

    id: str  # GraphQL node id; a restarted job gets a new one
    name: str
    state: CheckState
    required: bool
    url: str
    workflow_run_id: int | None  # set only for GitHub Actions check runs
    workflow_id: int | None


class PullStatus(BaseModel):
    """Everything the engine needs to decide about a pull request, read in one GraphQL query."""

    model_config = ConfigDict(frozen=True)

    state: Literal["open", "closed", "merged"]
    draft: bool
    mergeable: Literal["MERGEABLE", "CONFLICTING", "UNKNOWN"]
    merge_state: str
    head_sha: str
    url: str
    checks: tuple[Check, ...]


def parse_check(node: dict[str, Any]) -> Check | None:
    """Map a ``statusCheckRollup`` context to a ``Check``; unknown context types give ``None``."""
    kind = node.get("__typename")
    if kind == "CheckRun":
        conclusion = node.get("conclusion")
        if node.get("status") != "COMPLETED" or conclusion == "ACTION_REQUIRED":
            state: CheckState = "pending"
        elif conclusion in SUCCESS_CONCLUSIONS:
            state = "success"
        else:
            state = "failure"
        run = (node.get("checkSuite") or {}).get("workflowRun")
        return Check(
            id=node["id"],
            name=node["name"],
            state=state,
            required=bool(node.get("isRequired")),
            url=node.get("detailsUrl") or "",
            workflow_run_id=run["databaseId"] if run else None,
            workflow_id=(run.get("workflow") or {}).get("databaseId") if run else None,
        )
    if kind == "StatusContext":
        status = node.get("state")
        return Check(
            id=node["id"],
            name=node["context"],
            state="success" if status == "SUCCESS" else "pending" if status in PENDING_STATUS_STATES else "failure",
            required=bool(node.get("isRequired")),
            url=node.get("targetUrl") or "",
            workflow_run_id=None,
            workflow_id=None,
        )
    return None


def parse_status(pull: dict[str, Any]) -> tuple[PullStatus, dict[str, Any]]:
    """Build the status from one GraphQL page; also return the page info of its checks."""
    commits = pull["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    contexts = rollup["contexts"] if rollup else {"nodes": [], "pageInfo": {"hasNextPage": False}}
    checks = (parse_check(node) for node in contexts["nodes"] if node)
    status = PullStatus(
        state=pull["state"].lower(),
        draft=pull["isDraft"],
        mergeable=pull["mergeable"],
        merge_state=pull["mergeStateStatus"],
        head_sha=pull["headRefOid"],
        url=pull["url"],
        checks=tuple(check for check in checks if check is not None),
    )
    return status, contexts["pageInfo"]


class GitHubClient:
    """The GitHub calls the engine makes, with every error raised as ``GitHubError``."""

    def __init__(
        self,
        token: str,
        api_url: str = DEFAULT_API_URL,
        *,
        transport: BaseTransport | None = None,
        auto_retry: bool = True,
    ) -> None:
        self._github = GitHub(
            token,
            base_url=api_url,
            transport=transport,
            auto_retry=auto_retry,
            http_cache=False,
            user_agent=f"git-sleep/{__version__}",
        )

    def get_status(self, ref: PullRef) -> PullStatus:
        """Read the pull request and all checks of its head commit, following check pagination."""
        checks: list[Check] = []
        cursor = None
        while True:
            variables = {"owner": ref.owner, "repo": ref.repo, "number": ref.number, "cursor": cursor}
            with _translate_errors():
                data = self._github.graphql.request(STATUS_QUERY, variables)
            pull = (data.get("repository") or {}).get("pullRequest")
            if pull is None:
                raise GitHubError(HTTP_NOT_FOUND, f"pull request {ref} not found")
            status, page = parse_status(pull)
            checks.extend(status.checks)
            if not page.get("hasNextPage"):
                return status.model_copy(update={"checks": tuple(checks)})
            cursor = page["endCursor"]

    def rerun_failed_jobs(self, ref: PullRef, run_id: int) -> None:
        """Re-run the failed jobs of one workflow run."""
        with _translate_errors():
            self._github.rest.actions.re_run_workflow_failed_jobs(ref.owner, ref.repo, run_id)

    def merge_pull(self, ref: PullRef, merge_method: str, head_sha: str) -> None:
        """Merge the pull request, provided its head is still ``head_sha``."""
        with _translate_errors():
            self._github.rest.pulls.merge(ref.owner, ref.repo, ref.number, data={"merge_method": merge_method, "sha": head_sha})

    def update_branch(self, ref: PullRef, head_sha: str) -> None:
        """Merge the base branch into the pull request, provided its head is still ``head_sha``."""
        with _translate_errors():
            self._github.rest.pulls.update_branch(ref.owner, ref.repo, ref.number, data={"expected_head_sha": head_sha})


@contextmanager
def _translate_errors() -> Iterator[None]:
    """Turn githubkit exceptions into GitHubError, the only error type the engine knows."""
    try:
        yield
    except GraphQLFailed as e:
        errors = e.response.errors or []
        status = HTTP_NOT_FOUND if any(error.type == "NOT_FOUND" for error in errors) else None
        raise GitHubError(status, "; ".join(error.message for error in errors) or str(e)) from e
    except RequestFailed as e:
        raise GitHubError(e.response.status_code, _error_message(e.response.raw_response)) from e
    except GitHubException as e:
        raise GitHubError(None, str(e) or type(e).__name__) from e


def _error_message(response: Response) -> str:
    """The ``message`` of a GitHub error body, or the HTTP reason when there is none."""
    try:
        return response.json().get("message") or response.reason_phrase
    except (ValueError, AttributeError):
        return response.reason_phrase
