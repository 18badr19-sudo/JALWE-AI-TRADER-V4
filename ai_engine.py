from __future__ import annotations

import logging

from typing import Any, Optional


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# APEX AI ENGINE V3
# ============================================================

class AIEngine:
    """
    APEX AI ENGINE V3

    ROLE:
        Combine research evidence from:

            Scanner / Radar
                ↓
            PreBreakout
                ↓
            News
                ↓
            Liquidity
                ↓
            AI Research Aggregator
                ↓
            JALWE

    IMPORTANT:

        This engine NEVER:
            - buys
            - sells
            - submits orders
            - manages positions

        It does NOT output BUY / SELL.

        It produces research priority only:

            REJECT
            WATCH
            RESEARCH_CANDIDATE
            HIGH_PRIORITY_RESEARCH

        JALWE remains the final decision-maker.
    """

    VERSION = "3.0"

    # ========================================================
    # BASE WEIGHTS
    # ========================================================

    PREBREAKOUT_WEIGHT = 0.50
    LIQUIDITY_WEIGHT = 0.35
    NEWS_WEIGHT = 0.15

    # ========================================================
    # RESEARCH THRESHOLDS
    # ========================================================

    WATCH_THRESHOLD = 45.0

    RESEARCH_THRESHOLD = 60.0

    HIGH_PRIORITY_THRESHOLD = 75.0

    MIN_CONFIDENCE_FOR_RESEARCH = 60.0

    MIN_CONFIDENCE_HIGH_PRIORITY = 75.0

    # ========================================================
    # DATA QUALITY / SAFETY CAPS
    # ========================================================
    NO_NEWS_CONFIDENCE_CAP = 85.0
    LOW_DOLLAR_VOLUME_PENALTY = 12.0
    MAX_ALIGNMENT_BONUS_WITH_LOW_DOLLAR_VOLUME = 5.0
    MIN_LIQUIDITY_SCORE_HIGH_PRIORITY = 55.0


    # ========================================================
    # INIT
    # ========================================================

    def __init__(
        self,
        *,
        watch_threshold: Optional[float] = None,
        research_threshold: Optional[float] = None,
        high_priority_threshold: Optional[float] = None,
    ) -> None:

        self.enabled = True

        self.watch_threshold = float(
            watch_threshold
            if watch_threshold is not None
            else self.WATCH_THRESHOLD
        )

        self.research_threshold = float(
            research_threshold
            if research_threshold is not None
            else self.RESEARCH_THRESHOLD
        )

        self.high_priority_threshold = float(
            high_priority_threshold
            if high_priority_threshold is not None
            else self.HIGH_PRIORITY_THRESHOLD
        )


    # ========================================================
    # GENERIC VALUE ACCESS
    # Supports dicts + dataclasses/objects
    # ========================================================

    @staticmethod
    def _get(
        source: Any,
        key: str,
        default: Any = None,
    ) -> Any:

        if source is None:

            return default

        if isinstance(
            source,
            dict,
        ):

            return source.get(
                key,
                default,
            )

        return getattr(
            source,
            key,
            default,
        )


    # ========================================================
    # NUMERIC HELPERS
    # ========================================================

    @staticmethod
    def _float(
        value: Any,
        default: float = 0.0,
    ) -> float:

        try:

            if value is None:

                return float(
                    default
                )

            return float(
                value
            )

        except (
            TypeError,
            ValueError,
        ):

            return float(
                default
            )


    @staticmethod
    def _bounded(
        value: float,
        minimum: float = 0.0,
        maximum: float = 100.0,
    ) -> float:

        return max(
            minimum,
            min(
                float(value),
                maximum,
            ),
        )


    # ========================================================
    # LIST HELPERS
    # ========================================================

    @staticmethod
    def _list(
        value: Any,
    ) -> list:

        if value is None:

            return []

        if isinstance(
            value,
            list,
        ):

            return value

        if isinstance(
            value,
            tuple,
        ):

            return list(
                value
            )

        if isinstance(
            value,
            set,
        ):

            return list(
                value
            )

        return [
            value
        ]


    @staticmethod
    def _deduplicate(
        values: list,
    ) -> list:

        output = []

        seen = set()

        for value in values:

            text = str(
                value
            )

            if text in seen:

                continue

            seen.add(
                text
            )

            output.append(
                value
            )

        return output


    # ========================================================
    # PREBREAKOUT DATA
    # ========================================================

    def _extract_prebreakout(
        self,
        result: Any,
    ) -> dict[str, Any]:

        if result is None:

            return {
                "available": False,
                "score": None,
                "confidence": 0.0,
                "status": "UNAVAILABLE",
                "warnings": [],
                "reasons": [],
            }

        score = self._get(
            result,
            "score",
            None,
        )

        confidence = self._float(
            self._get(
                result,
                "data_confidence",
                0.0,
            )
        )

        status = str(
            self._get(
                result,
                "status",
                "UNKNOWN",
            )
        )

        warnings = self._list(
            self._get(
                result,
                "warnings",
                [],
            )
        )

        reasons = self._list(
            self._get(
                result,
                "reasons",
                [],
            )
        )

        return {

            "available":
                score is not None,

            "score":
                (
                    self._bounded(
                        self._float(
                            score
                        )
                    )
                    if score is not None
                    else None
                ),

            "confidence":
                self._bounded(
                    confidence
                ),

            "status":
                status,

            "warnings":
                warnings,

            "reasons":
                reasons,

            "score_5m":
                self._get(
                    result,
                    "score_5m",
                    None,
                ),

            "score_30m":
                self._get(
                    result,
                    "score_30m",
                    None,
                ),

            "score_1h":
                self._get(
                    result,
                    "score_1h",
                    None,
                ),

            "score_1d":
                self._get(
                    result,
                    "score_1d",
                    None,
                ),
        }


    # ========================================================
    # NEWS DATA
    # ========================================================

    def _extract_news(
        self,
        result: Any,
    ) -> dict[str, Any]:

        if result is None:

            return {
                "available": False,
                "score": None,
                "confidence": 0.0,
                "sentiment": "UNKNOWN",
                "status": "UNAVAILABLE",
                "catalysts": [],
                "risk_flags": [],
            }

        status = str(
            self._get(
                result,
                "status",
                "UNKNOWN",
            )
        ).upper()

        score = self._get(
            result,
            "news_score",
            None,
        )

        confidence = self._float(
            self._get(
                result,
                "confidence",
                0.0,
            )
        )

        # News confidence is 0 -> 1
        if confidence <= 1.0:

            confidence *= 100.0

        sentiment = str(
            self._get(
                result,
                "sentiment",
                "UNKNOWN",
            )
        ).upper()

        catalysts = self._list(
            self._get(
                result,
                "catalysts",
                [],
            )
        )

        risk_flags = self._list(
            self._get(
                result,
                "risk_flags",
                [],
            )
        )

        # ----------------------------------------------------
        # No news is not bearish.
        # We simply exclude news from weighted score.
        # ----------------------------------------------------

        valid_news = bool(

            status == "SUCCESS"

            and

            score is not None
        )

        return {

            "available":
                valid_news,

            "score":
                (
                    self._bounded(
                        self._float(
                            score
                        )
                    )
                    if valid_news
                    else None
                ),

            "confidence":
                self._bounded(
                    confidence
                ),

            "sentiment":
                sentiment,

            "status":
                status,

            "catalysts":
                catalysts,

            "risk_flags":
                risk_flags,
        }


    # ========================================================
    # LIQUIDITY DATA
    # ========================================================

    def _extract_liquidity(
        self,
        result: Any,
    ) -> dict[str, Any]:

        if result is None:

            return {
                "available": False,
                "score": None,
                "confidence": 0.0,
                "bias": "UNKNOWN",
                "risk_flags": [],
                "reasons": [],
            }

        status = str(
            self._get(
                result,
                "status",
                "UNKNOWN",
            )
        ).upper()

        score = self._get(
            result,
            "liquidity_score",
            None,
        )

        confidence = self._float(
            self._get(
                result,
                "confidence",
                0.0,
            )
        )

        # Liquidity confidence = 0 -> 1
        if confidence <= 1.0:

            confidence *= 100.0

        bias = str(
            self._get(
                result,
                "liquidity_bias",
                "UNKNOWN",
            )
        ).upper()

        risk_flags = self._list(
            self._get(
                result,
                "risk_flags",
                [],
            )
        )

        reasons = self._list(
            self._get(
                result,
                "reasons",
                [],
            )
        )

        available = bool(

            status == "SUCCESS"

            and

            score is not None
        )

        return {

            "available":
                available,

            "score":
                (
                    self._bounded(
                        self._float(
                            score
                        )
                    )
                    if available
                    else None
                ),

            "confidence":
                self._bounded(
                    confidence
                ),

            "bias":
                bias,

            "buy_pressure":
                self._float(
                    self._get(
                        result,
                        "buy_pressure",
                        0.0,
                    )
                ),

            "sell_pressure":
                self._float(
                    self._get(
                        result,
                        "sell_pressure",
                        0.0,
                    )
                ),

            "money_flow":
                self._float(
                    self._get(
                        result,
                        "money_flow",
                        0.0,
                    )
                ),

            "volume_acceleration":
                self._float(
                    self._get(
                        result,
                        "volume_acceleration",
                        0.0,
                    )
                ),

            "price_volume_confirmation":
                bool(
                    self._get(
                        result,
                        "price_volume_confirmation",
                        False,
                    )
                ),

            "absorption":
                bool(
                    self._get(
                        result,
                        "absorption",
                        False,
                    )
                ),

            "distribution":
                bool(
                    self._get(
                        result,
                        "distribution",
                        False,
                    )
                ),

            "risk_flags":
                risk_flags,

            "reasons":
                reasons,
        }


    # ========================================================
    # WEIGHTED RESEARCH SCORE
    # ========================================================

    def _weighted_score(
        self,
        *,
        pre: dict[str, Any],
        news: dict[str, Any],
        liquidity: dict[str, Any],
    ) -> tuple[
        float,
        dict[str, float],
    ]:

        components = []

        component_weights = {}

        if pre[
            "available"
        ]:

            components.append(

                (
                    float(
                        pre["score"]
                    ),

                    self.PREBREAKOUT_WEIGHT,
                )
            )

            component_weights[
                "prebreakout"
            ] = (
                self.PREBREAKOUT_WEIGHT
            )

        if liquidity[
            "available"
        ]:

            components.append(

                (
                    float(
                        liquidity["score"]
                    ),

                    self.LIQUIDITY_WEIGHT,
                )
            )

            component_weights[
                "liquidity"
            ] = (
                self.LIQUIDITY_WEIGHT
            )

        if news[
            "available"
        ]:

            components.append(

                (
                    float(
                        news["score"]
                    ),

                    self.NEWS_WEIGHT,
                )
            )

            component_weights[
                "news"
            ] = (
                self.NEWS_WEIGHT
            )

        total_weight = sum(

            weight

            for (
                _,
                weight,
            ) in components
        )

        if total_weight <= 0:

            return (
                0.0,
                component_weights,
            )

        score = (

            sum(

                component_score
                *
                weight

                for (
                    component_score,
                    weight,
                ) in components
            )

            /

            total_weight
        )

        return (

            round(
                self._bounded(
                    score
                ),
                2,
            ),

            component_weights,
        )


    # ========================================================
    # RESEARCH CONFIDENCE
    # ========================================================

    def _research_confidence(
        self,
        *,
        pre: dict[str, Any],
        news: dict[str, Any],
        liquidity: dict[str, Any],
    ) -> float:

        components = []

        if pre[
            "available"
        ]:

            components.append(

                (
                    pre[
                        "confidence"
                    ],

                    self.PREBREAKOUT_WEIGHT,
                )
            )

        if liquidity[
            "available"
        ]:

            components.append(

                (
                    liquidity[
                        "confidence"
                    ],

                    self.LIQUIDITY_WEIGHT,
                )
            )

        if news[
            "available"
        ]:

            components.append(

                (
                    news[
                        "confidence"
                    ],

                    self.NEWS_WEIGHT,
                )
            )

        total_weight = sum(

            weight

            for (
                _,
                weight,
            ) in components
        )

        if total_weight <= 0:

            return 0.0

        confidence = (

            sum(

                value
                *
                weight

                for (
                    value,
                    weight,
                ) in components
            )

            /

            total_weight
        )

        return round(
            self._bounded(
                confidence
            ),
            2,
        )


    # ========================================================
    # RISK PENALTIES
    # ========================================================

    @staticmethod
    def _risk_penalty(
        risk_flags: list[str],
    ) -> tuple[
        float,
        list[str],
    ]:

        penalties = {

            "BANKRUPTCY_RISK":
                35.0,

            "DELISTING_RISK":
                30.0,

            "DILUTION_RISK":
                22.0,

            "DILUTION_OR_OFFERING_RISK":
                22.0,

            "REVERSE_SPLIT_RISK":
                18.0,

            "STRONG_SELL_PRESSURE":
                18.0,

            "STRONG_NEGATIVE_MONEY_FLOW":
                18.0,

            "DISTRIBUTION_PROXY_DETECTED":
                16.0,

            "MULTI_TIMEFRAME_SELLING_PRESSURE":
                14.0,

            "NEGATIVE_NEWS_DOMINANCE":
                12.0,

            "BEARISH_PRICE_FLOW_CONFIRMATION":
                12.0,

            "SELL_PRESSURE_DOMINANT":
                10.0,

            "NEGATIVE_MONEY_FLOW":
                10.0,

            "RECALL_RISK":
                10.0,

            "LEGAL_CATALYST":
                8.0,

            "NASDAQ_COMPLIANCE_RISK":
                10.0,

            "WARRANT_DILUTION_RISK":
                8.0,

            "EXTREME_VOLUME_WITHOUT_PRICE_CONFIRMATION":
                7.0,

            "LIQUIDITY_TIMEFRAME_CONFLICT":
                5.0,

            "LOW_RECENT_DOLLAR_VOLUME":
                AIEngine.LOW_DOLLAR_VOLUME_PENALTY,

            "VOLUME_DECELERATION":
                4.0,

            "LOW_LIQUIDITY_DATA_CONFIDENCE":
                8.0,
        }

        penalty = 0.0

        applied = []

        for flag in risk_flags:

            value = penalties.get(
                str(flag),
                0.0,
            )

            if value <= 0:

                continue

            penalty += (
                value
            )

            applied.append(

                f"{flag}:-{value:.0f}"
            )

        # Do not allow penalties to exceed 60 points.
        penalty = min(
            penalty,
            60.0,
        )

        return (
            penalty,
            applied,
        )


    # ========================================================
    # PREBREAKOUT WARNING PENALTIES
    # ========================================================

    @staticmethod
    def _prebreakout_penalty(
        warnings: list[str],
    ) -> tuple[
        float,
        list[str],
    ]:

        penalty = 0.0

        applied = []

        for warning in warnings:

            text = str(
                warning
            ).upper()

            amount = 0.0

            if (
                "EXTREME_EXTENSION"
                in text
            ):

                amount = 15.0

            elif (
                "EXTENDED_MOVE"
                in text
            ):

                amount = 10.0

            elif (
                "DAILY_TREND_CONFLICT"
                in text
            ):

                amount = 6.0

            elif (
                "1H BEARISH"
                in text
            ):

                amount = 5.0

            elif (
                "30M BEARISH"
                in text
            ):

                amount = 4.0

            elif (
                "LOW_DATA_CONFIDENCE"
                in text
            ):

                amount = 8.0

            if amount > 0:

                penalty += (
                    amount
                )

                applied.append(

                    f"{warning}:-{amount:.0f}"
                )

        penalty = min(
            penalty,
            30.0,
        )

        return (
            penalty,
            applied,
        )


    # ========================================================
    # POSITIVE ALIGNMENT BONUS
    # ========================================================

    @staticmethod
    def _alignment_bonus(
        *,
        pre: dict[str, Any],
        news: dict[str, Any],
        liquidity: dict[str, Any],
    ) -> tuple[
        float,
        list[str],
    ]:

        bonus = 0.0

        evidence = []

        # ----------------------------------------------------
        # Strong technical + liquidity alignment
        # ----------------------------------------------------

        if (
            pre[
                "available"
            ]

            and

            liquidity[
                "available"
            ]

            and

            pre[
                "score"
            ]
            >= 55

            and

            liquidity[
                "score"
            ]
            >= 65

            and

            liquidity[
                "bias"
            ]
            ==
            "BULLISH"
        ):

            bonus += 6.0

            evidence.append(
                "PREBREAKOUT_LIQUIDITY_ALIGNMENT"
            )

        # ----------------------------------------------------
        # Price confirms flow
        # ----------------------------------------------------

        if liquidity.get(
            "price_volume_confirmation"
        ):

            bonus += 4.0

            evidence.append(
                "PRICE_VOLUME_CONFIRMED"
            )

        # ----------------------------------------------------
        # Absorption proxy
        # ----------------------------------------------------

        if liquidity.get(
            "absorption"
        ):

            bonus += 3.0

            evidence.append(
                "BULLISH_ABSORPTION_PROXY"
            )

        # ----------------------------------------------------
        # Positive flow
        # ----------------------------------------------------

        if (
            liquidity.get(
                "money_flow",
                0.0,
            )
            >= 20.0
        ):

            bonus += 3.0

            evidence.append(
                "STRONG_POSITIVE_FLOW"
            )

        # ----------------------------------------------------
        # Bullish recent news
        # ----------------------------------------------------

        if (
            news[
                "available"
            ]

            and

            news[
                "sentiment"
            ]
            ==
            "BULLISH"

            and

            news[
                "confidence"
            ]
            >= 40.0
        ):

            bonus += 4.0

            evidence.append(
                "BULLISH_NEWS_ALIGNMENT"
            )

        # ----------------------------------------------------
        # Positive catalyst
        # ----------------------------------------------------

        positive_catalysts = {

            "FDA",
            "APPROVAL",
            "EARNINGS",
            "GUIDANCE",
            "M&A",
            "CONTRACT",
            "PARTNERSHIP",
            "PRODUCT_LAUNCH",
        }

        catalyst_set = set(

            str(
                item
            ).upper()

            for item
            in news.get(
                "catalysts",
                []
            )
        )

        if (
            catalyst_set

            &

            positive_catalysts
        ):

            bonus += 3.0

            evidence.append(
                "POSITIVE_CATALYST"
            )

        return (
            min(
                bonus,
                15.0,
            ),
            evidence,
        )


    # ========================================================
    # MARKET / RESEARCH BIAS
    # ========================================================

    @staticmethod
    def _final_bias(
        *,
        final_score: float,
        liquidity_bias: str,
        sentiment: str,
    ) -> str:

        bullish_points = 0

        bearish_points = 0

        if final_score >= 65:

            bullish_points += 2

        elif final_score <= 35:

            bearish_points += 2

        if liquidity_bias == "BULLISH":

            bullish_points += 1

        elif liquidity_bias == "BEARISH":

            bearish_points += 1

        if sentiment == "BULLISH":

            bullish_points += 1

        elif sentiment == "BEARISH":

            bearish_points += 1

        if bullish_points >= 3:

            return "BULLISH"

        if bearish_points >= 3:

            return "BEARISH"

        return "MIXED"


    # ========================================================
    # VERDICT
    # ========================================================

    def _verdict(
        self,
        *,
        final_score: float,
        confidence: float,
        critical_risk: bool,
    ) -> str:

        if critical_risk:

            return "REJECT"

        if (
            final_score
            >=
            self.high_priority_threshold

            and

            confidence
            >=
            self.MIN_CONFIDENCE_HIGH_PRIORITY
        ):

            return (
                "HIGH_PRIORITY_RESEARCH"
            )

        if (
            final_score
            >=
            self.research_threshold

            and

            confidence
            >=
            self.MIN_CONFIDENCE_FOR_RESEARCH
        ):

            return (
                "RESEARCH_CANDIDATE"
            )

        if (
            final_score
            >=
            self.watch_threshold
        ):

            return "WATCH"

        return "REJECT"


    # ========================================================
    # CRITICAL RISKS
    # ========================================================

    @staticmethod
    def _critical_risk(
        risk_flags: list[str],
    ) -> bool:

        critical = {

            "BANKRUPTCY_RISK",

            "DELISTING_RISK",

            "STRONG_SELL_PRESSURE",

            "STRONG_NEGATIVE_MONEY_FLOW",

            "DISTRIBUTION_PROXY_DETECTED",
        }

        return any(

            str(flag)
            in critical

            for flag
            in risk_flags
        )


    # ========================================================
    # EVALUATE RESEARCH
    # ========================================================

    def evaluate_research(
        self,
        symbol: str,
        *,
        prebreakout_result: Any = None,
        news_result: Any = None,
        liquidity_result: Any = None,
    ) -> dict[str, Any]:

        symbol = str(
            symbol
            or ""
        ).strip().upper()

        if not symbol:

            return {

                "symbol":
                    "",

                "status":
                    "ERROR",

                "error":
                    "Symbol cannot be empty.",
            }

        try:

            # ------------------------------------------------
            # EXTRACT COMPONENTS
            # ------------------------------------------------

            pre = (
                self._extract_prebreakout(
                    prebreakout_result
                )
            )

            news = (
                self._extract_news(
                    news_result
                )
            )

            liquidity = (
                self._extract_liquidity(
                    liquidity_result
                )
            )

            # ------------------------------------------------
            # BASE SCORE
            # ------------------------------------------------

            (
                raw_score,
                component_weights,
            ) = (
                self._weighted_score(

                    pre=pre,

                    news=news,

                    liquidity=liquidity,
                )
            )

            # ------------------------------------------------
            # CONFIDENCE
            # ------------------------------------------------

            research_confidence = (
                self._research_confidence(

                    pre=pre,

                    news=news,

                    liquidity=liquidity,
                )
            )

            # ------------------------------------------------
            # DATA QUALITY CONFIDENCE CAPS
            # ------------------------------------------------
            confidence_caps = []

            if not news["available"]:
                confidence_caps.append(
                    self.NO_NEWS_CONFIDENCE_CAP
                )

            if not liquidity["available"]:
                confidence_caps.append(70.0)

            if not pre["available"]:
                confidence_caps.append(65.0)

            if confidence_caps:
                research_confidence = min(
                    research_confidence,
                    min(confidence_caps),
                )

            research_confidence = round(
                self._bounded(research_confidence),
                2,
            )

            # ------------------------------------------------
            # COLLECT RISK FLAGS
            # ------------------------------------------------

            risk_flags = []

            risk_flags.extend(
                news[
                    "risk_flags"
                ]
            )

            risk_flags.extend(
                liquidity[
                    "risk_flags"
                ]
            )

            risk_flags = (
                self._deduplicate(
                    risk_flags
                )
            )

            # ------------------------------------------------
            # RISK PENALTIES
            # ------------------------------------------------

            (
                risk_penalty,
                risk_penalty_details,
            ) = (
                self._risk_penalty(
                    risk_flags
                )
            )

            (
                pre_penalty,
                pre_penalty_details,
            ) = (
                self._prebreakout_penalty(

                    pre[
                        "warnings"
                    ]
                )
            )

            # ------------------------------------------------
            # ALIGNMENT BONUS
            # ------------------------------------------------

            (
                alignment_bonus,
                alignment_evidence,
            ) = (
                self._alignment_bonus(

                    pre=pre,

                    news=news,

                    liquidity=liquidity,
                )
            )

            if "LOW_RECENT_DOLLAR_VOLUME" in risk_flags:
                alignment_bonus = min(
                    alignment_bonus,
                    self.MAX_ALIGNMENT_BONUS_WITH_LOW_DOLLAR_VOLUME,
                )

            # ------------------------------------------------
            # FINAL SCORE
            # ------------------------------------------------

            final_score = (

                raw_score

                +

                alignment_bonus

                -

                risk_penalty

                -

                pre_penalty
            )

            final_score = round(

                self._bounded(
                    final_score
                ),

                2,
            )

            # ------------------------------------------------
            # EVIDENCE
            # ------------------------------------------------

            evidence = []

            if pre[
                "available"
            ]:

                evidence.append(

                    "PREBREAKOUT_SCORE="
                    f"{pre['score']:.2f}"
                )

            if liquidity[
                "available"
            ]:

                evidence.append(

                    "LIQUIDITY_SCORE="
                    f"{liquidity['score']:.2f}"
                )

                evidence.append(

                    "LIQUIDITY_BIAS="
                    f"{liquidity['bias']}"
                )

                evidence.append(

                    "BUY_PRESSURE="
                    f"{liquidity['buy_pressure']:.2f}%"
                )

                evidence.append(

                    "SELL_PRESSURE="
                    f"{liquidity['sell_pressure']:.2f}%"
                )

                evidence.append(

                    "MONEY_FLOW="
                    f"{liquidity['money_flow']:+.2f}"
                )

            if news[
                "available"
            ]:

                evidence.append(

                    "NEWS_SCORE="
                    f"{news['score']:.2f}"
                )

                evidence.append(

                    "NEWS_SENTIMENT="
                    f"{news['sentiment']}"
                )

            else:

                evidence.append(

                    "NEWS_STATUS="
                    f"{news['status']}"
                )

            evidence.extend(
                alignment_evidence
            )

            # ------------------------------------------------
            # CONFLICTS
            # ------------------------------------------------

            conflicts = []

            if (
                liquidity[
                    "bias"
                ]
                ==
                "BULLISH"

                and

                pre[
                    "available"
                ]

                and

                pre[
                    "score"
                ]
                <
                45
            ):

                conflicts.append(
                    "BULLISH_LIQUIDITY_BUT_WEAK_PREBREAKOUT"
                )

            if (
                liquidity[
                    "bias"
                ]
                ==
                "BEARISH"

                and

                pre[
                    "available"
                ]

                and

                pre[
                    "score"
                ]
                >=
                55
            ):

                conflicts.append(
                    "TECHNICAL_SETUP_BUT_BEARISH_LIQUIDITY"
                )

            if (
                news[
                    "sentiment"
                ]
                ==
                "BEARISH"

                and

                liquidity[
                    "bias"
                ]
                ==
                "BULLISH"
            ):

                conflicts.append(
                    "BULLISH_FLOW_BUT_BEARISH_NEWS"
                )

            if (
                news[
                    "sentiment"
                ]
                ==
                "BULLISH"

                and

                liquidity[
                    "bias"
                ]
                ==
                "BEARISH"
            ):

                conflicts.append(
                    "BULLISH_NEWS_BUT_BEARISH_FLOW"
                )

            if (
                "LIQUIDITY_TIMEFRAME_CONFLICT"
                in risk_flags
            ):

                conflicts.append(
                    "LIQUIDITY_TIMEFRAME_CONFLICT"
                )

            if (
                "EXTREME_VOLUME_WITHOUT_PRICE_CONFIRMATION"
                in risk_flags
            ):

                conflicts.append(
                    "VOLUME_WITHOUT_PRICE_CONFIRMATION"
                )

            conflicts = (
                self._deduplicate(
                    conflicts
                )
            )

            # ------------------------------------------------
            # CRITICAL RISK
            # ------------------------------------------------

            critical_risk = (
                self._critical_risk(
                    risk_flags
                )
            )

            # ------------------------------------------------
            # VERDICT
            # ------------------------------------------------

            verdict = (
                self._verdict(

                    final_score=(
                        final_score
                    ),

                    confidence=(
                        research_confidence
                    ),

                    critical_risk=(
                        critical_risk
                    ),
                )
            )

            # ------------------------------------------------
            # HIGH-PRIORITY LIQUIDITY GATE
            # ------------------------------------------------
            weak_high_priority_liquidity = (
                not liquidity["available"]
                or (
                    liquidity["score"] is not None
                    and liquidity["score"]
                    < self.MIN_LIQUIDITY_SCORE_HIGH_PRIORITY
                )
                or "LOW_RECENT_DOLLAR_VOLUME" in risk_flags
            )

            if (
                verdict == "HIGH_PRIORITY_RESEARCH"
                and weak_high_priority_liquidity
            ):
                verdict = "RESEARCH_CANDIDATE"
                conflicts.append(
                    "HIGH_PRIORITY_BLOCKED_BY_LIQUIDITY_QUALITY"
                )
                conflicts = self._deduplicate(conflicts)

            # ------------------------------------------------
            # BIAS
            # ------------------------------------------------

            bias = (
                self._final_bias(

                    final_score=(
                        final_score
                    ),

                    liquidity_bias=(
                        liquidity[
                            "bias"
                        ]
                    ),

                    sentiment=(
                        news[
                            "sentiment"
                        ]
                    ),
                )
            )

            # ------------------------------------------------
            # COMPONENT AVAILABILITY
            # ------------------------------------------------

            components_available = {

                "prebreakout":
                    bool(
                        pre[
                            "available"
                        ]
                    ),

                "news":
                    bool(
                        news[
                            "available"
                        ]
                    ),

                "liquidity":
                    bool(
                        liquidity[
                            "available"
                        ]
                    ),
            }

            # ------------------------------------------------
            # FINAL RESPONSE
            # ------------------------------------------------

            return {

                "symbol":
                    symbol,

                "research_score":
                    final_score,

                "raw_score":
                    raw_score,

                "confidence":
                    research_confidence,

                "confidence_caps":
                    confidence_caps,

                "bias":
                    bias,

                "verdict":
                    verdict,

                "prebreakout_score":
                    pre[
                        "score"
                    ],

                "prebreakout_confidence":
                    pre[
                        "confidence"
                    ],

                "prebreakout_status":
                    pre[
                        "status"
                    ],

                "score_5m":
                    pre.get(
                        "score_5m"
                    ),

                "score_30m":
                    pre.get(
                        "score_30m"
                    ),

                "score_1h":
                    pre.get(
                        "score_1h"
                    ),

                "score_1d":
                    pre.get(
                        "score_1d"
                    ),

                "news_score":
                    news[
                        "score"
                    ],

                "news_confidence":
                    news[
                        "confidence"
                    ],

                "news_sentiment":
                    news[
                        "sentiment"
                    ],

                "news_status":
                    news[
                        "status"
                    ],

                "catalysts":
                    news[
                        "catalysts"
                    ],

                "liquidity_score":
                    liquidity[
                        "score"
                    ],

                "liquidity_confidence":
                    liquidity[
                        "confidence"
                    ],

                "liquidity_bias":
                    liquidity[
                        "bias"
                    ],

                "buy_pressure":
                    liquidity.get(
                        "buy_pressure"
                    ),

                "sell_pressure":
                    liquidity.get(
                        "sell_pressure"
                    ),

                "money_flow":
                    liquidity.get(
                        "money_flow"
                    ),

                "volume_acceleration":
                    liquidity.get(
                        "volume_acceleration"
                    ),

                "price_volume_confirmation":
                    liquidity.get(
                        "price_volume_confirmation"
                    ),

                "absorption":
                    liquidity.get(
                        "absorption"
                    ),

                "distribution":
                    liquidity.get(
                        "distribution"
                    ),

                "alignment_bonus":
                    round(
                        alignment_bonus,
                        2,
                    ),

                "risk_penalty":
                    round(
                        risk_penalty,
                        2,
                    ),

                "prebreakout_penalty":
                    round(
                        pre_penalty,
                        2,
                    ),

                "evidence":
                    self._deduplicate(
                        evidence
                    ),

                "conflicts":
                    conflicts,

                "risk_flags":
                    risk_flags,

                "risk_penalty_details":
                    risk_penalty_details,

                "prebreakout_penalty_details":
                    pre_penalty_details,

                "components_available":
                    components_available,

                "component_weights":
                    component_weights,

                "critical_risk":
                    critical_risk,

                "status":
                    "SUCCESS",

                "order_execution_enabled":
                    False,
            }

        except Exception as exc:

            logger.exception(
                "AI research evaluation failed "
                "for %s: %s",
                symbol,
                exc,
            )

            return {

                "symbol":
                    symbol,

                "status":
                    "ERROR",

                "error":
                    str(
                        exc
                    ),

                "order_execution_enabled":
                    False,
            }


    # ========================================================
    # LEGACY NAME
    #
    # Kept so older code importing evaluate_opportunity
    # does not immediately break.
    # ========================================================

    def evaluate_opportunity(
        self,
        symbol: str,
        *,
        prebreakout_result: Any = None,
        news_result: Any = None,
        liquidity_result: Any = None,
    ) -> dict[str, Any]:

        return (
            self.evaluate_research(

                symbol,

                prebreakout_result=(
                    prebreakout_result
                ),

                news_result=(
                    news_result
                ),

                liquidity_result=(
                    liquidity_result
                ),
            )
        )


    # ========================================================
    # HEALTH
    # ========================================================

    def health_check(
        self,
    ) -> dict[str, Any]:

        return {

            "version":
                self.VERSION,

            "enabled":
                self.enabled,

            "role":
                "RESEARCH_AGGREGATOR",

            "prebreakout_weight":
                self.PREBREAKOUT_WEIGHT,

            "liquidity_weight":
                self.LIQUIDITY_WEIGHT,

            "news_weight":
                self.NEWS_WEIGHT,

            "watch_threshold":
                self.watch_threshold,

            "research_threshold":
                self.research_threshold,

            "high_priority_threshold":
                self.high_priority_threshold,

            "no_news_confidence_cap":
                self.NO_NEWS_CONFIDENCE_CAP,

            "low_dollar_volume_penalty":
                self.LOW_DOLLAR_VOLUME_PENALTY,

            "min_liquidity_score_high_priority":
                self.MIN_LIQUIDITY_SCORE_HIGH_PRIORITY,

            "random_scoring":
                False,

            "buy_sell_decision":
                False,

            "order_execution_enabled":
                False,
        }


# ============================================================
# STANDALONE
# ============================================================

if __name__ == "__main__":

    engine = (
        AIEngine()
    )

    print(
        engine.health_check()
    )
