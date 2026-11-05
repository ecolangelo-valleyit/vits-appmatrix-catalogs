from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from winget_catalog.generate import generate, write_output
from winget_catalog.publish import PublishError, publish_catalog

COMMIT = "bde9440299f9893e5a6dc230efa9d35591442623"
SHA = "a" * 64


def test_publish_writes_pages_files_from_the_generated_catalog(tmp_path: Path):
    generated = _generate(tmp_path)
    pages = tmp_path / "site"

    metadata = publish_catalog(generated, pages)

    windows = pages / "windows"
    assert sorted(path.name for path in windows.iterdir()) == [
        "catalog-metadata.json",
        "catalog.json.gz",
        "catalog.sha256",
    ]
    gzip_bytes = (windows / "catalog.json.gz").read_bytes()
    catalog_bytes = gzip.decompress(gzip_bytes)
    assert gzip_bytes.startswith(b"\x1f\x8b")
    assert hashlib.sha256(gzip_bytes).hexdigest() == metadata["compressedSha256"]
    assert hashlib.sha256(catalog_bytes).hexdigest() == metadata["uncompressedSha256"]
    assert (windows / "catalog.sha256").read_text(encoding="utf-8") == f"{metadata['compressedSha256']}  catalog.json.gz\n"
    assert metadata["catalogSchemaVersion"] == 1
    assert metadata["upstreamRepository"] == "microsoft/winget-pkgs"
    assert metadata["upstreamCommit"] == COMMIT
    assert metadata["generatorVersion"] == "1"
    assert metadata["applicationCount"] == 1
    assert metadata["installerCount"] == 1
    assert metadata["generatedAtUtc"].endswith("Z")
    assert "generatedAtUtc" not in json.loads(catalog_bytes)
    written = json.loads((windows / "catalog-metadata.json").read_bytes())
    assert written == metadata


def test_publish_is_deterministic_for_the_same_generated_files(tmp_path: Path):
    generated = _generate(tmp_path)
    first = tmp_path / "one"
    second = tmp_path / "two"

    publish_catalog(generated, first)
    publish_catalog(generated, second)

    for name in ("catalog.json.gz", "catalog.sha256", "catalog-metadata.json"):
        assert (first / "windows" / name).read_bytes() == (second / "windows" / name).read_bytes()


def test_publish_rejects_truncated_json_without_writing_pages_files(tmp_path: Path):
    generated = _generate(tmp_path)
    catalog = generated / "windows-catalog.json"
    catalog.write_bytes(catalog.read_bytes()[:40])

    with pytest.raises(PublishError, match="truncated"):
        publish_catalog(generated, tmp_path / "site")

    assert not (tmp_path / "site" / "windows").exists()


def test_publish_rejects_zero_applications(tmp_path: Path):
    root = tmp_path / "winget-pkgs" / "manifests"
    root.mkdir(parents=True)
    generated = tmp_path / "generated"
    write_output(generate(root, COMMIT), generated)

    with pytest.raises(PublishError, match="any applications"):
        publish_catalog(generated, tmp_path / "site")


def test_publish_rejects_duplicate_package_identifiers(tmp_path: Path):
    generated = _generate(tmp_path)
    _rewrite_catalog(generated, lambda catalog: catalog["applications"].append(dict(catalog["applications"][0])))

    with pytest.raises(PublishError, match="duplicate"):
        publish_catalog(generated, tmp_path / "site")


def test_publish_rejects_malformed_installer_collections(tmp_path: Path):
    generated = _generate(tmp_path)
    _rewrite_catalog(generated, lambda catalog: catalog["applications"][0].__setitem__("installers", {}))

    with pytest.raises(PublishError, match="installer"):
        publish_catalog(generated, tmp_path / "site")


def test_publish_rejects_a_checksum_mismatch(tmp_path: Path):
    generated = _generate(tmp_path)
    digest = generated / "windows-catalog.json.sha256"
    digest.write_text(("0" * 64) + "  windows-catalog.json\n", encoding="utf-8", newline="\n")

    with pytest.raises(PublishError, match="checksum"):
        publish_catalog(generated, tmp_path / "site")


def test_publish_rejects_a_damaged_gzip_file(tmp_path: Path):
    generated = _generate(tmp_path)
    gzip_path = generated / "windows-catalog.json.gz"
    payload = bytearray(gzip_path.read_bytes())
    payload[-1] ^= 0xFF
    gzip_path.write_bytes(payload)

    with pytest.raises(PublishError, match="gzip|Compressed"):
        publish_catalog(generated, tmp_path / "site")


def test_publish_rejects_a_severe_count_reduction_unless_approved(tmp_path: Path):
    generated = _generate(tmp_path)
    previous = tmp_path / "catalog-metadata.json"
    previous.write_text(json.dumps({"applicationCount": 4}) + "\n", encoding="utf-8")

    with pytest.raises(PublishError, match="explicit approval"):
        publish_catalog(generated, tmp_path / "site", previous)

    metadata = publish_catalog(generated, tmp_path / "approved", previous, allow_count_reduction=True)
    assert metadata["applicationCount"] == 1


def test_publish_allows_a_count_reduction_above_the_threshold(tmp_path: Path):
    generated = _generate(tmp_path)
    previous = tmp_path / "catalog-metadata.json"
    previous.write_text(json.dumps({"applicationCount": 1}) + "\n", encoding="utf-8")

    metadata = publish_catalog(generated, tmp_path / "site", previous)

    assert metadata["applicationCount"] == 1


def test_publish_rejects_a_commit_the_desktop_provider_cannot_read(tmp_path: Path):
    root = _source(tmp_path)
    generated = tmp_path / "generated"
    write_output(generate(root, "abc123"), generated)

    with pytest.raises(PublishError, match="source commit"):
        publish_catalog(generated, tmp_path / "site")


def _generate(tmp_path: Path) -> Path:
    generated = tmp_path / "generated"
    write_output(generate(_source(tmp_path), COMMIT), generated)
    return generated


def _source(tmp_path: Path) -> Path:
    root = tmp_path / "winget-pkgs"
    directory = root / "manifests" / "e" / "Example" / "App" / "1.0.0"
    directory.mkdir(parents=True)
    (directory / "Example.App.yaml").write_text(
        "PackageIdentifier: Example.App\nPackageVersion: 1.0.0\nDefaultLocale: en-US\nManifestType: version\nManifestVersion: 1.6.0\n",
        encoding="utf-8",
    )
    (directory / "Example.App.installer.yaml").write_text(
        "PackageIdentifier: Example.App\nPackageVersion: 1.0.0\nManifestType: installer\nManifestVersion: 1.6.0\n"
        "Installers:\n- Architecture: x64\n  InstallerUrl: https://downloads.example/setup.exe\n"
        f"  InstallerSha256: {SHA}\n",
        encoding="utf-8",
    )
    (directory / "Example.App.locale.en-US.yaml").write_text(
        "PackageIdentifier: Example.App\nPackageVersion: 1.0.0\nPackageLocale: en-US\nPackageName: Example App\n"
        "Publisher: Example Publisher\nShortDescription: Example description\nManifestType: defaultLocale\nManifestVersion: 1.6.0\n",
        encoding="utf-8",
    )
    return root


def _rewrite_catalog(generated: Path, mutate) -> None:
    catalog_path = generated / "windows-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    mutate(catalog)
    payload = (json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    catalog_path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (generated / "windows-catalog.json.sha256").write_text(f"{digest}  windows-catalog.json\n", encoding="utf-8", newline="\n")
    report_path = generated / "windows-catalog-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["catalogSha256"] = digest
    report["catalogBytes"] = len(payload)
    report_path.write_bytes((json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    from io import BytesIO

    buffer = BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as archive:
        archive.write(payload)
    (generated / "windows-catalog.json.gz").write_bytes(buffer.getvalue())
