from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================
# ENUMS
# ============================================================

class TradeSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    WATCH = "WATCH"
    REJECT = "REJECT"


class TradeStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class OrderStatus(str, Enum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    PANIC = "PANIC"
    RISK_OFF = "RISK_OFF"
    UNKNOWN = "UNKNOWN"


class StrategyName(str, Enum):
    PRE_BREAKOUT = "PRE_BREAKOUT"
    MOMENTUM_BREAKOUT = "MOMENTUM_BREAKOUT"
    PANIC_BOUNCE = "PANIC_BOUNCE"
    LIQUIDITY_EXPANSION = "LIQUIDITY_EXPANSION"


# ============================================================
# MARKET DATA MODEL
# ============================================================

@dataclass
class MarketSnapshot:
    symbol: str
    timestamp: datetime
    price: float

    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None

    spread: Optional[float] = None
    spread_pct: Optional[float] = None

    volume: Optional[float] = None
    dollar_volume: Optional[float] = None

    rvol: Optional[float] = None
    vwap: Optional[float] = None
    atr: Optional[float] = None

    data_is_stale: bool = False
    data_quality_ok: bool = True

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# FEATURE MODEL
# ============================================================

@dataclass
class FeatureSnapshot:
    """
    Canonical feature model for JALWE V4.

    جميع المحركات تستخدم هذا التعريف:
    - Feature Engine
    - Opportunity Engine
    - AI Engine
    - Risk Engine
    - Learning Engine
    - Database
    """

    symbol: str

    timestamp: datetime = field(
        default_factory=utc_now
    )

    # --------------------------------------------------------
    # PRICE / RETURNS
    # --------------------------------------------------------

    price: Optional[float] = None

    return_1: Optional[float] = None
    return_5: Optional[float] = None

    # --------------------------------------------------------
    # VOLUME / LIQUIDITY
    # --------------------------------------------------------

    volume: Optional[float] = None
    avg_volume: Optional[float] = None

    rvol: Optional[float] = None

    dollar_volume: Optional[float] = None

    volume_acceleration: Optional[float] = None

    liquidity_score: Optional[float] = None
    liquidity_imbalance: Optional[float] = None

    # --------------------------------------------------------
    # VWAP
    # --------------------------------------------------------

    vwap: Optional[float] = None

    distance_from_vwap_pct: Optional[float] = None

    above_vwap: Optional[bool] = None

    # --------------------------------------------------------
    # VOLATILITY
    # --------------------------------------------------------

    atr: Optional[float] = None
    atr_pct: Optional[float] = None

    # --------------------------------------------------------
    # MOVING AVERAGES
    # --------------------------------------------------------

    ema_9: Optional[float] = None
    ema_20: Optional[float] = None

    sma_50: Optional[float] = None

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    rsi_14: Optional[float] = None

    momentum_score: Optional[float] = None

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    high_20: Optional[float] = None
    low_20: Optional[float] = None

    distance_to_high_20_pct: Optional[float] = None

    range_compression: Optional[float] = None

    # --------------------------------------------------------
    # NEWS / OPTIONS
    # --------------------------------------------------------

    news_score: Optional[float] = None

    options_flow_score: Optional[float] = None

    # --------------------------------------------------------
    # MARKET REGIME
    # --------------------------------------------------------

    market_regime: MarketRegime = (
        MarketRegime.UNKNOWN
    )

    # --------------------------------------------------------
    # DATA QUALITY
    # --------------------------------------------------------

    data_quality_ok: bool = False

    data_is_stale: bool = False

    # --------------------------------------------------------
    # EXTENSIONS
    # --------------------------------------------------------

    metadata: dict[str, Any] = field(
        default_factory=dict
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),

            "price": self.price,
            "return_1": self.return_1,
            "return_5": self.return_5,

            "volume": self.volume,
            "avg_volume": self.avg_volume,
            "rvol": self.rvol,
            "dollar_volume": self.dollar_volume,
            "volume_acceleration": (
                self.volume_acceleration
            ),

            "liquidity_score": (
                self.liquidity_score
            ),

            "liquidity_imbalance": (
                self.liquidity_imbalance
            ),

            "vwap": self.vwap,

            "distance_from_vwap_pct": (
                self.distance_from_vwap_pct
            ),

            "above_vwap": self.above_vwap,

            "atr": self.atr,
            "atr_pct": self.atr_pct,

            "ema_9": self.ema_9,
            "ema_20": self.ema_20,
            "sma_50": self.sma_50,

            "rsi_14": self.rsi_14,

            "momentum_score": (
                self.momentum_score
            ),

            "high_20": self.high_20,
            "low_20": self.low_20,

            "distance_to_high_20_pct": (
                self.distance_to_high_20_pct
            ),

            "range_compression": (
                self.range_compression
            ),

            "news_score": self.news_score,

            "options_flow_score": (
                self.options_flow_score
            ),

            "market_regime": (
                self.market_regime.value
            ),

            "data_quality_ok": (
                self.data_quality_ok
            ),

            "data_is_stale": (
                self.data_is_stale
            ),

            "metadata": self.metadata,
        }


# ============================================================
# SIGNAL MODEL
# ============================================================

@dataclass
class TradeSignal:
    symbol: str
    strategy: StrategyName
    action: SignalAction

    score: float

    entry_price: float
    stop_price: float

    target_1: float
    target_2: float
    target_3: float

    reason: str

    ai_probability: Optional[float] = None

    signal_id: str = field(
        default_factory=lambda: str(
            uuid4()
        )
    )

    created_at: datetime = field(
        default_factory=utc_now
    )

    features: Optional[
        FeatureSnapshot
    ] = None

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# RISK MODEL
# ============================================================

@dataclass
class RiskDecision:
    approved: bool
    reason: str

    quantity: int = 0

    risk_amount: float = 0.0
    risk_pct: float = 0.0

    position_value: float = 0.0

    stop_price: Optional[float] = None

    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# BROKER ORDER MODEL
# ============================================================

@dataclass
class BrokerOrder:
    symbol: str
    side: TradeSide
    quantity: int

    order_id: Optional[str] = None
    client_order_id: Optional[str] = None

    status: OrderStatus = (
        OrderStatus.NEW
    )

    requested_price: Optional[float] = None
    filled_price: Optional[float] = None

    filled_quantity: int = 0

    submitted_at: Optional[
        datetime
    ] = None

    filled_at: Optional[
        datetime
    ] = None

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# TRADE MODEL
# ============================================================

@dataclass
class Trade:
    symbol: str
    strategy: StrategyName

    quantity: int

    entry_price: float
    stop_price: float

    target_1: float
    target_2: float
    target_3: float

    trade_id: str = field(
        default_factory=lambda: str(
            uuid4()
        )
    )

    signal_id: Optional[str] = None

    broker_order_id: Optional[str] = None

    status: TradeStatus = (
        TradeStatus.PENDING
    )

    opened_at: Optional[
        datetime
    ] = None

    closed_at: Optional[
        datetime
    ] = None

    exit_price: Optional[float] = None

    exit_reason: Optional[str] = None

    realized_pnl: float = 0.0

    realized_pnl_pct: float = 0.0

    max_favorable_excursion: Optional[
        float
    ] = None

    max_adverse_excursion: Optional[
        float
    ] = None

    metadata: dict[str, Any] = field(
        default_factory=dict
    )