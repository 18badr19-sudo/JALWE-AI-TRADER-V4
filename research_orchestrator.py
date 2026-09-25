from __future__ import annotations

import logging

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


from scanner_engine import ScannerEngine
from pre_breakout_engine import PreBreakoutEngine
from news_engine import NewsEngine
from liquidity_engine import LiquidityEngine
from ai_engine import AIEngine


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# RESEARCH PACKET
# ============================================================

@dataclass
class ResearchPacket:

    symbol: str

    source: str = "APEX"

    research_score: float = 0.0
    confidence: float = 0.0

    bias: str = "MIXED"
    verdict: str = "REJECT"

    # --------------------------------------------------------
    # RADAR
    # --------------------------------------------------------

    radar_score: Optional[float] = None
    radar_rvol: Optional[float] = None
    radar_change_pct: Optional[float] = None
    radar_price: Optional[float] = None

    # --------------------------------------------------------
    # PREBREAKOUT
    # --------------------------------------------------------

    prebreakout_score: Optional[float] = None
    prebreakout_confidence: Optional[float] = None
    prebreakout_status: Optional[str] = None

    score_5m: Optional[float] = None
    score_30m: Optional[float] = None
    score_1h: Optional[float] = None
    score_1d: Optional[float] = None

    # --------------------------------------------------------
    # NEWS
    # --------------------------------------------------------

    news_score: Optional[float] = None
    news_confidence: Optional[float] = None

    news_sentiment: str = "UNKNOWN"
    news_status: str = "UNKNOWN"

    catalysts: list[str] = field(
        default_factory=list
    )

    headlines: list[str] = field(
        default_factory=list
    )

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquidity_score: Optional[float] = None
    liquidity_confidence: Optional[float] = None

    liquidity_bias: str = "UNKNOWN"

    buy_pressure: Optional[float] = None
    sell_pressure: Optional[float] = None

    money_flow: Optional[float] = None
    volume_acceleration: Optional[float] = None

    price_volume_confirmation: bool = False

    absorption: bool = False
    distribution: bool = False

    # --------------------------------------------------------
    # AI RESEARCH
    # --------------------------------------------------------

    evidence: list[str] = field(
        default_factory=list
    )

    conflicts: list[str] = field(
        default_factory=list
    )

    risk_flags: list[str] = field(
        default_factory=list
    )

    alignment_bonus: float = 0.0
    risk_penalty: float = 0.0
    prebreakout_penalty: float = 0.0

    critical_risk: bool = False

    # --------------------------------------------------------
    # METADATA
    # --------------------------------------------------------

    created_at: str = field(
        default_factory=lambda: (
            datetime.now(
                timezone.utc
            ).isoformat()
        )
    )

    order_execution_enabled: bool = False


    def to_dict(
        self,
    ) -> dict[str, Any]:

        return asdict(
            self
        )


# ============================================================
# RESEARCH CYCLE RESULT
# ============================================================

@dataclass
class ResearchCycleResult:

    status: str

    radar_count: int = 0

    valid_prebreakout_count: int = 0

    deep_research_count: int = 0

    packet_count: int = 0

    shortlisted_count: int = 0

    published_count: int = 0

    bridge_status: str = "NOT_RUN"

    bridge_error: Optional[str] = None

    packets: list[
        ResearchPacket
    ] = field(
        default_factory=list
    )

    shortlist: list[
        ResearchPacket
    ] = field(
        default_factory=list
    )

    errors: list[str] = field(
        default_factory=list
    )

    started_at: Optional[str] = None

    finished_at: Optional[str] = None


# ============================================================
# RESEARCH ORCHESTRATOR
# ============================================================

class ResearchOrchestrator:
    """
    APEX RESEARCH ORCHESTRATOR V2

    PIPELINE:

        ScannerEngine
            ↓
        Ranked Candidates
            ↓
        PreBreakout
        5M + 30M + 1H + 1D
            ↓
        Data Confidence Filter
            ↓
        Best Research Candidates
            ↓
        NewsEngine
            ↓
        LiquidityEngine
            ↓
        AIEngine
            ↓
        ResearchPacket
            ↓
        Shortlist
            ↓
        JALWE V4 Research Bridge

    IMPORTANT:

        APEX = RESEARCH ONLY.

        This module NEVER:

            - submits broker orders
            - buys
            - sells
            - manages positions
            - changes JALWE risk
            - bypasses JALWE DecisionEngine

        JALWE independently decides whether
        the research should be rejected,
        watched, or considered further.
    """

    VERSION = "2.0"

    DEFAULT_RADAR_TOP_N = 20

    DEFAULT_DEEP_RESEARCH_TOP_N = 5

    DEFAULT_MIN_PRE_CONFIDENCE = 75.0


    # ========================================================
    # INIT
    # ========================================================

    def __init__(
        self,
        *,
        scanner: Optional[
            ScannerEngine
        ] = None,
        prebreakout: Optional[
            PreBreakoutEngine
        ] = None,
        news: Optional[
            NewsEngine
        ] = None,
        liquidity: Optional[
            LiquidityEngine
        ] = None,
        ai: Optional[
            AIEngine
        ] = None,
    ) -> None:

        # ----------------------------------------------------
        # SCANNER
        # ----------------------------------------------------

        self.scanner = (
            scanner
            or
            ScannerEngine()
        )

        # ----------------------------------------------------
        # PREBREAKOUT
        # ----------------------------------------------------

        self.prebreakout = (
            prebreakout
            or
            PreBreakoutEngine(
                self.scanner.alpaca,
                None,
            )
        )

        # ----------------------------------------------------
        # NEWS
        # ----------------------------------------------------

        self.news = (
            news
            or
            NewsEngine()
        )

        # ----------------------------------------------------
        # LIQUIDITY
        # ----------------------------------------------------

        self.liquidity = (
            liquidity
            or
            LiquidityEngine(
                self.scanner.alpaca
            )
        )

        # ----------------------------------------------------
        # AI RESEARCH AGGREGATOR
        # ----------------------------------------------------

        self.ai = (
            ai
            or
            AIEngine()
        )

        # ----------------------------------------------------
        # JALWE BRIDGE
        #
        # Lazy import intentionally used.
        #
        # If JALWE is temporarily unavailable,
        # Apex research still works.
        # ----------------------------------------------------

        self.jalwe_bridge = None

        self.jalwe_bridge_error = None

        try:

            from jalwe_bridge_client import (
                JalweBridgeClient,
            )

            self.jalwe_bridge = (
                JalweBridgeClient()
            )

        except Exception as exc:

            self.jalwe_bridge = None

            self.jalwe_bridge_error = str(
                exc
            )

            logger.warning(
                "JALWE bridge unavailable: %s",
                exc,
            )


    # ========================================================
    # SAFE FLOAT
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


    # ========================================================
    # NORMALIZE SYMBOL
    # ========================================================

    @staticmethod
    def _symbol(
        value: Any,
    ) -> str:

        return str(
            value
            or ""
        ).strip().upper()


    # ========================================================
    # BUILD RESEARCH PACKET
    # ========================================================

    def _build_packet(
        self,
        *,
        candidate: Any,
        pre_result: Any,
        news_result: dict[str, Any],
        liquidity_result: Any,
        ai_result: dict[str, Any],
    ) -> ResearchPacket:

        symbol = self._symbol(
            ai_result.get(
                "symbol",
                "",
            )
        )

        news_confidence = (
            self._float(
                news_result.get(
                    "confidence",
                    0.0,
                )
            )
        )

        # NewsEngine confidence is normally 0-1.
        if news_confidence <= 1.0:

            news_confidence *= 100.0

        liquidity_confidence = (
            self._float(
                getattr(
                    liquidity_result,
                    "confidence",
                    0.0,
                )
            )
        )

        # Liquidity confidence is normally 0-1.
        if liquidity_confidence <= 1.0:

            liquidity_confidence *= 100.0

        return ResearchPacket(

            symbol=symbol,

            source="APEX",

            # ------------------------------------------------
            # FINAL APEX AI
            # ------------------------------------------------

            research_score=(
                self._float(
                    ai_result.get(
                        "research_score",
                        0.0,
                    )
                )
            ),

            confidence=(
                self._float(
                    ai_result.get(
                        "confidence",
                        0.0,
                    )
                )
            ),

            bias=str(
                ai_result.get(
                    "bias",
                    "MIXED",
                )
                or
                "MIXED"
            ).upper(),

            verdict=str(
                ai_result.get(
                    "verdict",
                    "REJECT",
                )
                or
                "REJECT"
            ).upper(),

            # ------------------------------------------------
            # RADAR
            # ------------------------------------------------

            radar_score=getattr(
                candidate,
                "rank_score",
                None,
            ),

            radar_rvol=getattr(
                candidate,
                "relative_volume",
                None,
            ),

            radar_change_pct=getattr(
                candidate,
                "change_pct",
                None,
            ),

            radar_price=getattr(
                candidate,
                "price",
                None,
            ),

            # ------------------------------------------------
            # PREBREAKOUT
            # ------------------------------------------------

            prebreakout_score=getattr(
                pre_result,
                "score",
                None,
            ),

            prebreakout_confidence=getattr(
                pre_result,
                "data_confidence",
                None,
            ),

            prebreakout_status=getattr(
                pre_result,
                "status",
                None,
            ),

            score_5m=getattr(
                pre_result,
                "score_5m",
                None,
            ),

            score_30m=getattr(
                pre_result,
                "score_30m",
                None,
            ),

            score_1h=getattr(
                pre_result,
                "score_1h",
                None,
            ),

            score_1d=getattr(
                pre_result,
                "score_1d",
                None,
            ),

            # ------------------------------------------------
            # NEWS
            # ------------------------------------------------

            news_score=(
                news_result.get(
                    "news_score"
                )
            ),

            news_confidence=(
                news_confidence
            ),

            news_sentiment=str(
                news_result.get(
                    "sentiment",
                    "UNKNOWN",
                )
                or
                "UNKNOWN"
            ).upper(),

            news_status=str(
                news_result.get(
                    "status",
                    "UNKNOWN",
                )
                or
                "UNKNOWN"
            ).upper(),

            catalysts=list(
                news_result.get(
                    "catalysts",
                    [],
                )
                or
                []
            ),

            headlines=list(
                news_result.get(
                    "headlines",
                    [],
                )
                or
                []
            )[:5],

            # ------------------------------------------------
            # LIQUIDITY
            # ------------------------------------------------

            liquidity_score=getattr(
                liquidity_result,
                "liquidity_score",
                None,
            ),

            liquidity_confidence=(
                liquidity_confidence
            ),

            liquidity_bias=str(
                getattr(
                    liquidity_result,
                    "liquidity_bias",
                    "UNKNOWN",
                )
                or
                "UNKNOWN"
            ).upper(),

            buy_pressure=getattr(
                liquidity_result,
                "buy_pressure",
                None,
            ),

            sell_pressure=getattr(
                liquidity_result,
                "sell_pressure",
                None,
            ),

            money_flow=getattr(
                liquidity_result,
                "money_flow",
                None,
            ),

            volume_acceleration=getattr(
                liquidity_result,
                "volume_acceleration",
                None,
            ),

            price_volume_confirmation=bool(
                getattr(
                    liquidity_result,
                    "price_volume_confirmation",
                    False,
                )
            ),

            absorption=bool(
                getattr(
                    liquidity_result,
                    "absorption",
                    False,
                )
            ),

            distribution=bool(
                getattr(
                    liquidity_result,
                    "distribution",
                    False,
                )
            ),

            # ------------------------------------------------
            # AI DETAILS
            # ------------------------------------------------

            evidence=list(
                ai_result.get(
                    "evidence",
                    [],
                )
                or
                []
            ),

            conflicts=list(
                ai_result.get(
                    "conflicts",
                    [],
                )
                or
                []
            ),

            risk_flags=list(
                ai_result.get(
                    "risk_flags",
                    [],
                )
                or
                []
            ),

            alignment_bonus=(
                self._float(
                    ai_result.get(
                        "alignment_bonus",
                        0.0,
                    )
                )
            ),

            risk_penalty=(
                self._float(
                    ai_result.get(
                        "risk_penalty",
                        0.0,
                    )
                )
            ),

            prebreakout_penalty=(
                self._float(
                    ai_result.get(
                        "prebreakout_penalty",
                        0.0,
                    )
                )
            ),

            critical_risk=bool(
                ai_result.get(
                    "critical_risk",
                    False,
                )
            ),

            order_execution_enabled=False,
        )


    # ========================================================
    # PUBLISH SHORTLIST TO JALWE
    # ========================================================

    def _publish_to_jalwe(
        self,
        result: ResearchCycleResult,
    ) -> None:

        # ----------------------------------------------------
        # No shortlist = nothing to send.
        # ----------------------------------------------------

        if not result.shortlist:

            result.published_count = 0

            result.bridge_status = (
                "NO_SHORTLIST"
            )

            return

        # ----------------------------------------------------
        # Bridge missing.
        # ----------------------------------------------------

        if self.jalwe_bridge is None:

            result.published_count = 0

            result.bridge_status = (
                "BRIDGE_UNAVAILABLE"
            )

            result.bridge_error = (
                self.jalwe_bridge_error
                or
                "JALWE bridge unavailable."
            )

            return

        # ----------------------------------------------------
        # Publish research.
        # ----------------------------------------------------

        try:

            published = (
                self.jalwe_bridge
                .publish_shortlist(
                    result.shortlist
                )
            )

            result.published_count = len(
                published
            )

            result.bridge_status = (
                "SUCCESS"
            )

        except Exception as exc:

            logger.exception(
                "Publishing Apex research "
                "to JALWE failed: %s",
                exc,
            )

            result.published_count = 0

            result.bridge_status = (
                "ERROR"
            )

            result.bridge_error = str(
                exc
            )

            result.errors.append(
                "JALWE_BRIDGE:"
                +
                str(
                    exc
                )
            )


    # ========================================================
    # RUN COMPLETE RESEARCH CYCLE
    # ========================================================

    def run_cycle(
        self,
        *,
        radar_top_n: int = (
            DEFAULT_RADAR_TOP_N
        ),
        deep_research_top_n: int = (
            DEFAULT_DEEP_RESEARCH_TOP_N
        ),
        min_pre_confidence: float = (
            DEFAULT_MIN_PRE_CONFIDENCE
        ),
        publish_to_jalwe: bool = True,
    ) -> ResearchCycleResult:

        started_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        result = ResearchCycleResult(

            status="STARTED",

            started_at=(
                started_at
            ),
        )

        # ====================================================
        # 1. SCANNER
        # ====================================================

        try:

            radar = (
                self.scanner
                .run_radar(
                    top_n=max(
                        1,
                        int(
                            radar_top_n
                        ),
                    )
                )
            )

        except Exception as exc:

            result.status = (
                "SCANNER_ERROR"
            )

            result.errors.append(
                str(
                    exc
                )
            )

            result.finished_at = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            return result

        radar_status = str(
            getattr(
                radar,
                "status",
                "",
            )
            or
            ""
        ).upper()

        if radar_status != "SUCCESS":

            result.status = (
                "NO_RADAR_RESULTS"
            )

            radar_error = getattr(
                radar,
                "error",
                None,
            )

            if radar_error:

                result.errors.append(
                    str(
                        radar_error
                    )
                )

            result.finished_at = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            return result

        radar_candidates = list(
            getattr(
                radar,
                "ranked_candidates",
                [],
            )
            or
            []
        )

        result.radar_count = len(
            radar_candidates
        )

        if not radar_candidates:

            result.status = (
                "NO_RADAR_RESULTS"
            )

            result.finished_at = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            return result


        # ====================================================
        # 2. PREBREAKOUT
        # ====================================================

        pre_results: list[
            tuple[
                Any,
                Any,
            ]
        ] = []

        for candidate in (
            radar_candidates
        ):

            symbol = self._symbol(
                getattr(
                    candidate,
                    "symbol",
                    "",
                )
            )

            if not symbol:

                continue

            try:

                pre = (
                    self.prebreakout
                    .analyze_symbol(

                        symbol,

                        radar_context=(
                            candidate
                        ),
                    )
                )

            except Exception as exc:

                result.errors.append(

                    f"{symbol}:PRE:"
                    f"{str(exc)}"
                )

                continue

            confidence = self._float(
                getattr(
                    pre,
                    "data_confidence",
                    0.0,
                )
            )

            if (
                confidence
                <
                float(
                    min_pre_confidence
                )
            ):

                continue

            pre_results.append(
                (
                    candidate,
                    pre,
                )
            )

        result.valid_prebreakout_count = (
            len(
                pre_results
            )
        )

        # ----------------------------------------------------
        # Best technical candidates first.
        # ----------------------------------------------------

        pre_results.sort(

            key=lambda item: (

                self._float(
                    getattr(
                        item[1],
                        "score",
                        0.0,
                    )
                ),

                self._float(
                    getattr(
                        item[1],
                        "data_confidence",
                        0.0,
                    )
                ),

                self._float(
                    getattr(
                        item[0],
                        "rank_score",
                        0.0,
                    )
                ),
            ),

            reverse=True,
        )

        deep_candidates = (
            pre_results[
                :max(
                    1,
                    int(
                        deep_research_top_n
                    ),
                )
            ]
        )

        result.deep_research_count = (
            len(
                deep_candidates
            )
        )

        if not deep_candidates:

            result.status = (
                "NO_DEEP_RESEARCH_CANDIDATES"
            )

            result.finished_at = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            return result


        # ====================================================
        # 3. NEWS
        # 4. LIQUIDITY
        # 5. AI
        # ====================================================

        packets: list[
            ResearchPacket
        ] = []

        for (
            candidate,
            pre,
        ) in deep_candidates:

            symbol = self._symbol(
                getattr(
                    candidate,
                    "symbol",
                    "",
                )
            )

            if not symbol:

                continue

            # ------------------------------------------------
            # NEWS
            # ------------------------------------------------

            try:

                news = (
                    self.news
                    .fetch_symbol_news(
                        symbol
                    )
                )

            except Exception as exc:

                logger.exception(
                    "News failure for %s",
                    symbol,
                )

                news = {

                    "symbol":
                        symbol,

                    "news_score":
                        None,

                    "confidence":
                        0.0,

                    "sentiment":
                        "UNKNOWN",

                    "status":
                        "ERROR",

                    "catalysts":
                        [],

                    "headlines":
                        [],

                    "risk_flags":
                        [
                            "NEWS_ENGINE_ERROR"
                        ],
                }

                result.errors.append(

                    f"{symbol}:NEWS:"
                    f"{str(exc)}"
                )

            # ------------------------------------------------
            # LIQUIDITY
            # ------------------------------------------------

            try:

                liquidity = (
                    self.liquidity
                    .analyze_symbol(
                        symbol
                    )
                )

            except Exception as exc:

                logger.exception(
                    "Liquidity failure for %s",
                    symbol,
                )

                result.errors.append(

                    f"{symbol}:LIQUIDITY:"
                    f"{str(exc)}"
                )

                continue

            # ------------------------------------------------
            # AI
            # ------------------------------------------------

            try:

                ai_result = (
                    self.ai
                    .evaluate_research(

                        symbol,

                        prebreakout_result=(
                            pre
                        ),

                        news_result=(
                            news
                        ),

                        liquidity_result=(
                            liquidity
                        ),
                    )
                )

            except Exception as exc:

                logger.exception(
                    "AI failure for %s",
                    symbol,
                )

                result.errors.append(

                    f"{symbol}:AI:"
                    f"{str(exc)}"
                )

                continue

            ai_status = str(
                ai_result.get(
                    "status",
                    "",
                )
                or
                ""
            ).upper()

            if ai_status != "SUCCESS":

                result.errors.append(

                    f"{symbol}:AI_STATUS:"
                    f"{ai_status}"
                )

                continue

            # ------------------------------------------------
            # PACKET
            # ------------------------------------------------

            packet = (
                self._build_packet(

                    candidate=(
                        candidate
                    ),

                    pre_result=(
                        pre
                    ),

                    news_result=(
                        news
                    ),

                    liquidity_result=(
                        liquidity
                    ),

                    ai_result=(
                        ai_result
                    ),
                )
            )

            packets.append(
                packet
            )


        # ====================================================
        # 6. FINAL SORT
        # ====================================================

        packets.sort(

            key=lambda packet: (

                packet.research_score,

                packet.confidence,

                packet.prebreakout_score
                or
                0.0,
            ),

            reverse=True,
        )

        result.packets = (
            packets
        )

        result.packet_count = (
            len(
                packets
            )
        )


        # ====================================================
        # 7. SHORTLIST
        # ====================================================

        allowed_verdicts = {

            "WATCH",

            "RESEARCH_CANDIDATE",

            "HIGH_PRIORITY_RESEARCH",
        }

        shortlist: list[
            ResearchPacket
        ] = []

        for packet in packets:

            if (
                packet.verdict
                not in
                allowed_verdicts
            ):

                continue

            if packet.critical_risk:

                continue

            shortlist.append(
                packet
            )

        result.shortlist = (
            shortlist
        )

        result.shortlisted_count = (
            len(
                shortlist
            )
        )


        # ====================================================
        # 8. SEND SHORTLIST TO JALWE AUTOMATICALLY
        # ====================================================

        if publish_to_jalwe:

            self._publish_to_jalwe(
                result
            )

        else:

            result.published_count = 0

            result.bridge_status = (
                "DISABLED_FOR_THIS_CYCLE"
            )


        # ====================================================
        # 9. FINISH
        # ====================================================

        result.status = (
            "SUCCESS"
        )

        result.finished_at = (
            datetime.now(
                timezone.utc
            ).isoformat()
        )

        return result


    # ========================================================
    # RUN ONCE
    # ========================================================

    def run_once(
        self,
    ) -> ResearchCycleResult:

        return (
            self.run_cycle(
                publish_to_jalwe=True
            )
        )


    # ========================================================
    # HEALTH CHECK
    # ========================================================

    def health_check(
        self,
    ) -> dict[str, Any]:

        bridge_health = {

            "available":
                False,

            "error":
                self.jalwe_bridge_error,
        }

        if self.jalwe_bridge is not None:

            try:

                bridge_health = (
                    self.jalwe_bridge
                    .health_check()
                )

                bridge_health[
                    "available"
                ] = True

            except Exception as exc:

                bridge_health = {

                    "available":
                        False,

                    "error":
                        str(
                            exc
                        ),
                }

        return {

            "version":
                self.VERSION,

            "role":
                "APEX_RESEARCH_ORCHESTRATOR",

            "scanner":
                self.scanner.health_check(),

            "prebreakout":
                self.prebreakout.health_check(),

            "news":
                self.news.health_check(),

            "liquidity":
                self.liquidity.health_check(),

            "ai":
                self.ai.health_check(),

            "jalwe_bridge":
                bridge_health,

            "pipeline": [

                "SCANNER",

                "PREBREAKOUT",

                "NEWS",

                "LIQUIDITY",

                "AI",

                "RESEARCH_PACKET",

                "SHORTLIST",

                "JALWE_BRIDGE",
            ],

            "auto_publish_to_jalwe":
                True,

            "research_only":
                True,

            "order_execution_enabled":
                False,
        }


# ============================================================
# STANDALONE
# ============================================================

if __name__ == "__main__":

    engine = (
        ResearchOrchestrator()
    )

    cycle = (
        engine.run_cycle(
            publish_to_jalwe=True
        )
    )

    print(
        "======================================"
    )

    print(
        "APEX RESEARCH ORCHESTRATOR V2"
    )

    print(
        "======================================"
    )

    print(
        "STATUS:",
        cycle.status,
    )

    print(
        "RADAR:",
        cycle.radar_count,
    )

    print(
        "VALID PRE:",
        cycle.valid_prebreakout_count,
    )

    print(
        "DEEP:",
        cycle.deep_research_count,
    )

    print(
        "PACKETS:",
        cycle.packet_count,
    )

    print(
        "SHORTLIST:",
        cycle.shortlisted_count,
    )

    print(
        "PUBLISHED:",
        cycle.published_count,
    )

    print(
        "BRIDGE:",
        cycle.bridge_status,
    )

    if cycle.bridge_error:

        print(
            "BRIDGE ERROR:",
            cycle.bridge_error,
        )

    print()

    # --------------------------------------------------------
    # ALL RESEARCH PACKETS
    # --------------------------------------------------------

    for (
        index,
        packet,
    ) in enumerate(
        cycle.packets,
        start=1,
    ):

        print(

            index,

            packet.symbol,

            "| SCORE:",
            packet.research_score,

            "| CONF:",
            packet.confidence,

            "| BIAS:",
            packet.bias,

            "| VERDICT:",
            packet.verdict,

            "| PRE:",
            packet.prebreakout_score,

            "| NEWS:",
            packet.news_score,

            "| LIQ:",
            packet.liquidity_score,
        )

    # --------------------------------------------------------
    # SHORTLIST
    # --------------------------------------------------------

    if cycle.shortlist:

        print()

        print(
            "----- SENT TO JALWE -----"
        )

        for packet in (
            cycle.shortlist
        ):

            print(

                packet.symbol,

                "| VERDICT:",
                packet.verdict,

                "| SCORE:",
                packet.research_score,

                "| CONF:",
                packet.confidence,
            )

    # --------------------------------------------------------
    # ERRORS
    # --------------------------------------------------------

    if cycle.errors:

        print()

        print(
            "ERRORS:"
        )

        for error in (
            cycle.errors
        ):

            print(
                "-",
                error,
            )
