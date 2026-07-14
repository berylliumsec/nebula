# Signed APT promotion

This directory prepares a static, signed APT repository; it does not create or push `berylliumsec/nebula-apt`.

The promotion pipeline must download the immutable managed DEB and its checksum from a published GitHub Release, verify the checksum, and then run:

```console
./publish-deb.sh Nebula-VERSION-linux-x86_64.deb PUBLIC_REPOSITORY_ROOT GPG_KEY_ID
```

Publish `PUBLIC_REPOSITORY_ROOT` at `https://berylliumsec.github.io/nebula-apt`. Keep the signing key in a protected release environment and publish only its exported `nebula-archive-keyring.asc`. The DEB owns `/usr/bin/nebula`, which launches the bundled desktop, and `/usr/bin/nebula-core`, which provides administration and diagnostics. The managed desktop build never invokes the Tauri updater.
