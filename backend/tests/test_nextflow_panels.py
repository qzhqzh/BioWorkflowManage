from copy import deepcopy
import gzip

import pytest

from compiler_core import canonical_digest
from workflows.analysis_products import publish_analysis_product_version, AnalysisProductError
from workflows.execution_engines import ExecutionSnapshotError, NEXTFLOW, validate_execution_snapshot
from workflows.integration_api import IntegrationAPIError, _preflight_workflow
from workflows.models import AnalysisProduct
from workflows.nextflow_runtime import _nextflow_arguments
from test_nextflow_runtime import _nextflow_version


pytestmark = pytest.mark.django_db


def panel_version():
    version = _nextflow_version()
    binding = {"input": "panel", "parameter": "panel", "bed_parameter": "amp_target",
               "options": {"LC103": "panels/LC103.bed", "HS430": "panels/HS430.bed"}}
    version.runtime_manifest["fixed_params"] = {}
    version.runtime_manifest["panel_binding"] = binding
    version.compiled_bundle["files"].update({"panels/LC103.bed": "chr1\t1\t10\n", "panels/HS430.bed": "chr1\t20\t30\n"})
    port = {"name": "panel", "wdl_type": "String", "semantic_type": "bio.panel.id", "required": False,
            "default": "LC103", "constraints": {"enum": ["LC103", "HS430"]}}
    version.interface_contract["inputs"].append(port)
    version.workflow_graph["nodes"].append({"id": "panel", "type": "workflow_input", "port": port})
    version.semantic_digest = canonical_digest(version.workflow_graph)
    version.compiled_digest = canonical_digest(version.compiled_bundle)
    version.pk = None
    version.version = 2
    version.save()
    return version


def test_selected_panel_changes_both_cli_parameter_and_bed(tmp_path):
    version = panel_version()
    source = tmp_path / "source"
    (source / "panels").mkdir(parents=True)
    for panel in ("LC103", "HS430"):
        (source / "panels" / f"{panel}.bed").write_text("chr1\t1\t10\n")
        args = _nextflow_arguments(
            "nextflow", entrypoint=source / "main.nf", config_path=tmp_path / "nextflow.config",
            fastq_list=tmp_path / "reads.csv", source_directory=source, database_path=tmp_path,
            attempt_directory=tmp_path / "attempt", manifest=version.runtime_manifest, task_name="S001",
            workflow_name="lc103_amp", input_values={"lc103_amp.panel": panel},
        )
        assert args.count("--panel") == args.count("--amp_target") == 1
        assert args[args.index("--panel") + 1] == panel
        assert args[args.index("--amp_target") + 1] == str(source / "panels" / f"{panel}.bed")


@pytest.mark.parametrize("invalid", ["missing", "traversal", "duplicate-flag"])
def test_panel_binding_cannot_escape_or_omit_fixed_bed(invalid):
    version = panel_version()
    manifest = version.runtime_manifest
    if invalid == "missing":
        del version.compiled_bundle["files"]["panels/HS430.bed"]
    elif invalid == "traversal":
        manifest["panel_binding"]["options"]["HS430"] = "../HS430.bed"
    else:
        manifest["fixed_params"]["panel"] = "LC103"
    with pytest.raises(ExecutionSnapshotError):
        validate_execution_snapshot(NEXTFLOW, version.compiled_bundle, manifest)


def test_preflight_defaults_and_validates_panel_before_submission(tmp_path, settings):
    settings.INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE = False
    settings.ANALYSIS_MIN_AVAILABLE_MEMORY_GB = 0
    settings.ANALYSIS_RAWDATA_ROOT = tmp_path
    settings.ANALYSIS_RAWDATA_EXECUTION_ROOT = tmp_path
    for mate in (1, 2):
        with gzip.open(tmp_path / f"S001_R{mate}.fastq.gz", "wt") as f:
            f.write("@read/" + str(mate) + "\nACGT\n+\nIIII\n")
    version = panel_version()
    product = AnalysisProduct.objects.create(code="amp-panels", name="AMP")
    published, _ = publish_analysis_product_version(product, contract_version="1.0.0", workflow_version=version, actor="test")
    body = {"analysis_product": {"analysis_code": product.code, "contract_version": "1.0.0"},
            "inputs": {f"read{mate}": {"root_alias": "rawdata", "relative_path": f"S001_R{mate}.fastq.gz"} for mate in (1, 2)}}
    default = _preflight_workflow(body, client_id="okb")
    assert default["input_values"]["lc103_amp.panel"] == "LC103"
    body["inputs"]["panel"] = "HS430"
    selected = _preflight_workflow(body, client_id="okb")
    assert selected["input_values"]["lc103_amp.panel"] == "HS430"
    frozen = deepcopy(selected["input_values"])
    body["inputs"]["panel"] = "unknown"
    with pytest.raises(IntegrationAPIError) as error:
        _preflight_workflow(body, client_id="okb")
    assert error.value.code == "INPUT_CONSTRAINT_INVALID"
    assert frozen["lc103_amp.panel"] == "HS430"
    assert published.interface_contract["inputs"][-1]["default"] == "LC103"


def test_published_panel_contract_must_match_bed_choices(settings):
    settings.INTEGRATION_REQUIRE_SIGNED_WORKFLOW_PACKAGE = False
    version = panel_version()
    version.interface_contract["inputs"][-1]["constraints"]["enum"].append("missing")
    product = AnalysisProduct.objects.create(code="amp-panels", name="AMP")
    with pytest.raises(AnalysisProductError, match="Panel"):
        publish_analysis_product_version(product, contract_version="1.0.0", workflow_version=version, actor="test")
