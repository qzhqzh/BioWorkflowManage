import hashlib
from pathlib import Path

import pytest

from workflows.execution_engines import (
    ExecutionSnapshotError,
    selected_uploaded_panel,
    validate_nextflow_panel_interface,
)
from workflows.nextflow_runtime import _nextflow_arguments
from workflows.panel_inputs import validate_binding
from test_nextflow_runtime import _runtime_manifest

BED = "chr1\t100\t200\tRegion1\t.\t.\t120\t180\n"
BINDING = {
    "input": "panel",
    "bed_input": "panel_bed",
    "sha256_input": "panel_bed_sha256",
    "parameter": "panel",
    "bed_parameter": "amp_target",
    "reference_fai": "hg19.fai",
}


def configured(tmp_path):
    manifest = _runtime_manifest()
    manifest["fixed_params"] = {}
    manifest["panel_input_binding"] = BINDING
    (tmp_path / "hg19.fai").write_text("chr1\t1000\t0\t0\t0\n")
    values = {
        "amp.panel": "HS430",
        "amp.panel_bed": BED,
        "amp.panel_bed_sha256": hashlib.sha256(BED.encode()).hexdigest(),
    }
    return manifest, values


def test_uploaded_panel_is_materialized_from_frozen_input(tmp_path):
    manifest, values = configured(tmp_path)
    args = _nextflow_arguments(
        "nextflow",
        entrypoint=tmp_path / "main.nf",
        config_path=tmp_path / "config",
        fastq_list=tmp_path / "reads",
        source_directory=tmp_path,
        database_path=tmp_path,
        attempt_directory=tmp_path / "attempt",
        manifest=manifest,
        task_name="sample",
        input_values=values,
        workflow_name="amp",
    )
    assert args[args.index("--panel") + 1] == "HS430"
    bed = Path(args[args.index("--amp_target") + 1])
    assert bed.read_text() == BED
    assert bed.name == f"panel-{values['amp.panel_bed_sha256']}.bed"
    values["amp.panel_bed"] = BED.replace("120", "130")
    with pytest.raises(ExecutionSnapshotError, match="SHA256"):
        selected_uploaded_panel(manifest, values, "amp", database_path=tmp_path)
    assert bed.read_text() == BED


@pytest.mark.parametrize("change", ["code", "columns", "bounds", "reference"])
def test_invalid_uploaded_panel_fails_before_execution(tmp_path, change):
    manifest, values = configured(tmp_path)
    if change == "code":
        values["amp.panel"] = "../../bed"
    elif change == "columns":
        values["amp.panel_bed"] = "chr1\t1\t2\n"
    elif change == "bounds":
        values["amp.panel_bed"] = BED.replace("120", "220")
    else:
        values["amp.panel_bed"] = BED.replace("chr1", "chr2")
    values["amp.panel_bed_sha256"] = hashlib.sha256(
        values["amp.panel_bed"].encode()
    ).hexdigest()
    with pytest.raises(ExecutionSnapshotError):
        selected_uploaded_panel(manifest, values, "amp", database_path=tmp_path)


def test_binding_must_be_explicit_and_cannot_override_managed_flags():
    with pytest.raises(ValueError):
        validate_binding({**BINDING, "bed_parameter": "results"}, reserved={"results"})
    with pytest.raises(ExecutionSnapshotError):
        validate_nextflow_panel_interface(
            {"panel_input_binding": BINDING}, {"inputs": []}
        )
