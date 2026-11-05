"""Normalize one winget-pkgs version directory into a catalog record."""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from winget_catalog.yaml_load import YamlDocumentError, load_manifest

MAX_STRING = 8_192
MAX_URL = 4_096
MAX_INSTALLERS = 200

_ID_PART = r"[^\.\s\\/:*?\"<>|\x00-\x1f]{1,32}"
PACKAGE_ID = re.compile(rf"^{_ID_PART}(?:\.{_ID_PART}){{1,7}}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

_STRUCTURAL_KEYS = {
    "PackageIdentifier",
    "PackageVersion",
    "Channel",
    "ManifestType",
    "ManifestVersion",
    "Installers",
}

_INSTALLER_FIELDS = {
    "architecture": "Architecture",
    "installerType": "InstallerType",
    "scope": "Scope",
    "locale": "InstallerLocale",
    "url": "InstallerUrl",
    "sha256": "InstallerSha256",
    "signatureSha256": "SignatureSha256",
    "productCode": "ProductCode",
    "packageFamilyName": "PackageFamilyName",
    "nestedInstallerType": "NestedInstallerType",
    "minimumOsVersion": "MinimumOSVersion",
    "releaseDate": "ReleaseDate",
}

_LOCALE_FIELDS = {
    "publisher": "Publisher",
    "publisherUrl": "PublisherUrl",
    "publisherSupportUrl": "PublisherSupportUrl",
    "privacyUrl": "PrivacyUrl",
    "author": "Author",
    "packageName": "PackageName",
    "homepage": "PackageUrl",
    "license": "License",
    "licenseUrl": "LicenseUrl",
    "copyright": "Copyright",
    "copyrightUrl": "CopyrightUrl",
    "shortDescription": "ShortDescription",
    "description": "Description",
    "moniker": "Moniker",
    "releaseNotesUrl": "ReleaseNotesUrl",
    "purchaseUrl": "PurchaseUrl",
    "installationNotes": "InstallationNotes",
}


class ManifestError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class VersionIdentity:
    package_dir: Path
    version_dir: Path
    identifier: str
    version: str
    channel: str


def read_identity(version_dir: Path, package_dir: Path) -> VersionIdentity:
    paths = _yaml_paths(version_dir)
    preferred = [path for path in paths if not _is_detail_manifest(path)]
    documents = _load_paths(preferred or paths)
    identity = _identity_from_documents(documents, version_dir, package_dir)
    if identity is not None or not preferred or len(preferred) == len(paths):
        if identity is None:
            raise ManifestError("missing-version", "Version manifest is missing.")
        return identity
    documents = _load_paths(paths)
    identity = _identity_from_documents(documents, version_dir, package_dir)
    if identity is None:
        raise ManifestError("missing-version", "Version manifest is missing.")
    return identity


def _identity_from_documents(documents: list[dict[str, Any]], version_dir: Path, package_dir: Path) -> VersionIdentity | None:
    preferred = []
    fallback = []
    for document in documents:
        manifest_type = _text(document.get("ManifestType")).casefold()
        if manifest_type in {"version", "singleton"} or (
            "PackageVersion" in document and "Installers" not in document and "PackageLocale" not in document
        ):
            preferred.append(document)
        elif "PackageVersion" in document:
            fallback.append(document)

    chosen_group = preferred or fallback
    if not chosen_group:
        return None

    identifiers = {_text(item.get("PackageIdentifier")) for item in chosen_group}
    versions = {_text(item.get("PackageVersion")) for item in chosen_group}
    if len(identifiers) != 1 or len(versions) != 1:
        raise ManifestError("conflicting-version", "Version files do not agree.")

    identifier = identifiers.pop()
    version = versions.pop()
    if not PACKAGE_ID.fullmatch(identifier):
        raise ManifestError("invalid-package-id", "Package identifier is invalid.")
    if version == "" or len(version) > 128:
        raise ManifestError("invalid-version", "Package version is invalid.")

    channels = {_text(item.get("Channel")) for item in chosen_group}
    channel = channels.pop() if len(channels) == 1 else ""
    if len(channels) > 1:
        raise ManifestError("conflicting-version", "Version channels do not agree.")
    return VersionIdentity(package_dir, version_dir, identifier, version, channel)


def _is_detail_manifest(path: Path) -> bool:
    name = path.name.casefold()
    return ".installer." in name or ".locale." in name


def _yaml_paths(version_dir: Path) -> list[Path]:
    return sorted(path for path in version_dir.iterdir() if path.is_file() and path.suffix.casefold() in {".yaml", ".yml"})


def build_application(identity: VersionIdentity) -> dict[str, Any]:
    documents = _load_documents(identity.version_dir)
    singleton = next(
        (item for item in documents if _text(item.get("ManifestType")).casefold() == "singleton"),
        None,
    )
    if singleton is not None:
        locale = singleton
        installer_document = singleton
        default_locale = _text(singleton.get("PackageLocale"))
        extra_locales: list[dict[str, Any]] = []
    else:
        version_document = next(
            (item for item in documents if _text(item.get("ManifestType")).casefold() == "version"),
            None,
        )
        default_locale = _text((version_document or {}).get("DefaultLocale"))
        installer_documents = [
            item for item in documents
            if _text(item.get("ManifestType")).casefold() == "installer" or "Installers" in item
        ]
        if len(installer_documents) != 1:
            raise ManifestError(
                "missing-installer" if not installer_documents else "conflicting-version",
                "Installer manifest is missing." if not installer_documents else "More than one installer manifest is present.",
            )
        installer_document = installer_documents[0]
        locale_documents = [
            item for item in documents
            if _text(item.get("ManifestType")).casefold() in {"locale", "defaultlocale"} or "PackageLocale" in item
        ]
        if installer_document is None:
            raise ManifestError("missing-installer", "Installer manifest is missing.")
        if default_locale == "":
            raise ManifestError("missing-locale", "Default locale is missing.")

        default_documents = [
            item for item in locale_documents
            if _text(item.get("ManifestType")).casefold() == "defaultlocale"
            or _text(item.get("PackageLocale")).casefold() == default_locale.casefold()
        ]
        if len(default_documents) != 1:
            raise ManifestError("missing-locale", "Default locale manifest is missing or duplicated.")
        locale = default_documents[0]
        extra_locales = [
            item for item in locale_documents
            if item is not locale and _text(item.get("PackageLocale")).casefold() != default_locale.casefold()
        ]

    name = _limited(_text(locale.get("PackageName")))
    if name == "":
        raise ManifestError("missing-name", "Package name is missing.")

    installers, rejected = _installers(installer_document)
    if not installers:
        detail = rejected[0] if rejected else "No installers were valid."
        raise ManifestError("invalid-installer", detail)

    application: dict[str, Any] = {
        "id": identity.identifier,
        "name": name,
        "version": identity.version,
        "installers": installers,
    }
    _copy_text(application, "publisher", locale.get("Publisher"))
    description = _text(locale.get("ShortDescription")) or _text(locale.get("Description"))
    _copy_text(application, "description", description)
    homepage = _text(locale.get("PackageUrl"))
    if _metadata_url(homepage):
        application["homepage"] = homepage
    if identity.channel:
        application["channel"] = identity.channel

    locale_record = _locale_record(locale, default_locale)
    if locale_record:
        application["locale"] = locale_record
    other_locales = [record for item in extra_locales if (record := _locale_record(item, _text(item.get("PackageLocale"))))]
    if other_locales:
        application["locales"] = sorted(other_locales, key=lambda item: item.get("locale", ""))

    additional = _additional(installer_document, set(_INSTALLER_FIELDS.values()) | _STRUCTURAL_KEYS | {"InstallerSwitches", "NestedInstallerFiles", "Dependencies"})
    if additional:
        application["additionalMetadata"] = additional
    if rejected:
        application["rejectedInstallers"] = rejected
    return application


def _installers(document: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    raw_installers = document.get("Installers")
    if not isinstance(raw_installers, list) or not raw_installers:
        return [], ["Installer list is missing."]
    if len(raw_installers) > MAX_INSTALLERS:
        return [], ["Installer list is too large."]

    defaults = {
        key: value
        for key, value in document.items()
        if key not in _STRUCTURAL_KEYS and key != "Installers"
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []
    for entry in raw_installers:
        if not isinstance(entry, dict):
            rejected.append("Installer entry is invalid.")
            continue
        try:
            accepted.append(_one_installer(defaults, entry))
        except ManifestError as error:
            rejected.append(error.detail or error.reason)

    accepted.sort(key=_installer_key)
    return accepted, rejected


def _one_installer(defaults: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in entry.items():
        if key == "InstallerSwitches" and isinstance(merged.get(key), dict) and isinstance(value, dict):
            switches = dict(merged[key])
            switches.update(value)
            merged[key] = switches
        else:
            merged[key] = value

    url = _text(merged.get("InstallerUrl"))
    if not _https_url(url):
        raise ManifestError("invalid-installer", "Installer URL is not an accepted HTTPS address.")
    architecture = _text(merged.get("Architecture"))
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", architecture):
        raise ManifestError("invalid-installer", "Installer architecture is missing.")

    record: dict[str, Any] = {"architecture": architecture, "url": url}
    for output_key, source_key in _INSTALLER_FIELDS.items():
        if output_key in {"architecture", "url"}:
            continue
        if source_key not in merged or merged[source_key] in (None, ""):
            continue
        value = merged[source_key]
        if source_key == "InstallerSha256":
            sha = _text(value)
            if not _SHA256.fullmatch(sha):
                raise ManifestError("invalid-installer", "Installer SHA256 is invalid.")
            record[output_key] = sha.lower()
            continue
        if source_key == "SignatureSha256":
            sha = _text(value)
            if not _SHA256.fullmatch(sha):
                raise ManifestError("invalid-installer", "Signature SHA256 is invalid.")
            record[output_key] = sha.lower()
            continue
        if isinstance(value, str):
            record[output_key] = _limited(value)
        elif _json_safe(value):
            record[output_key] = value

    switches = merged.get("InstallerSwitches")
    if switches is not None:
        if not isinstance(switches, dict) or any(not isinstance(item, str) for item in switches.values()):
            raise ManifestError("invalid-installer", "Installer switches are invalid.")
        record["switches"] = {str(key): _limited(value) for key, value in sorted(switches.items())}

    nested = merged.get("NestedInstallerFiles")
    if nested is not None:
        record["nestedInstallerFiles"] = _nested_files(nested)
    dependencies = merged.get("Dependencies")
    if dependencies is not None:
        if not _json_safe(dependencies):
            raise ManifestError("invalid-installer", "Installer dependencies are invalid.")
        record["dependencies"] = dependencies

    consumed = set(_INSTALLER_FIELDS.values()) | {"InstallerSwitches", "NestedInstallerFiles", "Dependencies", "InstallerUrl", "Architecture"}
    additional = _additional(merged, consumed | _STRUCTURAL_KEYS)
    if additional:
        record["additionalMetadata"] = additional
    return record


def _nested_files(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_INSTALLERS:
        raise ManifestError("invalid-installer", "Nested installer files are invalid.")
    files: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ManifestError("invalid-installer", "Nested installer files are invalid.")
        relative = _text(entry.get("RelativeFilePath"))
        if not _safe_relative_path(relative):
            raise ManifestError("invalid-installer", "Nested installer path is not a relative file path.")
        item = {"relativeFilePath": relative}
        alias = _text(entry.get("PortableCommandAlias"))
        if alias:
            item["portableCommandAlias"] = _limited(alias)
        files.append(item)
    return files


def _locale_record(document: dict[str, Any], locale: str) -> dict[str, Any]:
    record: dict[str, Any] = {}
    if locale:
        record["locale"] = locale
    for output_key, source_key in _LOCALE_FIELDS.items():
        if source_key not in document or document[source_key] in (None, ""):
            continue
        value = document[source_key]
        if isinstance(value, str):
            if output_key == "homepage" or output_key.endswith("Url"):
                if not _metadata_url(value):
                    continue
            record[output_key] = _limited(value)
        elif source_key == "Tags" and isinstance(value, list) and all(isinstance(item, str) for item in value):
            record["tags"] = [_limited(item) for item in value]
        elif _json_safe(value):
            record[output_key] = value
    tags = document.get("Tags")
    if isinstance(tags, list) and all(isinstance(item, str) for item in tags):
        record["tags"] = [_limited(item) for item in tags]
    return record


def _additional(document: dict[str, Any], consumed: set[str]) -> dict[str, Any]:
    additional: dict[str, Any] = {}
    for key, value in document.items():
        if key in consumed or value in (None, ""):
            continue
        if _json_safe(value):
            additional[key] = value
    return additional


def _load_documents(version_dir: Path) -> list[dict[str, Any]]:
    return _load_paths(_yaml_paths(version_dir))


def _load_paths(paths: list[Path]) -> list[dict[str, Any]]:
    if not paths:
        raise ManifestError("missing-version", "Version manifest is missing.")
    documents: list[dict[str, Any]] = []
    for path in paths:
        if path.stat().st_size > 1_048_576:
            raise ManifestError("invalid-yaml", "Manifest is too large.")
        try:
            text = path.read_text(encoding="utf-8-sig")
            documents.append(load_manifest(text))
            load_manifest.parsed_files += 1
        except (OSError, UnicodeError, YamlDocumentError) as error:
            raise ManifestError("invalid-yaml", "Manifest YAML is invalid.") from error
    return documents


def _installer_key(installer: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(installer.get("architecture", "")),
        str(installer.get("installerType", "")),
        str(installer.get("scope", "")),
        str(installer.get("locale", "")),
        str(installer.get("url", "")),
        str(installer.get("sha256", "")),
    )


def _metadata_url(value: str) -> bool:
    if value.startswith("http://"):
        return _https_url("https://" + value[len("http://"):])
    return _https_url(value)


def _https_url(value: str) -> bool:
    if value == "" or len(value) > MAX_URL or "\\" in value:
        return False
    if any(ord(character) < 32 for character in value):
        return False
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname is None or parts.username or parts.password:
        return False
    host = parts.hostname.casefold().rstrip(".")
    if " " in host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return False
    return True


def _safe_relative_path(value: str) -> bool:
    if value == "" or len(value) > 512 or ":" in value:
        return False
    if value.startswith(("/", "\\")):
        return False
    parts = value.replace("\\", "/").split("/")
    meaningful = [part for part in parts if part not in {"", "."}]
    return bool(meaningful) and ".." not in parts


def _json_safe(value: Any, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if isinstance(value, str):
            return len(value) <= MAX_STRING
        return True
    if isinstance(value, list):
        return len(value) <= 500 and all(_json_safe(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return (
            len(value) <= 200
            and all(isinstance(key, str) and len(key) <= 128 and _json_safe(item, depth + 1) for key, item in value.items())
        )
    return False


def _copy_text(target: dict[str, Any], key: str, value: Any) -> None:
    text = _limited(value) if isinstance(value, str) else ""
    if text:
        target[key] = text


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _limited(value: str) -> str:
    text = value.strip()
    if len(text) > MAX_STRING:
        raise ManifestError("invalid-manifest", "Manifest field is too large.")
    return text
