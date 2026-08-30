#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPENAPI = ROOT / "schemas" / "integration-openapi-v1.json"
DEFAULT_PROFILE = ROOT / "examples" / "reference_connector" / "contract-surface.json"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"JSON top level must be object: {path}")
    return value


def canonical_digest(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _iter_refs(value: Any):
    if isinstance(value, dict):
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                raise AssertionError("OpenAPI $ref must be a string")
            yield reference
        for item in value.values():
            yield from _iter_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_refs(item)


def _decode_pointer_token(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _component_target(reference: str) -> tuple[str, str]:
    prefix = "#/components/"
    if not reference.startswith(prefix):
        raise AssertionError(
            f"Reference Connector contract contains unsupported $ref: {reference}"
        )
    parts = reference[len(prefix) :].split("/")
    if len(parts) != 2 or not all(parts):
        raise AssertionError(
            f"Reference Connector contract contains unsupported component $ref: {reference}"
        )
    return _decode_pointer_token(parts[0]), _decode_pointer_token(parts[1])


def _component_closure(
    components: dict[str, Any],
    sources: list[Any],
    seeds: set[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    pending = set(seeds)
    for source in sources:
        pending.update(_component_target(reference) for reference in _iter_refs(source))
    visited: set[tuple[str, str]] = set()
    projection: dict[str, dict[str, Any]] = {}
    while pending:
        section, name = min(pending)
        pending.remove((section, name))
        if (section, name) in visited:
            continue
        section_values = components.get(section)
        if not isinstance(section_values, dict) or name not in section_values:
            raise AssertionError(f"OpenAPI component is missing: {section}/{name}")
        component = section_values[name]
        projection.setdefault(section, {})[name] = component
        visited.add((section, name))
        pending.update(
            _component_target(reference)
            for reference in _iter_refs(component)
            if _component_target(reference) not in visited
        )
    return projection


def connector_contract_projection(
    openapi: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "openapi_version",
        "parameters",
        "operations",
        "schemas",
        "webhooks",
        "projection_digest",
    }
    if profile.get("schema_version") != 1 or set(profile) != allowed:
        raise AssertionError("Reference Connector contract profile shape is invalid")
    info = openapi.get("info")
    if not isinstance(info, dict) or info.get("version") != profile.get(
        "openapi_version"
    ):
        raise AssertionError("Reference Connector OpenAPI version is unsupported")
    components = openapi.get("components")
    paths = openapi.get("paths")
    webhooks = openapi.get("webhooks")
    if not all(isinstance(item, dict) for item in (components, paths, webhooks)):
        raise AssertionError("Integration OpenAPI is missing paths/components/webhooks")
    security_schemes = components.get("securitySchemes")
    parameters = components.get("parameters")
    schemas = components.get("schemas")
    if not all(
        isinstance(item, dict)
        for item in (security_schemes, parameters, schemas)
    ):
        raise AssertionError("Integration OpenAPI components are incomplete")
    if "serviceToken" not in security_schemes:
        raise AssertionError("Integration OpenAPI serviceToken scheme is missing")

    parameter_projection: dict[str, Any] = {}
    requested_parameters = profile.get("parameters")
    if not isinstance(requested_parameters, list) or len(requested_parameters) != len(
        set(requested_parameters)
    ):
        raise AssertionError("Reference Connector parameter list is invalid")
    for name in requested_parameters:
        if name not in parameters:
            raise AssertionError(f"Reference Connector parameter is missing: {name}")
        parameter_projection[name] = parameters[name]

    operation_projection: dict[str, Any] = {}
    operation_ids: set[str] = set()
    operations = profile.get("operations")
    if not isinstance(operations, list) or not operations:
        raise AssertionError("Reference Connector operations are missing")
    for item in operations:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "method",
            "operation_id",
        }:
            raise AssertionError("Reference Connector operation entry is invalid")
        path = item["path"]
        method = str(item["method"]).casefold()
        operation_id = item["operation_id"]
        operation = paths.get(path, {}).get(method)
        if not isinstance(operation, dict):
            raise AssertionError(f"Reference Connector operation is missing: {method} {path}")
        if operation.get("operationId") != operation_id:
            raise AssertionError(
                f"Reference Connector operationId drift: {method} {path}"
            )
        if operation_id in operation_ids:
            raise AssertionError(f"Duplicate Reference Connector operationId: {operation_id}")
        operation_ids.add(operation_id)
        operation_projection[f"{method.upper()} {path}"] = operation

    schema_projection: dict[str, Any] = {}
    requested_schemas = profile.get("schemas")
    if not isinstance(requested_schemas, list) or len(requested_schemas) != len(
        set(requested_schemas)
    ):
        raise AssertionError("Reference Connector schema list is invalid")
    for name in requested_schemas:
        if name not in schemas:
            raise AssertionError(f"Reference Connector schema is missing: {name}")
        schema_projection[name] = schemas[name]

    webhook_projection: dict[str, Any] = {}
    requested_webhooks = profile.get("webhooks")
    if not isinstance(requested_webhooks, list) or len(requested_webhooks) != len(
        set(requested_webhooks)
    ):
        raise AssertionError("Reference Connector webhook list is invalid")
    for name in requested_webhooks:
        if name not in webhooks:
            raise AssertionError(f"Reference Connector webhook is missing: {name}")
        webhook_projection[name] = webhooks[name]

    component_projection = _component_closure(
        components,
        [
            openapi.get("security"),
            operation_projection,
            webhook_projection,
        ],
        {
            *(('parameters', name) for name in requested_parameters),
            *(('schemas', name) for name in requested_schemas),
            ("securitySchemes", "serviceToken"),
        },
    )

    return {
        "openapi_version": info["version"],
        "global_security": openapi.get("security"),
        "component_roots": {
            "parameters": sorted(parameter_projection),
            "schemas": sorted(schema_projection),
            "securitySchemes": ["serviceToken"],
        },
        "components": component_projection,
        "operations": operation_projection,
        "webhooks": webhook_projection,
    }


def validate_reference_connector_contract(
    openapi: dict[str, Any],
    profile: dict[str, Any],
    *,
    check_digest: bool = True,
) -> str:
    digest = canonical_digest(connector_contract_projection(openapi, profile))
    if check_digest and profile.get("projection_digest") != digest:
        raise AssertionError(
            "Reference Connector contract drift detected: "
            f"expected {profile.get('projection_digest')}, got {digest}"
        )
    return digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openapi", type=Path, default=DEFAULT_OPENAPI)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--print-digest", action="store_true")
    args = parser.parse_args()
    digest = validate_reference_connector_contract(
        load_json(args.openapi),
        load_json(args.profile),
        check_digest=not args.print_digest,
    )
    print(digest)


if __name__ == "__main__":
    main()
