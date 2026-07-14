# Migrating from Nebula 2 to Nebula 3

Nebula 3 installs alongside Nebula 2 and does not modify a Nebula 2 engagement
during import. Keep the original engagement directory until the imported
Project and its evidence have been verified.

## Before upgrading Nebula 3

1. Quit the Nebula desktop and stop any manually started Core process.
2. Back up the Nebula 3 application-data directory, including `core/nebula.db`,
   `core/artifacts`, and `core/workspaces` when present.
3. Install the signed native update and open Nebula normally.

Core applies additive database migrations before serving the desktop. A first
launch against a truly empty database creates one renameable **Scratch
Project**. An upgraded or imported database does not receive an extra Scratch
Project, and intentionally deleting Scratch does not cause it to return.

Administrators can apply and validate migrations without opening the desktop:

```console
nebula-core migrate
nebula-core doctor --json
```

Use `--data-dir PATH` with both commands when validating a non-default or copied
data directory. Never run two Core processes against the same local data
directory while migrating it.

## Import a Nebula 2 engagement

Quit Nebula 2 first so its files cannot change during import, then run:

```console
nebula-core import-2x "/path/to/nebula-2-engagement"
```

The importer checksums the source before and after the operation, writes new
Nebula 3 records and content-addressed artifacts, and reports the new Project
ID. Symbolic links are recorded but not followed. If the source changes or the
import fails, database records are not committed and newly copied blobs are
cleaned up.

An external Chroma directory is skipped by default because it crosses the
selected engagement boundary. Import it only after reviewing that path:

```console
nebula-core import-2x "/path/to/nebula-2-engagement" \
  --allow-external-knowledge
```

Model credentials and provider secrets are not imported. Configure an
assistant separately in **Settings → Setup**; terminal use remains available
without a model.

## Verify the result

Open Nebula, select the imported Project, and verify its scope, assets, notes,
findings, evidence, and source metadata. Preserve the original Nebula 2 folder
until that review is complete. For an additional integrity check, export the
imported Project to a new path:

```console
nebula-core export PROJECT_ID imported-project.nebula.zip
```

The export is a portable, integrity-manifested record; it is not currently a
full application-data restore mechanism.

## Recovery

If a Nebula 3 application-data migration fails, leave the failed directory
unchanged for diagnosis, restore the pre-upgrade copy to a different directory,
and run `nebula-core doctor --data-dir RESTORED_PATH --json`. Do not copy only
the SQLite file while omitting its artifact and workspace directories.
