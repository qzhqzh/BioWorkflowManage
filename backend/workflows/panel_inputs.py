"""Optional immutable, bounded BED configuration for an explicitly published workflow."""
import hashlib
import re
from pathlib import Path

from .panel_bed import validate_bed

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
SEMANTICS = {"input": "bio.panel.id", "bed_input": "bio.panel.bed_text", "sha256_input": "core.checksum.sha256"}


def validate_binding(binding, *, reserved):
    if not isinstance(binding, dict) or set(binding) != {*SEMANTICS, "parameter", "bed_parameter", "reference_fai"}:
        raise ValueError("panel_input_binding 字段无效。")
    names = [binding[key] for key in (*SEMANTICS, "parameter", "bed_parameter")]
    if any(not isinstance(name, str) or not IDENTIFIER.fullmatch(name) for name in names):
        raise ValueError("Panel 输入或参数名称无效。")
    if len({binding[key] for key in SEMANTICS}) != 3 or binding["parameter"] == binding["bed_parameter"]:
        raise ValueError("Panel 输入或参数名称重复。")
    if {binding["parameter"], binding["bed_parameter"]} & reserved:
        raise ValueError("Panel 参数不得覆盖其他固定参数。")
    path = binding["reference_fai"]
    if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts or "\\" in path or "\x00" in path:
        raise ValueError("Panel 参考索引必须是固定数据库中的安全相对路径。")


def validate_interface(binding, interface):
    ports = {port.get("name"): port for port in interface.get("inputs", [])}
    for key, semantic in SEMANTICS.items():
        port = ports.get(binding[key], {})
        if port.get("wdl_type") != "String" or port.get("semantic_type") != semantic or not port.get("required", True):
            raise ValueError("动态 Panel 必须声明编号、BED 内容及 SHA256 三个必填字符串输入。")
    if ports[binding["input"]].get("constraints", {}).get("enum"):
        raise ValueError("动态 Panel 输入不能使用固定枚举。")


def select_panel(binding, inputs, workflow_name, *, database_path=None):
    values = {key: inputs.get(f"{workflow_name}.{binding[key]}") for key in SEMANTICS}
    if not isinstance(values["input"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", values["input"]):
        raise ValueError("Panel 编号格式无效。")
    content, _, digest = validate_bed(values["bed_input"])
    # The caller must send the normalized, exact content whose identity was fixed at upload.
    if content != values["bed_input"] or values["sha256_input"] != digest:
        raise ValueError("Panel BED 内容与 SHA256 不一致。")
    if database_path is not None:
        root = Path(database_path).resolve()
        index = root / binding["reference_fai"]
        current = root
        for part in Path(binding["reference_fai"]).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Panel 参考索引不能使用符号链接。")
        if not index.resolve().is_relative_to(root) or not index.is_file():
            raise ValueError("Panel 对应的参考基因组索引尚未就绪。")
        sizes = {row[0]: int(row[1]) for line in index.read_text().splitlines() if (row := line.split("\t")) and len(row) > 1}
        validate_bed(content, chromosome_sizes=sizes)
    return {"code": values["input"], "content": content, "sha256": digest}


def materialize_panel(selection, directory):
    directory = Path(directory) / "inputs"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Panel 暂存目录无效。")
    path = directory / f"panel-{selection['sha256']}.bed"
    content = selection["content"].encode("utf-8")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != selection["sha256"]:
            raise ValueError("Panel 暂存文件与固定版本不一致。")
    else:
        with path.open("xb") as output:
            output.write(content)
        path.chmod(0o444)
    return path
