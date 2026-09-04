from .base import (  # noqa: F401
    PaymentProvider,
    ProviderError,
    ProviderRejected,
    ProviderResult,
    ProviderRetriable,
    ProviderUnknownState,
)
from .mock_razorpay import Fail, MockRazorpay  # noqa: F401


def build_provider(mode: str = "mock", *, timeout: float | None = None):
    """Factory used by the API. `mock` keeps the demo offline and deterministic.

    `timeout` comes from KERNEL_PROVIDER_TIMEOUT_S. It must be threaded through here,
    or the configured value is silently ignored and REST calls always use the
    adapter's own default.
    """
    if mode == "mock":
        return MockRazorpay()
    # `rest` is the name used in .env.example and the docs; the others are aliases
    # kept because they read naturally in shell history.
    if mode in ("rest", "razorpay", "test", "live_test"):
        from .razorpay_rest import RazorpayRestClient

        if timeout is None:
            return RazorpayRestClient()
        return RazorpayRestClient(timeout=timeout)
    raise ValueError(f"unknown provider mode {mode!r}")
