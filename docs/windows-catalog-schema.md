# Windows catalog schema

The published catalog is schema version 1. The desktop application reads the decompressed JSON. It does not read `catalog-metadata.json` in order to accept the catalog.

## Catalog document

`windows/catalog.json.gz` decompresses to one UTF-8 JSON object. The generator writes that JSON with sorted keys, two-space indentation, and a trailing line feed. The document does not contain a generation timestamp.

| Field | Required | Meaning |
| --- | --- | --- |
| `schemaVersion` | yes | Number `1`. |
| `generatorVersion` | yes | Generator version string. The current generator writes `"1"`. |
| `source` | yes | `microsoft/winget-pkgs`. |
| `sourceCommit` | yes | Upstream commit, 7 to 64 hexadecimal characters. |
| `applications` | yes | Array of application records, ordered by `id`. |

An application record contains:

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Package identifier. Identifiers are unique. |
| `name` | yes | Display name from the default locale. |
| `version` | yes | Selected manifest version, kept as text. |
| `installers` | yes | Non-empty array of installer records. |
| `publisher` | no | Publisher name. |
| `description` | no | Short or long description. |
| `homepage` | no | HTTP or HTTPS package URL. |
| `channel` | no | Manifest channel, when the manifest has one. |
| `locale` | no | Default-locale fields. |
| `locales` | no | Additional locale records. |
| `additionalMetadata` | no | Remaining manifest values that are safe to store as data. |

An installer record contains `architecture` and `url`. The URL is HTTPS. A space or a signature query is preserved. Optional fields include installer type, scope, locale, SHA256, signature SHA256, product code, package family name, switches, nested installer type, nested files, minimum OS version, release date, dependencies, and additional metadata. Missing optional values are omitted rather than written as null.

Installer records are ordered by architecture, installer type, scope, locale, URL, and SHA256. Nested paths are relative manifest paths. A path that climbs out of the archive or names a drive is not published.

The catalog does not contain credentials, Entra tokens, or installer file bytes.

## Metadata document

`windows/catalog-metadata.json` is calculated from the generated catalog, its gzip file, and the generation report:

| Field | Source |
| --- | --- |
| `catalogSchemaVersion` | `schemaVersion` in the catalog JSON. |
| `upstreamRepository` | `microsoft/winget-pkgs`. |
| `upstreamCommit` | `sourceCommit` in the catalog JSON. |
| `generatorVersion` | `generatorVersion` in the catalog JSON. |
| `applicationCount` | Number of application records. |
| `installerCount` | Number of installer records. |
| `generatedAtUtc` | Timestamp already stored in the generation report. |
| `compressedSha256` | SHA256 of `catalog.json.gz`. |
| `uncompressedSha256` | SHA256 of the decompressed catalog JSON. |

`windows/catalog.sha256` repeats the compressed checksum in the form `<hex>  catalog.json.gz`.

## File consistency

The three published files are uploaded in one Pages artifact. A client should still treat `catalog.json.gz` as the only required file. Pages does not give a client a transactional read across separate URLs, so metadata from one deployment and a catalog from another deployment must not be combined.
