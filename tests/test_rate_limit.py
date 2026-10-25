from app.core.rate_limit import SlidingWindowLimiter


def test_sliding_window_limiter_rejects_bursts_and_bounds_keys():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, max_keys=2)

    assert limiter.allow('client-a')
    assert limiter.allow('client-a')
    assert not limiter.allow('client-a')

    assert limiter.allow('client-b')
    assert limiter.allow('client-c')
    assert len(limiter._events) <= 2
