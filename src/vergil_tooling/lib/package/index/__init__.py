"""Package-repository index generation (spec §7.2).

The index is a pure function of the configured products' release assets:
``config`` reads ``packages.toml``, ``collect`` downloads and verifies the
packaged stable releases, and ``retention`` chooses what the index keeps.
"""
