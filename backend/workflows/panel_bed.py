"""Validation for the hg19 amplicon BED8 format used by the published workflow."""
import hashlib
import re

MAX_BED_BYTES = 1024 * 1024


def validate_bed(text, *, chromosome_sizes=None):
    if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BED_BYTES:
        raise ValueError("BED 文件不能为空，且不能超过 1 MiB。")
    lines = []
    names = set()
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip() or line.startswith(("#", "track ", "browser ")):
            continue
        cells = line.split("\t")
        if len(cells) != 8:
            raise ValueError(f"第 {number} 行需要 8 列，以制表符分隔；可下载预置 BED 查看格式。")
        if any(not cell or len(cell) > 256 or re.search(r"[\x00-\x20\x7f]", cell) for cell in cells):
            raise ValueError(f"第 {number} 行包含空值、空格或无效字符。")
        chrom, name = cells[0], cells[3]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", chrom) or name in names:
            raise ValueError(f"第 {number} 行染色体格式无效或区域名称重复。")
        try:
            start, end, inner_start, inner_end = (int(cells[i]) for i in (1, 2, 6, 7))
        except ValueError as error:
            raise ValueError(f"第 {number} 行的第 2、3、7、8 列必须是整数坐标。") from error
        if not 0 <= start <= inner_start < inner_end <= end:
            raise ValueError(f"第 {number} 行坐标无效；内部区域必须位于扩增子范围内。")
        if chromosome_sizes is not None and (chrom not in chromosome_sizes or end > chromosome_sizes[chrom]):
            raise ValueError(f"第 {number} 行的染色体或坐标不在当前 hg19 参考基因组范围内。")
        names.add(name)
        lines.append("\t".join(cells))
    if not lines:
        raise ValueError("BED 文件没有有效的目标区域。")
    content = "\n".join(lines) + "\n"
    return content, len(lines), hashlib.sha256(content.encode("utf-8")).hexdigest()
