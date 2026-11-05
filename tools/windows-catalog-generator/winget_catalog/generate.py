"""Build a deterministic Windows catalog from a local winget-pkgs checkout."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from winget_catalog import GENERATOR_VERSION, SOURCE_NAME
from winget_catalog.manifest import ManifestError, VersionIdentity, build_application, read_identity
from winget_catalog.version import compare
from winget_catalog.yaml_load import load_manifest


@dataclass
class GenerationResult:
    catalog: dict[str, Any]
    report: dict[str, Any]
    catalog_bytes: bytes


@dataclass
class _PackageVersions:
    directories: set[Path] = field(default_factory=set)
    versions: list[VersionIdentity] = field(default_factory=list)
    failures: list[tuple[Path, str, str]] = field(default_factory=list)


def generate(source: Path, source_commit: str | None = None) -> GenerationResult:
    root = source.resolve()
    manifests = root / "manifests" if (root / "manifests").is_dir() else root
    if not manifests.is_dir():
        raise FileNotFoundError(f"Manifest directory does not exist: {manifests}")

    commit = source_commit or _git_commit(root)
    discovered = 0
    load_manifest.parsed_files = 0
    packages: dict[str, _PackageVersions] = defaultdict(_PackageVersions)
    loose_failures: list[dict[str, str]] = []

    for version_dir in _version_directories(manifests):
        yaml_files = _yaml_files(version_dir)
        discovered += len(yaml_files)
        package_dir = version_dir.parent
        try:
            identity = read_identity(version_dir, package_dir)
        except ManifestError as error:
            loose_failures.append(_skip("", version_dir, error.reason, error.detail))
            continue
        except Exception:
            loose_failures.append(_skip("", version_dir, "invalid-yaml", "Manifest YAML is invalid."))
            continue
        bucket = packages[identity.identifier]
        bucket.directories.add(package_dir)
        bucket.versions.append(identity)

    applications: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = list(loose_failures)
    for identifier in sorted(packages):
        bucket = packages[identifier]
        if len(bucket.directories) > 1:
            skipped.append(_skip(identifier, next(iter(bucket.directories)), "duplicate-package-id", "Package identifier appears in more than one directory."))
            continue
        chosen, ambiguous = _choose_latest(bucket.versions)
        if ambiguous:
            skipped.append(_skip(identifier, bucket.versions[0].version_dir, "ambiguous-version", "Versions cannot be ordered: " + ", ".join(sorted({item.version for item in ambiguous}))))
            continue
        if chosen is None:
            continue
        newer_failure = _newer_unreadable(chosen, loose_failures)
        if newer_failure is not None:
            skipped.append(_skip(identifier, Path(newer_failure), "unreadable-newer-version", "A newer version directory could not be read."))
            continue
        try:
            application = build_application(chosen)
        except ManifestError as error:
            skipped.append(_skip(identifier, chosen.version_dir, error.reason, error.detail))
            continue
        except Exception:
            skipped.append(_skip(identifier, chosen.version_dir, "invalid-yaml", "Manifest YAML is invalid."))
            continue
        rejected = application.pop("rejectedInstallers", [])
        for detail in rejected:
            skipped.append(_skip(identifier, chosen.version_dir, "rejected-installer", str(detail)))
        applications.append(application)

    applications.sort(key=lambda item: item["id"])
    catalog = {
        "applications": applications,
        "generatorVersion": GENERATOR_VERSION,
        "schemaVersion": 1,
        "source": SOURCE_NAME,
        "sourceCommit": commit,
    }
    catalog_bytes = _canonical_json(catalog)
    catalog_sha = hashlib.sha256(catalog_bytes).hexdigest()
    installer_count = sum(len(item["installers"]) for item in applications)
    report = {
        "applicationCount": len(applications),
        "catalogBytes": len(catalog_bytes),
        "catalogGzipBytes": len(_gzip(catalog_bytes)),
        "catalogSha256": catalog_sha,
        "generatedAtUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatorVersion": GENERATOR_VERSION,
        "installerCount": installer_count,
        "manifestsDiscovered": discovered,
        "manifestsParsed": load_manifest.parsed_files,
        "packageDirectories": len({path for bucket in packages.values() for path in bucket.directories}),
        "skipped": sorted(skipped, key=lambda item: (item["id"], item["reason"], item["path"])),
        "skippedCount": len(skipped),
        "source": SOURCE_NAME,
        "sourceCommit": commit,
        "versionDirectories": sum(len(bucket.versions) for bucket in packages.values()) + len(loose_failures),
    }
    return GenerationResult(catalog, report, catalog_bytes)


def write_output(result: GenerationResult, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    catalog_path = output / "windows-catalog.json"
    gzip_path = output / "windows-catalog.json.gz"
    report_path = output / "windows-catalog-report.json"
    digest_path = output / "windows-catalog.json.sha256"
    catalog_path.write_bytes(result.catalog_bytes)
    gzip_path.write_bytes(_gzip(result.catalog_bytes))
    report_path.write_bytes(_canonical_json(result.report))
    digest_path.write_text(f"{result.report['catalogSha256']}  windows-catalog.json\n", encoding="utf-8", newline="\n")


def validate_catalog(catalog: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if catalog.get("schemaVersion") != 1:
        errors.append("schemaVersion")
    if catalog.get("source") != SOURCE_NAME:
        errors.append("source")
    if not isinstance(catalog.get("sourceCommit"), str) or catalog["sourceCommit"] == "":
        errors.append("sourceCommit")
    applications = catalog.get("applications")
    if not isinstance(applications, list):
        return errors + ["applications"]
    identifiers = [item.get("id") for item in applications if isinstance(item, dict)]
    if identifiers != sorted(identifiers) or len(identifiers) != len(set(identifiers)):
        errors.append("application order")
    for application in applications:
        if not isinstance(application, dict):
            errors.append("application")
            continue
        if not application.get("id") or not application.get("name") or not application.get("version"):
            errors.append(f"{application.get('id', '')} required")
        installers = application.get("installers")
        if not isinstance(installers, list) or not installers:
            errors.append(f"{application.get('id', '')} installers")
            continue
        keys = [
            (
                item.get("architecture", ""),
                item.get("installerType", ""),
                item.get("scope", ""),
                item.get("locale", ""),
                item.get("url", ""),
                item.get("sha256", ""),
            )
            for item in installers
        ]
        if keys != sorted(keys):
            errors.append(f"{application['id']} installer order")
        for installer in installers:
            url = installer.get("url", "")
            if not isinstance(url, str) or not url.startswith("https://"):
                errors.append(f"{application['id']} url")
            sha = installer.get("sha256")
            if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
                errors.append(f"{application['id']} sha256")
    return errors


def _choose_latest(versions: list[VersionIdentity]) -> tuple[VersionIdentity | None, list[VersionIdentity]]:
    best: list[VersionIdentity] = []
    for identity in versions:
        if not best:
            best = [identity]
            continue
        order = compare(identity.version, best[0].version)
        if order > 0:
            best = [identity]
        elif order == 0:
            best.append(identity)
    if len(best) != 1:
        return None, best
    return best[0], []


def _newer_unreadable(chosen: VersionIdentity, failures: list[dict[str, str]]) -> str | None:
    for failure in failures:
        path = Path(failure["path"])
        if path.parent != chosen.version_dir.parent:
            continue
        if failure["reason"] not in {"invalid-yaml", "missing-version", "invalid-version", "conflicting-version"}:
            continue
        try:
            if compare(path.name, chosen.version) > 0:
                return failure["path"]
        except Exception:
            continue
    return None


def _version_directories(manifests: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirnames, filenames in os.walk(manifests):
        if any(name.casefold().endswith((".yaml", ".yml")) for name in filenames):
            found.append(Path(current))
            dirnames.clear()
    return sorted(found)


def _yaml_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"})


def _skip(identifier: str, path: Path, reason: str, detail: str) -> dict[str, str]:
    return {
        "detail": detail[:200],
        "id": identifier,
        "path": path.as_posix(),
        "reason": reason,
    }


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _gzip(payload: bytes) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as archive:
        archive.write(payload)
    return buffer.getvalue()


def _git_commit(source: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Source commit is required when the checkout has no Git HEAD.") from error
    commit = completed.stdout.strip()
    if not commit:
        raise RuntimeError("Source commit is required when the checkout has no Git HEAD.")
    return commit
