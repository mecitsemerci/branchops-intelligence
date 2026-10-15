"""Delivery outcomes signalled by outbox handlers (ticketing, notifications) to the drain loop."""

from __future__ import annotations


class RetryLaterError(Exception):
    """Prerequisite not met yet (e.g. the ticket was not created yet): retry without consuming an attempt."""

    def __init__(self, reason: str, delay_seconds: float = 30.0) -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds


class PermanentDeliveryError(Exception):
    """The item can never be delivered (bad configuration, rejected payload): dead-letter immediately."""
