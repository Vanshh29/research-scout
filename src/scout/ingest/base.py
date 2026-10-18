"""The `Source` protocol: adding a new source means adding one module that implements it."""

from typing import Protocol

import httpx

from scout.models import Item


class Source(Protocol):
    name: str

    def fetch(self, client: httpx.Client) -> list[Item]:
        """Fetch and normalize new items. Raise on failure; the caller isolates each source."""
        ...
