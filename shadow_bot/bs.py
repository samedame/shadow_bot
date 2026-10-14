"""Black-Scholes helpers, standard library only.

bs_put is a line-for-line port of the study's strategies.bs_put (no dividends, flat vol); the C6 MODEL path
must match it. The normal CDF uses erfc, which agrees with scipy.stats.norm.cdf to ~1e-16 relative.
"""
from __future__ import annotations

import math

_SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


def bs_put(S0: float, K: float, T: float, r: float, sig: float):
    """-> (price, delta) of a European put; delta = N(d1) - 1. Expired or zero-vol: intrinsic and -1/0."""
    if T <= 0 or sig <= 0:
        return max(K - S0, 0.0), (-1.0 if K > S0 else 0.0)
    d1 = (math.log(S0 / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    price = K * math.exp(-r * T) * norm_cdf(-d2) - S0 * norm_cdf(-d1)
    return float(price), float(norm_cdf(d1) - 1.0)


def bs_call(S0: float, K: float, T: float, r: float, sig: float, q: float = 0.0):
    """-> (price, delta) of a European call (continuous dividend yield q)."""
    if T <= 0 or sig <= 0:
        return max(S0 - K, 0.0), (1.0 if S0 > K else 0.0)
    d1 = (math.log(S0 / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    price = S0 * math.exp(-q * T) * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)
    return float(price), float(math.exp(-q * T) * norm_cdf(d1))


def bs_delta(cp: int, S0: float, K: float, T: float, r: float, sig: float, q: float = 0.0):
    """Delta from an implied volatility, or None when it cannot be computed (zero/NaN vol, expired, bad inputs).
    Used only as the fallback when the feed carries no delta, and for the 'alt selection' diagnostic."""
    try:
        if not (S0 > 0 and K > 0 and T > 0 and sig and sig > 0 and not math.isnan(sig)):
            return None
        d1 = (math.log(S0 / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    disc = math.exp(-q * T)
    return disc * norm_cdf(d1) if cp > 0 else disc * (norm_cdf(d1) - 1.0)
