#!/usr/bin/env python3
"""Build an ANNOVAR text database .idx file without copying the database."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def build_index(database: Path, output: Path, bin_size: int) -> tuple[int, int]:
    database_size = database.stat().st_size
    temp_output = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    bins_written = 0
    lines_read = 0
    current_key: tuple[bytes, int] | None = None
    current_offset = 0
    seen_chromosomes: set[bytes] = set()
    previous_chromosome: bytes | None = None
    previous_position = -1
    started_at = time.monotonic()
    next_progress = 1024**3

    try:
        with database.open("rb", buffering=1024 * 1024) as source, temp_output.open(
            "w", encoding="ascii", buffering=1024 * 1024
        ) as target:
            target.write(f"#BIN\t{bin_size}\t{database_size}\n")
            while line := source.readline():
                line_offset = current_offset
                current_offset += len(line)
                if current_offset >= next_progress:
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    processed_gib = current_offset / 1024**3
                    print(
                        f"progress {processed_gib:.1f} GiB / "
                        f"{database_size / 1024**3:.1f} GiB "
                        f"({processed_gib / elapsed * 60:.1f} GiB/min)",
                        flush=True,
                    )
                    next_progress += 1024**3
                if not line.strip() or line.startswith(b"#"):
                    continue
                fields = line.split(b"\t", 2)
                if len(fields) < 2:
                    raise ValueError(f"Malformed database line at byte {line_offset}")
                chromosome = fields[0].removeprefix(b"chr")
                try:
                    position = int(fields[1])
                except ValueError as error:
                    raise ValueError(
                        f"Invalid position at byte {line_offset}: {fields[1]!r}"
                    ) from error
                if chromosome != previous_chromosome:
                    if chromosome in seen_chromosomes:
                        raise ValueError(
                            f"Chromosome {chromosome.decode(errors='replace')} is not contiguous"
                        )
                    seen_chromosomes.add(chromosome)
                    previous_chromosome = chromosome
                    previous_position = -1
                if position < previous_position:
                    raise ValueError(
                        f"Database is not position-sorted at byte {line_offset}"
                    )
                previous_position = position
                key = (chromosome, position - position % bin_size)
                if current_key is None:
                    current_key = key
                    current_bin_offset = line_offset
                elif key != current_key:
                    target.write(
                        f"{current_key[0].decode()}\t{current_key[1]}\t"
                        f"{current_bin_offset}\t{line_offset}\n"
                    )
                    bins_written += 1
                    current_key = key
                    current_bin_offset = line_offset
                lines_read += 1

            if current_key is not None:
                target.write(
                    f"{current_key[0].decode()}\t{current_key[1]}\t"
                    f"{current_bin_offset}\t{database_size}\n"
                )
                bins_written += 1
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_output, output)
    except BaseException:
        temp_output.unlink(missing_ok=True)
        raise
    return lines_read, bins_written


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--bin-size", type=int, default=100)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.bin_size <= 0:
        parser.error("--bin-size must be positive")
    database = args.database.resolve()
    if not database.is_file():
        parser.error(f"database does not exist: {database}")
    output = args.output or database.with_name(f"{database.name}.idx")
    if output.exists() and not args.force:
        parser.error(f"output already exists: {output}")
    lines_read, bins_written = build_index(database, output, args.bin_size)
    print(
        f"indexed {lines_read} records into {bins_written} bins: {output}",
        flush=True,
    )


if __name__ == "__main__":
    main()
