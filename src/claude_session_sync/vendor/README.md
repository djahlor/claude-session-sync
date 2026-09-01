# Bundled storage sources

This directory contains the small native dependency set used to build the
`layoutdb` helper locally during installation.

- `leveldb` is LevelDB 1.20 as distributed with `classic-level` 3.0.0. Its BSD
  license is in `leveldb/LICENSE`.
- `snappy` is the Snappy source distributed with `classic-level` 3.0.0. Its
  license notice is in `snappy/COPYING`.

Only source and header files needed by the helper are included. No downloaded
executable is installed.
