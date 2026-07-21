# Homebrew promotion

This directory is a publication template; it does not create or mutate the external `berylliumsec/homebrew-tap` repository.

After a signed GitHub Release has passed an upgrade cycle, replace the three `@...@` placeholders in `nebula.rb.in` using the version and SHA-256 values from that immutable release. Commit the result as `Casks/nebula.rb` in the tap. Both managed DMGs omit the in-app updater. The cask exposes the desktop executable as `nebula` and the bundled administration CLI as `nebula-core` without modifying the application bundle.

Validate before promotion:

```console
brew audit --cask --strict nebula
brew install --cask ./Casks/nebula.rb
nebula-core doctor --json
nebula --self-test
```
