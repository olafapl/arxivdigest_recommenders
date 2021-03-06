import asyncio
import time
import numpy as np
import numpy.typing as npt
from urllib.parse import urlparse
from typing import Optional, List, Tuple, Any, Sequence, TypeVar, Iterator


T = TypeVar("T")


def extract_s2_id(user: dict) -> Optional[str]:
    """Extract S2 ID from a user's Semantic Scholar profile link.

    :param user: User.
    :return: User S2 author ID.
    """
    s2_id = str(urlparse(user["semantic_scholar_profile"]).path).split("/")[-1]
    return s2_id if len(s2_id) > 0 else None


def pad_shortest(a: npt.ArrayLike, b: npt.ArrayLike, pad: Any = 0):
    """Pad the shortest of two arrays in order to make them the same length.

    :param a: Vector a.
    :param b: Vector b.
    :param pad: Padding object.
    :return: Padded vectors.
    """
    len_diff = len(a) - len(b)
    if len_diff > 0:
        b = np.pad(b, (0, len_diff), constant_values=pad)
    elif len_diff < 0:
        a = np.pad(a, (0, abs(len_diff)), constant_values=pad)
    return a, b


def padded_cosine_sim(a: npt.ArrayLike, b: npt.ArrayLike) -> float:
    """Find the cosine similarity between two vectors. The shortest vector is padded with zeros.

    :param a: Vector a.
    :param b: Vector b.
    :return: Cosine similarity.
    """
    if not np.count_nonzero(a) or not np.count_nonzero(b):
        return 0.0
    a, b = pad_shortest(a, b)
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))


async def gather(*args):
    """Wrapper around asyncio.gather that ignores and excludes exceptions."""
    results = await asyncio.gather(*args, return_exceptions=True)
    return [result for result in results if not isinstance(result, Exception)]


class AsyncRateLimiter:
    """Limits the amount of times a section of code is entered within a window of time."""

    def __init__(self, max_enters: int, window_size: int):
        """
        :param max_enters: Max number of enters inside a window.
        :param window_size: Window size in seconds.
        """
        self.max_enters = max_enters
        self.window_size = window_size
        self.enters = []
        self.lock = None

    async def __aenter__(self):
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            if len(self.enters) == self.max_enters:
                time_to_new_window = max(
                    self.enters[0] + self.window_size - time.monotonic(), 0
                )
                await asyncio.sleep(time_to_new_window)
                self.enters.clear()
            self.enters.append(time.monotonic())

    async def __aexit__(self, *err):
        pass


def chunks(seq: Sequence[T], chunk_size: int) -> Iterator[Sequence[T]]:
    """Divide a sequence into chunks.

    :param seq: Sequence:
    :param chunk_size: Chunk size.
    """
    for i in range(0, len(seq), chunk_size):
        yield seq[i : i + chunk_size]
