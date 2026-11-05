from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from winget_catalog.generate import generate, validate_catalog
from winget_catalog.version import compare

SHA_A = "a" * 64
SHA_B = "b" * 64
SIGNED_URL = "https://downloads.example/setup.exe?sig=eyJhbGciOiJIUzI1NiJ9.payload.signature"


def test_version_ordering_matches_winget_rules():
    assert compare("1", "2") < 0
    assert compare("1.2.0", "1.10.0") < 0
    assert compare("13.9.8", "14.0") < 0
    assert compare("0.0.1-alpha", "0.0.1-beta") < 0
    assert compare("0.0.1-alpha", "0.0.1") < 0
    assert compare("22.0.0-rc", "22.0.0") < 0
    assert compare("1.0", "1.0.0") == 0
    assert compare("1.0.1", "1.0.1-alpha") > 0


def test_valid_package_selects_numeric_latest_and_preserves_installers(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.2.0", installer_type="msi")
    _version(root, "Example.App", "1.10.0", installer_type="wix", second_architecture=True)
    _version(root, "Example.App", "1.9.0-beta", installer_type="exe")

    result = generate(root, "abc123")
    application = result.catalog["applications"][0]

    assert validate_catalog(result.catalog) == []
    assert application["id"] == "Example.App"
    assert application["version"] == "1.10.0"
    assert application["name"] == "Example App"
    assert application["publisher"] == "Example Publisher"
    assert [item["architecture"] for item in application["installers"]] == ["x64", "x86"]
    assert {item["installerType"] for item in application["installers"]} == {"wix"}
    assert result.catalog["sourceCommit"] == "abc123"
    assert "generatedAtUtc" not in result.catalog


def test_missing_optional_fields_stay_absent(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", publisher=None, description=None, homepage=None, sha=None)

    application = generate(root, "abc123").catalog["applications"][0]

    assert "publisher" not in application
    assert "description" not in application
    assert "homepage" not in application
    assert "sha256" not in application["installers"][0]


def test_installer_override_scope_and_type(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(
        root,
        "Example.App",
        "1.0.0",
        installer_type="wix",
        scope="machine",
        extra_installer="""
- Architecture: x86
  InstallerType: msi
  InstallerUrl: https://downloads.example/setup-x86.msi
  InstallerSha256: {sha}
""".format(sha=SHA_B),
    )

    installers = {item["architecture"]: item for item in generate(root, "abc123").catalog["applications"][0]["installers"]}

    assert installers["x64"]["installerType"] == "wix"
    assert installers["x64"]["scope"] == "machine"
    assert installers["x86"]["installerType"] == "msi"
    assert installers["x86"]["scope"] == "machine"


def test_locale_selection_keeps_default_locale_and_preserves_others(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", extra_locale=True)

    application = generate(root, "abc123").catalog["applications"][0]

    assert application["name"] == "Example App"
    assert application["locale"]["locale"] == "en-US"
    assert application["locales"][0]["locale"] == "de-DE"
    assert application["locales"][0]["packageName"] == "Beispiel"


def test_windows_nested_paths_and_spaces_in_https_urls_are_preserved(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(
        root,
        "Example.App",
        "1.0.0",
        installer_type="zip",
        url="https://downloads.example/KeePass 1.x/setup.exe",
        nested=True,
        nested_path=r"app\Example.exe",
    )

    installer = generate(root, "abc123").catalog["applications"][0]["installers"][0]

    assert installer["url"] == "https://downloads.example/KeePass 1.x/setup.exe"
    assert installer["nestedInstallerFiles"][0]["relativeFilePath"] == r"app\Example.exe"


def test_current_directory_nested_paths_are_preserved(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", installer_type="zip", nested=True, nested_path=r".\app\Example.exe")

    installer = generate(root, "abc123").catalog["applications"][0]["installers"][0]

    assert installer["nestedInstallerFiles"][0]["relativeFilePath"] == r".\app\Example.exe"


def test_parent_and_drive_nested_paths_are_rejected(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.Parent", "1.0.0", installer_type="zip", nested=True, nested_path=r"..\Example.exe")
    _version(root, "Example.Drive", "1.0.0", installer_type="zip", nested=True, nested_path=r"C:\Example.exe")

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert {item["reason"] for item in result.report["skipped"]} == {"invalid-installer"}


def test_nested_installers_and_switches_are_data(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(
        root,
        "Example.App",
        "1.0.0",
        installer_type="zip",
        switches={"Custom": "cmd.exe /c echo hi"},
        nested=True,
    )

    installer = generate(root, "abc123").catalog["applications"][0]["installers"][0]

    assert installer["installerType"] == "zip"
    assert installer["nestedInstallerType"] == "portable"
    assert installer["nestedInstallerFiles"][0]["relativeFilePath"] == "app/Example.exe"
    assert installer["switches"]["Custom"] == "cmd.exe /c echo hi"


def test_invalid_yaml_is_reported_without_executing_tags(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    version = _package_dir(root, "Example.App") / "1.0.0"
    version.mkdir(parents=True)
    marker = tmp_path / "pwned"
    (version / "Example.App.yaml").write_text(
        f"!!python/object/apply:pathlib.Path.write_text [{marker.as_posix()!r}, x]\n",
        encoding="utf-8",
    )

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert result.report["skipped"][0]["reason"] == "invalid-yaml"
    assert not marker.exists()


def test_invalid_sha256_drops_only_that_installer(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", extra_installer=f"""
- Architecture: x86
  InstallerUrl: https://downloads.example/setup-x86.msi
  InstallerSha256: not-a-hash
""", sha=SHA_A)

    application = generate(root, "abc123").catalog["applications"][0]

    assert [item["architecture"] for item in application["installers"]] == ["x64"]
    assert application["installers"][0]["sha256"] == SHA_A


def test_signed_vendor_url_is_preserved(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", url=SIGNED_URL)

    assert generate(root, "abc123").catalog["applications"][0]["installers"][0]["url"] == SIGNED_URL


def test_local_and_http_installer_urls_are_rejected(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", url="http://downloads.example/setup.exe")
    _version(root, "Example.Local", "1.0.0", url=r"file:///C:/setup.exe")

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert {item["reason"] for item in result.report["skipped"]} == {"invalid-installer"}


def test_missing_installer_manifest_skips_package(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    directory = _package_dir(root, "Example.App") / "1.0.0"
    directory.mkdir(parents=True)
    (directory / "Example.App.yaml").write_text(_version_yaml("Example.App", "1.0.0"), encoding="utf-8")
    (directory / "Example.App.locale.en-US.yaml").write_text(_locale_yaml("Example.App", "1.0.0"), encoding="utf-8")

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert result.report["skipped"][0]["reason"] == "missing-installer"


def test_duplicate_package_identifiers_are_not_merged(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0")
    other = root / "manifests" / "o" / "Other" / "App" / "2.0.0"
    other.mkdir(parents=True)
    _write_version_files(other, "Example.App", "2.0.0")

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert result.report["skipped"][0]["reason"] == "duplicate-package-id"


def test_ambiguous_versions_are_skipped(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0")
    _version(root, "Example.App", "1.0.0")

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert result.report["skipped"][0]["reason"] == "ambiguous-version"


def test_unquoted_numeric_version_keeps_trailing_zero(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.10")

    assert generate(root, "abc123").catalog["applications"][0]["version"] == "1.10"


def test_output_is_deterministic(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0")
    _version(root, "Example.Other", "2.0.0")

    first = generate(root, "abc123")
    second = generate(root, "abc123")

    assert first.catalog_bytes == second.catalog_bytes
    assert validate_catalog(first.catalog) == []


def test_empty_repository(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    (root / "manifests").mkdir(parents=True)

    result = generate(root, "abc123")

    assert result.catalog["applications"] == []
    assert result.report["applicationCount"] == 0
    assert validate_catalog(result.catalog) == []


def test_large_synthetic_catalog_stays_ordered(tmp_path: Path):
    root = tmp_path / "winget-pkgs"
    for index in range(200):
        _version(root, f"Example.App{index:03d}", "1.0.0")

    applications = generate(root, "abc123").catalog["applications"]

    assert len(applications) == 200
    assert [item["id"] for item in applications] == sorted(item["id"] for item in applications)


def test_generation_does_not_download_installers_or_execute_manifests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_connect(*args, **kwargs):
        raise AssertionError("installer download attempted")

    def fail_process(*args, **kwargs):
        raise AssertionError("manifest command executed")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)
    monkeypatch.setattr(subprocess, "run", fail_process)
    root = tmp_path / "winget-pkgs"
    _version(root, "Example.App", "1.0.0", url=SIGNED_URL, switches={"Custom": "cmd.exe /c calc.exe"})

    application = generate(root, "abc123").catalog["applications"][0]

    assert application["installers"][0]["url"] == SIGNED_URL
    assert application["installers"][0]["switches"]["Custom"] == "cmd.exe /c calc.exe"


def _version(
    root: Path,
    identifier: str,
    version: str,
    *,
    installer_type: str = "exe",
    scope: str | None = None,
    publisher: str | None = "Example Publisher",
    description: str | None = "Example description",
    homepage: str | None = "https://example.invalid",
    sha: str | None = SHA_A,
    url: str = "https://downloads.example/setup.exe",
    second_architecture: bool = False,
    extra_installer: str = "",
    extra_locale: bool = False,
    switches: dict[str, str] | None = None,
    nested: bool = False,
    nested_path: str = "app/Example.exe",
) -> None:
    directory = _package_dir(root, identifier) / version
    directory.mkdir(parents=True)
    _write_version_files(
        directory,
        identifier,
        version,
        installer_type=installer_type,
        scope=scope,
        publisher=publisher,
        description=description,
        homepage=homepage,
        sha=sha,
        url=url,
        second_architecture=second_architecture,
        extra_installer=extra_installer,
        extra_locale=extra_locale,
        switches=switches,
        nested=nested,
        nested_path=nested_path,
    )


def _package_dir(root: Path, identifier: str) -> Path:
    publisher, name = identifier.split(".", 1)
    return root / "manifests" / publisher[0].lower() / publisher / name


def _write_version_files(directory: Path, identifier: str, version: str, **options) -> None:
    (directory / f"{identifier}.yaml").write_text(_version_yaml(identifier, version), encoding="utf-8")
    (directory / f"{identifier}.installer.yaml").write_text(_installer_yaml(identifier, version, **options), encoding="utf-8")
    (directory / f"{identifier}.locale.en-US.yaml").write_text(
        _locale_yaml(
            identifier,
            version,
            publisher=options.get("publisher", "Example Publisher"),
            description=options.get("description", "Example description"),
            homepage=options.get("homepage", "https://example.invalid"),
        ),
        encoding="utf-8",
    )
    if options.get("extra_locale"):
        (directory / f"{identifier}.locale.de-DE.yaml").write_text(
            _locale_yaml(identifier, version, locale="de-DE", name="Beispiel", manifest_type="locale"),
            encoding="utf-8",
        )


def _version_yaml(identifier: str, version: str) -> str:
    return f"""PackageIdentifier: {identifier}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.6.0
"""


def _locale_yaml(
    identifier: str,
    version: str,
    *,
    locale: str = "en-US",
    name: str = "Example App",
    publisher: str | None = "Example Publisher",
    description: str | None = "Example description",
    homepage: str | None = "https://example.invalid",
    manifest_type: str = "defaultLocale",
) -> str:
    lines = [
        f"PackageIdentifier: {identifier}",
        f"PackageVersion: {version}",
        f"PackageLocale: {locale}",
        f"PackageName: {name}",
    ]
    if publisher:
        lines.append(f"Publisher: {publisher}")
    if description:
        lines.append(f"ShortDescription: {description}")
    if homepage:
        lines.append(f"PackageUrl: {homepage}")
    lines.append(f"ManifestType: {manifest_type}")
    lines.append("ManifestVersion: 1.6.0")
    return "\n".join(lines) + "\n"


def _installer_yaml(
    identifier: str,
    version: str,
    *,
    installer_type: str = "exe",
    scope: str | None = None,
    sha: str | None = SHA_A,
    url: str = "https://downloads.example/setup.exe",
    second_architecture: bool = False,
    extra_installer: str = "",
    switches: dict[str, str] | None = None,
    nested: bool = False,
    nested_path: str = "app/Example.exe",
    **_ignored,
) -> str:
    lines = [
        f"PackageIdentifier: {identifier}",
        f"PackageVersion: {version}",
        f"InstallerType: {installer_type}",
    ]
    if scope:
        lines.append(f"Scope: {scope}")
    if switches:
        lines.append("InstallerSwitches:")
        for key, value in switches.items():
            lines.append(f"  {key}: {value}")
    if nested:
        lines.append("NestedInstallerType: portable")
        lines.append("NestedInstallerFiles:")
        lines.append(f"  - RelativeFilePath: {nested_path}")
        lines.append("    PortableCommandAlias: example")
    lines.append("Installers:")
    lines.append("- Architecture: x64")
    lines.append(f"  InstallerUrl: {url}")
    if sha:
        lines.append(f"  InstallerSha256: {sha}")
    if second_architecture:
        lines.append("- Architecture: x86")
        lines.append("  InstallerUrl: https://downloads.example/setup-x86.exe")
        lines.append(f"  InstallerSha256: {SHA_B}")
    if extra_installer:
        lines.append(extra_installer.strip("\n"))
    lines.append("ManifestType: installer")
    lines.append("ManifestVersion: 1.6.0")
    return "\n".join(lines) + "\n"
