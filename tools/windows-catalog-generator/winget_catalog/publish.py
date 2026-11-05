"""Validate generator output and write the GitHub Pages catalog files."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from winget_catalog import SOURCE_NAME
from winget_catalog.generate import validate_catalog

# A new catalog below this share of the last published application count is rejected
# unless the manual run sets --allow-count-reduction. Scheduled runs cannot set it.
SEVERE_REDUCTION_PERCENT = 75
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class PublishError(Exception):
    """The catalog cannot be published."""


def publish_catalog(
    generated_dir: Path,
    pages_dir: Path,
    previous_metadata: Path | None = None,
    allow_count_reduction: bool = False,
) -> dict[str, Any]:
    catalog_bytes = _read(generated_dir / "windows-catalog.json")
    gzip_bytes = _read(generated_dir / "windows-catalog.json.gz")
    digest_text = (generated_dir / "windows-catalog.json.sha256").read_text(encoding="utf-8")
    report = _read_json(generated_dir / "windows-catalog-report.json")
    catalog = _read_catalog(catalog_bytes)
    applications = catalog.get("applications")
    if not isinstance(applications, list):
        raise PublishError("Catalog application records are malformed.")
    if len(applications) == 0:
        raise PublishError("Catalog did not contain any applications.")
    if any(not isinstance(item, dict) for item in applications):
        raise PublishError("Catalog application records are malformed.")

    identifiers = [item.get("id") for item in applications]
    if any(not isinstance(item, str) or item == "" for item in identifiers) or len(identifiers) != len(set(identifiers)):
        raise PublishError("Catalog contains duplicate package identifiers.")
    for application in applications:
        installers = application.get("installers")
        if not isinstance(installers, list) or not installers or any(not isinstance(item, dict) for item in installers):
            raise PublishError("Catalog installer collections are malformed.")

    errors = validate_catalog(catalog)
    if errors:
        raise PublishError("Catalog validation failed: " + ", ".join(errors[:10]))

    source_commit = catalog.get("sourceCommit")
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        raise PublishError("Catalog source commit is not a 7 to 64 character hexadecimal value.")
    if catalog.get("generatorVersion") in (None, ""):
        raise PublishError("Catalog generator version is missing.")

    uncompressed = hashlib.sha256(catalog_bytes).hexdigest()
    if digest_text != f"{uncompressed}  windows-catalog.json\n":
        raise PublishError("Uncompressed catalog checksum does not match the catalog file.")
    try:
        decoded = gzip.decompress(gzip_bytes)
    except (OSError, EOFError) as error:
        raise PublishError("Compressed catalog is not a valid gzip file.") from error
    if decoded != catalog_bytes:
        raise PublishError("Compressed catalog does not expand to the catalog file.")
    if not gzip_bytes.startswith(b"\x1f\x8b"):
        raise PublishError("Compressed catalog is not a valid gzip file.")
    compressed = hashlib.sha256(gzip_bytes).hexdigest()

    installer_count = sum(len(item["installers"]) for item in applications)
    if report.get("catalogSha256") != uncompressed:
        raise PublishError("Generation report checksum does not match the catalog file.")
    if report.get("applicationCount") != len(applications) or report.get("installerCount") != installer_count:
        raise PublishError("Generation report counts do not match the catalog file.")
    generated_at = report.get("generatedAtUtc")
    if not isinstance(generated_at, str) or _TIMESTAMP.fullmatch(generated_at) is None:
        raise PublishError("Generation report timestamp is missing.")
    if report.get("source") != catalog.get("source") or report.get("sourceCommit") != source_commit:
        raise PublishError("Generation report source does not match the catalog file.")

    _reject_count_reduction(len(applications), previous_metadata, allow_count_reduction)

    metadata = {
        "applicationCount": len(applications),
        "catalogSchemaVersion": catalog["schemaVersion"],
        "compressedSha256": compressed,
        "generatedAtUtc": generated_at,
        "generatorVersion": catalog["generatorVersion"],
        "installerCount": installer_count,
        "uncompressedSha256": uncompressed,
        "upstreamCommit": source_commit,
        "upstreamRepository": SOURCE_NAME,
    }
    if catalog.get("source") != SOURCE_NAME:
        raise PublishError("Catalog source is not microsoft/winget-pkgs.")

    _write_pages(pages_dir, gzip_bytes, compressed, metadata)
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a generated Windows catalog and write the Pages files.")
    parser.add_argument("--generated", required=True, type=Path, help="Directory containing the generator output.")
    parser.add_argument("--pages", required=True, type=Path, help="Directory that will contain windows/.")
    parser.add_argument("--previous-metadata", type=Path, help="Last published catalog-metadata.json.")
    parser.add_argument(
        "--allow-count-reduction",
        action="store_true",
        help=f"Publish even when the application count falls below {SEVERE_REDUCTION_PERCENT} percent of the published count.",
    )
    args = parser.parse_args(argv)
    try:
        metadata = publish_catalog(args.generated, args.pages, args.previous_metadata, args.allow_count_reduction)
    except (OSError, PublishError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        f"applications={metadata['applicationCount']} "
        f"installers={metadata['installerCount']} "
        f"compressedSha256={metadata['compressedSha256']}"
    )
    return 0


def _reject_count_reduction(application_count: int, previous_metadata: Path | None, allow_count_reduction: bool) -> None:
    if previous_metadata is None:
        return
    if not previous_metadata.is_file():
        raise PublishError("Previous catalog metadata was not found.")
    previous = _read_json(previous_metadata)
    previous_count = previous.get("applicationCount")
    if not isinstance(previous_count, int) or previous_count < 1:
        raise PublishError("Previous catalog metadata does not contain an application count.")
    if application_count * 100 < previous_count * SEVERE_REDUCTION_PERCENT and not allow_count_reduction:
        raise PublishError(
            f"Application count fell from {previous_count} to {application_count}. "
            f"A drop below {SEVERE_REDUCTION_PERCENT} percent of the published count requires explicit approval."
        )


def _write_pages(pages_dir: Path, gzip_bytes: bytes, compressed: str, metadata: dict[str, Any]) -> None:
    pages_dir.mkdir(parents=True, exist_ok=True)
    staging = pages_dir.parent / f".{pages_dir.name}.windows-staging"
    destination = pages_dir / "windows"
    if staging.exists():
        shutil.rmtree(staging)
    windows = staging / "windows"
    windows.mkdir(parents=True)
    moved = False
    try:
        (windows / "catalog.json.gz").write_bytes(gzip_bytes)
        (windows / "catalog.sha256").write_text(f"{compressed}  catalog.json.gz\n", encoding="utf-8", newline="\n")
        (windows / "catalog-metadata.json").write_bytes(_canonical_json(metadata))
        _verify_pages(windows, gzip_bytes, compressed, metadata)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(windows), str(destination))
        moved = True
        _verify_pages(destination, gzip_bytes, compressed, metadata)
    except Exception:
        if moved:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _verify_pages(windows: Path, gzip_bytes: bytes, compressed: str, metadata: dict[str, Any]) -> None:
    names = sorted(path.name for path in windows.iterdir())
    if names != ["catalog-metadata.json", "catalog.json.gz", "catalog.sha256"]:
        raise PublishError("Pages directory does not contain only the catalog files.")
    written_gzip = (windows / "catalog.json.gz").read_bytes()
    written_compressed = hashlib.sha256(written_gzip).hexdigest()
    written_digest = (windows / "catalog.sha256").read_text(encoding="utf-8")
    written_metadata = _read_json(windows / "catalog-metadata.json")
    if written_gzip != gzip_bytes or written_compressed != compressed:
        raise PublishError("Written compressed catalog checksum does not match.")
    if written_digest != f"{compressed}  catalog.json.gz\n":
        raise PublishError("Written catalog checksum file does not match.")
    if written_metadata != metadata:
        raise PublishError("Written catalog metadata does not match the generated files.")
    if hashlib.sha256(gzip.decompress(written_gzip)).hexdigest() != metadata["uncompressedSha256"]:
        raise PublishError("Written compressed catalog does not match the uncompressed checksum.")


def _read_catalog(payload: bytes) -> dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf") or not payload.endswith(b"}\n"):
        raise PublishError("Catalog JSON is truncated.")
    try:
        text = payload.decode("utf-8")
        catalog = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublishError("Catalog JSON is truncated.") from error
    if not isinstance(catalog, dict):
        raise PublishError("Catalog JSON is truncated.")
    return catalog


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublishError(f"{path.name} is not valid JSON.") from error
    if not isinstance(value, dict):
        raise PublishError(f"{path.name} is not valid JSON.")
    return value


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise PublishError(f"{path.name} was not found.") from error


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
