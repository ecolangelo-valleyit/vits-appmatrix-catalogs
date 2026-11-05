# VITS AppMatrix catalogs

This repository generates and publishes the Windows application catalog used by VITS AppMatrix.

The desktop application downloads one HTTPS document. It does not clone `microsoft/winget-pkgs`, and it does not use WinGet, PowerShell, the Microsoft Store, or an administrator account to read the catalog.

The intended public catalog address is:

`https://ecolangelo-valleyit.github.io/vits-appmatrix-catalogs/windows/catalog.json.gz`

That address is the planned GitHub Pages location. It is not a verified live URL until this repository's Pages site is enabled and a successful workflow deployment is reviewed.

## Upstream data

The catalog is built from the public manifest repository [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs). Package metadata and installer URLs come from those YAML manifests. This repository does not download installer files, execute manifest commands, or install packages.

Microsoft publishes the manifest repository. VITS AppMatrix publishes a derived JSON index.

## Generation

The generator is `tools/windows-catalog-generator/`. Python dependencies are pinned in `requirements.txt`.

From that directory, with a local checkout of `microsoft/winget-pkgs`:

```powershell
python -m pytest
python -m winget_catalog --source C:\path\to\winget-pkgs --output out --source-commit <commit> --require-applications
python ..\..\scripts\publish_windows_catalog.py --generated out --pages ..\..\site
```

Generation reads the checkout. It selects one latest version per package, keeps every accepted installer, and writes:

- `windows-catalog.json`
- `windows-catalog.json.gz` with gzip modification time 0
- `windows-catalog.json.sha256` for the uncompressed JSON
- `windows-catalog-report.json`, including the generation timestamp

The catalog JSON omits the generation timestamp so an unchanged upstream commit keeps a stable checksum.

## Schedule

GitHub Actions runs the workflow every Monday at 12:00 UTC (`0 12 * * 1`). The schedule is the seven-day publication cadence. It is not the desktop application's refresh timer.

A second run waits for the current run. It does not start another `winget-pkgs` generation, and it does not cancel a deployment that is already running.

## Manual regeneration

From the Actions tab, run **Publish Windows catalog**. The optional `winget_ref` input selects the `microsoft/winget-pkgs` ref. The default is `master`.

The optional `allow_count_reduction` input approves a publication whose application count is below 75 percent of the last published count. Scheduled runs cannot set that input. They fail closed.

## Public files

One Pages deployment contains:

| Path | Role |
| --- | --- |
| `windows/catalog.json.gz` | Primary catalog. This is the file the desktop application should request. |
| `windows/catalog.sha256` | SHA256 of `catalog.json.gz`. |
| `windows/catalog-metadata.json` | Counts, source commit, generator version, generation time, and both checksums. |

`catalog.sha256` is the checksum of the compressed file. The metadata document also records the SHA256 of the uncompressed JSON.

## Output schema

`docs/windows-catalog-schema.md` describes the catalog document and the metadata document.

The catalog uses schema version 1 and source `microsoft/winget-pkgs`. Applications are ordered by package identifier. Each application has an identifier, name, version, and at least one HTTPS installer. Null fields are omitted.

## Validation

The workflow tests the generator, generates the catalog, and then runs `scripts/publish_windows_catalog.py`. Publication is rejected when any of these are true:

- The JSON is truncated or is not valid UTF-8 JSON.
- The catalog contains no applications.
- A package identifier is duplicated.
- An installer collection is missing or is not a list of installer records.
- The schema, source, source commit, or generator version is not acceptable to the desktop reader.
- The uncompressed checksum, gzip contents, or generation report does not match the catalog file.
- The application count is below 75 percent of the last published count, and the manual approval input was not set.

Checksums and counts in `catalog-metadata.json` are calculated from the generated files. The timestamp is the timestamp already recorded in the generation report.

## Failure behavior

Validation runs before the Pages artifact is uploaded. The deploy job runs only after that upload. A failed generation, test, or validation does not call the Pages deploy action, so the last successful Pages deployment stays in place.

The workflow does not upload a partial directory. The publication script writes the three files only after validation and removes an incomplete destination if the final check fails.

GitHub Pages replaces a deployment as a unit, but it is not a transactional update from a client's point of view. A client that requests more than one file can observe those files from different deployments if a new deployment or a cache boundary falls between the requests. The compressed catalog is therefore the only file a client needs. The checksum and metadata files are companions from the same deployment, and the desktop application does not have to fetch them in order to read the catalog.

## Deployment setup

This repository does not enable GitHub Pages by itself. Before the workflow can deploy, an administrator needs to set all of the following:

- Actions are enabled.
- This workflow file is on the default branch, `main`.
- Settings → Pages → Build and deployment → Source is **GitHub Actions**.
- The workflow token can request `pages: read` for the preflight check, and the deploy job can request `pages: write` and `id-token: write`.
- No personal access token is added. `microsoft/winget-pkgs` is public, and the workflow uses `GITHUB_TOKEN`.

The first workflow step calls `actions/configure-pages` with enablement turned off. If Pages is not already set to GitHub Actions, that step fails and the workflow does not checkout `winget-pkgs` or deploy a site.

The generation job timeout is 180 minutes. Catalog generation itself is about 55 minutes after the upstream checkout is available. The checkout of `microsoft/winget-pkgs` is included in that job.

## Desktop integration

VITS AppMatrix reads the catalog with its HTTPS Windows catalog provider. The provider is not given this URL until a later reviewed step. The published JSON, after gzip decompression, is the document that provider already accepts.

The desktop HTTP client decompresses a response body that starts with the gzip bytes `1F 8B` even when the response has no `Content-Encoding` header. It also accepts a response that the HTTP stack already decompressed because `Content-Encoding` was `gzip`. A `.json.gz` file served as `application/gzip` does not require a desktop code change for decompression.

The catalog JSON does not contain the generation time. The desktop snapshot's refresh time is the local time of a successful download. The upstream generation time stays in `catalog-metadata.json`.

## Source attribution

Package manifests are from [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs), maintained by Microsoft. Installer files remain on the URLs published in those manifests. This repository stores the URLs and the accompanying manifest metadata. It does not store credentials, Entra tokens, or installer bytes.
