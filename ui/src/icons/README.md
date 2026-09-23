# Nebula UI icons

The generated registry replaces production Lucide glyphs with the curated Phosphor 2.1.1 mappings from `nebula-icon-pack.zip`. It intentionally does not replace Nebula's N monogram, favicon, or operating-system app icons.

Regenerate with `node scripts/import_icon_pack.mjs <extracted-pack-directory>`. The importer verifies the pack manifest before writing code. Phosphor is MIT licensed; see `LICENSE-Phosphor.txt`.
