"""JSON schema of the plan file, generated from the pydantic file schema in ``config``.

The committed ``plan.schema.json`` is this module's output; ``tests/test_schema.py`` fails when they differ.
"""

from __future__ import annotations

from json import dumps
from re import sub
from typing import Any

from git_sleep.config import PR_URL_PATTERN, PlanFile

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "https://raw.githubusercontent.com/pseusys/git-sleep/main/plan.schema.json"
SCHEMA_TITLE = "git-sleep plan"
JSON_INDENT = 2

# JSON schema patterns are ECMA-262 and match anywhere: drop the Python-only group names, anchor, allow a trailing slash.
PR_URL_SCHEMA_PATTERN = "^" + sub(r"\?P<\w+>", "", PR_URL_PATTERN) + "/?$"
NULL_SCHEMA = {"type": "null"}


def plan_schema() -> dict[str, Any]:
    """The plan file schema, including the shorthands the models accept before validation."""
    schema = _without_nulls(PlanFile.model_json_schema())
    definitions = schema.pop("$defs")

    entry = definitions["PullEntry"]["properties"]
    entry["url"]["pattern"] = PR_URL_SCHEMA_PATTERN
    depends_on = entry["depends_on"]
    entry["depends_on"] = {key: value for key, value in depends_on.items() if key not in ("type", "items")} | {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}

    bare_url = {"type": "string", "pattern": PR_URL_SCHEMA_PATTERN, "description": 'A bare pull request URL, the same as {"url": <URL>}.'}
    definitions["Pull"] = {"anyOf": [{"$ref": "#/$defs/PullEntry"}, bare_url]}
    pull = {"$ref": "#/$defs/Pull"}
    schema["properties"]["pulls"]["items"] = pull
    bare_list = {"type": "array", "minItems": 1, "items": pull, "description": 'A bare list of pull requests, the same as {"pulls": [...]}.'}

    return {"$schema": JSON_SCHEMA_DIALECT, "$id": SCHEMA_ID, "title": SCHEMA_TITLE, "anyOf": [schema, bare_list], "$defs": definitions}


def schema_json() -> str:
    """``plan_schema()`` as the text of ``plan.schema.json``."""
    return dumps(plan_schema(), indent=JSON_INDENT, ensure_ascii=False) + "\n"


def _without_nulls(node: Any) -> Any:
    """Drop the ``null`` branches and ``null`` defaults pydantic emits for optional settings: ``null`` only means "not set"."""
    if isinstance(node, list):
        return [_without_nulls(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: _without_nulls(value) for key, value in node.items() if not (key == "default" and value is None)}
    branches = node.get("anyOf")
    if branches is not None and NULL_SCHEMA in branches:
        rest = [branch for branch in branches if branch != NULL_SCHEMA]
        node.pop("anyOf")
        node = rest[0] | node if len(rest) == 1 else node | {"anyOf": rest}
    return node
