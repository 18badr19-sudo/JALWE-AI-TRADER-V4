from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from alpaca.common.enums import Sort
from alpaca.data.historical import NewsClient
from alpaca.data.requests import NewsRequest

from core.config import settings
from core.models import FeatureSnapshot


logger = logging.getLogger(__name__)


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class NewsItem:
    article_id: str
    headline: str
    summary: str

    created_at: datetime

    source: Optional[str] = None
    url: Optional[str] = None

    symbols: list[str] = field(
        default_factory=list
    )

    catalyst: str = "OTHER"

    sentiment_score: float = 50.0
    importance_score: float = 0.0
    freshness_weight: float = 0.0


@dataclass
class NewsAnalysis:
    symbol: str

    available: bool
    score: Optional[float]

    catalyst_detected: bool
    catalyst_type: Optional[str]

    article_count: int

    strongest_headline: Optional[str]

    positive_count: int = 0
    negative_count: int = 0
    neutral_count: int = 0

    articles: list[NewsItem] = field(
        default_factory=list
    )

    warnings: list[str] = field(
        default_factory=list
    )


class NewsEngineError(RuntimeError):
    pass


# ============================================================
# NEWS ENGINE
# ============================================================

class NewsEngine:
    """
    JALWE AI TRADER V4 - News/Catalyst Engine.

    Features:
    - Real Alpaca news only.
    - No fake news fallback.
    - News freshness scoring.
    - Catalyst classification.
    - Positive / negative event scoring.
    - Duplicate removal.
    - Can write news_score into FeatureSnapshot.

    Score interpretation:

        70-100  = supportive / positive catalyst
        40-60   = neutral / mixed
        0-30    = negative catalyst

    A missing news feed returns score=None,
    not a fake neutral score.
    """

    POSITIVE_TERMS = {
        "beats estimates": 12,
        "beat estimates": 12,
        "earnings beat": 12,
        "revenue beat": 10,
        "raises guidance": 16,
        "raised guidance": 16,
        "guidance raised": 16,
        "record revenue": 10,
        "record sales": 10,
        "profit surge": 8,
        "strong demand": 7,
        "fda approval": 20,
        "approved by fda": 20,
        "wins contract": 14,
        "awarded contract": 14,
        "contract award": 14,
        "strategic partnership": 8,
        "share buyback": 10,
        "stock buyback": 10,
        "dividend increase": 8,
        "price target raised": 6,
        "upgrade": 7,
        "upgraded": 7,
        "acquisition offer": 12,
        "to acquire": 8,
    }

    NEGATIVE_TERMS = {
        "misses estimates": 12,
        "missed estimates": 12,
        "earnings miss": 12,
        "revenue miss": 10,
        "lowers guidance": 16,
        "lowered guidance": 16,
        "guidance cut": 16,
        "cuts guidance": 16,
        "secondary offering": 16,
        "public offering": 12,
        "share offering": 14,
        "dilution": 16,
        "bankruptcy": 25,
        "chapter 11": 25,
        "delisting": 20,
        "sec investigation": 18,
        "doj investigation": 18,
        "fraud investigation": 20,
        "lawsuit": 8,
        "clinical trial failed": 22,
        "trial failure": 22,
        "fda rejection": 22,
        "fda rejects": 22,
        "downgrade": 7,
        "downgraded": 7,
        "price target cut": 6,
        "recall": 10,
        "data breach": 12,
    }

    def __init__(
        self,
        lookback_hours: int = 24,
        limit: int = 50,
    ) -> None:

        if lookback_hours <= 0:
            raise ValueError(
                "lookback_hours must be greater than zero."
            )

        if not 1 <= limit <= 50:
            raise ValueError(
                "limit must be between 1 and 50."
            )

        if not settings.ALPACA_API_KEY:
            raise RuntimeError(
                "ALPACA_API_KEY is missing."
            )

        if not settings.ALPACA_SECRET_KEY:
            raise RuntimeError(
                "ALPACA_SECRET_KEY is missing."
            )

        self.lookback_hours = lookback_hours
        self.limit = limit

        self.client = NewsClient(
            api_key=settings.ALPACA_API_KEY,
            secret_key=settings.ALPACA_SECRET_KEY,
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _normalize_symbol(
        symbol: str,
    ) -> str:

        normalized = str(
            symbol or ""
        ).strip().upper()

        if not normalized:
            raise ValueError(
                "Symbol cannot be empty."
            )

        return normalized

    @staticmethod
    def _to_utc(
        value: Any,
    ) -> datetime:

        if isinstance(
            value,
            datetime,
        ):
            timestamp = value

        else:
            timestamp = datetime.fromisoformat(
                str(value).replace(
                    "Z",
                    "+00:00",
                )
            )

        if timestamp.tzinfo is None:

            timestamp = timestamp.replace(
                tzinfo=timezone.utc
            )

        return timestamp.astimezone(
            timezone.utc
        )

    # ========================================================
    # FRESHNESS
    # ========================================================

    @staticmethod
    def _freshness_weight(
        created_at: datetime,
    ) -> float:

        now = datetime.now(
            timezone.utc
        )

        age_hours = max(
            (
                now
                - created_at
            ).total_seconds() / 3600.0,
            0.0,
        )

        if age_hours <= 1:
            return 1.00

        if age_hours <= 3:
            return 0.90

        if age_hours <= 6:
            return 0.75

        if age_hours <= 12:
            return 0.55

        if age_hours <= 24:
            return 0.35

        return 0.15

    # ========================================================
    # CATALYST CLASSIFICATION
    # ========================================================

    @staticmethod
    def _classify_catalyst(
        text: str,
    ) -> str:

        value = text.lower()

        if any(
            term in value
            for term in [
                "earnings",
                "eps",
                "revenue",
                "quarter results",
            ]
        ):
            return "EARNINGS"

        if "guidance" in value:
            return "GUIDANCE"

        if any(
            term in value
            for term in [
                "fda",
                "clinical trial",
                "phase 1",
                "phase 2",
                "phase 3",
            ]
        ):
            return "BIOTECH"

        if any(
            term in value
            for term in [
                "acquire",
                "acquisition",
                "merger",
                "takeover",
            ]
        ):
            return "M&A"

        if any(
            term in value
            for term in [
                "contract",
                "partnership",
                "agreement",
            ]
        ):
            return "CONTRACT"

        if any(
            term in value
            for term in [
                "offering",
                "dilution",
                "share sale",
            ]
        ):
            return "OFFERING"

        if any(
            term in value
            for term in [
                "sec investigation",
                "doj",
                "lawsuit",
                "fraud",
            ]
        ):
            return "LEGAL"

        if any(
            term in value
            for term in [
                "upgrade",
                "downgrade",
                "price target",
            ]
        ):
            return "ANALYST"

        return "OTHER"

    # ========================================================
    # SENTIMENT / MATERIALITY
    # ========================================================

    def _score_article(
        self,
        headline: str,
        summary: str,
    ) -> tuple[float, float]:

        text = (
            f"{headline} {summary}"
        ).lower()

        score = 50.0
        importance = 10.0

        for term, weight in (
            self.POSITIVE_TERMS.items()
        ):

            if term in text:
                score += weight
                importance += weight

        for term, weight in (
            self.NEGATIVE_TERMS.items()
        ):

            if term in text:
                score -= weight
                importance += weight

        catalyst = (
            self._classify_catalyst(
                text
            )
        )

        catalyst_importance = {
            "EARNINGS": 20,
            "GUIDANCE": 25,
            "BIOTECH": 30,
            "M&A": 25,
            "CONTRACT": 18,
            "OFFERING": 25,
            "LEGAL": 25,
            "ANALYST": 12,
            "OTHER": 0,
        }

        importance += (
            catalyst_importance.get(
                catalyst,
                0,
            )
        )

        score = max(
            0.0,
            min(
                score,
                100.0,
            ),
        )

        importance = max(
            0.0,
            min(
                importance,
                100.0,
            ),
        )

        return (
            round(
                score,
                2,
            ),
            round(
                importance,
                2,
            ),
        )

    # ========================================================
    # FETCH NEWS
    # ========================================================

    def get_recent_news(
        self,
        symbol: str,
    ) -> list[NewsItem]:

        symbol = self._normalize_symbol(
            symbol
        )

        end = datetime.now(
            timezone.utc
        )

        start = end - timedelta(
            hours=self.lookback_hours
        )

        request = NewsRequest(
            symbols=symbol,
            start=start,
            end=end,
            sort=Sort.DESC,
            limit=self.limit,
        )

        try:
            response = self.client.get_news(
                request
            )

        except Exception as exc:

            logger.exception(
                "News request failed | symbol=%s",
                symbol,
            )

            raise NewsEngineError(
                f"News unavailable for {symbol}"
            ) from exc

        raw_articles = getattr(
            response,
            "news",
            [],
        )

        if not raw_articles:
            return []

        articles: list[
            NewsItem
        ] = []

        seen: set[str] = set()

        for article in raw_articles:

            headline = str(
                getattr(
                    article,
                    "headline",
                    "",
                )
                or ""
            ).strip()

            if not headline:
                continue

            article_id = str(
                getattr(
                    article,
                    "id",
                    headline,
                )
            )

            # Prevent duplicates.
            dedupe_key = (
                article_id
                or headline.lower()
            )

            if dedupe_key in seen:
                continue

            seen.add(
                dedupe_key
            )

            summary = str(
                getattr(
                    article,
                    "summary",
                    "",
                )
                or ""
            ).strip()

            created_at_raw = getattr(
                article,
                "created_at",
                None,
            )

            if created_at_raw is None:
                continue

            created_at = self._to_utc(
                created_at_raw
            )

            score, importance = (
                self._score_article(
                    headline,
                    summary,
                )
            )

            combined_text = (
                f"{headline} {summary}"
            )

            catalyst = (
                self._classify_catalyst(
                    combined_text
                )
            )

            symbols = list(
                getattr(
                    article,
                    "symbols",
                    [],
                )
                or []
            )

            source = getattr(
                article,
                "source",
                None,
            )

            url = getattr(
                article,
                "url",
                None,
            )

            articles.append(
                NewsItem(
                    article_id=article_id,
                    headline=headline,
                    summary=summary,
                    created_at=created_at,
                    source=(
                        str(source)
                        if source
                        else None
                    ),
                    url=(
                        str(url)
                        if url
                        else None
                    ),
                    symbols=[
                        str(item).upper()
                        for item in symbols
                    ],
                    catalyst=catalyst,
                    sentiment_score=score,
                    importance_score=importance,
                    freshness_weight=(
                        self._freshness_weight(
                            created_at
                        )
                    ),
                )
            )

        return articles

    # ========================================================
    # ANALYSIS
    # ========================================================

    def analyze(
        self,
        symbol: str,
    ) -> NewsAnalysis:

        symbol = self._normalize_symbol(
            symbol
        )

        try:
            articles = self.get_recent_news(
                symbol
            )

        except NewsEngineError as exc:

            return NewsAnalysis(
                symbol=symbol,
                available=False,
                score=None,
                catalyst_detected=False,
                catalyst_type=None,
                article_count=0,
                strongest_headline=None,
                warnings=[
                    str(exc)
                ],
            )

        if not articles:

            return NewsAnalysis(
                symbol=symbol,
                available=True,
                score=None,
                catalyst_detected=False,
                catalyst_type=None,
                article_count=0,
                strongest_headline=None,
                warnings=[
                    "No recent news found."
                ],
            )

        positive_count = 0
        negative_count = 0
        neutral_count = 0

        weighted_total = 0.0
        total_weight = 0.0

        strongest_article = None
        strongest_weight = -1.0

        for article in articles:

            if article.sentiment_score >= 60:
                positive_count += 1

            elif article.sentiment_score <= 40:
                negative_count += 1

            else:
                neutral_count += 1

            weight = (
                article.freshness_weight
                * max(
                    article.importance_score,
                    10.0,
                )
            )

            weighted_total += (
                article.sentiment_score
                * weight
            )

            total_weight += weight

            if weight > strongest_weight:

                strongest_weight = weight
                strongest_article = article

        score = None

        if total_weight > 0:

            score = round(
                weighted_total
                / total_weight,
                2,
            )

        catalyst_articles = [
            article
            for article in articles
            if article.catalyst != "OTHER"
        ]

        catalyst_detected = bool(
            catalyst_articles
        )

        catalyst_type = None

        if strongest_article is not None:
            catalyst_type = (
                strongest_article.catalyst
            )

        return NewsAnalysis(
            symbol=symbol,
            available=True,
            score=score,
            catalyst_detected=(
                catalyst_detected
            ),
            catalyst_type=(
                catalyst_type
            ),
            article_count=len(
                articles
            ),
            strongest_headline=(
                strongest_article.headline
                if strongest_article
                else None
            ),
            positive_count=(
                positive_count
            ),
            negative_count=(
                negative_count
            ),
            neutral_count=(
                neutral_count
            ),
            articles=articles,
        )

    # ========================================================
    # FEATURE INTEGRATION
    # ========================================================

    def apply_to_features(
        self,
        features: FeatureSnapshot,
    ) -> NewsAnalysis:
        """
        Analyze symbol news and attach the resulting
        score to the canonical FeatureSnapshot.
        """

        analysis = self.analyze(
            features.symbol
        )

        features.news_score = (
            analysis.score
        )

        features.metadata[
            "news_available"
        ] = analysis.available

        features.metadata[
            "news_article_count"
        ] = analysis.article_count

        features.metadata[
            "news_catalyst"
        ] = analysis.catalyst_type

        return analysis


# ============================================================
# LAZY SINGLETON
# ============================================================

news_engine: Optional[
    NewsEngine
] = None


def get_news_engine() -> NewsEngine:

    global news_engine

    if news_engine is None:
        news_engine = NewsEngine()

    return news_engine