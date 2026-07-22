from datetime import datetime, timezone, timedelta

import pytest

from config.settings import Settings
from services.signal_consumer import validate_signal


def make_settings(max_price_deviation_pct: float = 0.5) -> Settings:
    return Settings(
        hl_account_address="0x0",
        hl_api_private_key="key",
        badgerbot_api_key="key",
        position_size_pct=0.1,
        position_size_usd=None,
        risk_pct=None,
        max_signal_age_seconds=60,
        max_price_deviation_pct=max_price_deviation_pct,
        telegram_bot_token="token",
        telegram_authorized_user_id=1,
        position_poll_interval_seconds=15,
        algorithms=["Ethereum Main"],
    )


def make_signal(price: float, tp_price: float, sl_price: float, age_seconds: float = 0) -> dict:
    dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "coin_symbol": "BTC",
        "price": str(price),
        "tp_price": str(tp_price),
        "sl_price": str(sl_price),
        "mode": "long",
        "dispatched_at": dispatched_at.isoformat(),
    }


class TestStaleSignal:
    def test_stale_signal_dropped(self):
        signal = make_signal(price=100, tp_price=110, sl_price=95, age_seconds=61)
        result = validate_signal(signal, mark_price=100, settings=make_settings())
        assert result is not None
        assert "stale" in result

    def test_fresh_signal_passes_age_check(self):
        signal = make_signal(price=100, tp_price=110, sl_price=95, age_seconds=30)
        result = validate_signal(signal, mark_price=100, settings=make_settings())
        assert result is None


class TestTpErosion:
    # LONG: entry=100, tp=110 → original TP% = 10%
    # threshold with factor 0.5 = 5%
    # remaining TP% must be >= 5% to pass

    def test_mark_at_signal_price_passes(self):
        # remaining TP% = 10% = original → well above threshold
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=100, settings=make_settings(0.5))
        assert result is None

    def test_mark_moved_toward_tp_but_within_threshold_passes(self):
        # mark=104, remaining TP% = abs((110-104)/104*100) = 5.77% > 5% → passes
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=104, settings=make_settings(0.5))
        assert result is None

    def test_mark_moved_toward_tp_beyond_threshold_dropped(self):
        # mark=106, remaining TP% = abs((110-106)/106*100) = 3.77% < 5% → dropped
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=106, settings=make_settings(0.5))
        assert result is not None
        assert "TP eroded" in result

    def test_mark_above_tp_dropped(self):
        # mark=112, remaining TP% = abs((110-112)/112*100) = 1.79% < 5% → dropped
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=112, settings=make_settings(0.5))
        assert result is not None

    def test_mark_below_signal_price_passes(self):
        # LONG: mark dipped slightly below entry → TP has more room, SL still has enough room.
        # mark=98.5: TP remaining 11.5 > 5 (pass), SL remaining 3.5 > 2.5 (pass).
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=98.5, settings=make_settings(0.5))
        assert result is None

    def test_short_signal_mark_moved_toward_tp(self):
        # SHORT: entry=100, tp=90 → original TP% = 10%, threshold = 5%
        # mark=96, remaining TP% = abs((90-96)/96*100) = 6.25% > 5% → passes
        signal = make_signal(price=100, tp_price=90, sl_price=105)
        result = validate_signal(signal, mark_price=96, settings=make_settings(0.5))
        assert result is None

    def test_short_signal_tp_eroded_dropped(self):
        # SHORT: entry=100, tp=90 → original TP% = 10%, threshold = 5%
        # mark=95, remaining TP% = abs((90-95)/95*100) = 5.26% > 5% → passes (just inside)
        # mark=94.5, remaining TP% = abs((90-94.5)/94.5*100) = 4.76% < 5% → dropped
        signal = make_signal(price=100, tp_price=90, sl_price=105)
        result = validate_signal(signal, mark_price=94.5, settings=make_settings(0.5))
        assert result is not None
        assert "TP eroded" in result

    def test_factor_1_0_requires_full_tp_remaining(self):
        # factor=1.0 means remaining TP must be >= 100% of original → any move toward TP drops it
        # mark=101 (tiny move toward TP on long): remaining TP% = abs((110-101)/101*100) = 8.91% < 10% → dropped
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=101, settings=make_settings(1.0))
        assert result is not None

    def test_factor_0_0_never_drops_on_tp_erosion(self):
        # factor=0.0: threshold is 0%, remaining TP can never be negative → always passes
        signal = make_signal(price=100, tp_price=110, sl_price=95)
        result = validate_signal(signal, mark_price=109, settings=make_settings(0.0))
        assert result is None
