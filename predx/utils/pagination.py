from __future__ import annotations
from typing import Callable, Generator, Any, Optional, Tuple


def cursor_paginate(
    fetch_fn: Callable[[Optional[str]], Tuple[list, Optional[str]]],
    max_items: Optional[int] = None,
) -> Generator[Any, None, None]:
    """
    Generic cursor-based pagination generator.

    fetch_fn(cursor) -> (items, next_cursor)
    Stops when next_cursor is falsy or max_items is reached.

    Example:
        def _fetch(cursor):
            data = client._request("GET", "/markets", params={"cursor": cursor, "limit": 200})
            return data["markets"], data.get("cursor")

        for market in cursor_paginate(_fetch):
            process(market)
    """
    cursor = None
    yielded = 0

    while True:
        items, next_cursor = fetch_fn(cursor)
        for item in items:
            if max_items is not None and yielded >= max_items:
                return
            yield item
            yielded += 1

        if not next_cursor:
            break
        cursor = next_cursor


def offset_paginate(
    fetch_fn: Callable[[int], Tuple[list, int]],
    page_size: int = 100,
    max_items: Optional[int] = None,
) -> Generator[Any, None, None]:
    """
    Generic offset-based pagination generator.

    fetch_fn(offset) -> (items, total_or_has_more)
    Returns empty items list to signal end of results.

    Example:
        def _fetch(offset):
            resp = client.get("/markets", params={"offset": offset, "limit": 100})
            return resp.json(), int(resp.headers.get("X-Total-Count", 0))

        for market in offset_paginate(_fetch):
            process(market)
    """
    offset = 0
    yielded = 0

    while True:
        items, total = fetch_fn(offset)
        if not items:
            break
        for item in items:
            if max_items is not None and yielded >= max_items:
                return
            yield item
            yielded += 1
        offset += len(items)
        # Stop if we've consumed all items or total is known and reached
        if total and offset >= total:
            break
