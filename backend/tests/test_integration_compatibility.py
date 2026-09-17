"""Provider contract guard and executable examples, independent of any consumer."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.validate_integration_compatibility import (
    BASELINES,
    CURRENT,
    compatibility_errors,
    load_json,
    validate_archive_unchanged,
    validate_baselines,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def contract():
    return load_json(ROOT / CURRENT)


def test_current_contract_preserves_every_archived_baseline():
    assert validate_baselines() >= 1


def test_guard_allows_independent_additions_and_documentation(contract):
    changed = deepcopy(contract)
    changed["info"]["version"] = "1.8.0"
    changed["info"]["description"] = "Clarified wording"
    changed["paths"]["/new-capability"] = {
        "get": {"responses": {"200": {"description": "OK"}}}
    }
    changed["paths"]["/tools"]["post"] = {"responses": {"200": {"description": "OK"}}}
    changed["paths"]["/tools"]["get"]["parameters"] = [
        {
            "name": "opt_in",
            "in": "query",
            "required": False,
            "schema": {"type": "boolean"},
        }
    ]
    changed["components"]["schemas"]["NewCapability"] = {"type": "object"}
    changed["components"]["schemas"]["AnalysisRun"]["properties"]["optional_label"] = {
        "type": "string"
    }
    assert compatibility_errors(contract, changed) == []


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("openapi",), "3.0.0"),
        (("info", "version"), "2.0.0"),
        (("info", "version"), "1.0.0"),
        (("info", "version"), "latest"),
        (("servers",), [{"url": "/api/v2/integration"}]),
        (("security",), []),
        (("paths", "/analysis-runs", "post", "x-required-scope"), "analysis:admin"),
        (("components", "schemas", "AnalysisRun", "required"), ["id"]),
        (
            ("components", "schemas", "AnalysisRun", "properties", "status", "enum"),
            ["running", "done"],
        ),
        (
            (
                "components",
                "schemas",
                "AnalysisRun",
                "properties",
                "status_version",
                "type",
            ),
            "string",
        ),
        (
            (
                "components",
                "schemas",
                "AnalysisRun",
                "properties",
                "status_version",
                "minimum",
            ),
            2,
        ),
        (("components", "schemas", "AnalysisProductRef", "additionalProperties"), True),
        (("components", "parameters", "IdempotencyKey", "required"), False),
        (("webhooks", "analysisRunTerminal", "post", "requestBody", "required"), False),
    ],
)
def test_guard_rejects_existing_contract_changes(contract, path, replacement):
    changed = deepcopy(contract)
    cursor = changed
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = replacement
    assert compatibility_errors(contract, changed)


@pytest.mark.parametrize(
    "path",
    [
        ("paths", "/analysis-runs"),
        ("paths", "/analysis-runs", "post"),
        ("components", "schemas", "AnalysisRun", "properties", "output_status"),
        (
            "components",
            "schemas",
            "AnalysisProductVersion",
            "properties",
            "description",
        ),
        ("webhooks", "analysisRunTerminal"),
    ],
)
def test_guard_rejects_removal_including_fields_named_description(contract, path):
    changed = deepcopy(contract)
    cursor = changed
    for part in path[:-1]:
        cursor = cursor[part]
    del cursor[path[-1]]
    assert compatibility_errors(contract, changed)


def test_guard_rejects_new_required_or_closed_object_property(contract):
    for schema_name, required in (("AnalysisRun", True), ("AnalysisProductRef", False)):
        changed = deepcopy(contract)
        schema = changed["components"]["schemas"][schema_name]
        schema["properties"]["new_field"] = {"type": "string"}
        if required:
            schema["required"].append("new_field")
        assert compatibility_errors(contract, changed)


def test_guard_allows_reordering_but_not_adding_statuses(contract):
    changed = deepcopy(contract)
    schema = changed["components"]["schemas"]["AnalysisRun"]
    schema["required"].reverse()
    schema["properties"]["status"]["enum"].reverse()
    changed["paths"]["/analysis-runs"]["post"]["parameters"].reverse()
    assert compatibility_errors(contract, changed) == []
    schema["properties"]["status"]["enum"].append("paused")
    assert compatibility_errors(contract, changed)


def test_guard_optional_parameters_and_literal_defaults(contract):
    changed = deepcopy(contract)
    parameters = changed["paths"]["/analysis-runs"]["get"]["parameters"]
    parameters.append({"name": "extra", "in": "query", "schema": {"type": "string"}})
    assert compatibility_errors(contract, changed) == []
    parameters[-1]["required"] = True
    assert compatibility_errors(contract, changed)
    parameters[-1] = {"$ref": "#/components/parameters/RunId"}
    assert compatibility_errors(contract, changed)
    parameters[-1] = {"$ref": "#/components/parameters/RequestId"}
    assert compatibility_errors(contract, changed) == []
    # Python considers True == 1; a JSON contract must not.
    for old, new in ((True, 1), ({"description": "old"}, {"description": "new"})):
        before = deepcopy(contract)
        before["paths"]["/analysis-runs"]["get"]["parameters"][0]["schema"][
            "default"
        ] = old
        after = deepcopy(before)
        after["paths"]["/analysis-runs"]["get"]["parameters"][0]["schema"][
            "default"
        ] = new
        assert compatibility_errors(before, after)


def test_security_scheme_named_description_is_not_annotation():
    before = {"security": [{"description": ["read"]}]}
    assert compatibility_errors(before, {"security": [{}]})


@pytest.mark.parametrize(
    "restriction",
    [
        {"additionalProperties": False},
        {"additionalProperties": {"type": "string"}},
        {"patternProperties": {".*": {"type": "string"}}},
        {"unevaluatedProperties": False},
    ],
)
def test_guard_does_not_guess_additions_to_constrained_objects(restriction):
    before = {
        "components": {
            "schemas": {
                "Example": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    **restriction,
                }
            }
        }
    }
    after = deepcopy(before)
    after["components"]["schemas"]["Example"]["properties"]["new"] = {"type": "string"}
    assert compatibility_errors(before, after)


def test_guard_does_not_guess_additions_under_negation():
    before = {
        "components": {
            "schemas": {
                "Example": {
                    "not": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    }
                }
            }
        }
    }
    after = deepcopy(before)
    after["components"]["schemas"]["Example"]["not"]["properties"]["new"] = {
        "type": "string"
    }
    assert compatibility_errors(before, after)


@pytest.fixture
def archived_repository(tmp_path):
    def git(*arguments):
        return subprocess.check_output(
            ["git", *arguments], cwd=tmp_path, text=True
        ).strip()

    git("init", "--quiet")
    archive = tmp_path / BASELINES / "integration-v1-1.0.0.json"
    archive.parent.mkdir(parents=True)
    archive.write_text('{"info":{"version":"1.0.0"}}\n', encoding="utf-8")
    git("add", str(BASELINES))
    git(
        "-c",
        "user.name=Contract Test",
        "-c",
        "user.email=contract@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "baseline",
    )
    return tmp_path, archive, git("rev-parse", "HEAD")


def test_archive_accepts_new_baseline_but_not_overwriting_history(archived_repository):
    root, archive, sha = archived_repository
    validate_archive_unchanged(sha, root)
    archive.with_name("integration-v1-1.1.0.json").write_text("{}\n", encoding="utf-8")
    validate_archive_unchanged(sha, root)
    archive.write_text("{}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="must not be rewritten"):
        validate_archive_unchanged(sha, root)


def test_archive_rejects_deletion_and_untrusted_refs(archived_repository):
    root, archive, sha = archived_repository
    archive.unlink()
    with pytest.raises(AssertionError, match="must not be rewritten"):
        validate_archive_unchanged(sha, root)
    with pytest.raises(AssertionError, match="full commit SHA"):
        validate_archive_unchanged("main", root)


def test_guard_fails_closed_when_archive_missing(tmp_path, contract):
    current = tmp_path / CURRENT
    current.parent.mkdir(parents=True)
    current.write_text(json.dumps(contract), encoding="utf-8")
    with pytest.raises(AssertionError, match="baseline is missing"):
        validate_baselines(tmp_path)


def test_openapi_schemas_are_valid_and_component_references_resolve(contract):
    for schema in contract["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)

    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                assert reference.startswith("#/components/")
                target = contract
                for key in reference[2:].split("/"):
                    target = target[key.replace("~1", "/").replace("~0", "~")]
            for item in value.values():
                check_refs(item)
        elif isinstance(value, list):
            for item in value:
                check_refs(item)

    check_refs(contract)


def test_quickstart_requests_match_openapi_and_fixed_product(contract):
    product = load_json(ROOT / "examples/nextflow-lc103/product-manifest.json")
    examples = ROOT / "examples/integration-quickstart"
    preflight = load_json(examples / "lc103-preflight.json")
    submission = load_json(examples / "lc103-submission.json")
    for payload, schema_name in (
        (preflight, "AnalysisPreflight"),
        (submission, "AnalysisSubmission"),
    ):
        Draft202012Validator(
            {
                "$ref": f"#/components/schemas/{schema_name}",
                "components": contract["components"],
            }
        ).validate(payload)
        assert payload["analysis_product"] == {
            "analysis_code": product["analysis_product"]["code"],
            "contract_version": product["analysis_product"]["contract_version"],
        }
        inputs = product["interface_contract"]["inputs"]
        assert set(payload["inputs"]) == {item["name"] for item in inputs}
        for item in inputs:
            assert item["wdl_type"] == "Array[File]"
            value = payload["inputs"][item["name"]]
            assert isinstance(value, list) and value
            assert all(reference["root_alias"] == "rawdata" for reference in value)
    assert preflight == {key: submission[key] for key in preflight}
