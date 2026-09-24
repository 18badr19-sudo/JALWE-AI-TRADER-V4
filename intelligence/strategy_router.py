from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from intelligence.opportunity_engine import (
    OpportunityAnalysis,
    OpportunityGrade,
    StrategyCandidate,
    StrategyName,
)
from market.market_regime import (
    MarketRegime,
    MarketRegimeResult,
)


@dataclass
class RoutedOpportunity:
    symbol: str

    approved: bool

    strategy: Optional[StrategyName]

    raw_score: float
    routed_score: float

    grade: OpportunityGrade

    original_risk_pct: float
    final_risk_pct: float

    market_regime: MarketRegime
    regime_confidence: float

    trigger_price: Optional[float]
    invalidation_price: Optional[float]

    reasons: list[str] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )

    candidates: list[
        StrategyCandidate
    ] = field(
        default_factory=list
    )


class StrategyRouter:
    """
    JALWE V4 - Strategy Router

    OpportunityEngine finds valid setups.

    StrategyRouter decides whether an already-valid
    setup is suitable for the CURRENT market regime.

    Safety rules:
    - Does NOT place orders.
    - Cannot revive a rejected opportunity.
    - Cannot increase risk above the original risk.
    - Can reduce risk.
    - Can completely block trading.
    - PANIC / UNKNOWN block new entries.
    - RiskEngine remains final authority.
    """

    def __init__(self) -> None:
        pass

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _clamp_score(
        score: float,
    ) -> float:

        return max(
            0.0,
            min(
                100.0,
                float(score),
            ),
        )

    @staticmethod
    def _grade(
        score: float,
    ) -> OpportunityGrade:

        if score >= 90:
            return OpportunityGrade.A_PLUS

        if score >= 82:
            return OpportunityGrade.A

        if score >= 74:
            return OpportunityGrade.B

        return OpportunityGrade.REJECT

    @staticmethod
    def _base_risk_for_grade(
        grade: OpportunityGrade,
    ) -> float:

        if grade == OpportunityGrade.A_PLUS:
            return 1.50

        if grade == OpportunityGrade.A:
            return 1.00

        if grade == OpportunityGrade.B:
            return 0.50

        return 0.0

    @staticmethod
    def _candidate_copy(
        candidate: StrategyCandidate,
        score: float,
    ) -> StrategyCandidate:

        return StrategyCandidate(
            strategy=candidate.strategy,

            score=score,

            trigger_price=(
                candidate.trigger_price
            ),

            invalidation_price=(
                candidate.invalidation_price
            ),

            reasons=list(
                candidate.reasons
            ),

            warnings=list(
                candidate.warnings
            ),

            metadata=dict(
                candidate.metadata
            ),
        )

    # ========================================================
    # MARKET REGIME SETTINGS
    # ========================================================

    def _settings_for_regime(
        self,
        result: MarketRegimeResult,
    ) -> dict:

        regime = result.regime

        # ----------------------------------------------------
        # PANIC
        # ----------------------------------------------------

        if regime == MarketRegime.PANIC:

            return {
                "allow_entries": False,

                "minimum_score": 101.0,

                "risk_multiplier": 0.0,

                "adjustments": {},

                "reason": (
                    "PANIC regime: new long entries "
                    "are disabled."
                ),
            }

        # ----------------------------------------------------
        # UNKNOWN
        # ----------------------------------------------------

        if regime == MarketRegime.UNKNOWN:

            return {
                "allow_entries": False,

                "minimum_score": 101.0,

                "risk_multiplier": 0.0,

                "adjustments": {},

                "reason": (
                    "Market regime is unknown."
                ),
            }

        # ----------------------------------------------------
        # BULL TREND
        # ----------------------------------------------------

        if regime == MarketRegime.BULL_TREND:

            return {
                "allow_entries": True,

                "minimum_score": 74.0,

                "risk_multiplier": 1.0,

                "adjustments": {
                    StrategyName.MOMENTUM_BREAKOUT: 6.0,
                    StrategyName.PULLBACK_CONTINUATION: 8.0,
                    StrategyName.CATALYST_BREAKOUT: 5.0,
                    StrategyName.VWAP_RECLAIM: 3.0,
                },

                "reason": (
                    "Bull trend supports long "
                    "momentum and continuation setups."
                ),
            }

        # ----------------------------------------------------
        # SIDEWAYS
        # ----------------------------------------------------

        if regime == MarketRegime.SIDEWAYS:

            return {
                "allow_entries": True,

                "minimum_score": 82.0,

                "risk_multiplier": 0.70,

                "adjustments": {
                    StrategyName.MOMENTUM_BREAKOUT: -12.0,
                    StrategyName.PULLBACK_CONTINUATION: -5.0,
                    StrategyName.CATALYST_BREAKOUT: 0.0,
                    StrategyName.VWAP_RECLAIM: 8.0,
                },

                "reason": (
                    "Sideways market favors selective "
                    "VWAP setups and penalizes "
                    "breakout chasing."
                ),
            }

        # ----------------------------------------------------
        # BEAR TREND
        # ----------------------------------------------------

        if regime == MarketRegime.BEAR_TREND:

            return {
                "allow_entries": True,

                # JALWE currently trades LONG setups.
                # Long entries must be exceptional
                # during bearish market conditions.
                "minimum_score": 90.0,

                "risk_multiplier": 0.50,

                "adjustments": {
                    StrategyName.MOMENTUM_BREAKOUT: -18.0,
                    StrategyName.PULLBACK_CONTINUATION: -15.0,
                    StrategyName.CATALYST_BREAKOUT: 2.0,
                    StrategyName.VWAP_RECLAIM: -5.0,
                },

                "reason": (
                    "Bear trend requires exceptional "
                    "long setups and reduced risk."
                ),
            }

        # ----------------------------------------------------
        # HIGH VOLATILITY
        # ----------------------------------------------------

        if regime == MarketRegime.HIGH_VOLATILITY:

            trend_state = (
                result.trend_state
                or "UNKNOWN"
            ).upper()

            # -----------------------------
            # HIGH VOL + BULLISH
            # -----------------------------

            if trend_state == "BULLISH":

                return {
                    "allow_entries": True,

                    "minimum_score": 86.0,

                    "risk_multiplier": 0.60,

                    "adjustments": {
                        StrategyName.MOMENTUM_BREAKOUT: -7.0,
                        StrategyName.PULLBACK_CONTINUATION: 5.0,
                        StrategyName.CATALYST_BREAKOUT: 2.0,
                        StrategyName.VWAP_RECLAIM: 3.0,
                    },

                    "reason": (
                        "High volatility with bullish "
                        "trend: prefer pullbacks and "
                        "confirmed reclaims."
                    ),
                }

            # -----------------------------
            # HIGH VOL + BEARISH
            # -----------------------------

            if trend_state == "BEARISH":

                return {
                    "allow_entries": True,

                    "minimum_score": 92.0,

                    "risk_multiplier": 0.40,

                    "adjustments": {
                        StrategyName.MOMENTUM_BREAKOUT: -20.0,
                        StrategyName.PULLBACK_CONTINUATION: -15.0,
                        StrategyName.CATALYST_BREAKOUT: 0.0,
                        StrategyName.VWAP_RECLAIM: -8.0,
                    },

                    "reason": (
                        "High volatility with bearish "
                        "trend: only exceptional long "
                        "setups are permitted."
                    ),
                }

            # -----------------------------
            # HIGH VOL + MIXED
            # -----------------------------

            return {
                "allow_entries": True,

                "minimum_score": 90.0,

                "risk_multiplier": 0.50,

                "adjustments": {
                    StrategyName.MOMENTUM_BREAKOUT: -15.0,
                    StrategyName.PULLBACK_CONTINUATION: -5.0,
                    StrategyName.CATALYST_BREAKOUT: 0.0,
                    StrategyName.VWAP_RECLAIM: 2.0,
                },

                "reason": (
                    "High volatility: reduced risk "
                    "and stricter confirmation."
                ),
            }

        # ----------------------------------------------------
        # FALLBACK
        # ----------------------------------------------------

        return {
            "allow_entries": False,

            "minimum_score": 101.0,

            "risk_multiplier": 0.0,

            "adjustments": {},

            "reason": (
                "Unsupported market regime."
            ),
        }

    # ========================================================
    # ROUTE OPPORTUNITY
    # ========================================================

    def route(
        self,
        opportunity: OpportunityAnalysis,
        market: MarketRegimeResult,
    ) -> RoutedOpportunity:

        settings = (
            self._settings_for_regime(
                market
            )
        )

        reasons = list(
            opportunity.reasons
        )

        warnings = list(
            opportunity.warnings
        )

        reasons.append(
            settings["reason"]
        )

        # ====================================================
        # CRITICAL SAFETY GATE
        # ====================================================
        #
        # StrategyRouter is NOT allowed to turn a rejected
        # OpportunityEngine setup into a valid trade.
        #
        # Example:
        #
        # Opportunity score = 57
        # Bull market adjustment = +8
        #
        # It must remain rejected.
        #
        # Market context can improve/rank an already-approved
        # setup, but it cannot create a setup from nothing.
        # ====================================================

        if not opportunity.approved:

            warnings.append(
                "OpportunityEngine rejected the setup. "
                "Market regime cannot revive a rejected setup."
            )

            return RoutedOpportunity(
                symbol=opportunity.symbol,

                approved=False,

                strategy=None,

                raw_score=opportunity.score,

                routed_score=opportunity.score,

                grade=OpportunityGrade.REJECT,

                original_risk_pct=(
                    opportunity.recommended_risk_pct
                ),

                final_risk_pct=0.0,

                market_regime=(
                    market.regime
                ),

                regime_confidence=(
                    market.confidence
                ),

                trigger_price=(
                    opportunity.trigger_price
                ),

                invalidation_price=(
                    opportunity.invalidation_price
                ),

                reasons=reasons,

                warnings=warnings,

                candidates=list(
                    opportunity.candidates
                ),
            )

        # ====================================================
        # ORIGINAL RISK SAFETY
        # ====================================================

        if (
            opportunity.recommended_risk_pct
            is None
            or opportunity.recommended_risk_pct <= 0
        ):

            warnings.append(
                "Opportunity has no approved risk allocation."
            )

            return RoutedOpportunity(
                symbol=opportunity.symbol,

                approved=False,

                strategy=None,

                raw_score=opportunity.score,

                routed_score=opportunity.score,

                grade=OpportunityGrade.REJECT,

                original_risk_pct=0.0,
                final_risk_pct=0.0,

                market_regime=(
                    market.regime
                ),

                regime_confidence=(
                    market.confidence
                ),

                trigger_price=(
                    opportunity.trigger_price
                ),

                invalidation_price=(
                    opportunity.invalidation_price
                ),

                reasons=reasons,

                warnings=warnings,

                candidates=list(
                    opportunity.candidates
                ),
            )

        # ====================================================
        # REGIME CONFIDENCE SAFETY
        # ====================================================

        if market.confidence < 0.50:

            warnings.append(
                "Market regime confidence is too low."
            )

            return RoutedOpportunity(
                symbol=opportunity.symbol,

                approved=False,

                strategy=None,

                raw_score=opportunity.score,

                routed_score=0.0,

                grade=OpportunityGrade.REJECT,

                original_risk_pct=(
                    opportunity.recommended_risk_pct
                ),

                final_risk_pct=0.0,

                market_regime=(
                    market.regime
                ),

                regime_confidence=(
                    market.confidence
                ),

                trigger_price=(
                    opportunity.trigger_price
                ),

                invalidation_price=(
                    opportunity.invalidation_price
                ),

                reasons=reasons,

                warnings=warnings,

                candidates=[],
            )

        # ====================================================
        # MARKET REGIME ENTRY BLOCK
        # ====================================================

        if not settings["allow_entries"]:

            warnings.append(
                "New entries blocked by "
                "market regime."
            )

            return RoutedOpportunity(
                symbol=opportunity.symbol,

                approved=False,

                strategy=None,

                raw_score=opportunity.score,

                routed_score=0.0,

                grade=OpportunityGrade.REJECT,

                original_risk_pct=(
                    opportunity.recommended_risk_pct
                ),

                final_risk_pct=0.0,

                market_regime=(
                    market.regime
                ),

                regime_confidence=(
                    market.confidence
                ),

                trigger_price=(
                    opportunity.trigger_price
                ),

                invalidation_price=(
                    opportunity.invalidation_price
                ),

                reasons=reasons,

                warnings=warnings,

                candidates=[],
            )

        # ====================================================
        # ADJUST EACH STRATEGY FOR CURRENT MARKET
        # ====================================================

        routed_candidates: list[
            StrategyCandidate
        ] = []

        adjustments = settings[
            "adjustments"
        ]

        for candidate in opportunity.candidates:

            adjustment = float(
                adjustments.get(
                    candidate.strategy,
                    0.0,
                )
            )

            adjusted_score = (
                self._clamp_score(
                    candidate.score
                    + adjustment
                )
            )

            copied_candidate = (
                self._candidate_copy(
                    candidate,
                    adjusted_score,
                )
            )

            copied_candidate.metadata[
                "raw_strategy_score"
            ] = candidate.score

            copied_candidate.metadata[
                "market_regime"
            ] = market.regime.value

            copied_candidate.metadata[
                "regime_confidence"
            ] = market.confidence

            copied_candidate.metadata[
                "regime_adjustment"
            ] = adjustment

            copied_candidate.metadata[
                "routed_strategy_score"
            ] = adjusted_score

            routed_candidates.append(
                copied_candidate
            )

        routed_candidates.sort(
            key=lambda item: item.score,
            reverse=True,
        )

        # ====================================================
        # NO STRATEGY CANDIDATES
        # ====================================================

        if not routed_candidates:

            warnings.append(
                "No strategy candidates available."
            )

            return RoutedOpportunity(
                symbol=opportunity.symbol,

                approved=False,

                strategy=None,

                raw_score=opportunity.score,

                routed_score=0.0,

                grade=OpportunityGrade.REJECT,

                original_risk_pct=(
                    opportunity.recommended_risk_pct
                ),

                final_risk_pct=0.0,

                market_regime=(
                    market.regime
                ),

                regime_confidence=(
                    market.confidence
                ),

                trigger_price=None,
                invalidation_price=None,

                reasons=reasons,

                warnings=warnings,

                candidates=[],
            )

        # ====================================================
        # BEST STRATEGY AFTER REGIME ADJUSTMENT
        # ====================================================

        best = routed_candidates[0]

        routed_score = float(
            best.score
        )

        routed_grade = self._grade(
            routed_score
        )

        minimum_score = float(
            settings["minimum_score"]
        )

        approved = (
            routed_score >= minimum_score
            and routed_grade
            != OpportunityGrade.REJECT
        )

        # ====================================================
        # CALCULATE FINAL RISK
        # ====================================================

        final_risk = 0.0

        if approved:

            grade_risk = (
                self._base_risk_for_grade(
                    routed_grade
                )
            )

            risk_multiplier = float(
                settings[
                    "risk_multiplier"
                ]
            )

            regime_adjusted_risk = (
                grade_risk
                * risk_multiplier
            )

            # ------------------------------------------------
            # CRITICAL:
            #
            # StrategyRouter can NEVER increase risk above
            # the OpportunityEngine recommendation.
            # ------------------------------------------------

            final_risk = min(
                regime_adjusted_risk,
                float(
                    opportunity
                    .recommended_risk_pct
                ),
            )

            # ------------------------------------------------
            # SYSTEM HARD CEILING
            # ------------------------------------------------
            #
            # Default A+ max = 1.5%.
            #
            # The future exceptional 2% mode will require
            # a separate explicit RiskEngine approval.
            # ------------------------------------------------

            final_risk = min(
                final_risk,
                1.50,
            )

            final_risk = round(
                max(
                    0.0,
                    final_risk,
                ),
                2,
            )

            # No executable trade with zero risk allocation.
            if final_risk <= 0:

                approved = False

                warnings.append(
                    "Final risk allocation is zero."
                )

        # ====================================================
        # REJECTED BY REGIME THRESHOLD
        # ====================================================

        if not approved:

            final_risk = 0.0

            warnings.append(
                (
                    "Best strategy did not meet "
                    f"regime minimum score "
                    f"{minimum_score:.0f}."
                )
            )

        # ====================================================
        # FINAL RESULT
        # ====================================================

        return RoutedOpportunity(
            symbol=opportunity.symbol,

            approved=approved,

            strategy=(
                best.strategy
                if approved
                else None
            ),

            raw_score=opportunity.score,

            routed_score=round(
                routed_score,
                2,
            ),

            grade=(
                routed_grade
                if approved
                else OpportunityGrade.REJECT
            ),

            original_risk_pct=(
                opportunity.recommended_risk_pct
            ),

            final_risk_pct=(
                final_risk
            ),

            market_regime=(
                market.regime
            ),

            regime_confidence=(
                market.confidence
            ),

            trigger_price=(
                best.trigger_price
            ),

            invalidation_price=(
                best.invalidation_price
            ),

            reasons=reasons,

            warnings=warnings,

            candidates=routed_candidates,
        )


# ============================================================
# LAZY SINGLETON
# ============================================================

_strategy_router: Optional[
    StrategyRouter
] = None


def get_strategy_router(
) -> StrategyRouter:

    global _strategy_router

    if _strategy_router is None:
        _strategy_router = (
            StrategyRouter()
        )

    return _strategy_router