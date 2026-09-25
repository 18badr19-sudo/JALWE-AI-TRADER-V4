from __future__ import annotations

import logging
import os

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from dotenv import load_dotenv


# ============================================================
# PATH / ENV
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# NEWS ENGINE V3
# APEX -> RESEARCH PROVIDER FOR JALWE
# ============================================================

class NewsEngine:
    """
    APEX NEWS ENGINE V3

    ROLE:
        Collect and analyze news for research.

    PIPELINE:

        Ranked Radar
            ↓
        Multi-Timeframe PreBreakout
            ↓
        News Engine V3
            ↓
        Liquidity / AI
            ↓
        Research Packet
            ↓
        JALWE

    IMPORTANT:

        This engine NEVER:
            - buys
            - sells
            - submits orders
            - manages positions
            - forces JALWE to trade

        News is only one research input.
    """

    NEWS_URL = (
        "https://data.alpaca.markets/v1beta1/news"
    )

    DEFAULT_LIMIT = 12

    DEFAULT_MAX_AGE_HOURS = 72.0

    REQUEST_TIMEOUT_SECONDS = 8


    # ========================================================
    # POSITIVE LANGUAGE
    # ========================================================

    POSITIVE_KEYWORDS = {

        "beat": 0.20,
        "beats": 0.20,
        "beats estimates": 0.30,

        "growth": 0.15,

        "upgrade": 0.20,
        "upgraded": 0.20,

        "approval": 0.30,
        "approved": 0.25,
        "fda approval": 0.40,

        "partnership": 0.20,
        "strategic partnership": 0.25,

        "contract": 0.18,
        "awarded contract": 0.25,

        "record revenue": 0.25,
        "record sales": 0.25,

        "profit": 0.15,
        "profitable": 0.18,

        "raises guidance": 0.35,
        "raised guidance": 0.35,

        "positive guidance": 0.30,

        "acquisition": 0.15,
        "acquire": 0.15,

        "merger": 0.15,

        "launch": 0.10,
        "expansion": 0.10,

        "surge": 0.12,

        "strong demand": 0.20,

        "breakthrough": 0.20,

        "milestone": 0.15,
    }


    # ========================================================
    # NEGATIVE LANGUAGE
    # ========================================================

    NEGATIVE_KEYWORDS = {

        "miss": -0.20,
        "misses": -0.20,
        "missed estimates": -0.30,

        "downgrade": -0.20,
        "downgraded": -0.20,

        "lawsuit": -0.25,
        "litigation": -0.20,

        "investigation": -0.25,

        "loss": -0.15,
        "losses": -0.15,

        "recall": -0.30,

        "fraud": -0.40,

        "bankruptcy": -0.60,

        "offering": -0.25,

        "public offering": -0.30,

        "registered direct": -0.30,

        "registered direct offering": -0.35,

        "dilution": -0.35,

        "warrant": -0.15,
        "warrants": -0.15,

        "shelf offering": -0.35,

        "at-the-market": -0.25,
        "atm offering": -0.25,

        "reverse split": -0.35,

        "delisting": -0.45,

        "nasdaq deficiency": -0.35,

        "noncompliance": -0.30,

        "cuts guidance": -0.35,
        "cut guidance": -0.35,

        "warning": -0.15,

        "weak demand": -0.20,

        "weak": -0.10,
    }


    # ========================================================
    # CATALYSTS
    # ========================================================

    CATALYST_KEYWORDS = {

        "fda": "FDA",

        "approval": "APPROVAL",

        "earnings": "EARNINGS",

        "guidance": "GUIDANCE",

        "acquisition": "M&A",

        "acquire": "M&A",

        "merger": "M&A",

        "contract": "CONTRACT",

        "partnership": "PARTNERSHIP",

        "launch": "PRODUCT_LAUNCH",

        "lawsuit": "LEGAL",

        "litigation": "LEGAL",

        "investigation": "LEGAL",

        "offering": "OFFERING",

        "registered direct": "OFFERING",

        "dilution": "DILUTION",

        "warrant": "WARRANTS",

        "reverse split": "REVERSE_SPLIT",

        "bankruptcy": "BANKRUPTCY",

        "recall": "RECALL",

        "delisting": "DELISTING",

        "nasdaq deficiency": "NASDAQ_COMPLIANCE",
    }


    # ========================================================
    # HIGH RISK CATALYSTS
    # ========================================================

    RISK_CATALYST_FLAGS = {

        "LEGAL":
            "LEGAL_CATALYST",

        "OFFERING":
            "DILUTION_OR_OFFERING_RISK",

        "DILUTION":
            "DILUTION_RISK",

        "WARRANTS":
            "WARRANT_DILUTION_RISK",

        "BANKRUPTCY":
            "BANKRUPTCY_RISK",

        "RECALL":
            "RECALL_RISK",

        "REVERSE_SPLIT":
            "REVERSE_SPLIT_RISK",

        "DELISTING":
            "DELISTING_RISK",

        "NASDAQ_COMPLIANCE":
            "NASDAQ_COMPLIANCE_RISK",
    }


    # ========================================================
    # INIT
    # ========================================================

    def __init__(
        self,
    ) -> None:

        # ----------------------------------------------------
        # Supports both old APEX env names
        # and newer JALWE env names.
        # ----------------------------------------------------

        self.api_key = (

            os.getenv(
                "APCA_API_KEY_ID"
            )

            or

            os.getenv(
                "ALPACA_API_KEY"
            )

            or

            ""
        ).strip()

        self.api_secret = (

            os.getenv(
                "APCA_API_SECRET_KEY"
            )

            or

            os.getenv(
                "ALPACA_SECRET_KEY"
            )

            or

            ""
        ).strip()

        self.base_url = (

            os.getenv(
                "APCA_API_BASE_URL"
            )

            or

            os.getenv(
                "ALPACA_BASE_URL"
            )

            or

            "https://paper-api.alpaca.markets"

        ).strip()

        self.news_url = (
            self.NEWS_URL
        )

        self.session = (
            requests.Session()
        )


    # ========================================================
    # CREDENTIALS
    # ========================================================

    def credentials_ready(
        self,
    ) -> bool:

        return bool(

            self.api_key

            and

            self.api_secret
        )


    # ========================================================
    # HEADERS
    # ========================================================

    def _headers(
        self,
    ) -> dict[str, str]:

        return {

            "APCA-API-KEY-ID":
                self.api_key,

            "APCA-API-SECRET-KEY":
                self.api_secret,
        }


    # ========================================================
    # NORMALIZE SYMBOL
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        value = str(
            symbol
            or ""
        ).strip().upper()

        if not value:

            raise ValueError(
                "Symbol cannot be empty."
            )

        return value


    # ========================================================
    # PARSE DATETIME
    # ========================================================

    @staticmethod
    def _parse_time(
        value: Any,
    ) -> Optional[datetime]:

        if not value:

            return None

        try:

            text = str(
                value
            ).strip()

            if text.endswith(
                "Z"
            ):

                text = (
                    text[:-1]
                    +
                    "+00:00"
                )

            dt = (
                datetime.fromisoformat(
                    text
                )
            )

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            return dt.astimezone(
                timezone.utc
            )

        except Exception:

            return None


    # ========================================================
    # NEWS AGE
    # ========================================================

    @staticmethod
    def _age_hours(
        created_at: Optional[
            datetime
        ],
    ) -> Optional[float]:

        if created_at is None:

            return None

        now = datetime.now(
            timezone.utc
        )

        try:

            age = (
                now
                -
                created_at
            ).total_seconds() / 3600.0

        except Exception:

            return None

        return max(
            0.0,
            float(age),
        )


    # ========================================================
    # RECENCY WEIGHT
    # ========================================================

    @staticmethod
    def _recency_weight(
        age_hours: Optional[float],
    ) -> float:

        if age_hours is None:

            return 0.35

        if age_hours <= 2:

            return 1.00

        if age_hours <= 6:

            return 0.95

        if age_hours <= 12:

            return 0.85

        if age_hours <= 24:

            return 0.70

        if age_hours <= 48:

            return 0.50

        if age_hours <= 72:

            return 0.35

        return 0.15


    # ========================================================
    # TEXT SENTIMENT
    # ========================================================

    def _score_text(
        self,
        text: str,
    ) -> float:

        normalized = str(
            text
            or ""
        ).lower()

        score = 0.0

        for (
            keyword,
            weight,
        ) in (
            self.POSITIVE_KEYWORDS.items()
        ):

            if keyword in normalized:

                score += (
                    weight
                )

        for (
            keyword,
            weight,
        ) in (
            self.NEGATIVE_KEYWORDS.items()
        ):

            if keyword in normalized:

                score += (
                    weight
                )

        return max(
            -1.0,
            min(
                float(score),
                1.0,
            ),
        )


    # ========================================================
    # CATALYST DETECTION
    # ========================================================

    def _detect_catalysts(
        self,
        text: str,
    ) -> list[str]:

        normalized = str(
            text
            or ""
        ).lower()

        catalysts: list[str] = []

        for (
            keyword,
            catalyst,
        ) in (
            self.CATALYST_KEYWORDS.items()
        ):

            if (
                keyword in normalized

                and

                catalyst not in catalysts
            ):

                catalysts.append(
                    catalyst
                )

        return catalysts


    # ========================================================
    # SENTIMENT LABEL
    # ========================================================

    @staticmethod
    def _sentiment_label(
        score: float,
    ) -> str:

        if score >= 0.20:

            return "BULLISH"

        if score <= -0.20:

            return "BEARISH"

        return "NEUTRAL"


    # ========================================================
    # EMPTY / ERROR RESPONSE
    # ========================================================

    @staticmethod
    def _empty_result(
        symbol: str,
        *,
        status: str,
        risk_flags: Optional[
            list[str]
        ] = None,
        error: Optional[str] = None,
    ) -> dict[str, Any]:

        result = {

            "symbol":
                symbol,

            "sentiment_score":
                None,

            "sentiment":
                "UNKNOWN",

            "news_score":
                None,

            "confidence":
                0.0,

            "catalyst_detected":
                False,

            "catalysts":
                [],

            "news_count":
                0,

            "recent_news_count":
                0,

            "fresh_news_count":
                0,

            "stale_news_count":
                0,

            "headlines":
                [],

            "articles":
                [],

            "positive_items":
                0,

            "negative_items":
                0,

            "neutral_items":
                0,

            "risk_flags":
                list(
                    risk_flags
                    or []
                ),

            "latest_news_at":
                None,

            "latest_news_age_hours":
                None,

            "status":
                status,
        }

        if error:

            result[
                "error"
            ] = str(
                error
            )

        return result


    # ========================================================
    # NEWS SCORE
    # ========================================================

    @staticmethod
    def _news_score(
        *,
        sentiment_score: float,
        recent_news_count: int,
        fresh_news_count: int,
        risk_flags: list[str],
    ) -> float:

        # ----------------------------------------------------
        # Actual valid news only.
        # 50 = neutral news sentiment.
        # ----------------------------------------------------

        score = (

            50.0

            +

            (
                sentiment_score
                *
                38.0
            )
        )

        # ----------------------------------------------------
        # Small activity bonus.
        # This is intentionally small:
        # volume of headlines is not automatically bullish.
        # ----------------------------------------------------

        score += min(
            recent_news_count
            *
            1.0,
            5.0,
        )

        score += min(
            fresh_news_count
            *
            0.75,
            3.0,
        )

        # ----------------------------------------------------
        # Risk penalties.
        # ----------------------------------------------------

        serious_flags = {

            "BANKRUPTCY_RISK",
            "DELISTING_RISK",
            "DILUTION_RISK",
            "DILUTION_OR_OFFERING_RISK",
            "REVERSE_SPLIT_RISK",
        }

        moderate_flags = {

            "LEGAL_CATALYST",
            "RECALL_RISK",
            "WARRANT_DILUTION_RISK",
            "NASDAQ_COMPLIANCE_RISK",
        }

        for flag in risk_flags:

            if flag in serious_flags:

                score -= 10.0

            elif flag in moderate_flags:

                score -= 5.0

        return round(

            max(
                0.0,
                min(
                    score,
                    100.0,
                ),
            ),

            2,
        )


    # ========================================================
    # CONFIDENCE
    # ========================================================

    @staticmethod
    def _confidence(
        *,
        news_count: int,
        fresh_news_count: int,
        scored_items: int,
        catalyst_count: int,
    ) -> float:

        if news_count <= 0:

            return 0.0

        scored_coverage = (

            scored_items
            /
            news_count
        )

        freshness_ratio = (

            fresh_news_count
            /
            news_count
        )

        confidence = (

            0.20

            +

            min(
                news_count,
                8,
            )
            *
            0.05

            +

            scored_coverage
            *
            0.18

            +

            freshness_ratio
            *
            0.12

            +

            min(
                catalyst_count,
                3,
            )
            *
            0.04
        )

        return round(

            min(
                max(
                    confidence,
                    0.0,
                ),
                0.95,
            ),

            3,
        )


    # ========================================================
    # FETCH SYMBOL NEWS
    # ========================================================

    def fetch_symbol_news(
        self,
        symbol: str,
        *,
        limit: int = DEFAULT_LIMIT,
        max_age_hours: float = (
            DEFAULT_MAX_AGE_HOURS
        ),
    ) -> dict[str, Any]:

        symbol = (
            self._normalize_symbol(
                symbol
            )
        )

        # ----------------------------------------------------
        # CREDENTIAL CHECK
        # ----------------------------------------------------

        if not self.credentials_ready():

            return (
                self._empty_result(

                    symbol,

                    status=(
                        "CREDENTIALS_MISSING"
                    ),

                    risk_flags=[
                        "NEWS_CREDENTIALS_MISSING"
                    ],
                )
            )

        # ----------------------------------------------------
        # REQUEST
        # ----------------------------------------------------

        params = {

            "symbols":
                symbol,

            "limit":
                max(
                    1,
                    min(
                        int(limit),
                        50,
                    ),
                ),

            "sort":
                "desc",
        }

        try:

            response = (
                self.session.get(

                    self.news_url,

                    headers=(
                        self._headers()
                    ),

                    params=params,

                    timeout=(
                        self.REQUEST_TIMEOUT_SECONDS
                    ),
                )
            )

        except requests.RequestException as exc:

            logger.warning(

                "News request failed "
                "for %s: %s",

                symbol,

                exc,
            )

            return (
                self._empty_result(

                    symbol,

                    status="API_ERROR",

                    risk_flags=[
                        "NEWS_API_UNAVAILABLE"
                    ],

                    error=str(
                        exc
                    ),
                )
            )

        # ----------------------------------------------------
        # HTTP STATUS
        # ----------------------------------------------------

        if response.status_code != 200:

            logger.warning(

                "News API HTTP %s for %s",

                response.status_code,

                symbol,
            )

            return (
                self._empty_result(

                    symbol,

                    status="HTTP_ERROR",

                    risk_flags=[

                        "NEWS_HTTP_"
                        +
                        str(
                            response.status_code
                        )
                    ],

                    error=(
                        response.text[:300]
                        if response.text
                        else None
                    ),
                )
            )

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        try:

            data = (
                response.json()
            )

        except ValueError as exc:

            return (
                self._empty_result(

                    symbol,

                    status="INVALID_JSON",

                    risk_flags=[
                        "NEWS_INVALID_JSON"
                    ],

                    error=str(
                        exc
                    ),
                )
            )

        news_items = (

            data.get(
                "news",
                []
            )

            or

            []
        )

        # ----------------------------------------------------
        # VALID API - NO NEWS
        #
        # IMPORTANT:
        # No news != neutral news.
        # ----------------------------------------------------

        if not news_items:

            return (
                self._empty_result(

                    symbol,

                    status="NO_NEWS",

                    risk_flags=[
                        "NO_RECENT_NEWS"
                    ],
                )
            )

        # ----------------------------------------------------
        # ANALYSIS COLLECTIONS
        # ----------------------------------------------------

        weighted_scores: list[
            tuple[
                float,
                float,
            ]
        ] = []

        headlines: list[str] = []

        articles: list[
            dict[str, Any]
        ] = []

        catalysts: list[str] = []

        latest_news_time: Optional[
            datetime
        ] = None

        latest_age_hours: Optional[
            float
        ] = None

        positive_items = 0

        negative_items = 0

        neutral_items = 0

        scored_items = 0

        recent_news_count = 0

        fresh_news_count = 0

        stale_news_count = 0


        # ====================================================
        # ANALYZE EACH NEWS ITEM
        # ====================================================

        for item in news_items:

            headline = str(

                item.get(
                    "headline",
                    "",
                )

                or

                ""

            ).strip()

            summary = str(

                item.get(
                    "summary",
                    "",
                )

                or

                ""

            ).strip()

            source = str(

                item.get(
                    "source",
                    "",
                )

                or

                ""

            ).strip()

            created_at = (
                self._parse_time(

                    item.get(
                        "created_at"
                    )

                    or

                    item.get(
                        "updated_at"
                    )
                )
            )

            age_hours = (
                self._age_hours(
                    created_at
                )
            )

            # ------------------------------------------------
            # Keep very old stories from dominating.
            # ------------------------------------------------

            if (
                age_hours is not None

                and

                age_hours
                >
                float(
                    max_age_hours
                )
            ):

                stale_news_count += 1

                continue

            recent_news_count += 1

            if (
                age_hours is not None

                and

                age_hours <= 24
            ):

                fresh_news_count += 1

            combined_text = (

                headline

                +

                " "

                +

                summary
            )

            raw_score = (
                self._score_text(
                    combined_text
                )
            )

            recency_weight = (
                self._recency_weight(
                    age_hours
                )
            )

            weighted_scores.append(

                (
                    raw_score,
                    recency_weight,
                )
            )

            # ------------------------------------------------
            # Sentiment counters
            # ------------------------------------------------

            if raw_score > 0:

                positive_items += 1

                scored_items += 1

            elif raw_score < 0:

                negative_items += 1

                scored_items += 1

            else:

                neutral_items += 1

            # ------------------------------------------------
            # Catalysts
            # ------------------------------------------------

            item_catalysts = (
                self._detect_catalysts(
                    combined_text
                )
            )

            for catalyst in (
                item_catalysts
            ):

                if catalyst not in catalysts:

                    catalysts.append(
                        catalyst
                    )

            # ------------------------------------------------
            # Latest article
            # ------------------------------------------------

            if created_at is not None:

                if (
                    latest_news_time is None

                    or

                    created_at
                    >
                    latest_news_time
                ):

                    latest_news_time = (
                        created_at
                    )

                    latest_age_hours = (
                        age_hours
                    )

            if headline:

                headlines.append(
                    headline
                )

            articles.append({

                "headline":
                    headline,

                "summary":
                    summary,

                "source":
                    source,

                "created_at":
                    (
                        created_at.isoformat()

                        if created_at

                        else None
                    ),

                "age_hours":
                    (
                        round(
                            age_hours,
                            2,
                        )

                        if age_hours
                        is not None

                        else None
                    ),

                "sentiment_score":
                    round(
                        raw_score,
                        4,
                    ),

                "recency_weight":
                    round(
                        recency_weight,
                        3,
                    ),

                "catalysts":
                    item_catalysts,
            })


        # ====================================================
        # NO RECENT NEWS AFTER AGE FILTER
        # ====================================================

        if recent_news_count <= 0:

            result = (
                self._empty_result(

                    symbol,

                    status="NO_RECENT_NEWS",

                    risk_flags=[
                        "NO_RECENT_NEWS"
                    ],
                )
            )

            result[
                "stale_news_count"
            ] = stale_news_count

            return result


        # ====================================================
        # WEIGHTED SENTIMENT
        # ====================================================

        total_weight = sum(

            weight

            for (
                _,
                weight,
            ) in weighted_scores
        )

        if total_weight > 0:

            sentiment_score = (

                sum(

                    score
                    *
                    weight

                    for (
                        score,
                        weight,
                    )
                    in weighted_scores
                )

                /

                total_weight
            )

        else:

            sentiment_score = 0.0

        sentiment_score = round(

            max(
                -1.0,
                min(
                    float(
                        sentiment_score
                    ),
                    1.0,
                ),
            ),

            4,
        )

        sentiment = (
            self._sentiment_label(
                sentiment_score
            )
        )


        # ====================================================
        # RISK FLAGS
        # ====================================================

        risk_flags: list[str] = []

        if (
            negative_items
            >
            positive_items
        ):

            risk_flags.append(
                "NEGATIVE_NEWS_DOMINANCE"
            )

        for catalyst in catalysts:

            risk_flag = (
                self.RISK_CATALYST_FLAGS.get(
                    catalyst
                )
            )

            if (
                risk_flag

                and

                risk_flag
                not in risk_flags
            ):

                risk_flags.append(
                    risk_flag
                )


        # ====================================================
        # SCORE
        # ====================================================

        news_score = (
            self._news_score(

                sentiment_score=(
                    sentiment_score
                ),

                recent_news_count=(
                    recent_news_count
                ),

                fresh_news_count=(
                    fresh_news_count
                ),

                risk_flags=(
                    risk_flags
                ),
            )
        )


        # ====================================================
        # CONFIDENCE
        # ====================================================

        confidence = (
            self._confidence(

                news_count=(
                    recent_news_count
                ),

                fresh_news_count=(
                    fresh_news_count
                ),

                scored_items=(
                    scored_items
                ),

                catalyst_count=(
                    len(
                        catalysts
                    )
                ),
            )
        )


        # ====================================================
        # RESULT
        # ====================================================

        return {

            "symbol":
                symbol,

            "sentiment_score":
                float(
                    sentiment_score
                ),

            "sentiment":
                sentiment,

            "news_score":
                float(
                    news_score
                ),

            "confidence":
                float(
                    confidence
                ),

            "catalyst_detected":
                bool(
                    catalysts
                ),

            "catalysts":
                catalysts,

            "news_count":
                int(
                    recent_news_count
                ),

            "recent_news_count":
                int(
                    recent_news_count
                ),

            "fresh_news_count":
                int(
                    fresh_news_count
                ),

            "stale_news_count":
                int(
                    stale_news_count
                ),

            "headlines":
                headlines[:10],

            "articles":
                articles[:10],

            "positive_items":
                int(
                    positive_items
                ),

            "negative_items":
                int(
                    negative_items
                ),

            "neutral_items":
                int(
                    neutral_items
                ),

            "risk_flags":
                risk_flags,

            "latest_news_at":
                (
                    latest_news_time
                    .isoformat()

                    if latest_news_time

                    else None
                ),

            "latest_news_age_hours":
                (
                    round(
                        latest_age_hours,
                        2,
                    )

                    if latest_age_hours
                    is not None

                    else None
                ),

            "status":
                "SUCCESS",
        }


    # ========================================================
    # EXTRACT SYMBOL FROM CANDIDATE
    # ========================================================

    @staticmethod
    def _candidate_symbol(
        candidate: Any,
    ) -> Optional[str]:

        if candidate is None:

            return None

        if isinstance(
            candidate,
            str,
        ):

            symbol = (
                candidate
            )

        elif isinstance(
            candidate,
            dict,
        ):

            symbol = (
                candidate.get(
                    "symbol"
                )
            )

        else:

            symbol = (
                getattr(
                    candidate,
                    "symbol",
                    None,
                )
            )

        symbol = str(
            symbol
            or ""
        ).strip().upper()

        return (
            symbol
            if symbol
            else None
        )


    # ========================================================
    # ANALYZE MULTIPLE RESEARCH CANDIDATES
    # ========================================================

    def analyze_candidates(
        self,
        candidates: Iterable[Any],
        *,
        top_n: int = 10,
        limit_per_symbol: int = 10,
        max_age_hours: float = 72.0,
    ) -> list[
        dict[str, Any]
    ]:

        symbols: list[str] = []

        seen: set[str] = set()

        for candidate in (
            candidates
            or []
        ):

            symbol = (
                self._candidate_symbol(
                    candidate
                )
            )

            if not symbol:

                continue

            if symbol in seen:

                continue

            seen.add(
                symbol
            )

            symbols.append(
                symbol
            )

            if len(
                symbols
            ) >= max(
                1,
                int(
                    top_n
                ),
            ):

                break

        results: list[
            dict[str, Any]
        ] = []

        for symbol in symbols:

            try:

                result = (
                    self.fetch_symbol_news(

                        symbol,

                        limit=(
                            limit_per_symbol
                        ),

                        max_age_hours=(
                            max_age_hours
                        ),
                    )
                )

            except Exception as exc:

                logger.exception(
                    "News analysis failed "
                    "for %s",
                    symbol,
                )

                result = (
                    self._empty_result(

                        symbol,

                        status="ERROR",

                        risk_flags=[
                            "NEWS_ENGINE_ERROR"
                        ],

                        error=str(
                            exc
                        ),
                    )
                )

            results.append(
                result
            )

        return results


    # ========================================================
    # HEALTH CHECK
    # ========================================================

    def health_check(
        self,
    ) -> dict[str, Any]:

        return {

            "version":
                "3.0",

            "env_file":
                str(
                    ENV_FILE
                ),

            "env_exists":
                ENV_FILE.exists(),

            "credentials_present":
                self.credentials_ready(),

            "paper_url":
                (
                    "paper-api.alpaca.markets"
                    in
                    self.base_url.lower()
                ),

            "news_url":
                self.news_url,

            "default_max_age_hours":
                self.DEFAULT_MAX_AGE_HOURS,

            "role":
                "RESEARCH_ONLY",

            "order_execution_enabled":
                False,
        }


# ============================================================
# LEGACY COMPATIBILITY
# ============================================================

def analyze_news_catalyst(
    symbol: str,
) -> dict[str, Any]:

    try:

        engine = (
            NewsEngine()
        )

        return (
            engine.fetch_symbol_news(
                symbol
            )
        )

    except Exception as exc:

        logger.exception(
            "Error in analyze_news_catalyst "
            "for %s",
            symbol,
        )

        return (
            NewsEngine._empty_result(

                str(
                    symbol
                    or ""
                ).upper(),

                status="ERROR",

                risk_flags=[
                    "NEWS_ENGINE_ERROR"
                ],

                error=str(
                    exc
                ),
            )
        )


# ============================================================
# STANDALONE
# ============================================================

if __name__ == "__main__":

    engine = (
        NewsEngine()
    )

    print(
        engine.health_check()
    )
