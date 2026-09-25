from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from core.config import (
    auto_paper_execution_ready,
    settings,
    validate_settings,
)
from core.database import database

from intelligence.decision_engine import (
    DecisionState,
    FinalTradeDecision,
    get_decision_engine,
)

from broker.alpaca_client import get_alpaca_client
from trading.execution_engine import get_execution_engine
from trading.reconciliation_engine import get_reconciliation_engine
from trading.recovery_engine import get_recovery_engine


logger = logging.getLogger(__name__)


# ============================================================
# ORCHESTRATOR STATES
# ============================================================

class PaperOrchestratorState(str, Enum):
    RECOVERY_BLOCKED = "RECOVERY_BLOCKED"

    REJECTED = "REJECTED"
    WATCHING = "WATCHING"

    READY_AUTO_DISABLED = "READY_AUTO_DISABLED"
    READY_BROKER_LOCKED = "READY_BROKER_LOCKED"
    READY_CONFIG_LOCKED = "READY_CONFIG_LOCKED"

    ENTRY_PREPARED = "ENTRY_PREPARED"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_PENDING = "ENTRY_PENDING"

    ENTRY_MANAGED = "ENTRY_MANAGED"

    ENTRY_TERMINAL_NO_FILL = "ENTRY_TERMINAL_NO_FILL"

    ENTRY_RECOVERY_REQUIRED = "ENTRY_RECOVERY_REQUIRED"

    ERROR = "ERROR"


# ============================================================
# RESULT
# ============================================================

@dataclass
class PaperOrchestratorResult:
    symbol: str
    state: PaperOrchestratorState

    decision: Optional[FinalTradeDecision] = None

    intent_id: Optional[str] = None
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    managed_trade_id: Optional[str] = None

    message: str = ""

    warnings: list[str] = field(
        default_factory=list
    )

    metadata: dict[str, Any] = field(
        default_factory=dict
    )


# ============================================================
# PAPER TRADE ORCHESTRATOR
# ============================================================

class PaperTradeOrchestrator:
    """
    JALWE AI TRADER V4
    Paper Trade Orchestrator V3.1

    PAPER TRADING ONLY.

    Safe lifecycle:

        Startup Recovery
            ↓
        SAFE TO TRADE
            ↓
        Decision Engine
            ↓
        READY_FOR_PAPER_EXECUTION
            ↓
        Config Safety Locks
            ↓
        Generate Intent ID
        Generate Client Order ID
            ↓
        Save EntryIntent FIRST
            ↓
        SQLite COMMIT
            ↓
        Submit PAPER order
        using SAME client_order_id
            ↓
        Save broker snapshot
            ↓
        Reconciliation
            ↓
        FILLED / PARTIAL / CANCEL / REJECT
            ↓
        Recovery Engine
            ↓
        ManagedTrade
            ↓
        Intent = MANAGED

    Execution requires:

        PAPER_TRADING = True
        ALLOW_LIVE_TRADING = False

        JALWE_AUTO_PAPER_EXECUTION = true
        JALWE_BROKER_SUBMISSION = true

        RecoveryEngine safe
        DecisionEngine ready
        Risk gate passed

    Constructor arguments can make execution MORE restrictive,
    but they cannot bypass disabled .env safety switches.
    """

    def __init__(
        self,
        auto_execution: Optional[bool] = None,
        broker_submission_enabled: Optional[bool] = None,
        entry_timeout_seconds: int = 30,
    ) -> None:

        # ----------------------------------------------------
        # Global config validation
        # ----------------------------------------------------

        validate_settings()

        configured_auto = bool(
            settings.AUTO_PAPER_EXECUTION
        )

        configured_broker = bool(
            settings.BROKER_SUBMISSION_ENABLED
        )

        # ----------------------------------------------------
        # Constructor cannot override disabled ENV locks
        # ----------------------------------------------------

        if auto_execution is None:
            self.auto_execution = configured_auto
        else:
            self.auto_execution = bool(
                auto_execution
                and configured_auto
            )

        if broker_submission_enabled is None:
            self.broker_submission_enabled = (
                configured_broker
            )
        else:
            self.broker_submission_enabled = bool(
                broker_submission_enabled
                and configured_broker
            )

        self.entry_timeout_seconds = int(
            entry_timeout_seconds
        )

        if self.entry_timeout_seconds <= 0:
            raise ValueError(
                "entry_timeout_seconds must be positive."
            )

        # ----------------------------------------------------
        # Engines
        # ----------------------------------------------------

        self.decision_engine = (
            get_decision_engine()
        )

        self.execution_engine = (
            get_execution_engine()
        )

        self.reconciliation_engine = (
            get_reconciliation_engine()
        )

        self.recovery_engine = (
            get_recovery_engine()
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _value(
        value: Any,
    ) -> Any:
        """
        Return Enum.value when available.
        """

        return getattr(
            value,
            "value",
            value,
        )

    @staticmethod
    def _activity_time(
        activity: dict[str, Any],
    ) -> str:
        for key in (
            "transaction_time",
            "at",
            "date",
            "timestamp",
            "created_at",
        ):
            value = str(
                activity.get(key)
                or ""
            ).strip()

            if value:
                return value

        return datetime.now(
            timezone.utc
        ).isoformat()

    @staticmethod
    def _activity_amount(
        activity: dict[str, Any],
    ) -> Optional[float]:
        for key in (
            "net_amount",
            "amount",
            "cash",
        ):
            value = activity.get(key)

            try:
                if value is not None:
                    return float(value)
            except (
                TypeError,
                ValueError,
            ):
                continue

        return None

    @staticmethod
    def _sync_strategy_wallet_adjustments() -> float:
        """
        Synchronize NEW Alpaca PAPER cash deposits/withdrawals.

        First run establishes a baseline at the current time so
        the existing Alpaca PAPER balance is NOT imported into
        JALWE's strategy wallet.
        """

        if settings.CAPITAL_MODE != "strategy_wallet":
            return 0.0

        now = datetime.now(
            timezone.utc
        )

        baseline_key = (
            "capital_flow_baseline_utc"
        )

        last_sync_key = (
            "capital_flow_last_sync_utc"
        )

        baseline = (
            database
            .get_strategy_capital_state(
                baseline_key
            )
        )

        if not baseline:
            stamp = now.isoformat()

            database.set_strategy_capital_state(
                baseline_key,
                stamp,
            )

            database.set_strategy_capital_state(
                last_sync_key,
                stamp,
            )

            logger.info(
                "JALWE strategy-wallet baseline initialized | %s",
                stamp,
            )

            return (
                database
                .get_strategy_capital_adjustment_total()
            )

        last_sync = (
            database
            .get_strategy_capital_state(
                last_sync_key
            )
            or baseline
        )

        try:
            parsed_last_sync = (
                datetime.fromisoformat(
                    str(last_sync)
                    .replace(
                        "Z",
                        "+00:00",
                    )
                )
            )

            if parsed_last_sync.tzinfo is None:
                parsed_last_sync = (
                    parsed_last_sync
                    .replace(
                        tzinfo=timezone.utc
                    )
                )

        except Exception:
            parsed_last_sync = now

        # Avoid a broker activity request for every symbol.
        if (
            now - parsed_last_sync
        ).total_seconds() < 300:
            return (
                database
                .get_strategy_capital_adjustment_total()
            )

        try:
            activities = (
                get_alpaca_client()
                .get_cash_transfer_activities(
                    after=str(last_sync),
                    limit=100,
                )
            )
        except Exception as exc:
            logger.warning(
                "Strategy wallet cash-flow sync failed: %s",
                exc,
            )

            return (
                database
                .get_strategy_capital_adjustment_total()
            )

        inserted = 0

        for activity in activities:
            activity_type = str(
                activity.get(
                    "activity_type",
                    "",
                )
                or ""
            ).upper()

            if activity_type not in {
                "CSD",
                "CSW",
            }:
                continue

            raw_amount = (
                PaperTradeOrchestrator
                ._activity_amount(
                    activity
                )
            )

            if raw_amount is None:
                continue

            amount = (
                abs(raw_amount)
                if activity_type == "CSD"
                else -abs(raw_amount)
            )

            activity_time = (
                PaperTradeOrchestrator
                ._activity_time(
                    activity
                )
            )

            raw_id = str(
                activity.get("id")
                or activity.get(
                    "activity_id"
                )
                or activity.get(
                    "event_id"
                )
                or activity.get(
                    "ref_id"
                )
                or ""
            ).strip()

            if raw_id:
                event_key = (
                    "ALPACA-CASH-"
                    + raw_id
                )
            else:
                canonical = json.dumps(
                    activity,
                    sort_keys=True,
                    default=str,
                ).encode(
                    "utf-8"
                )

                event_key = (
                    "ALPACA-CASH-"
                    + hashlib.sha256(
                        canonical
                    ).hexdigest()
                )

            if database.record_strategy_capital_adjustment(
                event_key=event_key,
                activity_type=activity_type,
                amount=amount,
                activity_time=activity_time,
                metadata={
                    "source": "ALPACA_PAPER",
                    "raw": activity,
                },
            ):
                inserted += 1

        database.set_strategy_capital_state(
            last_sync_key,
            now.isoformat(),
        )

        if inserted:
            logger.info(
                "Strategy wallet cash flows synchronized | new=%s",
                inserted,
            )

        return (
            database
            .get_strategy_capital_adjustment_total()
        )

    @staticmethod
    def _strategy_equity_snapshot() -> tuple[
        float,
        float,
        float,
    ]:
        starting_capital = float(
            settings.STRATEGY_STARTING_CAPITAL
        )

        total_realized = float(
            database.get_strategy_realized_pnl_total()
        )

        capital_adjustments = 0.0

        if settings.CAPITAL_MODE == "strategy_wallet":
            capital_adjustments = float(
                PaperTradeOrchestrator
                ._sync_strategy_wallet_adjustments()
            )

        current_equity = (
            starting_capital
            + capital_adjustments
            + total_realized
        )

        now_ny = datetime.now(
            ZoneInfo("America/New_York")
        )

        start_ny = now_ny.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        start_utc = (
            start_ny
            .astimezone(timezone.utc)
            .isoformat()
        )

        realized_today = float(
            database.get_strategy_realized_pnl_since(
                start_utc
            )
        )

        adjustments_today = 0.0

        if settings.CAPITAL_MODE == "strategy_wallet":
            adjustments_today = float(
                database
                .get_strategy_capital_adjustment_since(
                    start_utc
                )
            )

        # Remove today's PnL and cash flows from current equity
        # so deposits/withdrawals do not look like trading PnL
        # to the daily-loss circuit breaker.
        daily_start_equity = (
            current_equity
            - realized_today
            - adjustments_today
        )

        return (
            current_equity,
            daily_start_equity,
            realized_today,
        )

    @staticmethod
    def _default_equity() -> float:
        current, _daily_start, _today = (
            PaperTradeOrchestrator
            ._strategy_equity_snapshot()
        )

        return current

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        symbol = str(
            symbol or ""
        ).strip().upper()

        if not symbol:
            raise ValueError(
                "Symbol cannot be empty."
            )

        return symbol

    # ========================================================
    # ID GENERATION
    # ========================================================

    @staticmethod
    def _new_entry_ids(
        symbol: str,
    ) -> tuple[str, str]:

        symbol = (
            PaperTradeOrchestrator
            ._normalize_symbol(symbol)
        )

        token = (
            uuid4()
            .hex[:20]
            .upper()
        )

        intent_id = (
            f"ENTRY-{symbol}-{token}"
        )

        client_order_id = (
            f"JALWE-FINAL-{token}"
        )

        return (
            intent_id,
            client_order_id,
        )

    # ========================================================
    # STARTUP RECOVERY
    # ========================================================

    def run_startup_recovery(
        self,
    ) -> dict[str, Any]:

        return (
            self.recovery_engine
            .recover_all()
        )

    # ========================================================
    # ANALYZE
    # ========================================================

    def analyze_symbol(
        self,
        symbol: str,
        *,
        strategy_equity: Optional[float] = None,
        daily_start_equity: Optional[float] = None,
    ) -> FinalTradeDecision:

        symbol = self._normalize_symbol(
            symbol
        )

        if (
            strategy_equity is None
            or daily_start_equity is None
        ):
            (
                current_equity,
                current_daily_start,
                _realized_today,
            ) = self._strategy_equity_snapshot()

            if strategy_equity is None:
                strategy_equity = (
                    current_equity
                )

            if daily_start_equity is None:
                daily_start_equity = (
                    current_daily_start
                )

        return (
            self.decision_engine
            .analyze(
                symbol,
                strategy_equity=float(
                    strategy_equity
                ),
                daily_start_equity=float(
                    daily_start_equity
                ),
            )
        )

    # ========================================================
    # CREATE ENTRY INTENT
    # ========================================================

    def _create_entry_intent(
        self,
        decision: FinalTradeDecision,
    ) -> tuple[str, str]:

        (
            intent_id,
            client_order_id,
        ) = self._new_entry_ids(
            decision.symbol
        )

        strategy = self._value(
            getattr(
                decision,
                "strategy",
                None,
            )
        )

        setup_grade = self._value(
            getattr(
                decision,
                "setup_grade",
                None,
            )
        )

        market_regime = self._value(
            getattr(
                decision,
                "market_regime",
                None,
            )
        )

        decision_state = self._value(
            getattr(
                decision,
                "state",
                "",
            )
        )

        database.create_entry_intent(
            intent_id=intent_id,
            client_order_id=client_order_id,

            symbol=decision.symbol,

            quantity=int(
                decision.quantity
            ),

            planned_entry=float(
                decision.entry_price
            ),

            stop_price=float(
                decision.stop_price
            ),

            target_1=float(
                decision.target_1
            ),

            target_2=float(
                decision.target_2
            ),

            target_3=float(
                decision.target_3
            ),

            risk_pct=float(
                decision.risk_pct
            ),

            strategy=strategy,
            setup_grade=setup_grade,
            decision_state=str(
                decision_state
            ),

            decision_gates=dict(
                getattr(
                    decision,
                    "gates",
                    {},
                )
                or {}
            ),

            metadata={
                "orchestrator": "PAPER_V3_1",

                "market_regime": (
                    market_regime
                ),

                "ai_score": getattr(
                    decision,
                    "ai_score",
                    None,
                ),

                "opportunity_score": getattr(
                    decision,
                    "opportunity_score",
                    None,
                ),

                "session_strategy_score": getattr(
                    decision,
                    "session_strategy_score",
                    None,
                ),

                "breakout_score": getattr(
                    decision,
                    "breakout_score",
                    None,
                ),

                "decision_reason": getattr(
                    decision,
                    "reason",
                    "",
                ),

                "warnings": list(
                    getattr(
                        decision,
                        "warnings",
                        [],
                    )
                    or []
                ),

                "strategy_starting_capital": (
                    settings
                    .STRATEGY_STARTING_CAPITAL
                ),
            },
        )

        return (
            intent_id,
            client_order_id,
        )

    # ========================================================
    # REJECTED
    # ========================================================

    @staticmethod
    def _rejected_result(
        decision: FinalTradeDecision,
    ) -> PaperOrchestratorResult:

        return PaperOrchestratorResult(
            symbol=decision.symbol,

            state=(
                PaperOrchestratorState
                .REJECTED
            ),

            decision=decision,

            message=str(
                getattr(
                    decision,
                    "reason",
                    "",
                )
            ),

            warnings=list(
                getattr(
                    decision,
                    "warnings",
                    [],
                )
                or []
            ),

            metadata={
                "ready_for_execution": False,

                "gates": dict(
                    getattr(
                        decision,
                        "gates",
                        {},
                    )
                    or {}
                ),
            },
        )

    # ========================================================
    # WATCHING
    # ========================================================

    @staticmethod
    def _watching_result(
        decision: FinalTradeDecision,
    ) -> PaperOrchestratorResult:

        return PaperOrchestratorResult(
            symbol=decision.symbol,

            state=(
                PaperOrchestratorState
                .WATCHING
            ),

            decision=decision,

            message=str(
                getattr(
                    decision,
                    "reason",
                    "",
                )
            ),

            warnings=list(
                getattr(
                    decision,
                    "warnings",
                    [],
                )
                or []
            ),

            metadata={
                "ready_for_execution": False,

                "strategy": getattr(
                    decision,
                    "strategy",
                    None,
                ),

                "entry_price": getattr(
                    decision,
                    "entry_price",
                    None,
                ),

                "stop_price": getattr(
                    decision,
                    "stop_price",
                    None,
                ),

                "gates": dict(
                    getattr(
                        decision,
                        "gates",
                        {},
                    )
                    or {}
                ),
            },
        )

    # ========================================================
    # CONFIG EXECUTION READY
    # ========================================================

    def _config_execution_ready(
        self,
    ) -> bool:

        return bool(
            auto_paper_execution_ready()
            and self.auto_execution
            and self.broker_submission_enabled
        )

    # ========================================================
    # RUN SYMBOL
    # ========================================================

    def run_symbol(
        self,
        symbol: str,
        *,
        strategy_equity: Optional[float] = None,
        daily_start_equity: Optional[float] = None,
    ) -> PaperOrchestratorResult:

        try:
            symbol = self._normalize_symbol(
                symbol
            )

        except Exception as exc:
            return PaperOrchestratorResult(
                symbol="",
                state=PaperOrchestratorState.ERROR,
                message=str(exc),
            )

        # ====================================================
        # 1. STARTUP RECOVERY
        # ====================================================

        try:
            recovery = (
                self.run_startup_recovery()
            )

        except Exception as exc:
            logger.exception(
                "Startup recovery failed."
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .RECOVERY_BLOCKED
                ),

                message=(
                    "Startup recovery failed. "
                    "New entries are blocked."
                ),

                warnings=[
                    str(exc)
                ],
            )

        if not bool(
            recovery.get(
                "safe_to_trade",
                False,
            )
        ):
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .RECOVERY_BLOCKED
                ),

                message=(
                    "RecoveryEngine found an "
                    "unresolved broker/database "
                    "condition. New entries are blocked."
                ),

                metadata={
                    "recovery": recovery,
                },
            )

        # ====================================================
        # 2. DECISION ENGINE
        # ====================================================

        try:
            decision = self.analyze_symbol(
                symbol,

                strategy_equity=(
                    strategy_equity
                ),

                daily_start_equity=(
                    daily_start_equity
                ),
            )

        except Exception as exc:
            logger.exception(
                "DecisionEngine failed | symbol=%s",
                symbol,
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ERROR
                ),

                message=(
                    "DecisionEngine failed."
                ),

                warnings=[
                    str(exc)
                ],

                metadata={
                    "recovery": recovery,
                },
            )

        # ====================================================
        # 3. REJECTED
        # ====================================================

        if (
            decision.state
            == DecisionState.REJECTED
        ):
            return self._rejected_result(
                decision
            )

        # ====================================================
        # 4. WATCHING
        # ====================================================

        if (
            decision.state
            == DecisionState.WATCHING
        ):
            return self._watching_result(
                decision
            )

        # ====================================================
        # 5. READY CONSISTENCY
        # ====================================================

        if not (
            decision.state
            == DecisionState
            .READY_FOR_PAPER_EXECUTION

            and

            bool(
                decision
                .ready_for_execution
            )
        ):
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ERROR
                ),

                decision=decision,

                message=(
                    "DecisionEngine returned "
                    "an inconsistent READY state."
                ),
            )

        # ====================================================
        # 6. AUTO EXECUTION LOCK
        # ====================================================

        if not self.auto_execution:
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .READY_AUTO_DISABLED
                ),

                decision=decision,

                message=(
                    "Trade passed all JALWE analysis "
                    "gates, but "
                    "JALWE_AUTO_PAPER_EXECUTION "
                    "is disabled."
                ),

                warnings=list(
                    getattr(
                        decision,
                        "warnings",
                        [],
                    )
                    or []
                ),

                metadata={
                    "auto_execution": False,

                    "broker_submission_enabled": (
                        self
                        .broker_submission_enabled
                    ),

                    "broker_order_submitted": False,

                    "entry": decision.entry_price,
                    "stop": decision.stop_price,

                    "target_1": decision.target_1,
                    "target_2": decision.target_2,
                    "target_3": decision.target_3,

                    "quantity": decision.quantity,
                    "risk_pct": decision.risk_pct,
                },
            )

        # ====================================================
        # 7. BROKER SUBMISSION LOCK
        # ====================================================

        if not self.broker_submission_enabled:
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .READY_BROKER_LOCKED
                ),

                decision=decision,

                message=(
                    "AUTO PAPER is enabled, "
                    "but JALWE_BROKER_SUBMISSION "
                    "is disabled."
                ),

                metadata={
                    "auto_execution": True,

                    "broker_submission_enabled": False,

                    "broker_order_submitted": False,
                },
            )

        # ====================================================
        # 8. GLOBAL CONFIG SAFETY
        # ====================================================

        if not self._config_execution_ready():
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .READY_CONFIG_LOCKED
                ),

                decision=decision,

                message=(
                    "Global configuration safety "
                    "check blocked broker execution."
                ),

                metadata={
                    "paper_trading": (
                        settings.PAPER_TRADING
                    ),

                    "live_trading": (
                        settings
                        .ALLOW_LIVE_TRADING
                    ),

                    "config_auto": (
                        settings
                        .AUTO_PAPER_EXECUTION
                    ),

                    "config_broker_submission": (
                        settings
                        .BROKER_SUBMISSION_ENABLED
                    ),

                    "execution_ready": (
                        auto_paper_execution_ready()
                    ),
                },
            )

        # ====================================================
        # 9. SAVE ENTRY INTENT BEFORE BROKER CALL
        # ====================================================

        try:
            (
                intent_id,
                client_order_id,
            ) = self._create_entry_intent(
                decision
            )

        except Exception as exc:
            logger.exception(
                "Entry intent creation failed | symbol=%s",
                symbol,
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ERROR
                ),

                decision=decision,

                message=(
                    "EntryIntent could not be "
                    "persisted. No broker order "
                    "was submitted."
                ),

                warnings=[
                    str(exc)
                ],

                metadata={
                    "broker_order_submitted": False,
                },
            )

        # ====================================================
        # 10. SUBMIT PAPER BUY
        # ====================================================

        try:
            submitted_order = (
                self.execution_engine
                .submit_final_decision(
                    decision,
                    client_order_id=(
                        client_order_id
                    ),
                )
            )

        except Exception as exc:
            logger.exception(
                "Entry submission failed or "
                "result uncertain | "
                "intent_id=%s symbol=%s",
                intent_id,
                symbol,
            )

            # IMPORTANT:
            # Keep EntryIntent unresolved.
            #
            # RecoveryEngine can search Alpaca using
            # the already-persisted client_order_id.

            try:
                database.mark_entry_intent_error(
                    intent_id,
                    (
                        "Broker submission failed "
                        "or result uncertain: "
                        + str(exc)
                    ),
                    terminal=False,
                )

            except Exception:
                logger.exception(
                    "Unable to mark EntryIntent "
                    "as ERROR."
                )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_RECOVERY_REQUIRED
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                message=(
                    "Entry submission result is "
                    "uncertain. EntryIntent was "
                    "preserved for recovery. "
                    "Do not submit another BUY."
                ),

                warnings=[
                    str(exc)
                ],
            )

        # ====================================================
        # 11. SAVE FIRST BROKER SNAPSHOT
        # ====================================================

        try:
            intent_row = (
                database
                .register_entry_submission(
                    intent_id,
                    submitted_order,
                )
            )

        except Exception as exc:
            logger.exception(
                "Broker order submitted but "
                "database snapshot failed | "
                "intent_id=%s",
                intent_id,
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_RECOVERY_REQUIRED
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    submitted_order
                    .order_id
                ),

                message=(
                    "Broker order exists but "
                    "database snapshot failed. "
                    "RecoveryEngine must resolve it."
                ),

                warnings=[
                    str(exc)
                ],
            )

        # ====================================================
        # 12. RECONCILE ENTRY ORDER
        # ====================================================

        try:
            reconciled_order = (
                self.reconciliation_engine
                .wait_for_terminal_state(
                    submitted_order,
                    timeout_seconds=(
                        self.entry_timeout_seconds
                    ),
                    poll_interval_seconds=1,
                )
            )

        except Exception as exc:
            logger.exception(
                "Entry reconciliation interrupted | "
                "intent_id=%s",
                intent_id,
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_RECOVERY_REQUIRED
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    submitted_order
                    .order_id
                ),

                message=(
                    "Entry order was submitted "
                    "but reconciliation did not finish. "
                    "RecoveryEngine will resume it."
                ),

                warnings=[
                    str(exc)
                ],
            )

        # ====================================================
        # 13. SAVE RECONCILED SNAPSHOT
        # ====================================================

        try:
            intent_row = (
                database
                .update_entry_intent_from_broker_order(
                    intent_id,
                    reconciled_order,
                )
            )

        except Exception as exc:
            logger.exception(
                "Unable to persist reconciled "
                "broker state | intent_id=%s",
                intent_id,
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_RECOVERY_REQUIRED
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    reconciled_order
                    .order_id
                ),

                message=(
                    "Broker state is known "
                    "but database persistence failed. "
                    "Recovery is required."
                ),

                warnings=[
                    str(exc)
                ],
            )

        intent_state = str(
            intent_row.get(
                "state",
                "",
            )
            or ""
        ).strip().upper()

        # ====================================================
        # 14. ORDER STILL ACTIVE
        # ====================================================

        if intent_state in {
            "PREPARED",
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "ERROR",
        }:
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_PENDING
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    intent_row.get(
                        "broker_order_id"
                    )
                ),

                message=(
                    "Entry remains unresolved at "
                    "Alpaca. New entries stay blocked "
                    "until recovery."
                ),

                metadata={
                    "intent_state": intent_state,

                    "filled_quantity": (
                        intent_row.get(
                            "filled_quantity"
                        )
                    ),

                    "broker_status": (
                        intent_row.get(
                            "broker_status"
                        )
                    ),
                },
            )

        # ====================================================
        # 15. TERMINAL ENTRY
        # ====================================================

        try:
            recovery_result = (
                self.recovery_engine
                .recover_entry_intent(
                    intent_row
                )
            )

        except Exception as exc:
            logger.exception(
                "Final entry management failed | "
                "intent_id=%s",
                intent_id,
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_RECOVERY_REQUIRED
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    intent_row.get(
                        "broker_order_id"
                    )
                ),

                message=(
                    "Entry reached terminal broker "
                    "state but could not be converted "
                    "safely into ManagedTrade."
                ),

                warnings=[
                    str(exc)
                ],
            )

        # ====================================================
        # 16. MANAGED SUCCESS
        # ====================================================

        if recovery_result.get(
            "managed",
            False,
        ):
            managed_trade_id = (
                recovery_result.get(
                    "managed_trade_id"
                )
            )

            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_MANAGED
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    intent_row.get(
                        "broker_order_id"
                    )
                ),

                managed_trade_id=(
                    managed_trade_id
                ),

                message=(
                    "Paper entry confirmed, "
                    "persisted, and under "
                    "TradeManager control."
                ),

                metadata={
                    "entry_recovery": (
                        recovery_result
                    ),

                    "intent_state": "MANAGED",
                },
            )

        # ====================================================
        # 17. TERMINAL WITHOUT POSITION
        # ====================================================

        if recovery_result.get(
            "resolved",
            False,
        ):
            return PaperOrchestratorResult(
                symbol=symbol,

                state=(
                    PaperOrchestratorState
                    .ENTRY_TERMINAL_NO_FILL
                ),

                decision=decision,

                intent_id=intent_id,

                client_order_id=(
                    client_order_id
                ),

                broker_order_id=(
                    intent_row.get(
                        "broker_order_id"
                    )
                ),

                message=(
                    "Entry order terminated "
                    "without a position to manage."
                ),

                metadata={
                    "entry_recovery": (
                        recovery_result
                    ),
                },
            )

        # ====================================================
        # 18. STILL UNRESOLVED
        # ====================================================

        return PaperOrchestratorResult(
            symbol=symbol,

            state=(
                PaperOrchestratorState
                .ENTRY_RECOVERY_REQUIRED
            ),

            decision=decision,

            intent_id=intent_id,

            client_order_id=(
                client_order_id
            ),

            broker_order_id=(
                intent_row.get(
                    "broker_order_id"
                )
            ),

            message=(
                "Entry lifecycle remains unresolved. "
                "New entries must remain blocked."
            ),

            metadata={
                "entry_recovery": (
                    recovery_result
                ),
            },
        )

    # ========================================================
    # SAFETY STATUS
    # ========================================================

    def safety_status(
        self,
    ) -> dict[str, Any]:

        return {
            "paper_only": (
                settings.PAPER_TRADING
            ),

            "live_trading_allowed": (
                settings.ALLOW_LIVE_TRADING
            ),

            "auto_execution": (
                self.auto_execution
            ),

            "broker_submission_enabled": (
                self.broker_submission_enabled
            ),

            "config_auto_execution": (
                settings.AUTO_PAPER_EXECUTION
            ),

            "config_broker_submission": (
                settings.BROKER_SUBMISSION_ENABLED
            ),

            "configuration_execution_ready": (
                auto_paper_execution_ready()
            ),

            "strategy_starting_capital": (
                settings
                .STRATEGY_STARTING_CAPITAL
            ),

            "pending_entry_recovery_ready": True,

            "pending_exit_recovery_ready": True,

            "startup_reconciliation_ready": True,

            "decision_engine_available": (
                self.decision_engine
                is not None
            ),

            "execution_engine_available": (
                self.execution_engine
                is not None
            ),

            "reconciliation_engine_available": (
                self.reconciliation_engine
                is not None
            ),

            "recovery_engine_available": (
                self.recovery_engine
                is not None
            ),
        }


# ============================================================
# LAZY SINGLETON
# ============================================================

_paper_trade_orchestrator: Optional[
    PaperTradeOrchestrator
] = None


def get_paper_trade_orchestrator(
) -> PaperTradeOrchestrator:

    global _paper_trade_orchestrator

    if _paper_trade_orchestrator is None:
        _paper_trade_orchestrator = (
            PaperTradeOrchestrator(
                auto_execution=None,
                broker_submission_enabled=None,
                entry_timeout_seconds=30,
            )
        )

    return _paper_trade_orchestrator