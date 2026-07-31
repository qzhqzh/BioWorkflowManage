import gzip
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import miniwdl_runtime  # noqa: E402


def write_fastq(path, mate, read_id="read-1"):
    with gzip.open(path, mode="wt", encoding="ascii") as handle:
        handle.write(f"@{read_id}/{mate}\nACGT\n+\nIIII\n")


def test_execution_wdls_pass_miniwdl_static_check():
    wdl_paths = [
        ROOT / "examples" / "miniwdl-execution" / "smoke" / "workflow.wdl",
        ROOT
        / "examples"
        / "miniwdl-execution"
        / "cases"
        / "fastp-bwa"
        / "run-ready.wdl",
    ]

    completed = subprocess.run(
        [
            "miniwdl",
            "check",
            "--no-outside-imports",
            *(str(path) for path in wdl_paths),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_fastp_case_preflight_resolves_only_files_under_case_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MINIWDL_RUN_ROOT", str(tmp_path))
    root = miniwdl_runtime.runtime_root()
    manifest = miniwdl_runtime.load_case("fastp")

    miniwdl_runtime.prepare_case(manifest, root)
    resolved, missing = miniwdl_runtime.resolve_case_inputs(root, manifest)

    assert len(missing) == 2
    input_directory = root / "cases" / "fastp" / "inputs"
    assert set(resolved) == {
        "fastp_demo.input_reads_1",
        "fastp_demo.input_reads_2",
    }
    assert all(
        Path(value).is_relative_to(input_directory) for value in resolved.values()
    )

    write_fastq(input_directory / "sample_R1.fastq.gz", 1)
    write_fastq(input_directory / "sample_R2.fastq.gz", 2)

    resolved_after_data, missing_after_data = (
        miniwdl_runtime.resolve_case_inputs(root, manifest)
    )
    assert missing_after_data == []
    miniwdl_runtime.validate_case_input_contents(
        manifest,
        resolved_after_data,
    )


def test_case_input_symlink_cannot_escape_runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setenv("MINIWDL_RUN_ROOT", str(root))
    root = miniwdl_runtime.runtime_root()
    manifest = miniwdl_runtime.load_case("fastp")
    input_directory = miniwdl_runtime.case_input_directory(root, manifest)
    input_directory.mkdir(parents=True)
    outside = tmp_path / "outside.fastq.gz"
    outside.write_bytes(b"outside")
    (input_directory / "sample_R1.fastq.gz").symlink_to(outside)

    with pytest.raises(miniwdl_runtime.RuntimeHarnessError, match="越过案例目录"):
        miniwdl_runtime.resolve_case_inputs(root, manifest)


def test_fastp_bwa_case_keeps_compiled_runtime_blockers_visible():
    manifest = miniwdl_runtime.load_case("fastp-bwa")

    assert manifest.runnable is True
    assert manifest.wdl.name == "run-ready.wdl"
    assert manifest.source_wdl is not None
    assert manifest.source_wdl.name == "workflow.wdl"
    assert manifest.source_execution_status == "blocked"
    assert len(manifest.source_runtime_blockers) == 2
    assert {output.wdl_name for output in manifest.expected_outputs} == {
        "fastp_bwa_runtime.output_aligned_bam",
        "fastp_bwa_runtime.output_fastp_html",
        "fastp_bwa_runtime.output_fastp_json",
    }


def test_preflight_rejects_placeholder_fastq(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIWDL_RUN_ROOT", str(tmp_path))
    root = miniwdl_runtime.runtime_root()
    manifest = miniwdl_runtime.load_case("fastp")
    input_directory = miniwdl_runtime.case_input_directory(root, manifest)
    input_directory.mkdir(parents=True)
    (input_directory / "sample_R1.fastq.gz").write_bytes(b"placeholder")
    write_fastq(input_directory / "sample_R2.fastq.gz", 2)
    resolved, missing = miniwdl_runtime.resolve_case_inputs(root, manifest)

    assert missing == []
    with pytest.raises(
        miniwdl_runtime.RuntimeHarnessError,
        match="gzip FASTQ",
    ):
        miniwdl_runtime.validate_case_input_contents(manifest, resolved)


def test_static_wdl_inventory_includes_case_and_compiled_sources():
    paths = {
        path.relative_to(ROOT).as_posix()
        for path in miniwdl_runtime.static_wdls()
    }

    assert paths == {
        "examples/phase1-fastp/expected/workflow.wdl",
        "examples/phase1-fastp-bwa/expected/workflow.wdl",
        "examples/miniwdl-execution/smoke/workflow.wdl",
        "examples/miniwdl-execution/cases/fastp-bwa/run-ready.wdl",
    }


def test_output_verification_rejects_empty_files(tmp_path):
    output = tmp_path / "empty.bam"
    output.touch()

    with pytest.raises(
        miniwdl_runtime.RuntimeHarnessError,
        match="文件输出为空",
    ):
        miniwdl_runtime.verify_expected_outputs(
            (
                miniwdl_runtime.ExpectedOutput(
                    wdl_name="workflow.output",
                    output_type="File",
                ),
            ),
            {"outputs": {"workflow.output": str(output)}},
        )
