"""Recovery for the 2026-05-17 13:18 zombie-reconcile incident.

Sequence:
  1. Re-place 10 SL orders that were cancelled by position_monitor's heuristic
     (the legitimate 0.0541 ETH portion of the LONG that lost its stop-loss).
  2. Look up live OIDs for ALL 32 trigger orders covering the 16 legitimate lots
     (rows 337-352) and populate tp_oid/sl_oid in the DB. With OIDs stored, the
     heuristic fallback in _cancel_counterpart_order / _find_matching_trade is
     never reached.
  3. Restore status='OPEN' (and clear pnl/closed_at) for the 12 rows that were
     wrongly closed during the incident.
  4. Mark rows 266-336 (71 zombies) as MANUAL with pnl=0.

Idempotent: re-running skips SL placement when a matching SL already exists.
"""
import asyncio
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperliquid.utils import constants
from hyperliquid.info import Info
from config.settings import load_settings
from services.trade_executor import (
    build_exchange, safe_spot_meta, _round_price, _trigger_limit_px,
    _match_trigger_oid,
)
from storage.trade_log import DB_PATH

COIN = "ETH"
IS_LONG = True
CLOSING_IS_BUY = not IS_LONG

# (trade_id, size, sl_px) — restore EXACTLY what was cancelled.
SLS_TO_RESTORE = [
    (337, 0.0083, 2153.10),
    (339, 0.0047, 2098.90),
    (342, 0.0047, 2122.25),
    (346, 0.0047, 2124.87),
    (347, 0.0047, 2097.66),
    (348, 0.0047, 2070.45),
    (349, 0.0082, 2150.51),
    (350, 0.0047, 2123.12),
    (351, 0.0047, 2095.73),
    (352, 0.0047, 2068.34),
]

LEGITIMATE_ROW_IDS = list(range(337, 353))
ZOMBIE_ROW_IDS = list(range(266, 337))


def fetch_flat_orders(info, settings):
    raw = info.frontend_open_orders(settings.hl_account_address)
    flat = []
    for o in raw:
        flat.append(o)
        flat.extend(o.get("children", []))
    return flat


async def place_missing_sls(info, settings, exchange) -> None:
    print("\n=== STEP 1: Re-place cancelled SLs ===")
    flat = await asyncio.to_thread(fetch_flat_orders, info, settings)
    for trade_id, size, sl_px in SLS_TO_RESTORE:
        existing = _match_trigger_oid(flat, COIN, sl_px, size)
        if existing:
            print(f"  [skip] trade {trade_id}: SL already alive | oid={existing}")
            continue

        sl_px_r = _round_price(exchange, COIN, sl_px)
        sl_limit = _trigger_limit_px(exchange, COIN, CLOSING_IS_BUY, sl_px_r)
        order = {
            "coin": COIN, "is_buy": CLOSING_IS_BUY, "sz": size,
            "limit_px": sl_limit, "reduce_only": True,
            "order_type": {"trigger": {"triggerPx": sl_px_r, "isMarket": True, "tpsl": "sl"}},
        }
        result = await asyncio.to_thread(exchange.bulk_orders, [order], None)
        if result.get("status") != "ok":
            print(f"  [FAIL] trade {trade_id}: result={result}")
            continue
        inner = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
        print(f"  [ok]   trade {trade_id} sl={sl_px} sz={size} | inner={inner!r}")
        await asyncio.sleep(0.2)
        flat = await asyncio.to_thread(fetch_flat_orders, info, settings)


def _assign_unique_oids(rows, orders, coin, price_field, kind_filter):
    """1:1 greedy assignment of DB rows to HL trigger orders. Picks the lowest
    (price_distance) pair first, removes both from the pool, repeats.

    Returns {row_id: oid_str}. Rows with no valid order get omitted (then stored as NULL).
    """
    candidates = [
        o for o in orders
        if o.get("coin") == coin and o.get("triggerPx") and kind_filter(o.get("orderType", ""))
    ]
    pairs = []
    for r in rows:
        target_px = float(r[price_field])
        size = float(r["size"])
        for o in candidates:
            try:
                o_px = float(o["triggerPx"])
                o_sz = float(o.get("sz", 0))
            except (TypeError, ValueError):
                continue
            if target_px <= 0 or size <= 0:
                continue
            if abs(o_px - target_px) / target_px >= 0.001:
                continue
            if abs(o_sz - size) / size >= 0.01:
                continue
            pairs.append((abs(o_px - target_px), r["id"], o["oid"]))
    pairs.sort()  # smallest distance first
    used_oids: set = set()
    used_rows: set = set()
    assignment: dict = {}
    for _, row_id, oid in pairs:
        if row_id in used_rows or oid in used_oids:
            continue
        assignment[row_id] = str(oid)
        used_rows.add(row_id)
        used_oids.add(oid)
    return assignment


async def populate_oids_for_legitimate_rows(info, settings) -> None:
    print("\n=== STEP 2: Populate tp_oid/sl_oid for legitimate rows 337-352 (unique 1:1) ===")
    flat = await asyncio.to_thread(fetch_flat_orders, info, settings)
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT id, size, tp_px, sl_px FROM trades WHERE id BETWEEN 337 AND 352"
        ).fetchall()
        tp_assign = _assign_unique_oids(
            rows, flat, COIN, "tp_px",
            lambda ot: "profit" in ot.lower(),
        )
        sl_assign = _assign_unique_oids(
            rows, flat, COIN, "sl_px",
            lambda ot: "stop" in ot.lower(),
        )
        for r in rows:
            tp_oid = tp_assign.get(r["id"])
            sl_oid = sl_assign.get(r["id"])
            con.execute(
                "UPDATE trades SET tp_oid=?, sl_oid=? WHERE id=?",
                (tp_oid, sl_oid, r["id"]),
            )
            print(f"  row {r['id']}: tp_oid={tp_oid or 'MISSING'} sl_oid={sl_oid or 'MISSING'}")
        con.commit()
    finally:
        con.close()


def restore_legitimate_statuses() -> None:
    print("\n=== STEP 3: Restore status=OPEN for wrongly-closed rows 337-352 ===")
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "UPDATE trades SET status='OPEN', pnl=NULL, closed_at=NULL, close_fee=NULL "
            "WHERE id BETWEEN 337 AND 352 AND status != 'OPEN'"
        )
        print(f"  Restored {cur.rowcount} rows to OPEN")
        con.commit()
    finally:
        con.close()


def mark_zombies_manual() -> None:
    print("\n=== STEP 4: Mark zombies 266-336 as MANUAL ===")
    # No status filter — position_monitor already (wrongly) wrote bogus TP/SL closures
    # against these during the incident, so we need to overwrite those too.
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "UPDATE trades SET status='MANUAL', pnl=0, close_fee=0, "
            "closed_at=COALESCE(closed_at, datetime('now')) "
            "WHERE id BETWEEN 266 AND 336"
        )
        print(f"  Marked {cur.rowcount} zombie rows as MANUAL with pnl=0")
        con.commit()
    finally:
        con.close()


def verify() -> None:
    print("\n=== STEP 5: Verify final DB state ===")
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, size, status, tp_oid, sl_oid FROM trades "
            "WHERE id BETWEEN 337 AND 352 ORDER BY id"
        ).fetchall()
        for r in rows:
            tp = r["tp_oid"] or "NULL"
            sl = r["sl_oid"] or "NULL"
            print(f"  row {r['id']:3} sz={r['size']:.4f} status={r['status']:6} tp_oid={tp} sl_oid={sl}")
        open_count = con.execute(
            "SELECT COUNT(*) FROM trades WHERE status IN ('OPEN','UNPROTECTED')"
        ).fetchone()[0]
        zombie_count = con.execute(
            "SELECT COUNT(*) FROM trades WHERE id BETWEEN 266 AND 336 AND status='MANUAL'"
        ).fetchone()[0]
        print(f"\n  Total OPEN/UNPROTECTED rows: {open_count} (expect 16)")
        print(f"  Zombies marked MANUAL: {zombie_count} (expect 71)")
    finally:
        con.close()


async def main() -> None:
    settings = load_settings()
    info = Info(constants.MAINNET_API_URL, skip_ws=True,
                spot_meta=safe_spot_meta(constants.MAINNET_API_URL))
    exchange = build_exchange(settings)

    await place_missing_sls(info, settings, exchange)
    await populate_oids_for_legitimate_rows(info, settings)
    restore_legitimate_statuses()
    mark_zombies_manual()
    verify()


asyncio.run(main())
