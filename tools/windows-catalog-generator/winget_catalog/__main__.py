"""Command line entry point for the Windows catalog generator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from winget_catalog.generate import generate, validate_catalog, write_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Windows application catalog from a winget-pkgs checkout.")
    parser.add_argument("--source", required=True, type=Path, help="Path to a winget-pkgs checkout.")
    parser.add_argument("--output", required=True, type=Path, help="Directory for the catalog, checksum, and report.")
    parser.add_argument("--source-commit", help="Upstream commit recorded in the catalog. Defaults to git rev-parse HEAD.")
    parser.add_argument("--require-applications", action="store_true", help="Fail when the catalog contains no applications.")
    args = parser.parse_args(argv)

    try:
        result = generate(args.source, args.source_commit)
    except (FileNotFoundError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1

    errors = validate_catalog(result.catalog)
    if errors:
        print("Catalog validation failed: " + ", ".join(errors[:10]), file=sys.stderr)
        return 1
    if args.require_applications and result.report["applicationCount"] == 0:
        print("Catalog did not contain any applications.", file=sys.stderr)
        return 1

    write_output(result, args.output)
    print(
        f"applications={result.report['applicationCount']} "
        f"bytes={result.report['catalogBytes']} "
        f"gzip={result.report['catalogGzipBytes']} "
        f"sha256={result.report['catalogSha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
