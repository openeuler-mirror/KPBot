"""Real memory backends; constructing an adapter does not load HTTP dependencies."""

from .openviking_http import OpenVikingBackend

__all__ = ["OpenVikingBackend"]
