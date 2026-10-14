"""Use standard-library APIs when available and backports on Python 3.9/3.10."""

import sys

if sys.version_info >= (3, 11):
    from asyncio import timeout

    import tomllib
else:
    import tomli as tomllib
    from async_timeout import timeout

__all__ = ["timeout", "tomllib"]
