from __future__ import annotations

import logging

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from intelligence.ai_engine import AIEngine
from core.database import database
from intelligence.breakout_confirmation_engine import get_breakout_confirmation_engine
from intelligence.external_research_bridge import get_external_research_bridge
from intelligence.feature_engine import FeatureEngine
from intelligence.news_engine import get_news_engine
from intelligence.opportunity_engine import get_opportunity_engine
from intelligence.options_flow_engine import get_options_flow_engine
from intelligence.session_strategy_engine import get_session_strategy_engine
from intelligence.strategy_router import get_strategy_router
from intelligence.trigger_engine import TriggerState, get_trigger_engine
from market.market_context import get_market_context_engine
from market.market_data import get_market_data
from market.session_features import get_session_feature_engine
from trading.risk_engine import get_risk_engine


logger = logging.getLogger(__name__)


# ============================================================
# FINAL DECISION STATE
# ============================================================

class DecisionState(str, Enum):
    REJECTED = "REJECTED"
    WATCHING = "WATCHING"
    READY_FOR_PAPER_EXECUTION = "READY_FOR_PAPER_EXECUTION"


# ============================================================
# FINAL DECISION
# ============================================================

@dataclass
class FinalTradeDecision:
    symbol: str
    state: DecisionState
    ready_for_execution: bool

    market_regime: Optional[str] = None
    strategy: Optional[str] = None
    setup_grade: Optional[str] = None

    opportunity_score: Optional[float] = None
    session_strategy_score: Optional[float] = None
    breakout_score: Optional[float] = None
    ai_score: Optional[float] = None

    risk_pct: float = 0.0
    quantity: int = 0

    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None

    reason: str = ""
    gates: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ============================================================
# DECISION ENGINE
# ============================================================

class DecisionEngine:
    """
    JALWE V4 - Central Decision Engine.

    Pipeline:

        External Apex Research (ADVISORY ONLY)
            ↓
        Market Data
            ↓
        Feature Engine
            ↓
        News + Options
            ↓
        AI Engine
            ↓
        Opportunity Engine
            ↓
        Market Context
            ↓
        Strategy Router
            ↓
        Session Features
            ↓
        Session Strategy
            ↓
        Trigger Engine
            ↓
        Breakout Confirmation
            ↓
        Risk Engine
            ↓
        FINAL DECISION

    Safety rules for external research:

        - Apex research is advisory only.
        - It is NOT a mandatory decision gate.
        - It can NEVER turn a rejected JALWE setup into an approved setup.
        - It can NEVER create READY_FOR_PAPER_EXECUTION by itself.
        - Stale research is recorded but ignored as active advisory evidence.
        - Bridge failure does NOT stop JALWE's own analysis.
        - External research NEVER submits broker orders.

    This engine DOES NOT submit broker orders.

    READY_FOR_PAPER_EXECUTION only means all native JALWE
    decision/risk gates passed. ExecutionEngine remains separate.
    """

    DEFAULT_EXTERNAL_RESEARCH_MAX_AGE_MINUTES = 30

    def __init__(self) -> None:
        self.market_data = get_market_data()
        self.feature_engine = FeatureEngine()
        self.news_engine = get_news_engine()
        self.options_engine = get_options_flow_engine()
        self.ai_engine = AIEngine()
        self.opportunity_engine = get_opportunity_engine()
        self.market_context_engine = get_market_context_engine()
        self.strategy_router = get_strategy_router()
        self.session_feature_engine = get_session_feature_engine()
        self.session_strategy_engine = get_session_strategy_engine()
        self.trigger_engine = get_trigger_engine()
        self.breakout_engine = get_breakout_confirmation_engine()
        self.risk_engine = get_risk_engine()
        self.external_research_bridge = get_external_research_bridge()

    # ========================================================
    # RESULT HELPERS
    # ========================================================

    @staticmethod
    def _reject(
        symbol: str,
        reason: str,
        *,
        gates: Optional[dict[str, bool]] = None,
        warnings: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> FinalTradeDecision:
        return FinalTradeDecision(
            symbol=symbol,
            state=DecisionState.REJECTED,
            ready_for_execution=False,
            reason=reason,
            gates=gates or {},
            warnings=warnings or [],
            metadata=metadata or {},
            **kwargs,
        )

    @staticmethod
    def _watch(
        symbol: str,
        reason: str,
        *,
        gates: Optional[dict[str, bool]] = None,
        warnings: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> FinalTradeDecision:
        return FinalTradeDecision(
            symbol=symbol,
            state=DecisionState.WATCHING,
            ready_for_execution=False,
            reason=reason,
            gates=gates or {},
            warnings=warnings or [],
            metadata=metadata or {},
            **kwargs,
        )

    @staticmethod
    def _merge_metadata(
        base: Optional[dict[str, Any]],
        extra: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        output = dict(base or {})
        output.update(dict(extra or {}))
        return output

    # ========================================================
    # SMART MARKET-DATA BACKFILL
    # ========================================================

    def _smart_backfill_bars(
        self,
        symbol: str,
        timeframe: str,
        bars: Any,
        diagnostics: dict[str, Any],
        bar_limit: int,
    ) -> tuple[Any, dict[str, Any], Optional[dict[str, Any]]]:
        """
        Retry sparse Alpaca history with a wider lookback.

        Safety:
        - Uses only real Alpaca bars.
        - Never fabricates/resamples missing candles.
        - Never lowers FeatureEngine's minimum-row requirement.
        - Stops after three bounded retries.
        """

        initial_valid = int(
            diagnostics.get(
                "valid_rows",
                0,
            )
            or 0
        )

        required_rows = int(
            diagnostics.get(
                "required_rows",
                0,
            )
            or 0
        )

        missing_columns = (
            diagnostics.get(
                "missing_columns",
                [],
            )
            or []
        )

        if (
            required_rows <= 0
            or initial_valid >= required_rows
            or missing_columns
        ):
            return (
                bars,
                diagnostics,
                None,
            )

        best_bars = bars
        best_diagnostics = dict(
            diagnostics
        )

        attempts: list[dict[str, Any]] = []

        next_limit = max(
            int(bar_limit) * 2,
            1000,
        )

        max_limit = min(
            10000,
            max(
                int(bar_limit) * 8,
                4000,
            ),
        )

        for _ in range(3):
            request_limit = min(
                next_limit,
                max_limit,
            )

            if any(
                int(item.get("limit") or 0)
                == request_limit
                for item in attempts
            ):
                break

            attempt: dict[str, Any] = {
                "limit": request_limit,
                "status": "STARTED",
            }

            try:
                candidate_bars = (
                    self.market_data.get_bars(
                        symbol,
                        timeframe,
                        request_limit,
                    )
                )

                candidate_diagnostics = (
                    self.feature_engine
                    .diagnose_input(
                        candidate_bars
                    )
                )

                candidate_valid = int(
                    candidate_diagnostics.get(
                        "valid_rows",
                        0,
                    )
                    or 0
                )

                attempt.update(
                    {
                        "status": "SUCCESS",
                        "valid_rows": candidate_valid,
                    }
                )

                if candidate_valid > int(
                    best_diagnostics.get(
                        "valid_rows",
                        0,
                    )
                    or 0
                ):
                    best_bars = (
                        candidate_bars
                    )
                    best_diagnostics = (
                        candidate_diagnostics
                    )

                attempts.append(
                    attempt
                )

                if candidate_valid >= required_rows:
                    break

            except Exception as exc:
                attempt.update(
                    {
                        "status": "ERROR",
                        "error": str(exc)[:300],
                    }
                )

                attempts.append(
                    attempt
                )

            if request_limit >= max_limit:
                break

            next_limit = min(
                request_limit * 2,
                max_limit,
            )

        final_valid = int(
            best_diagnostics.get(
                "valid_rows",
                0,
            )
            or 0
        )

        backfill_metadata = {
            "attempted": bool(
                attempts
            ),
            "succeeded": bool(
                final_valid >= required_rows
            ),
            "initial_valid_rows": (
                initial_valid
            ),
            "final_valid_rows": (
                final_valid
            ),
            "required_rows": (
                required_rows
            ),
            "attempt_count": len(
                attempts
            ),
            "attempts": attempts,
            "real_bars_only": True,
            "fabricated_bars": False,
        }

        logger.info(
            "Smart backfill | symbol=%s timeframe=%s "
            "initial=%s final=%s required=%s attempts=%s",
            symbol,
            timeframe,
            initial_valid,
            final_valid,
            required_rows,
            len(attempts),
        )

        return (
            best_bars,
            best_diagnostics,
            backfill_metadata,
        )

    # ========================================================
    # EXTERNAL APEX RESEARCH - ADVISORY ONLY
    # ========================================================

    def _load_external_research(
        self,
        symbol: str,
        warnings: list[str],
        *,
        max_age_minutes: int,
    ) -> dict[str, Any]:
        """
        Read the latest Apex/external research packet.

        IMPORTANT:
            This method only returns advisory context.
            It does not approve/reject a trade and does not alter gates.
        """

        snapshot: dict[str, Any] = {
            "available": False,
            "fresh": False,
            "advisory_only": True,
            "affects_mandatory_gate": False,
            "execution_authority": False,
            "max_age_minutes": int(max_age_minutes),
        }

        try:
            research = self.external_research_bridge.get_latest_research(symbol)
        except Exception as exc:
            logger.warning(
                "External research bridge read failed for %s: %s",
                symbol,
                exc,
            )
            warnings.append(
                f"External research unavailable: {exc}"
            )
            snapshot["status"] = "BRIDGE_ERROR"
            snapshot["error"] = str(exc)
            return snapshot

        if research is None:
            snapshot["status"] = "NOT_FOUND"
            return snapshot

        snapshot["available"] = True

        try:
            age_minutes = self.external_research_bridge.get_age_minutes(research)
        except Exception:
            age_minutes = None

        fresh = bool(
            age_minutes is not None
            and age_minutes <= float(max_age_minutes)
        )

        raw_metadata = dict(
            getattr(research, "metadata", {}) or {}
        )

        snapshot.update(
            {
                "status": "FRESH" if fresh else "STALE",
                "fresh": fresh,
                "source": getattr(research, "source", None),
                "age_minutes": (
                    round(float(age_minutes), 3)
                    if age_minutes is not None
                    else None
                ),
                "confidence": getattr(research, "confidence", 0.0),
                "sentiment": getattr(research, "sentiment", "UNKNOWN"),
                "news_score": getattr(research, "news_score", None),
                "catalyst": getattr(research, "catalyst", None),
                "market_bias": getattr(research, "market_bias", None),
                "summary": getattr(research, "summary", ""),
                "technical_notes": list(
                    getattr(research, "technical_notes", []) or []
                ),
                "headlines": list(
                    getattr(research, "headlines", []) or []
                ),
                "risk_flags": list(
                    getattr(research, "risk_flags", []) or []
                ),
                "created_at": getattr(research, "created_at", ""),
                "apex_verdict": (
                    raw_metadata.get("apex_verdict")
                    or raw_metadata.get("verdict")
                ),
                "apex_research_score": (
                    raw_metadata.get("apex_research_score")
                    if raw_metadata.get("apex_research_score") is not None
                    else raw_metadata.get("score")
                ),
                "apex_confidence_pct": (
                    raw_metadata.get("apex_confidence_pct")
                    if raw_metadata.get("apex_confidence_pct") is not None
                    else (
                        float(
                            getattr(
                                research,
                                "confidence",
                                0.0,
                            )
                            or 0.0
                        )
                        * 100.0
                    )
                ),
                "raw_metadata": raw_metadata,
            }
        )

        if not fresh:
            warnings.append(
                "External Apex research is stale and is not used as active advisory evidence."
            )
            return snapshot

        # Fresh Apex risk flags are surfaced as warnings only.
        # They do NOT become a JALWE mandatory gate.
        for flag in snapshot["risk_flags"]:
            text = str(flag).strip()
            if text:
                warnings.append(
                    f"APEX advisory risk: {text}"
                )

        return snapshot

    # ========================================================
    # NEWS ENRICHMENT
    # ========================================================

    def _apply_news(
        self,
        features: Any,
        warnings: list[str],
    ) -> None:
        try:
            self.news_engine.apply_to_features(features)
        except Exception as exc:
            logger.warning("News enrichment failed: %s", exc)
            warnings.append(f"News data unavailable: {exc}")

    # ========================================================
    # OPTIONS ENRICHMENT
    # ========================================================

    def _apply_options(
        self,
        features: Any,
        warnings: list[str],
    ) -> None:
        try:
            self.options_engine.apply_to_features(features)
        except Exception as exc:
            logger.warning("Options enrichment failed: %s", exc)
            warnings.append(f"Options data unavailable: {exc}")

    # ========================================================
    # MAIN ANALYSIS
    # ========================================================

    def analyze(
        self,
        symbol: str,
        *,
        strategy_equity: float = 100.0,
        daily_start_equity: float = 100.0,
        timeframe: str = "5m",
        bar_limit: int = 500,
        external_research_max_age_minutes: int = (
            DEFAULT_EXTERNAL_RESEARCH_MAX_AGE_MINUTES
        ),
    ) -> FinalTradeDecision:

        symbol = str(symbol or "").strip().upper()

        if not symbol:
            return self._reject(
                symbol="",
                reason="Symbol cannot be empty.",
            )

        if external_research_max_age_minutes <= 0:
            external_research_max_age_minutes = (
                self.DEFAULT_EXTERNAL_RESEARCH_MAX_AGE_MINUTES
            )

        warnings: list[str] = []

        gates: dict[str, bool] = {
            "market_data": False,
            "features": False,
            "ai": False,
            "opportunity": False,
            "market_regime": False,
            "strategy_router": False,
            "session_strategy": False,
            "trigger": False,
            "breakout_confirmation": False,
            "risk": False,
        }

        # ====================================================
        # 0. EXTERNAL APEX RESEARCH (ADVISORY ONLY)
        # ====================================================

        external_research = self._load_external_research(
            symbol,
            warnings,
            max_age_minutes=external_research_max_age_minutes,
        )

        base_metadata: dict[str, Any] = {
            "external_research": external_research,
            "external_research_advisory_only": True,
        }

        # ====================================================
        # 1. MARKET DATA
        # ====================================================

        try:
            bars = self.market_data.get_bars(
                symbol,
                timeframe,
                bar_limit,
            )
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Unable to load market data.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
            )

        if bars is None or bars.empty:
            return self._reject(
                symbol=symbol,
                reason="Market data returned no bars.",
                gates=gates,
                warnings=warnings,
                metadata=base_metadata,
            )

        gates["market_data"] = True

        # ====================================================
        # 2. FEATURE ENGINE
        # ====================================================

        feature_diagnostics = (
            self.feature_engine.diagnose_input(
                bars
            )
        )

        (
            bars,
            feature_diagnostics,
            backfill_metadata,
        ) = self._smart_backfill_bars(
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            diagnostics=feature_diagnostics,
            bar_limit=bar_limit,
        )

        if backfill_metadata is not None:
            base_metadata[
                "market_data_backfill"
            ] = backfill_metadata

        try:
            features = self.feature_engine.build(symbol, bars)
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Feature calculation failed.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
            )

        if not features.data_quality_ok or features.data_is_stale:
            valid_rows = int(
                feature_diagnostics.get(
                    "valid_rows",
                    0,
                )
                or 0
            )

            required_rows = int(
                feature_diagnostics.get(
                    "required_rows",
                    0,
                )
                or 0
            )

            missing_columns = (
                feature_diagnostics.get(
                    "missing_columns",
                    [],
                )
                or []
            )

            if missing_columns:
                quality_reason = (
                    "Feature quality failed: missing columns "
                    + ", ".join(
                        str(item)
                        for item in missing_columns
                    )
                    + "."
                )

            elif valid_rows < required_rows:
                quality_reason = (
                    "Feature quality failed: "
                    f"{valid_rows} valid bars, "
                    f"requires {required_rows}."
                )

            elif features.data_is_stale:
                quality_reason = (
                    "Feature freshness check failed."
                )

            else:
                quality_reason = (
                    "Feature data failed quality checks."
                )

            return self._reject(
                symbol=symbol,
                reason=quality_reason,
                gates=gates,
                warnings=warnings,
                metadata=self._merge_metadata(
                    base_metadata,
                    {
                        "feature_diagnostics": (
                            feature_diagnostics
                        )
                    },
                ),
            )

        gates["features"] = True

        # ====================================================
        # 3. OPTIONAL NATIVE JALWE EVIDENCE
        # ====================================================

        self._apply_news(features, warnings)
        self._apply_options(features, warnings)

        # Persist the complete feature state used by the decision.
        # LearningEngine later pairs closed PAPER trades with the
        # nearest snapshot around their entry time.
        try:
            database.save_feature_snapshot(
                symbol=symbol,
                snapshot=features,
            )
        except Exception as exc:
            warnings.append(
                f"Feature snapshot persistence failed: {exc}"
            )

        # ====================================================
        # 4. AI
        # ====================================================

        try:
            ai = self.ai_engine.evaluate(features)
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="AI analysis failed.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
            )

        ai_action = getattr(
            ai.action,
            "value",
            str(ai.action),
        )

        ai_buy = ai_action == "BUY"
        gates["ai"] = ai_buy

        # ====================================================
        # 5. OPPORTUNITY ENGINE
        # ====================================================

        try:
            opportunity = self.opportunity_engine.evaluate(features)
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Opportunity analysis failed.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
                ai_score=getattr(ai, "score", None),
            )

        gates["opportunity"] = bool(opportunity.approved)

        # ====================================================
        # 6. MARKET REGIME
        # ====================================================

        try:
            market = self.market_context_engine.get_regime_result()
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Market regime unavailable.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
                opportunity_score=opportunity.score,
                ai_score=getattr(ai, "score", None),
            )

        market_name = getattr(
            market.regime,
            "value",
            str(market.regime),
        )

        market_allowed = (
            market_name not in {"PANIC", "UNKNOWN"}
            and market.confidence >= 0.50
        )

        gates["market_regime"] = market_allowed

        # ====================================================
        # 7. STRATEGY ROUTER
        # ====================================================

        try:
            routed = self.strategy_router.route(
                opportunity,
                market,
            )
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Strategy routing failed.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
                market_regime=market_name,
                opportunity_score=opportunity.score,
                ai_score=getattr(ai, "score", None),
            )

        gates["strategy_router"] = bool(routed.approved)

        # ====================================================
        # EARLY QUALITY REJECTION
        # ====================================================

        if not opportunity.approved:
            return self._reject(
                symbol=symbol,
                reason="OpportunityEngine rejected the setup.",
                gates=gates,
                warnings=warnings + list(opportunity.warnings),
                metadata=base_metadata,
                market_regime=market_name,
                setup_grade=opportunity.grade.value,
                opportunity_score=opportunity.score,
                ai_score=getattr(ai, "score", None),
                entry_price=opportunity.trigger_price,
                stop_price=opportunity.invalidation_price,
            )

        if not routed.approved:
            return self._reject(
                symbol=symbol,
                reason="Market regime / strategy router rejected the setup.",
                gates=gates,
                warnings=warnings + list(routed.warnings),
                metadata=base_metadata,
                market_regime=market_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                ai_score=getattr(ai, "score", None),
                risk_pct=0.0,
                entry_price=routed.trigger_price,
                stop_price=routed.invalidation_price,
            )

        # ====================================================
        # 8. SESSION FEATURES
        # ====================================================

        try:
            session = self.session_feature_engine.build(
                symbol,
                bars,
            )
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Session feature analysis failed.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
                market_regime=market_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                ai_score=getattr(ai, "score", None),
            )

        # ====================================================
        # 9. SESSION STRATEGY
        # ====================================================

        try:
            session_strategy = self.session_strategy_engine.evaluate(
                features,
                session,
                bars,
            )
        except Exception as exc:
            return self._reject(
                symbol=symbol,
                reason="Session strategy analysis failed.",
                gates=gates,
                warnings=warnings + [str(exc)],
                metadata=base_metadata,
                market_regime=market_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                ai_score=getattr(ai, "score", None),
            )

        gates["session_strategy"] = bool(session_strategy.approved)

        session_strategy_name = None
        if session_strategy.strategy is not None:
            session_strategy_name = session_strategy.strategy.value

        if not session_strategy.approved:
            return self._reject(
                symbol=symbol,
                reason="No session strategy reached the required quality threshold.",
                gates=gates,
                warnings=warnings + list(session_strategy.warnings),
                metadata=base_metadata,
                market_regime=market_name,
                strategy=session_strategy_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                session_strategy_score=session_strategy.score,
                ai_score=getattr(ai, "score", None),
                risk_pct=0.0,
                entry_price=session_strategy.trigger_price,
                stop_price=session_strategy.invalidation_price,
            )

        # ====================================================
        # 10. TRIGGER
        # ====================================================

        trigger_current_price = (
            session.current_price
        )

        try:
            live_last_price = float(
                self.market_data.get_last_price(
                    symbol
                )
            )

            if live_last_price > 0:
                trigger_current_price = (
                    live_last_price
                )

                base_metadata[
                    "trigger_price_source"
                ] = "ALPACA_LAST_PRICE"

        except Exception as exc:
            warnings.append(
                "Latest-price trigger refresh failed; "
                "using session price: "
                + str(exc)
            )

            base_metadata[
                "trigger_price_source"
            ] = "SESSION_PRICE_FALLBACK"

        trigger = self.trigger_engine.evaluate(
            session_strategy,
            current_price=trigger_current_price,
            bars=bars,
        )

        gates["trigger"] = bool(trigger.approved_for_entry)

        if trigger.state in {
            TriggerState.SETUP_FOUND,
            TriggerState.WAITING_FOR_TRIGGER,
        }:
            return self._watch(
                symbol=symbol,
                reason=trigger.reason,
                gates=gates,
                warnings=warnings,
                metadata=base_metadata,
                market_regime=market_name,
                strategy=session_strategy_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                session_strategy_score=session_strategy.score,
                ai_score=getattr(ai, "score", None),
                risk_pct=routed.final_risk_pct,
                entry_price=trigger.trigger_price,
                stop_price=trigger.stop_price,
            )

        if not trigger.approved_for_entry:
            return self._reject(
                symbol=symbol,
                reason=trigger.reason,
                gates=gates,
                warnings=warnings,
                metadata=base_metadata,
                market_regime=market_name,
                strategy=session_strategy_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                session_strategy_score=session_strategy.score,
                ai_score=getattr(ai, "score", None),
                entry_price=trigger.trigger_price,
                stop_price=trigger.stop_price,
            )

        # ====================================================
        # 11. BREAKOUT CONFIRMATION
        # ====================================================

        breakout = self.breakout_engine.evaluate(
            trigger,
            bars,
            features,
        )

        gates["breakout_confirmation"] = bool(
            breakout.approved_for_entry
        )

        if not breakout.approved_for_entry:
            return self._reject(
                symbol=symbol,
                reason=breakout.reason,
                gates=gates,
                warnings=warnings + list(breakout.warnings),
                metadata=base_metadata,
                market_regime=market_name,
                strategy=session_strategy_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                session_strategy_score=session_strategy.score,
                breakout_score=breakout.score,
                ai_score=getattr(ai, "score", None),
                risk_pct=0.0,
                entry_price=breakout.close_price,
                stop_price=breakout.stop_price,
            )

        # ====================================================
        # 12. RISK ENGINE
        # ====================================================

        risk = self.risk_engine.evaluate(
            features=features,
            ai_analysis=ai,
            daily_start_equity=daily_start_equity,
            strategy_equity=strategy_equity,
            recommended_risk_pct=routed.final_risk_pct,
            setup_grade=routed.grade.value,
            confirmed_entry_price=breakout.close_price,
            confirmed_stop_price=breakout.stop_price,
            confirmation_approved=True,
        )

        gates["risk"] = bool(risk.approved)

        if not risk.approved:
            return self._reject(
                symbol=symbol,
                reason=risk.reason,
                gates=gates,
                warnings=warnings,
                metadata=self._merge_metadata(
                    base_metadata,
                    {"risk_metadata": risk.metadata},
                ),
                market_regime=market_name,
                strategy=session_strategy_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                session_strategy_score=session_strategy.score,
                breakout_score=breakout.score,
                ai_score=getattr(ai, "score", None),
                risk_pct=0.0,
                entry_price=breakout.close_price,
                stop_price=breakout.stop_price,
            )

        # ====================================================
        # 13. ALL NATIVE JALWE GATES MUST PASS
        # ====================================================

        all_required_gates = all(gates.values())

        if not all_required_gates:
            return self._reject(
                symbol=symbol,
                reason="One or more mandatory decision gates failed.",
                gates=gates,
                warnings=warnings,
                metadata=base_metadata,
                market_regime=market_name,
                strategy=session_strategy_name,
                setup_grade=routed.grade.value,
                opportunity_score=routed.routed_score,
                session_strategy_score=session_strategy.score,
                breakout_score=breakout.score,
                ai_score=getattr(ai, "score", None),
            )

        # ====================================================
        # READY FOR PAPER EXECUTION
        # External research did NOT create this state.
        # Every native JALWE gate above passed independently.
        # ====================================================

        return FinalTradeDecision(
            symbol=symbol,
            state=DecisionState.READY_FOR_PAPER_EXECUTION,
            ready_for_execution=True,
            market_regime=market_name,
            strategy=session_strategy_name,
            setup_grade=routed.grade.value,
            opportunity_score=routed.routed_score,
            session_strategy_score=session_strategy.score,
            breakout_score=breakout.score,
            ai_score=getattr(ai, "score", None),
            risk_pct=risk.risk_pct,
            quantity=risk.quantity,
            entry_price=breakout.close_price,
            stop_price=risk.stop_price,
            target_1=risk.target_1,
            target_2=risk.target_2,
            target_3=risk.target_3,
            reason="All native JALWE decision gates passed.",
            gates=gates,
            warnings=warnings,
            metadata=self._merge_metadata(
                base_metadata,
                {
                    "requested_risk_pct": routed.final_risk_pct,
                    "trigger_price": trigger.trigger_price,
                    "breakout_state": breakout.state.value,
                    "risk_metadata": risk.metadata,
                    "external_research_did_not_authorize_execution": True,
                },
            ),
        )


# ============================================================
# LAZY SINGLETON
# ============================================================

_decision_engine: Optional[DecisionEngine] = None


def get_decision_engine() -> DecisionEngine:
    global _decision_engine

    if _decision_engine is None:
        _decision_engine = DecisionEngine()

    return _decision_engine
