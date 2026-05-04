"""Black-Scholes-Merton option pricing and Greeks."""
import numpy as np
from scipy.stats import norm


def _d1_d2(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return 0.0, 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def price(S, K, T, r, sigma, option_type='put'):
    """Return BSM theoretical price. T in years."""
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if option_type == 'call':
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def delta(S, K, T, r, sigma, option_type='put'):
    """Return BSM delta."""
    d1, _ = _d1_d2(S, K, T, r, sigma)
    if option_type == 'call':
        return norm.cdf(d1)
    return norm.cdf(d1) - 1.0


def strike_for_delta(S, T, r, sigma, target_delta, option_type='put'):
    """Binary search for the strike that produces the target delta.

    target_delta should be positive (e.g. 0.25); sign is inferred from option_type.
    """
    target = -abs(target_delta) if option_type == 'put' else abs(target_delta)
    lo, hi = S * 0.40, S * 1.60
    for _ in range(80):
        mid = (lo + hi) / 2.0
        d = delta(S, mid, T, r, sigma, option_type)
        if option_type == 'put':
            # put delta is negative; target is e.g. -0.25
            # d > target means less negative (too OTM) → raise strike
            if d > target:
                lo = mid
            else:
                hi = mid
        else:
            # call delta positive; target e.g. 0.30
            # d > target means too ITM → raise strike (search upper half)
            if d > target:
                lo = mid
            else:
                hi = mid
        if abs(d - target) < 1e-4:
            break
    return round(mid, 2)
