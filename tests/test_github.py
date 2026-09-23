"""Tests for ``git_sleep.github``: real githubkit calls against a mocked HTTP transport."""

from collections.abc import Callable
from json import loads
from typing import Any

from httpx import ConnectError, MockTransport, Request, Response
from pytest import mark, raises

from git_sleep.config import PullRef
from git_sleep.github import Check, CheckState, GitHubClient, GitHubError, parse_check

REF = PullRef(owner="acme", repo="lib", number=12)


def check_run(status: str = "COMPLETED", conclusion: str | None = "SUCCESS", run: bool = True, required: bool = False, id: str = "CR_1") -> dict[str, Any]:
    node = {
        "__typename": "CheckRun",
        "id": id,
        "name": "build",
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": "https://github.com/acme/lib/actions/runs/7/job/1",
        "isRequired": required,
        "checkSuite": {"workflowRun": {"databaseId": 7, "workflow": {"databaseId": 3}} if run else None},
    }
    return node


def status_context(state: str, required: bool = True) -> dict[str, Any]:
    return {
        "__typename": "StatusContext",
        "id": "SC_1",
        "context": "ci/external",
        "state": state,
        "targetUrl": "https://ci.example/1",
        "isRequired": required,
    }


def pull_payload(contexts: list[dict[str, Any]], has_next: bool = False, cursor: str | None = None, **changes: Any) -> dict[str, Any]:
    pull = {
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "abc",
        "url": "https://github.com/acme/lib/pull/12",
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "contexts": {
                                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                                "nodes": contexts,
                            }
                        }
                    }
                }
            ]
        },
    }
    pull.update(changes)
    return {"data": {"repository": {"pullRequest": pull}}}


class Recorder:
    """httpx transport handler returning queued responses and recording requests."""

    def __init__(self, *responses: Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def body(self, index: int = 0) -> Any:
        return loads(self.requests[index].content) if self.requests[index].content else None


def client(recorder: Recorder, api_url: str = "https://api.github.com") -> GitHubClient:
    return GitHubClient("secret", api_url, transport=MockTransport(recorder), auto_retry=False)


@mark.parametrize(
    ("node", "state"),
    [
        (check_run(conclusion="SUCCESS"), "success"),
        (check_run(conclusion="NEUTRAL"), "success"),
        (check_run(conclusion="SKIPPED"), "success"),
        (check_run(status="IN_PROGRESS", conclusion=None), "pending"),
        (check_run(status="QUEUED", conclusion=None), "pending"),
        (check_run(conclusion="ACTION_REQUIRED"), "pending"),
        (check_run(conclusion="FAILURE"), "failure"),
        (check_run(conclusion="TIMED_OUT"), "failure"),
        (check_run(conclusion="CANCELLED"), "failure"),
        (check_run(conclusion="STARTUP_FAILURE"), "failure"),
        (check_run(conclusion="STALE"), "failure"),
        (status_context("SUCCESS"), "success"),
        (status_context("PENDING"), "pending"),
        (status_context("EXPECTED"), "pending"),
        (status_context("FAILURE"), "failure"),
        (status_context("ERROR"), "failure"),
    ],
)
def test_check_states(node: dict[str, Any], state: CheckState) -> None:
    assert parse_check(node).state == state


def test_actions_check_run_carries_workflow_ids() -> None:
    assert parse_check(check_run(required=True)) == Check(
        id="CR_1",
        name="build",
        state="success",
        required=True,
        url="https://github.com/acme/lib/actions/runs/7/job/1",
        workflow_run_id=7,
        workflow_id=3,
    )


def test_external_checks_cannot_be_restarted() -> None:
    external_run = parse_check(check_run(run=False))
    status = parse_check(status_context("SUCCESS"))

    assert (external_run.workflow_run_id, external_run.workflow_id) == (None, None)
    assert (status.name, status.url, status.required, status.workflow_run_id) == (
        "ci/external",
        "https://ci.example/1",
        True,
        None,
    )


def test_unknown_context_type_is_ignored() -> None:
    assert parse_check({"__typename": "Something"}) is None


def test_get_status_follows_check_pagination() -> None:
    recorder = Recorder(
        Response(200, json=pull_payload([check_run(id="CR_1")], has_next=True, cursor="c1")),
        Response(200, json=pull_payload([status_context("PENDING")])),
    )

    status = client(recorder).get_status(REF)

    assert (status.state, status.draft, status.mergeable, status.merge_state, status.head_sha) == (
        "open",
        False,
        "MERGEABLE",
        "CLEAN",
        "abc",
    )
    assert [c.id for c in status.checks] == ["CR_1", "SC_1"]
    first, second = recorder.body(0), recorder.body(1)
    assert recorder.requests[0].url == "https://api.github.com/graphql"
    assert recorder.requests[0].headers["Authorization"].endswith("secret")
    assert first["variables"] == {"owner": "acme", "repo": "lib", "number": 12, "cursor": None}
    assert second["variables"]["cursor"] == "c1"


def test_get_status_without_checks() -> None:
    payload = pull_payload([], state="MERGED")
    payload["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
    recorder = Recorder(Response(200, json=payload))

    status = client(recorder).get_status(REF)

    assert (status.state, status.checks) == ("merged", ())


def test_github_enterprise_graphql_endpoint() -> None:
    recorder = Recorder(Response(200, json=pull_payload([])))

    client(recorder, "https://ghe.example/api/v3").get_status(REF)

    assert recorder.requests[0].url == "https://ghe.example/api/graphql"


def test_missing_pull_request_is_404() -> None:
    recorder = Recorder(
        Response(
            200,
            json={
                "data": {"repository": None},
                "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
            },
        )
    )

    with raises(GitHubError) as error:
        client(recorder).get_status(REF)

    assert error.value.status == 404
    assert "Could not resolve" in error.value.message


@mark.parametrize(
    ("call", "method", "path", "body", "response"),
    [
        (
            lambda c: c.rerun_failed_jobs(REF, 7),
            "POST",
            "/repos/acme/lib/actions/runs/7/rerun-failed-jobs",
            None,
            Response(201, json={}),
        ),
        (
            lambda c: c.merge_pull(REF, "squash", "abc"),
            "PUT",
            "/repos/acme/lib/pulls/12/merge",
            {"merge_method": "squash", "sha": "abc"},
            Response(200, json={"sha": "def", "merged": True, "message": "Merged"}),
        ),
        (
            lambda c: c.update_branch(REF, "abc"),
            "PUT",
            "/repos/acme/lib/pulls/12/update-branch",
            {"expected_head_sha": "abc"},
            Response(202, json={"message": "Updating pull request branch.", "url": "u"}),
        ),
    ],
)
def test_mutations(call: Callable[[GitHubClient], None], method: str, path: str, body: dict[str, Any] | None, response: Response) -> None:
    recorder = Recorder(response)

    call(client(recorder))

    request = recorder.requests[0]
    assert (request.method, request.url.path) == (method, path)
    assert (recorder.body() or None) == body


def test_http_error_carries_status_and_api_message() -> None:
    recorder = Recorder(Response(405, json={"message": "Pull Request is not mergeable"}))

    with raises(GitHubError) as error:
        client(recorder).merge_pull(REF, "merge", "abc")

    assert error.value.status == 405
    assert error.value.message == "Pull Request is not mergeable"


def test_bad_credentials_on_graphql() -> None:
    recorder = Recorder(Response(401, json={"message": "Bad credentials"}))

    with raises(GitHubError) as error:
        client(recorder).get_status(REF)

    assert (error.value.status, error.value.message) == (401, "Bad credentials")


def test_network_error_becomes_github_error() -> None:
    recorder = Recorder(ConnectError("connection refused"))

    with raises(GitHubError) as error:
        client(recorder).get_status(REF)

    assert error.value.status is None
    assert "connection refused" in error.value.message
