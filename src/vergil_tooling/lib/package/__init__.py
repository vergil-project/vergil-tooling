"""Binary OS packaging (epic vergil-project/.github#356).

Configuration errors in ``[package]`` are ``ConfigError`` raised from
``lib/config.py``; every other failure in this package is :class:`PackageError`.
"""


class PackageError(Exception):
    """A packaging failure. Always fatal to the command that raised it."""
