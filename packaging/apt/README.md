# Signed APT support

`publish-deb.sh` is the low-level single-distribution helper retained for local
packaging tests. The production public repository source, channel promotion,
protected signing, upgrade tests, and Pages deployment are maintained in the
ready-to-copy scaffold at `packaging/repositories/nebula-apt`.

The production workflow downloads only managed DEBs from immutable published
Nebula releases, verifies release checksums and GitHub provenance attestations,
and signs metadata with a dedicated subkey from the external repository’s
protected `apt-release` environment. No private signing material belongs here.
