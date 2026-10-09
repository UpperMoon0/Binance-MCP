"""Fail closed when a bounded read cannot satisfy advertised coverage."""
from .client import BinanceClientError


class Coverage:
    def __init__(self):
        self.total = None

    def complete(self, advertised, count: int, batch_size: int) -> bool:
        if advertised is not None:
            try:
                total = int(str(advertised))
            except (TypeError, ValueError):
                raise BinanceClientError("invalid pagination total; coverage incomplete") from None
            if total < 0 or (self.total is not None and total != self.total):
                raise BinanceClientError("pagination total changed; coverage incomplete")
            self.total = total
        if self.total is not None:
            if count > self.total:
                raise BinanceClientError("pagination exceeds advertised total; coverage incomplete")
            if count == self.total:
                return True
            if not batch_size:
                raise BinanceClientError("pagination ended before advertised total; coverage incomplete")
            return False
        return batch_size < 100
