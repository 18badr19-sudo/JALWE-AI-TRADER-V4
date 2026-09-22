from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TradeSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(str, Enum):
    BUY = "BUY"
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
    BULL = "BULL"
    BEAR = "BEAR"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    RISK_OFF = "RISK_OFF"
    UNKNOWN = "UNKNOWN"


class StrategyName(str, Enum):
    PRE_BREAKOUT = "PRE_BREAKOUT"
    MOMENTUM_BREAKOUT = "MOMENTUM_BREAKOUT"
    PANIC_BOUNCE = "PANIC_BOUNCE"


@dataclass
class MarketSnapshot:
    symbol: str
    timestamp: datetime

    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None

    volume: Optional[float] = None
    dollar_volume: Optional[float] = None
    rvol: Optional[float] = None

    vwap: Optional[float] = None
    atr: Optional[float] = None

    spread_pct: Optional[float] = None

    data_is_stale: bool = False

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureSnapshot:
    symbol: str
    timestamp: datetime

    price: float

    rvol: Optional[float] = None
    volume_acceleration: Optional[float] = None
    dollar_volume: Optional[float] = None

    vwap_distance_pct: Optional[float] = None
    atr: Optional[float] = None
    compression: Optional[float] = None
    distance_to_resistance_pct: Optional[float] = None

    spread_pct: Optional[float] = None
    liquidity_imbalance: Optional[float] = None

    news_score: Optional[float] = None
    options_flow_score: Optional[float] = None

    market_regime: MarketRegime = MarketRegime.UNKNOWN

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeSignal:
    symbol: str
    strategy: StrategyName
    action: SignalAction

    score: float
    ai_probability: Optional[float]

    entry_price: float
    stop_price: float

    target_1: float
    target_2: float
    target_3: float

    reason: str

    signal_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    created_at: datetime = field(
        default_factory=utc_now
    )

    features: Optional[FeatureSnapshot] = None


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


@dataclass
class BrokerOrder:
    symbol: str
    side: TradeSide
    quantity: int

    order_id: Optional[str] = None

    status: OrderStatus = OrderStatus.NEW

    requested_price: Optional[float] = None
    filled_price: Optional[float] = None
    filled_quantity: int = 0

    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None

    metadata: dict[str, Any] = field(default_factory=dict)


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
        default_factory=lambda: str(uuid4())
    )

    signal_id: Optional[str] = None
    broker_order_id: Optional[str] = None

    status: TradeStatus = TradeStatus.PENDING

    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None

    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0

    max_favorable_excursion: Optional[float] = None
    max_adverse_excursion: Optional[float] = None

    metadata: dict[str, Any] = field(default_factory=dict)
