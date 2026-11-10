"""Package-repository index generation (spec §7.2).

The index is a pure function of the configured products' release assets:
``config`` reads ``packages.toml``, ``retention`` chooses the releases the
index publishes and drops retired packages, and ``collect`` downloads and
verifies only those releases.
"""
