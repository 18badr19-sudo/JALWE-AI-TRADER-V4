from __future__ import annotations

# ============================================================
# PROJECT PATH FIX
# ============================================================
#
# هذا الملف موجود داخل:
#
#   tests\test_crash_recovery.py
#
# لذلك نضيف مجلد المشروع الرئيسي إلى sys.path
# حتى يستطيع Python استيراد:
#
#   core
#   broker
#   trading
#   intelligence
#
# ============================================================

import sys
from pathlib import Path


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parents[1]
)

if str(PROJECT_ROOT) not in sys.path:

    sys.path.insert(
        0,
        str(PROJECT_ROOT),
    )


# ============================================================
# IMPORTS
# ============================================================

from datetime import datetime, timezone
from types import SimpleNamespace

from core.database import database

from core.models import (
    BrokerOrder,
    OrderStatus,
    TradeSide,
)

from trading.recovery_engine import (
    get_recovery_engine,
)


# ============================================================
# TEST CONFIG
# ============================================================

ALL_GATES = {

    "market_data": True,

    "features": True,

    "ai": True,

    "opportunity": True,

    "market_regime": True,

    "strategy_router": True,

    "session_strategy": True,

    "trigger": True,

    "breakout_confirmation": True,

    "risk": True,
}


# ============================================================
# CLEANUP
# ============================================================

def cleanup() -> None:
    """
    Remove only simulated crash-recovery records.

    Real JALWE trades are untouched.
    """

    with database.connection() as conn:

        # ----------------------------------------------------
        # MANAGED TRADES
        # ----------------------------------------------------

        conn.execute(
            """
            DELETE FROM managed_trades
            WHERE trade_id LIKE 'TRADE-SIM-CRASH-%'
               OR trade_id LIKE 'SIM-CRASH-%'
               OR entry_order_id LIKE 'SIM-CRASH-%'
            """
        )

        # ----------------------------------------------------
        # ENTRY INTENTS
        # ----------------------------------------------------

        conn.execute(
            """
            DELETE FROM entry_intents
            WHERE intent_id LIKE 'SIM-CRASH-%'
               OR client_order_id
                  LIKE 'JALWE-FINAL-CRASH-%'
            """
        )

        # ----------------------------------------------------
        # BROKER ORDER SNAPSHOTS
        # ----------------------------------------------------

        conn.execute(
            """
            DELETE FROM broker_orders
            WHERE order_id LIKE 'SIM-CRASH-%'
               OR client_order_id
                  LIKE 'JALWE-FINAL-CRASH-%'
            """
        )


# ============================================================
# CREATE TEST ENTRY INTENT
# ============================================================

def create_intent(
    tag: str,
    symbol: str,
) -> tuple[str, str]:

    intent_id = (
        f"SIM-CRASH-{tag}"
    )

    client_order_id = (
        f"JALWE-FINAL-CRASH-{tag}"
    )

    database.create_entry_intent(

        intent_id=intent_id,

        client_order_id=(
            client_order_id
        ),

        symbol=symbol,

        quantity=5,

        planned_entry=10.00,

        stop_price=9.80,

        target_1=10.40,

        target_2=10.60,

        target_3=10.80,

        risk_pct=1.00,

        strategy=(
            "CRASH_TEST"
        ),

        setup_grade="A",

        decision_state=(
            "READY_FOR_PAPER_EXECUTION"
        ),

        decision_gates=(
            ALL_GATES
        ),

        metadata={

            "simulation": True,

            "crash_case": tag,
        },
    )

    return (
        intent_id,
        client_order_id,
    )


# ============================================================
# FAKE ALPACA RAW ORDER
# ============================================================

def fake_raw_order(
    *,
    order_id: str,
    client_order_id: str,
    symbol: str,
    status: str,
    filled_quantity: int = 0,
    filled_price: float | None = None,
):

    return SimpleNamespace(

        id=order_id,

        client_order_id=(
            client_order_id
        ),

        symbol=symbol,

        side="buy",

        qty=5,

        status=status,

        filled_qty=(
            filled_quantity
        ),

        filled_avg_price=(
            filled_price
        ),

        submitted_at=(
            datetime.now(
                timezone.utc
            )
        ),
    )


# ============================================================
# SAVE LOCAL BROKER SNAPSHOT
# ============================================================

def save_local_snapshot(
    *,
    intent_id: str,
    client_order_id: str,
    order_id: str,
    symbol: str,
    status: OrderStatus,
    filled_quantity: int = 0,
    filled_price: float | None = None,
) -> None:

    order = BrokerOrder(

        symbol=symbol,

        side=TradeSide.BUY,

        quantity=5,

        order_id=order_id,

        client_order_id=(
            client_order_id
        ),

        status=status,

        requested_price=10.00,

        filled_price=(
            filled_price
        ),

        filled_quantity=(
            filled_quantity
        ),

        submitted_at=(
            datetime.now(
                timezone.utc
            )
        ),

        metadata={
            "simulation": True,
            "crash_test": True,
        },
    )

    database.register_entry_submission(
        intent_id,
        order,
    )


# ============================================================
# TEST 1 - PREPARED CRASH
# ============================================================

def test_prepared_crash(
    recovery,
) -> None:
    """
    Scenario:

        SQLite:
            PREPARED

        Alpaca:
            order exists / accepted

    Meaning:

        Computer crashed after Alpaca accepted
        the order but before broker_order_id
        was saved locally.

    Recovery must find it using client_order_id.
    """

    intent_id, client_id = (
        create_intent(
            "PREPARED",
            "TCR1",
        )
    )

    before = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    assert (
        before["state"]
        == "PREPARED"
    )

    recovery._lookup_entry_order = (
        lambda intent:
        fake_raw_order(

            order_id=(
                "SIM-CRASH-ORDER-PREPARED"
            ),

            client_order_id=(
                client_id
            ),

            symbol="TCR1",

            status="accepted",

            filled_quantity=0,
        )
    )

    result = (
        recovery
        .recover_entry_intent(
            before
        )
    )

    after = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    assert (
        after["state"]
        == "SUBMITTED"
    )

    assert not result.get(
        "managed",
        False,
    )

    print(
        "1 PREPARED CRASH:",
        after["state"],
        "| MANAGED:",
        result.get(
            "managed",
            False,
        ),
    )


# ============================================================
# TEST 2 - SUBMITTED CRASH
# ============================================================

def test_submitted_crash(
    recovery,
) -> None:
    """
    Scenario:

        SQLite:
            SUBMITTED

        Alpaca:
            still accepted

    Recovery should preserve the pending order.
    """

    intent_id, client_id = (
        create_intent(
            "SUBMITTED",
            "TCR2",
        )
    )

    save_local_snapshot(

        intent_id=(
            intent_id
        ),

        client_order_id=(
            client_id
        ),

        order_id=(
            "SIM-CRASH-ORDER-SUBMITTED"
        ),

        symbol="TCR2",

        status=(
            OrderStatus.ACCEPTED
        ),
    )

    row = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    recovery._lookup_entry_order = (
        lambda intent:
        fake_raw_order(

            order_id=(
                "SIM-CRASH-ORDER-SUBMITTED"
            ),

            client_order_id=(
                client_id
            ),

            symbol="TCR2",

            status="accepted",
        )
    )

    result = (
        recovery
        .recover_entry_intent(
            row
        )
    )

    after = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    assert (
        after["state"]
        == "SUBMITTED"
    )

    assert not result.get(
        "managed",
        False,
    )

    print(
        "2 SUBMITTED CRASH:",
        after["state"],
        "| MANAGED:",
        result.get(
            "managed",
            False,
        ),
    )


# ============================================================
# TEST 3 - PARTIAL FILL CRASH
# ============================================================

def test_partial_crash(
    recovery,
) -> None:
    """
    Scenario:

        5 requested
        2 filled
        order still active

    Recovery must preserve:
        filled_quantity = 2

    and must NOT create ManagedTrade yet
    while the entry order is still active.
    """

    intent_id, client_id = (
        create_intent(
            "PARTIAL",
            "TCR3",
        )
    )

    save_local_snapshot(

        intent_id=(
            intent_id
        ),

        client_order_id=(
            client_id
        ),

        order_id=(
            "SIM-CRASH-ORDER-PARTIAL"
        ),

        symbol="TCR3",

        status=(
            OrderStatus
            .PARTIALLY_FILLED
        ),

        filled_quantity=2,

        filled_price=10.02,
    )

    row = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    recovery._lookup_entry_order = (
        lambda intent:
        fake_raw_order(

            order_id=(
                "SIM-CRASH-ORDER-PARTIAL"
            ),

            client_order_id=(
                client_id
            ),

            symbol="TCR3",

            status=(
                "partially_filled"
            ),

            filled_quantity=2,

            filled_price=10.02,
        )
    )

    result = (
        recovery
        .recover_entry_intent(
            row
        )
    )

    after = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    assert (
        after["state"]
        == "PARTIALLY_FILLED"
    )

    assert (
        int(
            after[
                "filled_quantity"
            ]
        )
        == 2
    )

    assert not result.get(
        "managed",
        False,
    )

    print(
        "3 PARTIAL CRASH:",
        after["state"],
        "| FILLED:",
        after[
            "filled_quantity"
        ],
        "| MANAGED:",
        result.get(
            "managed",
            False,
        ),
    )


# ============================================================
# TEST 4 - FILLED CRASH
# ============================================================

def test_filled_crash(
    recovery,
) -> None:
    """
    Scenario:

        SQLite may still say PREPARED
        but Alpaca says FILLED.

    Recovery must:

        - detect fill
        - verify broker position
        - create ManagedTrade
        - mark EntryIntent MANAGED
    """

    intent_id, client_id = (
        create_intent(
            "FILLED",
            "TCR4",
        )
    )

    row = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    recovery._lookup_entry_order = (
        lambda intent:
        fake_raw_order(

            order_id=(
                "SIM-CRASH-ORDER-FILLED"
            ),

            client_order_id=(
                client_id
            ),

            symbol="TCR4",

            status="filled",

            filled_quantity=5,

            filled_price=10.05,
        )
    )

    recovery.reconciliation.verify_position = (
        lambda symbol: {

            "exists": True,

            "symbol": symbol,

            "quantity": 5,

            "average_entry_price": (
                10.05
            ),

            "market_value": (
                50.25
            ),

            "unrealized_pl": (
                0.0
            ),
        }
    )

    result = (
        recovery
        .recover_entry_intent(
            row
        )
    )

    after = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    assert result.get(
        "managed",
        False,
    )

    assert (
        after["state"]
        == "MANAGED"
    )

    assert after.get(
        "managed_trade_id"
    )

    print(
        "4 FILLED CRASH:",
        after["state"],
        "| TRADE:",
        after[
            "managed_trade_id"
        ],
    )


# ============================================================
# TEST 5 - PARTIAL FILL THEN CANCEL
# ============================================================

def test_partial_cancel_crash(
    recovery,
) -> None:
    """
    Critical scenario:

        requested = 5
        filled = 2
        remaining 3 canceled

    The 2 filled shares really exist.

    Recovery must NOT throw them away.

    It must create ManagedTrade using
    the confirmed broker quantity.
    """

    intent_id, client_id = (
        create_intent(
            "PARTIAL-CANCEL",
            "TCR5",
        )
    )

    save_local_snapshot(

        intent_id=(
            intent_id
        ),

        client_order_id=(
            client_id
        ),

        order_id=(
            "SIM-CRASH-ORDER-PARTIAL-CANCEL"
        ),

        symbol="TCR5",

        status=(
            OrderStatus
            .PARTIALLY_FILLED
        ),

        filled_quantity=2,

        filled_price=10.03,
    )

    row = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    recovery._lookup_entry_order = (
        lambda intent:
        fake_raw_order(

            order_id=(
                "SIM-CRASH-ORDER-PARTIAL-CANCEL"
            ),

            client_order_id=(
                client_id
            ),

            symbol="TCR5",

            status="canceled",

            filled_quantity=2,

            filled_price=10.03,
        )
    )

    recovery.reconciliation.verify_position = (
        lambda symbol: {

            "exists": True,

            "symbol": symbol,

            "quantity": 2,

            "average_entry_price": (
                10.03
            ),

            "market_value": (
                20.06
            ),

            "unrealized_pl": (
                0.0
            ),
        }
    )

    result = (
        recovery
        .recover_entry_intent(
            row
        )
    )

    after = (
        database
        .get_entry_intent_row(
            intent_id
        )
    )

    assert result.get(
        "managed",
        False,
    )

    assert (
        after["state"]
        == "MANAGED"
    )

    assert after.get(
        "managed_trade_id"
    )

    print(
        "5 PARTIAL + CANCEL:",
        after["state"],
        "| TRADE:",
        after[
            "managed_trade_id"
        ],
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "========================================"
    )

    print(
        "JALWE V4 CRASH RECOVERY TEST"
    )

    print(
        "NO REAL BROKER ORDERS WILL BE SENT"
    )

    print(
        "========================================"
    )

    print()

    # --------------------------------------------------------
    # CLEAN OLD TEST DATA
    # --------------------------------------------------------

    cleanup()

    recovery = (
        get_recovery_engine()
    )

    # --------------------------------------------------------
    # SAVE ORIGINAL METHODS
    # --------------------------------------------------------

    original_lookup = (
        recovery
        ._lookup_entry_order
    )

    original_verify_position = (
        recovery
        .reconciliation
        .verify_position
    )

    try:

        # ====================================================
        # TEST 1
        # ====================================================

        test_prepared_crash(
            recovery
        )

        # ====================================================
        # TEST 2
        # ====================================================

        test_submitted_crash(
            recovery
        )

        # ====================================================
        # TEST 3
        # ====================================================

        test_partial_crash(
            recovery
        )

        # ====================================================
        # TEST 4
        # ====================================================

        test_filled_crash(
            recovery
        )

        # Restore position verification before
        # moving into the next independent test.

        recovery.reconciliation.verify_position = (
            original_verify_position
        )

        # ====================================================
        # TEST 5
        # ====================================================

        test_partial_cancel_crash(
            recovery
        )

        print()

        print(
            "========================================"
        )

        print(
            "ALL CRASH RECOVERY TESTS PASSED"
        )

        print(
            "========================================"
        )

    finally:

        # ====================================================
        # RESTORE ORIGINAL METHODS
        # ====================================================

        recovery._lookup_entry_order = (
            original_lookup
        )

        recovery.reconciliation.verify_position = (
            original_verify_position
        )

        # ====================================================
        # REMOVE ALL SIMULATED DATA
        # ====================================================

        cleanup()

        print()

        print(
            "SIM CRASH DATA CLEANED"
        )

        print(
            "UNRESOLVED AFTER CLEANUP:",
            len(
                database
                .get_unresolved_entry_intent_rows()
            ),
        )

        print(
            "ACTIVE TRADES AFTER CLEANUP:",
            len(
                database
                .get_active_managed_trade_rows()
            ),
        )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()