"""Filesystem options: an AWS profile applies to ``s3://`` URLs only.

On the EC2 host the buckets are FUSE-mounted and volume-mounted into the
container, so plain local paths are used and no profile is needed. Locally,
pass ``--aws-profile`` to read and write ``s3://`` URLs.
"""

_state: dict[str, str | None] = {"profile": None}


def set_profile(profile: str | None) -> None:
    """Set the AWS profile used for ``s3://`` URLs.

    Parameters
    ----------
    profile : str or None
        Profile name from ``~/.aws``, or None for the default configuration.
    """
    _state["profile"] = profile


def build_storage_options(url: str) -> dict[str, str]:
    """Build the fsspec storage options for a URL.

    Parameters
    ----------
    url : str
        A local path, ``file://`` or ``s3://`` URL.

    Returns
    -------
    dict of str to str
        ``{"profile": ...}`` for ``s3://`` URLs when a profile is set, else
        empty.
    """
    profile = _state["profile"]
    if url.startswith("s3://") and profile:
        return {"profile": profile}
    return {}
