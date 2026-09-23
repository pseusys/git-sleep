"""Loading and validation of the JSON plan describing PRs and their dependencies.

The plan is parsed in two layers: pydantic models mirroring the file (every setting optional, unknown keys
rejected), then a resolution step producing the frozen runtime models the engine works with.
"""

from __future__ import annotations

from json import JSONDecodeError, loads
from pathlib import Path
from re import fullmatch
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PR_URL_PATTERN = r"https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"

MergeMethod = Literal["merge", "squash", "rebase"]
ChecksMode = Literal["all", "required"]
Strategy = Literal["constant", "linear", "exponential"]

DEFAULT_POLL_INTERVAL = 60.0
DEFAULT_MERGE_METHOD: MergeMethod = "merge"
DEFAULT_CHECKS: ChecksMode = "all"
DEFAULT_UPDATE_BRANCH = False


class ConfigError(ValueError):
    """Raised when the plan file is malformed."""


# Runtime models


class _Frozen(BaseModel):
    """Base of the runtime models: immutable once resolved."""

    model_config = ConfigDict(frozen=True)


class PullRef(_Frozen):
    """A pull request, identified by repository owner, name and number."""

    owner: str
    repo: str
    number: int

    @classmethod
    def from_url(cls, url: str) -> PullRef:
        """Parse a ``https://<host>/<owner>/<repo>/pull/<number>`` URL."""
        match = fullmatch(PR_URL_PATTERN, url)
        if match is None:
            raise ValueError(f"not a pull request URL: {url!r}")
        return cls(owner=match["owner"], repo=match["repo"], number=int(match["number"]))

    def __str__(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


class RetryPolicy(_Frozen):
    """How many times failed workflow runs are restarted, and how long to wait before each restart."""

    max_attempts: int = 3
    strategy: Strategy = "exponential"
    delay: float = 60.0
    factor: float = 2.0
    max_delay: float = 3600.0

    def delay_for(self, attempt: int) -> float:
        """Delay before restart number ``attempt`` (0-based)."""
        if self.strategy == "constant":
            delay = self.delay
        elif self.strategy == "linear":
            delay = self.delay * (attempt + 1)
        else:
            delay = self.delay * self.factor**attempt
        return min(delay, self.max_delay)


class PullSpec(_Frozen):
    """One pull request of the plan with every setting resolved and dependencies as ids."""

    id: str
    url: str
    ref: PullRef
    depends_on: tuple[str, ...]
    merge_method: MergeMethod
    checks: ChecksMode
    retry: RetryPolicy
    update_branch: bool


class Plan(_Frozen):
    """The resolved plan the orchestrator runs."""

    pulls: tuple[PullSpec, ...]
    poll_interval: float = DEFAULT_POLL_INTERVAL


# File schema


class _Schema(BaseModel):
    """Base of the file schema: strict types, unknown keys rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)


class RetrySettings(_Schema):
    """When and how often the failed GitHub Actions runs of a pull request are restarted."""

    max_attempts: Annotated[int | None, Field(ge=0, description=f"Batch restarts of failed runs per pull request and head commit. Default: {RetryPolicy.model_fields['max_attempts'].default}.")] = None
    strategy: Annotated[Strategy | None, Field(description=f"How the delay grows between restarts: delay, delay × (n+1) or delay × factor^n. Default: {RetryPolicy.model_fields['strategy'].default}.")] = None
    delay: Annotated[float | None, Field(ge=0, description=f"Seconds before the first restart. Default: {RetryPolicy.model_fields['delay'].default:g}.")] = None
    factor: Annotated[float | None, Field(ge=1, description=f"Multiplier per restart for the exponential strategy. Default: {RetryPolicy.model_fields['factor'].default:g}.")] = None
    max_delay: Annotated[float | None, Field(ge=0, description=f"Upper bound on any delay, in seconds. Default: {RetryPolicy.model_fields['max_delay'].default:g}.")] = None


class Settings(_Schema):
    """Settings for every pull request; in a pull request, they override ``defaults``."""

    merge_method: Annotated[MergeMethod | None, Field(description=f"How the pull request is merged. Default: {DEFAULT_MERGE_METHOD}.")] = None
    checks: Annotated[ChecksMode | None, Field(description=f"Which checks must pass: all of them, or only those required by branch protection or rulesets. Default: {DEFAULT_CHECKS}.")] = None
    update_branch: Annotated[bool | None, Field(description=f"Update the branch from its base when it is behind. Default: {str(DEFAULT_UPDATE_BRANCH).lower()}.")] = None
    retry: Annotated[RetrySettings, Field(default_factory=RetrySettings, description="When and how often failed GitHub Actions runs are restarted.")]


class PullEntry(Settings):
    """A pull request to merge, with its dependencies and setting overrides."""

    url: Annotated[str, Field(description="Pull request URL: https://<host>/<owner>/<repo>/pull/<number>.")]
    id: Annotated[str | None, Field(min_length=1, description="Name used in depends_on. Default: the URL.")] = None
    depends_on: Annotated[list[str], Field(description="Ids or URLs of the pull requests that must be merged first; a single string is accepted.")] = []

    @model_validator(mode="before")
    @classmethod
    def _bare_url(cls, value: Any) -> Any:
        """Accept a bare URL string as a pull entry."""
        return {"url": value} if isinstance(value, str) else value

    @field_validator("depends_on", mode="before")
    @classmethod
    def _single_dependency(cls, value: Any) -> Any:
        """Accept a single reference instead of a list."""
        return [value] if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def _pull_url(cls, value: str) -> str:
        """Normalize the URL and check that it points at a pull request."""
        value = _normalize_url(value)
        PullRef.from_url(value)
        return value


class PlanFile(_Schema):
    """A git-sleep plan: pull requests to merge in dependency order, and how."""

    schema_uri: Annotated[str | None, Field(alias="$schema", title="Schema", description="JSON schema of this file, for editors; ignored by git-sleep.")] = None
    poll_interval: Annotated[float, Field(gt=0, description=f"Seconds between polls of every active pull request. Default: {DEFAULT_POLL_INTERVAL:g}.")] = DEFAULT_POLL_INTERVAL
    defaults: Annotated[Settings, Field(default_factory=Settings, description="Settings applied to every pull request unless it overrides them.")]
    pulls: Annotated[list[PullEntry], Field(min_length=1, description="Pull requests to merge: objects, or bare pull request URLs.")]

    @model_validator(mode="before")
    @classmethod
    def _bare_list(cls, value: Any) -> Any:
        """Accept a bare list of pull entries as the whole plan."""
        return {"pulls": value} if isinstance(value, list) else value


# Loading and resolution


def load_plan(path: Path) -> Plan:
    """Read, validate and resolve the plan file at ``path``."""
    try:
        raw = loads(path.read_text())
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    except JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}") from e
    return parse_plan(raw)


def parse_plan(raw: Any) -> Plan:
    """Validate and resolve an already decoded plan."""
    try:
        plan_file = PlanFile.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(_format_validation_error(e)) from e
    return _resolve(plan_file)


def _resolve(plan_file: PlanFile) -> Plan:
    """Layer each entry over ``defaults`` and the built-in defaults, then resolve dependencies."""
    defaults = plan_file.defaults
    pulls = []
    for entry in plan_file.pulls:
        retry = {**defaults.retry.model_dump(exclude_none=True), **entry.retry.model_dump(exclude_none=True)}
        pulls.append(
            PullSpec(
                id=entry.id or entry.url,
                url=entry.url,
                ref=PullRef.from_url(entry.url),
                depends_on=tuple(entry.depends_on),
                merge_method=_pick(entry.merge_method, defaults.merge_method, DEFAULT_MERGE_METHOD),
                checks=_pick(entry.checks, defaults.checks, DEFAULT_CHECKS),
                update_branch=_pick(entry.update_branch, defaults.update_branch, DEFAULT_UPDATE_BRANCH),
                retry=RetryPolicy(**retry),
            )
        )
    return Plan(pulls=tuple(_resolve_dependencies(pulls)), poll_interval=plan_file.poll_interval)


def _pick(*values: Any) -> Any:
    """Return the first value that is set."""
    return next(value for value in values if value is not None)


def _resolve_dependencies(pulls: list[PullSpec]) -> list[PullSpec]:
    """Map dependency references (ids or URLs) to ids and reject duplicates, unknowns and cycles."""
    urls: set[str] = set()
    aliases: dict[str, str] = {}
    for pull in pulls:
        if pull.url in urls:
            raise ConfigError(f"pull request listed twice: {pull.url}")
        if pull.id in aliases:
            raise ConfigError(f"duplicate pull request id: {pull.id!r}")
        urls.add(pull.url)
        aliases[pull.id] = pull.id
        aliases.setdefault(pull.url, pull.id)

    resolved = []
    for pull in pulls:
        deps: list[str] = []
        for dep in pull.depends_on:
            target = aliases.get(_normalize_url(dep))
            if target is None:
                raise ConfigError(f"{pull.id!r} depends on unknown pull request {dep!r}")
            if target == pull.id:
                raise ConfigError(f"{pull.id!r} depends on itself")
            if target not in deps:
                deps.append(target)
        resolved.append(pull.model_copy(update={"depends_on": tuple(deps)}))

    _check_acyclic({pull.id: pull.depends_on for pull in resolved})
    return resolved


def _check_acyclic(graph: dict[str, tuple[str, ...]]) -> None:
    """Raise ``ConfigError`` naming the path of the first dependency cycle found."""
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            cycle = path[path.index(node) :] + [node]
            raise ConfigError(f"dependency cycle: {' -> '.join(cycle)}")
        visiting.add(node)
        for dep in graph[node]:
            visit(dep, path + [node])
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node, [])


def _normalize_url(url: str) -> str:
    """Strip whitespace and trailing slashes, so a URL matches however it was written."""
    return url.strip().rstrip("/")


def _format_validation_error(error: ValidationError) -> str:
    """One ``location: message`` line per problem pydantic found."""
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        lines.append(f"{location}: {item['msg']}" if location else item["msg"])
    return "invalid plan:\n  " + "\n  ".join(lines)
