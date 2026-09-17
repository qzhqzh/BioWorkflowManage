#!/usr/bin/env python3
"""Conservative, provider-owned v1 contract guard; not a general schema differ."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINES = Path("schemas/compatibility")
CURRENT = Path("schemas/integration-openapi-v1.json")
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
ANNOTATIONS = {"description", "summary", "title", "example", "examples", "externalDocs"}
MAPS = {
    "paths",
    "webhooks",
    "components",
    "schemas",
    "properties",
    "patternProperties",
    "$defs",
    "parameters",
    "headers",
    "responses",
    "requestBodies",
    "securitySchemes",
    "content",
    "links",
    "callbacks",
    "encoding",
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"Expected JSON object: {path}")
    return value


def _at(document: Any, path: tuple[str, ...]) -> Any:
    for part in path:
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def compatibility_errors(previous: dict, current: dict) -> list[str]:
    """Keep existing constraints; allow independent operations and optional open fields.

    Intentional conservatism: enum/required changes and constraint relaxations also
    fail. They need directional compatibility support plus regression evidence.
    """
    errors: list[str] = []

    def reject(path, reason):
        pointer = "/" + "/".join(
            part.replace("~", "~0").replace("/", "~1") for part in path
        )
        errors.append(f"{pointer}: {reason}")

    def optional_parameter(item):
        if not isinstance(item, dict):
            return False
        if "$ref" in item:
            prefix = "#/components/parameters/"
            if not str(item["$ref"]).startswith(prefix):
                return False
            item = (
                current.get("components", {})
                .get("parameters", {})
                .get(
                    item["$ref"][len(prefix) :].replace("~1", "/").replace("~0", "~"),
                    {},
                )
            )
        return (
            bool(item.get("name"))
            and item.get("in") in {"query", "header", "cookie"}
            and item.get("required", False) is False
        )

    def new_key_allowed(path, key, value):
        if path in {("paths",), ("webhooks",)}:
            return True
        if len(path) == 2 and path[0] == "components":
            return True
        if len(path) == 2 and path[0] in {"paths", "webhooks"} and key in HTTP_METHODS:
            return True
        if path and path[-1] == "properties":
            old_parent = _at(previous, path[:-1])
            new_parent = _at(current, path[:-1])
            # A closed or constrained object's extension is not safe for old readers.
            return (
                old_parent.get("type") == "object"
                and old_parent.get("additionalProperties", True) is True
                and not old_parent.get("patternProperties")
                and old_parent.get("unevaluatedProperties", True) is True
                and not any(
                    part
                    in {
                        "not",
                        "if",
                        "then",
                        "else",
                        "oneOf",
                        "anyOf",
                        "allOf",
                        "dependentSchemas",
                    }
                    for part in path
                )
                and key not in new_parent.get("required", [])
            )
        if (
            key == "parameters"
            and len(path) in {2, 3}
            and path[0] in {"paths", "webhooks"}
        ):
            return isinstance(value, list) and all(
                optional_parameter(item) for item in value
            )
        return False

    def walk(old, new, path=()):
        if path == ("info", "version"):
            pattern = r"[0-9]+\.[0-9]+\.[0-9]+"
            if not all(
                isinstance(v, str) and re.fullmatch(pattern, v) for v in (old, new)
            ):
                reject(path, "expected numeric contract revision")
            elif old.split(".")[0] != new.split(".")[0] or tuple(
                map(int, new.split("."))
            ) < tuple(map(int, old.split("."))):
                reject(path, "v1 contract revision cannot change major or go backwards")
            return
        # Literal values must not lose a property called e.g. 'description'.
        if path and path[-1] == "security":
            if _json(old) != _json(new):
                reject(path, "existing security requirements changed")
            return
        if path and path[-1] in {"default", "const", "enum", "required"}:
            equal = _json(old) == _json(new)
            if (
                path[-1] in {"enum", "required"}
                and isinstance(old, list)
                and isinstance(new, list)
            ):
                equal = sorted(map(_json, old)) == sorted(map(_json, new))
            if not equal:
                reject(path, "existing value/constraint changed")
            return
        if type(old) is not type(new):
            reject(path, "existing JSON type changed")
        elif isinstance(old, dict):
            is_map = bool(path and path[-1] in MAPS)
            for key in old.keys() | new.keys():
                if not is_map and key in ANNOTATIONS:
                    continue
                child = (*path, key)
                if key not in new:
                    reject(child, "existing contract member removed")
                elif key not in old:
                    if not new_key_allowed(path, key, new[key]):
                        reject(child, "extension needs explicit compatibility support")
                else:
                    walk(old[key], new[key], child)
        elif isinstance(old, list):
            if path and path[-1] == "parameters":

                def identity(item):
                    return item.get("$ref") or f"{item.get('in')}:{item.get('name')}"

                if not all(isinstance(item, dict) for item in [*old, *new]):
                    reject(path, "invalid parameter list")
                    return
                new_items = {identity(item): (i, item) for i, item in enumerate(new)}
                if len(new_items) != len(new):
                    reject(path, "duplicate parameter")
                old_ids = {identity(item) for item in old}
                for i, item in enumerate(old):
                    target = new_items.get(identity(item))
                    if target is None:
                        reject(path, "existing parameter removed")
                    else:
                        # No schema additions are automatically allowed inside parameters.
                        if _json(_without_annotations(item)) != _json(
                            _without_annotations(target[1])
                        ):
                            reject((*path, str(i)), "existing parameter changed")
                for key, (_, item) in new_items.items():
                    if key not in old_ids and not optional_parameter(item):
                        reject(path, "new parameter is not explicitly optional")
            elif len(old) != len(new):
                reject(path, "existing alternatives/list changed")
            else:
                for i, (a, b) in enumerate(zip(old, new)):
                    walk(a, b, (*path, str(i)))
        elif old != new:
            reject(path, "existing value changed")

    walk(previous, current)
    return sorted(errors)


def _without_annotations(value, *, mapping=False):
    if isinstance(value, list):
        return [_without_annotations(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: item
        if key in {"default", "const", "enum"}
        else _without_annotations(item, mapping=key in MAPS)
        for key, item in value.items()
        if mapping or key not in ANNOTATIONS
    }


def validate_baselines(root: Path = ROOT) -> int:
    paths = sorted((root / BASELINES).glob("integration-v1-*.json"))
    if not paths:
        raise AssertionError("Integration v1 compatibility baseline is missing")
    current = load_json(root / CURRENT)
    for path in paths:
        errors = compatibility_errors(load_json(path), current)
        if errors:
            raise AssertionError(
                f"Compatibility guard failed against {path.name}:\n" + "\n".join(errors)
            )
    return len(paths)


def validate_archive_unchanged(base_ref: str, root: Path = ROOT) -> None:
    """PR base is a trusted full commit SHA, never shell-interpolated."""
    if not re.fullmatch(r"[0-9a-f]{40}", base_ref):
        raise AssertionError("--base-ref must be a full commit SHA")
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", base_ref, "--", str(BASELINES)],
        cwd=root,
        text=True,
    ).splitlines()
    for relative in paths:
        if not relative.endswith(".json"):
            continue
        before = subprocess.check_output(
            ["git", "show", f"{base_ref}:{relative}"], cwd=root
        )
        path = root / relative
        if not path.is_file() or path.read_bytes() != before:
            raise AssertionError(
                f"Historical baseline must not be rewritten or removed: {relative}"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref", help="PR base SHA; also reject historical archive edits"
    )
    args = parser.parse_args()
    if args.base_ref:
        validate_archive_unchanged(args.base_ref)
    print(
        f"Integration v1 compatibility: {validate_baselines()} historical baseline(s) passed."
    )


if __name__ == "__main__":
    main()
