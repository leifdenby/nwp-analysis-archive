"""Fixed-size time blocks: the unit of the long-time-chunk archive B."""

import datetime

import numpy as np

# Monday; blocks are 28 days from here so a block index is a floor division.
EPOCH = datetime.datetime(2000, 1, 3, tzinfo=datetime.UTC)
BLOCK_LENGTH = datetime.timedelta(days=28)


def get_block_index(t_analysis: datetime.datetime) -> int:
    """Get the index of the 28-day block containing an analysis time.

    Parameters
    ----------
    t_analysis : datetime.datetime
        Timezone-aware analysis time.

    Returns
    -------
    int
        The block index (0 starts at ``EPOCH``).
    """
    return (t_analysis - EPOCH) // BLOCK_LENGTH


def get_cycles_per_block(interval: datetime.timedelta) -> int:
    """Get the number of analysis times in a block.

    Parameters
    ----------
    interval : datetime.timedelta
        Time between analysis times, e.g. 3 hours.

    Returns
    -------
    int
        e.g. 224 for 3-hourly, 112 for 6-hourly.
    """
    return BLOCK_LENGTH // interval


def get_block_times(
    block_index: int, interval: datetime.timedelta
) -> list[datetime.datetime]:
    """Get all analysis times a complete block holds.

    Parameters
    ----------
    block_index : int
        The block index.
    interval : datetime.timedelta
        Time between analysis times.

    Returns
    -------
    list of datetime.datetime
        Timezone-aware times, ascending.
    """
    start = EPOCH + block_index * BLOCK_LENGTH
    return [start + i * interval for i in range(get_cycles_per_block(interval))]


def build_block_grid(
    block_index: int, interval: datetime.timedelta
) -> np.ndarray:
    """Build the regular time axis of a block as ``datetime64[ns]``.

    Parameters
    ----------
    block_index : int
        The block index.
    interval : datetime.timedelta
        Time between analysis times.

    Returns
    -------
    numpy.ndarray
        Naive UTC ``datetime64[ns]`` values, ascending.
    """
    return np.array(
        [
            np.datetime64(t.replace(tzinfo=None), "ns")
            for t in get_block_times(block_index, interval)
        ]
    )
