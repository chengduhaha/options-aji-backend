"""Tune third-party library logging for production readability.

Yahoo Finance often returns 404 for ``quoteSummary`` / fundamentals from some regions or IPs;
``yfinance`` logs those as ERROR even when price/options endpoints still work. Optional
filters drop only known-harmless lines. Disable filtering with env
``SUPPRESS_NOISY_PROVIDER_LOGS=false`` (see :class:`~app.config.Settings`).
"""

from __future__ import annotations

import logging


def apply_noise_filters(*, enabled: bool) -> None:
    if not enabled:
        return

    class _ThirdPartyNoiseFilter(logging.Filter):
        """Drop expected noise from yfinance (Yahoo API) and discord (text-only bots)."""

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            name = record.name

            if name.startswith("yfinance"):
                if "HTTP Error 404" in msg and "quoteSummary" in msg:
                    return False
                if "No fundamentals data found" in msg:
                    return False
                if "No earnings dates found" in msg:
                    return False
                if "symbol may be delisted" in msg:
                    return False

            if name.startswith("discord"):
                if "PyNaCl is not installed" in msg:
                    return False
                if "davey is not installed" in msg:
                    return False

            return True

    logging.getLogger().addFilter(_ThirdPartyNoiseFilter())
