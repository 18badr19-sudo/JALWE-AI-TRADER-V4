from __future__ import annotations

import json
import math
import os
import threading
import time

from typing import Any, Optional

from core.database import database


# ============================================================
# JALWE LEARNING ENGINE V1
# ============================================================
#
# Conservative PAPER-learning layer.
#
# It never submits orders and never rewrites source code.
# It learns bounded factor multipliers only from CLOSED JALWE
# trades that have a linked feature snapshot.
#
# Safety:
# - waits for a minimum number of completed trades
# - requires a minimum sample count per factor
# - caps every learned multiplier to a narrow range
# - recomputes from historical evidence instead of stacking
#   uncontrolled incremental changes
# ============================================================


class LearningEngine:
    VERSION = "1.0"

    FACTORS = (
        "liquidity",
        "rvol",
        "vwap",
        "trend",
        "momentum",
        "rsi",
        "breakout",
        "compression",
        "news",
        "options_flow",
        "market_regime",
    )

    def __init__(self) -> None:
        self.enabled = (
            os.getenv(
                "JALWE_LEARNING_ENABLED",
                "true",
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

        self.min_trades = max(
            10,
            int(
                os.getenv(
                    "JALWE_LEARNING_MIN_TRADES",
                    "20",
                )
            ),
        )

        self.min_factor_samples = max(
            5,
            int(
                os.getenv(
                    "JALWE_LEARNING_MIN_FACTOR_SAMPLES",
                    "10",
                )
            ),
        )

        self.max_history = max(
            self.min_trades,
            int(
                os.getenv(
                    "JALWE_LEARNING_MAX_HISTORY",
                    "250",
                )
            ),
        )

        self.max_adjustment = min(
            0.25,
            max(
                0.02,
                float(
                    os.getenv(
                        "JALWE_LEARNING_MAX_ADJUSTMENT",
                        "0.15",
                    )
                ),
            ),
        )

        self.edge_scale_pct = max(
            0.25,
            float(
                os.getenv(
                    "JALWE_LEARNING_EDGE_SCALE_PCT",
                    "2.0",
                )
            ),
        )

        self.cache_seconds = max(
            10,
            int(
                os.getenv(
                    "JALWE_LEARNING_CACHE_SECONDS",
                    "60",
                )
            ),
        )

        self._lock = threading.RLock()
        self._weights: dict[str, float] = {
            factor: 1.0
            for factor in self.FACTORS
        }
        self._cache_loaded_at = 0.0

        self._initialize_tables()
        self._refresh_cache(force=True)

    # ========================================================
    # DATABASE
    # ========================================================

    def _initialize_tables(self) -> None:
        with database.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_factor_weights (
                    factor_name TEXT PRIMARY KEY,
                    multiplier REAL NOT NULL DEFAULT 1.0,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    win_rate REAL,
                    avg_edge_pct REAL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    total_closed_trades INTEGER NOT NULL,
                    eligible_trades INTEGER NOT NULL,
                    changed_factors INTEGER NOT NULL,
                    learning_active INTEGER NOT NULL,
                    summary_json TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            for factor in self.FACTORS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO learning_factor_weights (
                        factor_name,
                        multiplier,
                        sample_count,
                        win_rate,
                        avg_edge_pct
                    )
                    VALUES (?, 1.0, 0, NULL, NULL)
                    """,
                    (factor,),
                )

    def _refresh_cache(
        self,
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()

        with self._lock:
            if (
                not force
                and now - self._cache_loaded_at
                < self.cache_seconds
            ):
                return

            try:
                with database.connection() as conn:
                    rows = conn.execute(
                        """
                        SELECT factor_name, multiplier
                        FROM learning_factor_weights
                        """
                    ).fetchall()

                for row in rows:
                    factor = str(row["factor_name"])

                    if factor not in self._weights:
                        continue

                    try:
                        value = float(row["multiplier"])
                    except (TypeError, ValueError):
                        value = 1.0

                    self._weights[factor] = self._clamp(
                        value,
                        1.0 - self.max_adjustment,
                        1.0 + self.max_adjustment,
                    )

                self._cache_loaded_at = now

            except Exception:
                # Scoring must remain usable if learning storage
                # is temporarily unavailable.
                self._weights = {
                    factor: 1.0
                    for factor in self.FACTORS
                }
                self._cache_loaded_at = now

    # ========================================================
    # PUBLIC SCORING API
    # ========================================================

    def get_multiplier(
        self,
        factor: str,
    ) -> float:
        if not self.enabled:
            return 1.0

        self._refresh_cache()

        return float(
            self._weights.get(
                str(factor),
                1.0,
            )
        )

    def apply(
        self,
        factor: str,
        points: float,
    ) -> float:
        return float(points) * self.get_multiplier(
            factor
        )

    def profile(self) -> dict[str, float]:
        self._refresh_cache()

        return {
            factor: round(
                float(value),
                4,
            )
            for factor, value in self._weights.items()
        }

    # ========================================================
    # LEARNING DATA
    # ========================================================

    def _load_closed_trade_samples(
        self,
    ) -> list[dict[str, Any]]:
        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    mt.trade_id,
                    NULL AS signal_id,
                    mt.symbol,
                    json_extract(
                        mt.metadata_json,
                        '$.strategy'
                    ) AS strategy,
                    mt.initial_quantity AS quantity,
                    mt.entry_price,
                    json_extract(
                        mt.metadata_json,
                        '$.realized_pnl'
                    ) AS realized_pnl,
                    json_extract(
                        mt.metadata_json,
                        '$.realized_pnl_pct'
                    ) AS realized_pnl_pct,
                    mt.closed_at,
                    (
                        SELECT fs.snapshot_json
                        FROM feature_snapshots fs
                        WHERE fs.symbol = mt.symbol
                        ORDER BY
                            ABS(
                                julianday(fs.created_at)
                                - julianday(mt.created_at)
                            ) ASC,
                            fs.id DESC
                        LIMIT 1
                    ) AS snapshot_json
                FROM managed_trades mt
                WHERE mt.stage = 'CLOSED'
                ORDER BY
                    COALESCE(mt.closed_at, mt.updated_at) DESC
                LIMIT ?
                """,
                (self.max_history,),
            ).fetchall()

        samples: list[dict[str, Any]] = []

        for row in rows:
            item = dict(row)
            raw_snapshot = item.get(
                "snapshot_json"
            )

            if not raw_snapshot:
                continue

            try:
                snapshot = json.loads(
                    raw_snapshot
                )
            except Exception:
                continue

            if not isinstance(
                snapshot,
                dict,
            ):
                continue

            pnl_pct = self._trade_pnl_pct(
                item
            )

            if pnl_pct is None:
                continue

            samples.append(
                {
                    "trade_id": item.get(
                        "trade_id"
                    ),
                    "signal_id": item.get(
                        "signal_id"
                    ),
                    "symbol": item.get(
                        "symbol"
                    ),
                    "strategy": item.get(
                        "strategy"
                    ),
                    "pnl_pct": pnl_pct,
                    "snapshot": snapshot,
                }
            )

        return samples

    @staticmethod
    def _safe_float(
        value: Any,
    ) -> Optional[float]:
        try:
            if value is None:
                return None

            result = float(value)

            if not math.isfinite(result):
                return None

            return result

        except (
            TypeError,
            ValueError,
        ):
            return None

    def _trade_pnl_pct(
        self,
        row: dict[str, Any],
    ) -> Optional[float]:
        # Derive percentage from realized dollars whenever
        # possible so unit conventions cannot drift.
        realized = self._safe_float(
            row.get("realized_pnl")
        )
        entry = self._safe_float(
            row.get("entry_price")
        )
        quantity = self._safe_float(
            row.get("quantity")
        )

        if (
            realized is not None
            and entry is not None
            and quantity is not None
            and entry > 0
            and quantity > 0
        ):
            notional = (
                entry
                * quantity
            )

            return self._clamp(
                (
                    realized
                    / notional
                    * 100.0
                ),
                -25.0,
                25.0,
            )

        fallback = self._safe_float(
            row.get("realized_pnl_pct")
        )

        if fallback is None:
            return None

        return self._clamp(
            fallback,
            -25.0,
            25.0,
        )

    # ========================================================
    # FACTOR SIGNALS
    # ========================================================

    def _factor_signal(
        self,
        factor: str,
        snapshot: dict[str, Any],
    ) -> Optional[float]:
        def num(key: str) -> Optional[float]:
            return self._safe_float(
                snapshot.get(key)
            )

        if factor == "liquidity":
            value = num("liquidity_score")

            if value is None:
                return None

            return self._clamp(
                (value - 50.0) / 50.0,
                -1.0,
                1.0,
            )

        if factor == "rvol":
            value = num("rvol")

            if value is None:
                return None

            return self._clamp(
                (value - 1.0) / 2.0,
                -1.0,
                1.0,
            )

        if factor == "vwap":
            above = snapshot.get("above_vwap")

            if above is True:
                return 1.0

            if above is False:
                return -1.0

            return None

        if factor == "trend":
            price = num("price")
            ema_9 = num("ema_9")
            ema_20 = num("ema_20")

            if (
                price is None
                or ema_9 is None
                or ema_20 is None
            ):
                return None

            if price > ema_9 > ema_20:
                return 1.0

            if price < ema_9 < ema_20:
                return -1.0

            return 0.0

        if factor == "momentum":
            value = num("momentum_score")

            if value is None:
                return None

            return self._clamp(
                (value - 50.0) / 50.0,
                -1.0,
                1.0,
            )

        if factor == "rsi":
            value = num("rsi_14")

            if value is None:
                return None

            if 50.0 <= value <= 70.0:
                return 1.0

            if 40.0 <= value < 50.0:
                return 0.35

            if value > 80.0:
                return -0.75

            if value < 30.0:
                return -0.60

            return 0.0

        if factor == "breakout":
            value = num(
                "distance_to_high_20_pct"
            )

            if value is None:
                return None

            if -0.5 <= value <= 1.0:
                return 1.0

            if value > 5.0:
                return -0.50

            return 0.0

        if factor == "compression":
            value = num(
                "range_compression"
            )

            if value is None:
                return None

            if value <= 0.35:
                return 1.0

            return 0.0

        if factor == "news":
            value = num("news_score")

            if value is None:
                return None

            return self._clamp(
                (value - 50.0) / 50.0,
                -1.0,
                1.0,
            )

        if factor == "options_flow":
            value = num(
                "options_flow_score"
            )

            if value is None:
                return None

            return self._clamp(
                (value - 50.0) / 50.0,
                -1.0,
                1.0,
            )

        if factor == "market_regime":
            raw = str(
                snapshot.get(
                    "market_regime"
                )
                or ""
            ).upper()

            if raw == "BULL_TREND":
                return 1.0

            if raw == "BEAR_TREND":
                return -0.75

            if raw == "HIGH_VOLATILITY":
                return -0.70

            if raw in {
                "PANIC",
                "RISK_OFF",
            }:
                return -1.0

            if raw:
                return 0.0

            return None

        return None

    # ========================================================
    # LEARNING CYCLE
    # ========================================================

    def run_learning_cycle(
        self,
    ) -> dict[str, Any]:
        samples = (
            self._load_closed_trade_samples()
        )

        total = len(samples)

        learning_active = bool(
            self.enabled
            and total >= self.min_trades
        )

        factor_results: dict[
            str,
            dict[str, Any],
        ] = {}

        changed = 0

        with self._lock:
            with database.connection() as conn:
                for factor in self.FACTORS:
                    evidence: list[
                        tuple[float, float]
                    ] = []

                    wins = 0

                    for sample in samples:
                        signal = self._factor_signal(
                            factor,
                            sample["snapshot"],
                        )

                        if signal is None:
                            continue

                        if abs(signal) < 0.05:
                            continue

                        pnl_pct = float(
                            sample["pnl_pct"]
                        )

                        evidence.append(
                            (
                                signal,
                                pnl_pct,
                            )
                        )

                        if pnl_pct > 0:
                            wins += 1

                    count = len(evidence)

                    avg_edge_pct: Optional[
                        float
                    ] = None

                    win_rate: Optional[
                        float
                    ] = None

                    multiplier = 1.0

                    if count > 0:
                        avg_edge_pct = sum(
                            signal * pnl
                            for signal, pnl
                            in evidence
                        ) / count

                        win_rate = (
                            wins
                            / count
                        )

                    if (
                        learning_active
                        and count
                        >= self.min_factor_samples
                        and avg_edge_pct
                        is not None
                    ):
                        sample_confidence = min(
                            1.0,
                            count / 50.0,
                        )

                        edge_strength = math.tanh(
                            avg_edge_pct
                            / self.edge_scale_pct
                        )

                        multiplier = (
                            1.0
                            + (
                                self.max_adjustment
                                * sample_confidence
                                * edge_strength
                            )
                        )

                        multiplier = self._clamp(
                            multiplier,
                            1.0
                            - self.max_adjustment,
                            1.0
                            + self.max_adjustment,
                        )

                    previous = (
                        self._weights.get(
                            factor,
                            1.0,
                        )
                    )

                    if (
                        abs(
                            multiplier
                            - previous
                        )
                        >= 0.0025
                    ):
                        changed += 1

                    conn.execute(
                        """
                        INSERT INTO learning_factor_weights (
                            factor_name,
                            multiplier,
                            sample_count,
                            win_rate,
                            avg_edge_pct,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(factor_name)
                        DO UPDATE SET
                            multiplier = excluded.multiplier,
                            sample_count = excluded.sample_count,
                            win_rate = excluded.win_rate,
                            avg_edge_pct = excluded.avg_edge_pct,
                            updated_at = CURRENT_TIMESTAMP
                        """,
                        (
                            factor,
                            multiplier,
                            count,
                            win_rate,
                            avg_edge_pct,
                        ),
                    )

                    factor_results[
                        factor
                    ] = {
                        "multiplier": round(
                            multiplier,
                            4,
                        ),
                        "samples": count,
                        "win_rate": (
                            round(
                                win_rate,
                                4,
                            )
                            if win_rate
                            is not None
                            else None
                        ),
                        "avg_edge_pct": (
                            round(
                                avg_edge_pct,
                                4,
                            )
                            if avg_edge_pct
                            is not None
                            else None
                        ),
                    }

                summary = {
                    "version": self.VERSION,
                    "enabled": self.enabled,
                    "learning_active": (
                        learning_active
                    ),
                    "eligible_trades": total,
                    "minimum_trades": (
                        self.min_trades
                    ),
                    "minimum_factor_samples": (
                        self.min_factor_samples
                    ),
                    "changed_factors": changed,
                    "factors": factor_results,
                }

                conn.execute(
                    """
                    INSERT INTO learning_runs (
                        total_closed_trades,
                        eligible_trades,
                        changed_factors,
                        learning_active,
                        summary_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        total,
                        total,
                        changed,
                        int(
                            learning_active
                        ),
                        json.dumps(
                            summary,
                            ensure_ascii=False,
                            default=str,
                        ),
                    ),
                )

            self._weights = {
                factor: float(
                    factor_results[
                        factor
                    ]["multiplier"]
                )
                for factor in self.FACTORS
            }

            self._cache_loaded_at = (
                time.monotonic()
            )

        return summary

    # ========================================================
    # STATUS
    # ========================================================

    def status(
        self,
    ) -> dict[str, Any]:
        self._refresh_cache()

        with database.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    factor_name,
                    multiplier,
                    sample_count,
                    win_rate,
                    avg_edge_pct,
                    updated_at
                FROM learning_factor_weights
                ORDER BY factor_name ASC
                """
            ).fetchall()

            trade_row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM managed_trades
                WHERE stage = 'CLOSED'
                  AND EXISTS (
                      SELECT 1
                      FROM feature_snapshots fs
                      WHERE fs.symbol = managed_trades.symbol
                  )
                """
            ).fetchone()

        eligible = int(
            trade_row["total"]
            if trade_row
            else 0
        )

        active = bool(
            self.enabled
            and eligible >= self.min_trades
        )

        return {
            "version": self.VERSION,
            "enabled": self.enabled,
            "active": active,
            "eligible_trades": eligible,
            "minimum_trades": (
                self.min_trades
            ),
            "minimum_factor_samples": (
                self.min_factor_samples
            ),
            "max_adjustment_pct": round(
                self.max_adjustment
                * 100.0,
                2,
            ),
            "weights": [
                {
                    "factor": row[
                        "factor_name"
                    ],
                    "multiplier": round(
                        float(
                            row[
                                "multiplier"
                            ]
                        ),
                        4,
                    ),
                    "samples": int(
                        row[
                            "sample_count"
                        ]
                    ),
                    "win_rate": (
                        round(
                            float(
                                row[
                                    "win_rate"
                                ]
                            ),
                            4,
                        )
                        if row[
                            "win_rate"
                        ]
                        is not None
                        else None
                    ),
                    "avg_edge_pct": (
                        round(
                            float(
                                row[
                                    "avg_edge_pct"
                                ]
                            ),
                            4,
                        )
                        if row[
                            "avg_edge_pct"
                        ]
                        is not None
                        else None
                    ),
                    "updated_at": row[
                        "updated_at"
                    ],
                }
                for row in rows
            ],
        }

    @staticmethod
    def _clamp(
        value: float,
        minimum: float,
        maximum: float,
    ) -> float:
        return max(
            minimum,
            min(
                float(value),
                maximum,
            ),
        )


_learning_engine: Optional[
    LearningEngine
] = None

_learning_lock = threading.Lock()


def get_learning_engine(
) -> LearningEngine:
    global _learning_engine

    if _learning_engine is None:
        with _learning_lock:
            if _learning_engine is None:
                _learning_engine = (
                    LearningEngine()
                )

    return _learning_engine
