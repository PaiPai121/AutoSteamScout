"""Small durable ledger for scans and cash conversion."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Iterator
from uuid import uuid4

from .domain import Assessment, Offer, PricingPolicy, Verdict, money, profit_meets_thresholds, project_cash
from .products import MarketProduct, ProductIdentity, product_guard
from .reconcile import REPORT_START_CHINA, SaleReconciliation, in_report_period, reconcile_orders
from .titles import TitleCatalog
from .wallet_accounting import movement_kind, reconcile_wallet

if TYPE_CHECKING:
    from .orders import AccountOrder
    from .payouts import WalletBill


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _order_scope_sql(alias: str = "") -> str:
    """SQLite normalizes dated Sonkwo timestamps to UTC; SteamPy uses China local time."""
    prefix = f"{alias}." if alias else ""
    start = REPORT_START_CHINA.strftime("%Y-%m-%d %H:%M:%S")
    return (f"(CASE WHEN {prefix}platform='sonkwo' "
            f"THEN datetime({prefix}occurred_at,'+8 hours') "
            f"ELSE datetime({prefix}occurred_at) END >= '{start}')")


class InvalidTradeTransition(ValueError):
    pass


class Repository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id TEXT PRIMARY KEY, keyword TEXT NOT NULL, status TEXT NOT NULL,
                    stage TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    discovered INTEGER NOT NULL DEFAULT 0, processed INTEGER NOT NULL DEFAULT 0,
                    opportunities INTEGER NOT NULL DEFAULT 0, warning_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS assessments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES scan_runs(id),
                    offer_title TEXT NOT NULL, offer_url TEXT NOT NULL,
                    buy_price TEXT NOT NULL, market_title TEXT, market_url TEXT,
                    market_price TEXT, target_sell_price TEXT, net_profit TEXT, roi TEXT,
                    verdict TEXT NOT NULL, reason TEXT NOT NULL, observed_at TEXT NOT NULL,
                    UNIQUE(run_id, offer_url)
                );
                CREATE INDEX IF NOT EXISTS idx_assessments_run ON assessments(run_id, id);
                CREATE TABLE IF NOT EXISTS market_observations (
                    run_id TEXT NOT NULL REFERENCES scan_runs(id), product_id TEXT NOT NULL,
                    region TEXT NOT NULL, fingerprint TEXT NOT NULL, observed_at TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY(run_id,product_id,region,fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_market_observations_scope
                    ON market_observations(product_id,region,fingerprint,observed_at);
                CREATE TABLE IF NOT EXISTS product_mappings (
                    sonkwo_id TEXT PRIMARY KEY, steampy_id TEXT NOT NULL,
                    offer_fingerprint TEXT NOT NULL, market_fingerprint TEXT NOT NULL,
                    offer_json TEXT NOT NULL, market_json TEXT NOT NULL,
                    note TEXT NOT NULL, confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id TEXT PRIMARY KEY, assessment_id INTEGER UNIQUE REFERENCES assessments(id),
                    title TEXT NOT NULL, offer_url TEXT NOT NULL, state TEXT NOT NULL,
                    actual_cost TEXT NOT NULL, ask_price TEXT, sale_gross TEXT,
                    sale_fee TEXT, payout TEXT, reference TEXT,
                    purchased_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL REFERENCES trades(id),
                    event TEXT NOT NULL, details TEXT NOT NULL, happened_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_orders (
                    platform TEXT NOT NULL, order_id TEXT NOT NULL,
                    title TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    status TEXT NOT NULL, quantity INTEGER NOT NULL,
                    amount TEXT, fee TEXT, net_amount TEXT,
                    channel TEXT NOT NULL DEFAULT 'ordinary',
                    alternate_titles TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(platform, order_id)
                );
                CREATE TABLE IF NOT EXISTS account_order_lines (
                    platform TEXT NOT NULL, order_id TEXT NOT NULL,
                    line_no INTEGER NOT NULL, title TEXT NOT NULL,
                    alternate_titles TEXT NOT NULL, quantity INTEGER NOT NULL,
                    unit_cost TEXT NOT NULL,
                    refunded_quantity INTEGER NOT NULL DEFAULT 0,
                    refund_amount TEXT NOT NULL DEFAULT '0.00',
                    refund_source TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(platform, order_id, line_no),
                    FOREIGN KEY(platform,order_id) REFERENCES account_orders(platform,order_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS purchase_refund_confirmations (
                    order_id TEXT NOT NULL, line_no INTEGER NOT NULL,
                    title TEXT NOT NULL, quantity INTEGER NOT NULL, unit_cost TEXT NOT NULL,
                    refunded_quantity INTEGER NOT NULL, refund_amount TEXT NOT NULL,
                    evidence TEXT NOT NULL, confirmed_at TEXT NOT NULL,
                    PRIMARY KEY(order_id,line_no)
                );
                CREATE TABLE IF NOT EXISTS account_sale_reconciliation (
                    sale_order_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    reason TEXT NOT NULL, buy_order_id TEXT,
                    buy_line_no INTEGER, buy_unit_no INTEGER,
                    buy_unit_cost TEXT, order_stage_spread TEXT
                );
                CREATE TABLE IF NOT EXISTS account_reconciliation_meta (
                    id INTEGER PRIMARY KEY CHECK(id = 1), completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_order_sync (
                    id INTEGER PRIMARY KEY CHECK(id = 1), completed_at TEXT NOT NULL,
                    source_coverage TEXT NOT NULL DEFAULT 'ordinary',
                    source_synced_at TEXT NOT NULL DEFAULT '{}',
                    warnings TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS wallet_bills (
                    bill_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                    amount TEXT NOT NULL, tx_type TEXT NOT NULL,
                    tx_id TEXT NOT NULL, cd_flag TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wallet_sync (
                    id INTEGER PRIMARY KEY CHECK(id = 1), completed_at TEXT NOT NULL,
                    balance TEXT, pending_balance TEXT, balance_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bank_receipts (
                    bill_id TEXT PRIMARY KEY, received_at TEXT NOT NULL,
                    amount TEXT NOT NULL, note TEXT NOT NULL, recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_account_orders_platform_time
                    ON account_orders(platform, occurred_at DESC);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(account_orders)")}
            assessment_columns = {row["name"] for row in db.execute("PRAGMA table_info(assessments)")}
            if "pricing" not in assessment_columns:
                db.execute("ALTER TABLE assessments ADD COLUMN pricing TEXT")
            if "offer_alternate_titles" not in assessment_columns:
                db.execute("ALTER TABLE assessments ADD COLUMN offer_alternate_titles TEXT NOT NULL DEFAULT '[]'")
            for name, declaration in (("matching", "TEXT"), ("market_candidates", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in assessment_columns:
                    db.execute(f"ALTER TABLE assessments ADD COLUMN {name} {declaration}")
            if "liquidity" not in assessment_columns:
                db.execute("ALTER TABLE assessments ADD COLUMN liquidity TEXT")
            if "alternate_titles" not in columns:
                db.execute("ALTER TABLE account_orders ADD COLUMN alternate_titles TEXT NOT NULL DEFAULT '[]'")
            if "channel" not in columns:
                db.execute("ALTER TABLE account_orders ADD COLUMN channel TEXT NOT NULL DEFAULT 'ordinary'")
            for name in ("product_id", "stock_created_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE account_orders ADD COLUMN {name} TEXT")
            wallet_columns = {row["name"] for row in db.execute("PRAGMA table_info(wallet_sync)")}
            for name in ("balance", "pending_balance", "balance_at"):
                if name not in wallet_columns:
                    db.execute(f"ALTER TABLE wallet_sync ADD COLUMN {name} TEXT")
            line_columns = {row["name"] for row in db.execute("PRAGMA table_info(account_order_lines)")}
            if "product_id" not in line_columns:
                db.execute("ALTER TABLE account_order_lines ADD COLUMN product_id TEXT")
            for name, declaration in (("refunded_quantity", "INTEGER NOT NULL DEFAULT 0"),
                                      ("refund_amount", "TEXT NOT NULL DEFAULT '0.00'"),
                                      ("refund_source", "TEXT NOT NULL DEFAULT ''")):
                if name not in line_columns:
                    db.execute(f"ALTER TABLE account_order_lines ADD COLUMN {name} {declaration}")
            # Older snapshots retained full-order refunds but omitted per-item
            # metadata. This known parent state is sufficient to migrate those
            # items without pretending that the platform was refreshed.
            full_refunds = db.execute("""SELECT l.order_id,l.line_no,l.quantity,l.unit_cost
                FROM account_order_lines l JOIN account_orders o
                ON o.platform=l.platform AND o.order_id=l.order_id
                WHERE l.platform='sonkwo' AND o.status='refunded'
                AND l.refunded_quantity<l.quantity""").fetchall()
            for line in full_refunds:
                db.execute("""UPDATE account_order_lines SET refunded_quantity=quantity,
                    refund_amount=?,refund_source='platform'
                    WHERE platform='sonkwo' AND order_id=? AND line_no=?""",
                    (str(money(Decimal(line["unit_cost"]) * line["quantity"])),
                     line["order_id"], line["line_no"]))
            sync_columns = {row["name"] for row in db.execute("PRAGMA table_info(account_order_sync)")}
            if "source_coverage" not in sync_columns:
                db.execute("""ALTER TABLE account_order_sync ADD COLUMN source_coverage TEXT
                              NOT NULL DEFAULT 'ordinary'""")
            if "source_synced_at" not in sync_columns:
                db.execute("""ALTER TABLE account_order_sync ADD COLUMN source_synced_at TEXT
                              NOT NULL DEFAULT '{}'""")
            if "warnings" not in sync_columns:
                db.execute("""ALTER TABLE account_order_sync ADD COLUMN warnings TEXT
                              NOT NULL DEFAULT '[]'""")
            # Earlier releases assigned costs to these rows but labelled them as
            # provisional. Historical accounting now accepts that FIFO allocation.
            db.execute("""UPDATE account_sale_reconciliation
                       SET status='fifo', reason='历史订单按先购先销配对购买来源和成本'
                       WHERE status='review' AND buy_order_id IS NOT NULL""")
            db.execute("""UPDATE account_sale_reconciliation
                       SET reason='单一购买来源，按先购先销计入成本'
                       WHERE status='candidate' AND reason<>?""",
                       ("单一购买来源，按先购先销计入成本",))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def begin_run(self, run_id: str, keyword: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO scan_runs(id,keyword,status,stage,started_at) VALUES(?,?,?,?,?)",
                (run_id, keyword, "running", "connecting", utc_now()),
            )

    def update_run(self, run_id: str, status: str, stage: str, discovered: int,
                   processed: int, opportunities: int, warning_count: int,
                   error: str | None = None, finished: bool = False) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE scan_runs SET status=?, stage=?, discovered=?, processed=?,
                   opportunities=?, warning_count=?, error=?, finished_at=? WHERE id=?""",
                (status, stage, discovered, processed, opportunities, warning_count,
                 error, utc_now() if finished else None, run_id),
            )

    def save_assessment(self, run_id: str, result: Assessment) -> int:
        quote = result.quote
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO assessments(run_id,offer_title,offer_url,buy_price,
                   market_title,market_url,market_price,target_sell_price,net_profit,roi,verdict,reason,observed_at,
                   pricing,offer_alternate_titles,matching,market_candidates,liquidity)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, result.offer.title, result.offer.url, str(result.offer.cost),
                 quote.title if quote else None, quote.url if quote else None,
                 str(quote.lowest_price) if quote else None,
                 str(result.target_sell_price) if result.target_sell_price is not None else None,
                 str(result.net_profit) if result.net_profit is not None else None,
                 str(result.roi) if result.roi is not None else None,
                 result.verdict.value, result.reason, utc_now(),
                 json.dumps({**result.pricing, "calculated_at": utc_now()}) if result.pricing else None,
                 json.dumps(result.offer.alternate_titles, ensure_ascii=False),
                 json.dumps(result.matching, ensure_ascii=False) if result.matching else None,
                 json.dumps(result.market_candidates, ensure_ascii=False),
                 json.dumps(result.liquidity, ensure_ascii=False) if result.liquidity else None),
            )
            if quote and quote.demand and quote.product:
                from .demand import scoped_snapshot
                if scoped_snapshot(quote.demand, quote.product):
                    snapshot = {key: value for key, value in quote.demand.items() if key != "trend"}
                    db.execute("""INSERT INTO market_observations
                        (run_id,product_id,region,fingerprint,observed_at,snapshot) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(run_id,product_id,region,fingerprint) DO UPDATE
                        SET observed_at=excluded.observed_at,snapshot=excluded.snapshot""",
                        (run_id, snapshot["product_id"], snapshot["region"], snapshot["fingerprint"],
                         snapshot["observed_at"], json.dumps(snapshot, ensure_ascii=False)))
            return int(cursor.lastrowid)

    def market_history(self, snapshot: dict) -> list[dict]:
        from datetime import timedelta
        from .demand import timestamp, HISTORY_DAYS
        since = (timestamp(snapshot["observed_at"]) - timedelta(days=HISTORY_DAYS)).astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            rows = db.execute("""SELECT snapshot FROM market_observations
                WHERE product_id=? AND region=? AND fingerprint=? AND observed_at>=? AND observed_at<?
                ORDER BY observed_at DESC LIMIT 2000""",
                (snapshot["product_id"], snapshot["region"], snapshot["fingerprint"], since, snapshot["observed_at"])).fetchall()
        return [json.loads(row["snapshot"]) for row in reversed(rows)]

    def reclassify_legacy_demand(self) -> int:
        """Old asking-price spreads have no demand evidence; keep their prices."""
        with self._connect() as db:
            rows = db.execute("SELECT id,run_id FROM assessments WHERE verdict='opportunity' AND liquidity IS NULL").fetchall()
            for row in rows:
                db.execute("""UPDATE assessments SET verdict='price_only',reason=reason||?,liquidity=? WHERE id=?""",
                           ("；旧扫描未采集求购需求，仅为挂价差；请重新扫描", json.dumps({
                               "status": "unverified", "reason": "旧报价没有需求证据，请重新扫描",
                               "recent_sales_7d": None, "recent_sales_30d": None, "estimated_sell_days": None}), row["id"]))
            for run_id in {row["run_id"] for row in rows}:
                db.execute("""UPDATE scan_runs SET opportunities=(SELECT COUNT(*) FROM assessments
                    WHERE run_id=? AND verdict='opportunity') WHERE id=?""", (run_id, run_id))
            return len(rows)

    def reprice_legacy_assessments(self, policy: PricingPolicy) -> int:
        """Repair old fee calculations from saved observations, without refetching.

        Keep the quote timestamp and previous derived values. New assessments
        already have a policy snapshot and retain the rates used at scan time.
        Account orders, inventory costs and actual cash records are unaffected.
        """
        with self._connect() as db:
            rows = db.execute("""SELECT * FROM assessments WHERE pricing IS NULL
                AND target_sell_price IS NOT NULL AND market_price IS NOT NULL
                AND verdict IN ('opportunity','low_margin')""").fetchall()
            run_ids = set()
            for row in rows:
                cost = Decimal(row["buy_price"])
                price = money(Decimal(row["market_price"]) - policy.undercut)
                previous = {key: row[key] for key in ("target_sell_price", "net_profit", "roi", "verdict")}
                if price <= 0:
                    pricing = {"model": "cash_principal_plus_withdrawal_fee_v1",
                               "calculated_at": utc_now(), "reused_quote": "true",
                               "previous_estimate": previous, "undercut": str(policy.undercut)}
                    db.execute("""UPDATE assessments SET target_sell_price=NULL,net_profit=NULL,roi=NULL,
                        verdict='needs_review',reason=?,pricing=? WHERE id=?""",
                        ("市场最低价不足以设置有效卖价", json.dumps(pricing), row["id"]))
                    run_ids.add(row["run_id"])
                    continue
                profit, roi, pricing = project_cash(cost, price, policy)
                pricing.update(calculated_at=utc_now(), reused_quote="true")
                pricing["previous_estimate"] = previous
                verdict = Verdict.OPPORTUNITY if profit_meets_thresholds(profit, cost, policy) else Verdict.LOW_MARGIN
                db.execute("""UPDATE assessments SET target_sell_price=?,net_profit=?,roi=?,
                    verdict=?,pricing=? WHERE id=?""",
                    (str(price), str(profit), str(roi), verdict.value, json.dumps(pricing), row["id"]))
                run_ids.add(row["run_id"])
            for run_id in run_ids:
                db.execute("""UPDATE scan_runs SET opportunities=(SELECT COUNT(*) FROM assessments
                    WHERE run_id=? AND verdict='opportunity') WHERE id=?""", (run_id, run_id))
            return len(rows)

    def runs(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM scan_runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def assessments(self, run_id: str | None = None, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            if run_id is None:
                row = db.execute(
                    """SELECT id FROM scan_runs WHERE status IN ('completed','completed_with_warnings')
                       ORDER BY rowid DESC LIMIT 1"""
                ).fetchone()
                run_id = row["id"] if row else None
            if run_id is None:
                return []
            rows = db.execute(
                "SELECT * FROM assessments WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)
            ).fetchall()
            results = [dict(row) for row in rows]
            for result in results:
                result["pricing"] = json.loads(result["pricing"]) if result["pricing"] else None
                result["offer_alternate_titles"] = json.loads(result["offer_alternate_titles"])
                result["matching"] = json.loads(result["matching"]) if result["matching"] else None
                result["market_candidates"] = json.loads(result["market_candidates"])
                result["liquidity"] = json.loads(result["liquidity"]) if result["liquidity"] else None
            return results

    def product_mapping(self, sonkwo_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM product_mappings WHERE sonkwo_id=?", (sonkwo_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["offer"] = json.loads(result.pop("offer_json"))
        result["market"] = json.loads(result.pop("market_json"))
        return result

    def product_mappings(self) -> list[dict]:
        with self._connect() as db:
            ids = [row["sonkwo_id"] for row in db.execute("SELECT sonkwo_id FROM product_mappings ORDER BY confirmed_at DESC")]
        return [self.product_mapping(product_id) for product_id in ids]

    def confirm_product_mapping(self, assessment_id: int, steampy_id: str, note: str = "") -> dict:
        """Bind only captured source products; arbitrary client-provided names/URLs cannot create mappings."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
            if not row:
                raise ValueError("扫描商品不存在")
            latest = db.execute("SELECT MAX(id) FROM assessments WHERE offer_url=?", (row["offer_url"],)).fetchone()[0]
            if latest != assessment_id:
                raise ValueError("该商品已有新扫描，请刷新后确认最新信息")
            matching = json.loads(row["matching"]) if row["matching"] else {}
            if not matching.get("offer"):
                raise ValueError("旧结果缺少商品身份快照，请重新扫描")
            offer_product = ProductIdentity.from_snapshot(matching["offer"])
            if not offer_product.detail_checked or offer_product.detail_issue:
                raise ValueError("杉果原商品详情尚未核对，不能保存对应关系")
            candidates = json.loads(row["market_candidates"])
            candidate = next((item for item in candidates if item.get("identity")
                              and item["identity"]["product_id"] == steampy_id), None)
            if not candidate:
                raise ValueError("SteamPy 商品不在本次采集的候选中")
            offer = Offer(row["offer_title"], row["offer_url"], Decimal(row["buy_price"]),
                          tuple(json.loads(row["offer_alternate_titles"])), offer_product)
            market_product = ProductIdentity.from_snapshot(candidate["identity"])
            market = MarketProduct(candidate["title"], tuple(candidate["alternate_titles"]), market_product)
            conflict = product_guard(offer, market)
            if conflict:
                raise ValueError(conflict)
            db.execute("""INSERT INTO product_mappings VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(sonkwo_id) DO UPDATE SET steampy_id=excluded.steampy_id,
                offer_fingerprint=excluded.offer_fingerprint,market_fingerprint=excluded.market_fingerprint,
                offer_json=excluded.offer_json,market_json=excluded.market_json,
                note=excluded.note,confirmed_at=excluded.confirmed_at""",
                (offer_product.product_id, market_product.product_id, offer_product.fingerprint,
                 market_product.fingerprint, json.dumps(offer_product.snapshot(), ensure_ascii=False),
                 json.dumps(market_product.snapshot(), ensure_ascii=False), note[:300], utc_now()))
        return self.product_mapping(offer_product.product_id)

    def clear_product_mapping(self, sonkwo_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM product_mappings WHERE sonkwo_id=?", (sonkwo_id,))

    def purchase(self, assessment_id: int, actual_cost: Decimal, reference: str = "") -> dict:
        cost = money(actual_cost)
        if cost <= 0:
            raise ValueError("实际买入成本必须大于零")
        with self._connect() as db:
            assessment = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
            if assessment is None:
                raise ValueError("扫描结果不存在")
            trade_id, now = uuid4().hex, utc_now()
            try:
                db.execute(
                    """INSERT INTO trades(id,assessment_id,title,offer_url,state,actual_cost,
                       reference,purchased_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (trade_id, assessment_id, assessment["offer_title"], assessment["offer_url"],
                     "purchased", str(cost), reference, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该扫描结果已登记买入") from exc
            self._event(db, trade_id, "purchased", {"actual_cost": str(cost), "reference": reference}, now)
            return self._trade(db, trade_id)

    def transition(self, trade_id: str, event: str, amount: Decimal | None = None,
                   fee: Decimal | None = None, reference: str = "") -> dict:
        transitions = {
            "listed": (("purchased",), "listed"),
            "sold": (("listed",), "sold"),
            "settled": (("sold",), "settled"),
            "refunded": (("purchased", "listed"), "refunded"),
            "written_off": (("purchased", "listed", "sold"), "written_off"),
        }
        if event not in transitions:
            raise InvalidTradeTransition("未知交易动作")
        allowed, next_state = transitions[event]
        if amount is None or money(amount) < 0 or (event in {"listed", "sold"} and amount <= 0):
            raise ValueError("此动作需要有效金额")
        value = money(amount)
        fee_value = money(fee or Decimal("0"))
        if fee_value < 0 or fee_value > value:
            raise ValueError("费用必须介于零和金额之间")
        with self._connect() as db:
            current = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if current is None:
                raise ValueError("交易不存在")
            if current["state"] not in allowed:
                raise InvalidTradeTransition(f"{current['state']} 状态不能执行 {event}")
            if event == "settled":
                sale_net = Decimal(current["sale_gross"]) - Decimal(current["sale_fee"] or "0")
                if value > sale_net:
                    raise ValueError("实际到账不能高于本笔交易扣除卖出费用后的金额")
            if event == "refunded" and value > Decimal(current["actual_cost"]):
                raise ValueError("退款不能高于本笔买入成本")
            now = utc_now()
            fields = {
                "listed": ("ask_price", str(value)),
                "sold": ("sale_gross", str(value)),
                "settled": ("payout", str(value)),
                "refunded": ("payout", str(value)),
                "written_off": ("payout", str(value)),
            }
            amount_field, amount_text = fields[event]
            db.execute(
                f"UPDATE trades SET state=?, {amount_field}=?, sale_fee=COALESCE(?,sale_fee), reference=?, updated_at=? WHERE id=?",
                (next_state, amount_text, str(fee_value) if event == "sold" else None,
                 reference or current["reference"], now, trade_id),
            )
            self._event(db, trade_id, event, {"amount": str(value), "fee": str(fee_value), "reference": reference}, now)
            return self._trade(db, trade_id)

    @staticmethod
    def _event(db: sqlite3.Connection, trade_id: str, event: str, details: dict, now: str) -> None:
        db.execute(
            "INSERT INTO trade_events(trade_id,event,details,happened_at) VALUES(?,?,?,?)",
            (trade_id, event, json.dumps(details, ensure_ascii=False), now),
        )

    @staticmethod
    def _trade(db: sqlite3.Connection, trade_id: str) -> dict:
        row = db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        return dict(row)

    def trades(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM trades ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def trade_events(self, trade_id: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM trade_events WHERE trade_id=? ORDER BY id", (trade_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def cash_report(self) -> dict[str, str | int]:
        with self._connect() as db:
            rows = db.execute("SELECT state,actual_cost,payout FROM trades").fetchall()
        open_states = {"purchased", "listed", "sold"}
        tied = sum((Decimal(row["actual_cost"]) for row in rows if row["state"] in open_states), Decimal("0"))
        awaiting_payout = [row for row in rows if row["state"] == "sold"]
        settled = [row for row in rows if row["state"] in {"settled", "refunded", "written_off"}]
        received = sum((Decimal(row["payout"]) for row in settled), Decimal("0"))
        spent = sum((Decimal(row["actual_cost"]) for row in settled), Decimal("0"))
        return {
            "open_positions": sum(row["state"] in open_states for row in rows),
            "capital_tied": str(money(tied)),
            "awaiting_payout": len(awaiting_payout),
            "settled_positions": len(settled),
            "cash_received": str(money(received)),
            "realized_profit": str(money(received - spent)),
        }

    @staticmethod
    def _replace_reconciliations(db: sqlite3.Connection,
                                 reconciliations: list[SaleReconciliation]) -> None:
        db.execute("DELETE FROM account_sale_reconciliation")
        db.executemany(
            """INSERT INTO account_sale_reconciliation(sale_order_id,status,reason,
               buy_order_id,buy_line_no,buy_unit_no,buy_unit_cost,order_stage_spread)
               VALUES(?,?,?,?,?,?,?,?)""",
            [(item.sale_order_id, item.status, item.reason,
              item.buy_order_id, item.buy_line_no, item.buy_unit_no,
              str(item.buy_unit_cost) if item.buy_unit_cost is not None else None,
              str(item.order_stage_spread) if item.order_stage_spread is not None else None)
             for item in reconciliations],
        )

    def replace_account_orders(self, orders: list[AccountOrder],
                               catalog: TitleCatalog | None = None,
                               *, source_coverage: tuple[str, ...] = ("ordinary",),
                               source_synced_at: dict[str, str] | None = None,
                               warnings: tuple[str, ...] = ()) -> None:
        """Publish a complete verified snapshot; failures retain the prior snapshot."""
        if len({(order.platform, order.order_id) for order in orders}) != len(orders):
            raise ValueError("账号订单编号重复")
        if any(order.platform not in {"sonkwo", "steampy"} for order in orders):
            raise ValueError("账号订单平台无效")
        if any(order.channel not in {"ordinary", "request"} or
               (order.platform == "sonkwo" and order.channel != "ordinary") for order in orders):
            raise ValueError("账号订单渠道无效")
        if set(source_coverage) - {"ordinary", "request"} or not source_coverage:
            raise ValueError("订单来源覆盖范围无效")
        if source_synced_at is not None and (not {"sonkwo", *source_coverage}.issubset(source_synced_at)
                                              or set(source_synced_at) - {"sonkwo", "ordinary", "request"}
                                              or not all(isinstance(value, str) and value
                                                         for value in source_synced_at.values())):
            raise ValueError("订单来源同步时间无效")
        if not all(isinstance(item, str) and 0 < len(item) <= 250 for item in warnings):
            raise ValueError("订单来源警告无效")
        if any(line.refunded_quantity < 0 or line.refunded_quantity > line.quantity
               or line.refund_amount < 0 or line.refund_amount > line.unit_cost * line.quantity
               for order in orders for line in order.lines):
            raise ValueError("购买明细的退款数量或金额无效")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            confirmations = {(row["order_id"], row["line_no"]): row for row in db.execute(
                "SELECT * FROM purchase_refund_confirmations")}
            effective_orders = []
            for order in orders:
                effective_lines = []
                for line in order.lines:
                    if order.platform == "sonkwo" and order.status == "refunded":
                        line = replace(line, refunded_quantity=line.quantity,
                                       refund_amount=money(line.unit_cost * line.quantity),
                                       refund_source="platform")
                    confirmed = confirmations.get((order.order_id, line.line_no)) if order.platform == "sonkwo" else None
                    if confirmed:
                        if (line.title != confirmed["title"] or line.quantity != confirmed["quantity"]
                                or line.unit_cost != Decimal(confirmed["unit_cost"])):
                            raise ValueError("退款确认对应的购买明细已改变，停止覆盖并保留原账本")
                        if line.refunded_quantity < confirmed["refunded_quantity"]:
                            line = replace(line, refunded_quantity=confirmed["refunded_quantity"],
                                           refund_amount=Decimal(confirmed["refund_amount"]),
                                           refund_source="user_confirmed")
                    effective_lines.append(line)
                effective_orders.append(replace(order, lines=tuple(effective_lines)))
            orders = effective_orders
            reconciliations = reconcile_orders(orders, catalog or TitleCatalog())
            db.execute("DELETE FROM account_sale_reconciliation")
            db.execute("DELETE FROM account_order_lines")
            db.execute("DELETE FROM account_orders")
            db.executemany(
                """INSERT INTO account_orders(platform,order_id,title,occurred_at,status,
                   quantity,amount,fee,net_amount,alternate_titles,channel,product_id,stock_created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(order.platform, order.order_id, order.title, order.occurred_at,
                  order.status, order.quantity,
                  str(order.amount) if order.amount is not None else None,
                  str(order.fee) if order.fee is not None else None,
                  str(order.net_amount) if order.net_amount is not None else None,
                  json.dumps(order.alternate_titles, ensure_ascii=False), order.channel,
                  order.product_id, order.stock_created_at)
                 for order in orders],
            )
            db.executemany(
                """INSERT INTO account_order_lines(platform,order_id,line_no,title,
                   alternate_titles,quantity,unit_cost,refunded_quantity,refund_amount,refund_source,product_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(order.platform, order.order_id, line.line_no, line.title,
                  json.dumps(line.alternate_titles, ensure_ascii=False), line.quantity,
                  str(line.unit_cost), line.refunded_quantity, str(money(line.refund_amount)), line.refund_source,
                  line.product_id)
                 for order in orders for line in order.lines],
            )
            self._replace_reconciliations(db, reconciliations)
            now = utc_now()
            source_times = source_synced_at or {name: now for name in {"sonkwo", *source_coverage}}
            db.execute("""INSERT INTO account_order_sync(id,completed_at,source_coverage,source_synced_at,warnings)
                       VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       completed_at=excluded.completed_at,
                       source_coverage=excluded.source_coverage,
                       source_synced_at=excluded.source_synced_at,
                       warnings=excluded.warnings""",
                       (now, ",".join(sorted(set(source_coverage))),
                        json.dumps(source_times, ensure_ascii=False),
                        json.dumps(warnings, ensure_ascii=False)))
            db.execute("""INSERT INTO account_reconciliation_meta(id,completed_at) VALUES(1,?)
                       ON CONFLICT(id) DO UPDATE SET completed_at=excluded.completed_at""",
                       (now,))

    def reconcile_existing_account_orders(self, catalog: TitleCatalog | None = None) -> int:
        """Reapply current identity rules to the stored snapshot without account access."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ready = db.execute("SELECT 1 FROM account_order_sync WHERE id=1").fetchone()
            if ready is None:
                return 0
            orders = self._read_account_orders(db)
            reconciliations = reconcile_orders(orders, catalog or TitleCatalog())
            self._replace_reconciliations(db, reconciliations)
            db.execute("""INSERT INTO account_reconciliation_meta(id,completed_at) VALUES(1,?)
                       ON CONFLICT(id) DO UPDATE SET completed_at=excluded.completed_at""",
                       (utc_now(),))
        return len(reconciliations)

    @staticmethod
    def _read_account_orders(db: sqlite3.Connection, platform: str | None = None) -> list[AccountOrder]:
        from .orders import AccountOrder, PurchaseLine

        where, params = (" WHERE platform=?", (platform,)) if platform else ("", ())
        orders = db.execute("SELECT * FROM account_orders" + where, params).fetchall()
        line_rows = db.execute("SELECT * FROM account_order_lines" + where + " ORDER BY line_no", params).fetchall()
        lines: dict[tuple[str, str], list[PurchaseLine]] = {}
        for row in line_rows:
            lines.setdefault((row["platform"], row["order_id"]), []).append(PurchaseLine(
                row["line_no"], row["title"], tuple(json.loads(row["alternate_titles"])),
                row["quantity"], Decimal(row["unit_cost"]), row["refunded_quantity"],
                Decimal(row["refund_amount"]), row["refund_source"], row["product_id"]))
        return [AccountOrder(
            row["platform"], row["order_id"], row["title"], row["occurred_at"],
            row["status"], row["quantity"],
            Decimal(row["amount"]) if row["amount"] is not None else None,
            Decimal(row["fee"]) if row["fee"] is not None else None,
            Decimal(row["net_amount"]) if row["net_amount"] is not None else None,
            tuple(json.loads(row["alternate_titles"])),
            tuple(lines.get((row["platform"], row["order_id"]), ())), row["channel"],
            row["product_id"], row["stock_created_at"])
            for row in orders]

    def confirm_purchase_refund(self, order_id: str, line_no: int, refunded_quantity: int,
                                refund_amount: Decimal, evidence: str,
                                catalog: TitleCatalog | None = None) -> None:
        """Apply a user's verified refund without changing source freshness or other items."""
        if not evidence.strip() or len(evidence) > 250 or refunded_quantity < 1:
            raise ValueError("退款确认依据或件数无效")
        refund_amount = money(refund_amount)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            line = db.execute("""SELECT * FROM account_order_lines
                                 WHERE platform='sonkwo' AND order_id=? AND line_no=?""",
                              (order_id, line_no)).fetchone()
            if line is None or refunded_quantity > line["quantity"] or refund_amount < 0 \
                    or refund_amount > Decimal(line["unit_cost"]) * line["quantity"]:
                raise ValueError("退款确认没有对应的购买商品或金额无效")
            db.execute("""INSERT INTO purchase_refund_confirmations
                       (order_id,line_no,title,quantity,unit_cost,refunded_quantity,refund_amount,evidence,confirmed_at)
                       VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id,line_no) DO UPDATE SET
                       refunded_quantity=excluded.refunded_quantity,refund_amount=excluded.refund_amount,
                       evidence=excluded.evidence,confirmed_at=excluded.confirmed_at""",
                       (order_id, line_no, line["title"], line["quantity"], line["unit_cost"],
                        refunded_quantity, str(refund_amount), evidence.strip(), utc_now()))
            if line["refund_source"] != "platform" or line["refunded_quantity"] < refunded_quantity:
                db.execute("""UPDATE account_order_lines SET refunded_quantity=?,refund_amount=?,
                              refund_source='user_confirmed' WHERE platform='sonkwo' AND order_id=? AND line_no=?""",
                           (refunded_quantity, str(refund_amount), order_id, line_no))
            self._replace_reconciliations(db, reconcile_orders(self._read_account_orders(db), catalog or TitleCatalog()))
            db.execute("""INSERT INTO account_reconciliation_meta(id,completed_at) VALUES(1,?)
                          ON CONFLICT(id) DO UPDATE SET completed_at=excluded.completed_at""", (utc_now(),))

    def account_order_summary(self) -> dict:
        with self._connect() as db:
            row = db.execute("""SELECT completed_at,source_coverage,source_synced_at,warnings
                                FROM account_order_sync WHERE id=1""").fetchone()
            wallet_row = db.execute("SELECT completed_at FROM wallet_sync WHERE id=1").fetchone()
            orders = db.execute(
                "SELECT platform,order_id,status,quantity,amount,fee,net_amount,channel FROM account_orders WHERE "
                + _order_scope_sql()).fetchall()
            purchase_lines = db.execute("""SELECT l.order_id,l.refunded_quantity,l.refund_amount,
                                             l.refund_source FROM account_order_lines l
                                             JOIN account_orders o ON o.platform=l.platform AND o.order_id=l.order_id
                                             WHERE l.platform='sonkwo' AND """ + _order_scope_sql("o")).fetchall()
            wallet_credits = db.execute(
                """SELECT tx_type,tx_id,amount FROM wallet_bills
                   WHERE tx_type IN ('K','AK') AND cd_flag='D' AND datetime(occurred_at)>=?""",
                (REPORT_START_CHINA.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchall()
            wallet_movements = db.execute(
                """SELECT tx_type,amount,cd_flag FROM wallet_bills
                   WHERE datetime(occurred_at)>=?""",
                (REPORT_START_CHINA.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchall()
        refunds_by_order: dict[str, tuple[int, Decimal]] = {}
        for line in purchase_lines:
            quantity, amount = refunds_by_order.get(line["order_id"], (0, Decimal("0")))
            refunds_by_order[line["order_id"]] = (quantity + line["refunded_quantity"],
                                                  amount + Decimal(line["refund_amount"]))
        sonkwo = {"orders": 0, "completed": 0, "units": 0, "spent": Decimal("0"),
                  "paid": Decimal("0"), "refund_amount": Decimal("0"),
                  "refunded_units": 0, "refunded_orders": 0}
        steampy = {"orders": 0, "sold": 0, "ordinary_sold": 0,
                   "request_sold": 0, "request_net": Decimal("0"),
                   "gross_known_sold": 0, "other": 0, "gross": Decimal("0"),
                   "fees": Decimal("0"), "sale_net": Decimal("0"), "other_statuses": {}}
        for order in orders:
            if order["platform"] == "sonkwo":
                sonkwo["orders"] += 1
                if order["status"] in {"completed", "refunded"}:
                    paid = Decimal(order["amount"])
                    refunded_quantity, refunded_amount = refunds_by_order.get(order["order_id"], (0, Decimal("0")))
                    if order["status"] == "refunded" and not refunded_quantity:
                        refunded_quantity, refunded_amount = order["quantity"], paid
                    active_quantity = order["quantity"] - refunded_quantity
                    if active_quantity > 0 and order["status"] == "completed":
                        sonkwo["completed"] += 1
                        sonkwo["units"] += active_quantity
                    sonkwo["paid"] += paid
                    sonkwo["refund_amount"] += refunded_amount
                    sonkwo["spent"] += paid - refunded_amount
                    sonkwo["refunded_units"] += refunded_quantity
                    sonkwo["refunded_orders"] += bool(refunded_quantity)
            elif order["platform"] == "steampy":
                steampy["orders"] += 1
                if order["status"] == "sold":
                    steampy["sold"] += 1
                    steampy[f"{order['channel']}_sold"] += 1
                    if order["amount"] is not None and order["fee"] is not None:
                        steampy["gross_known_sold"] += 1
                        steampy["gross"] += Decimal(order["amount"])
                        steampy["fees"] += Decimal(order["fee"])
                    steampy["sale_net"] += Decimal(order["net_amount"])
                    if order["channel"] == "request":
                        steampy["request_net"] += Decimal(order["net_amount"])
                else:
                    steampy["other"] += 1
                    statuses = steampy["other_statuses"]
                    label = ("request_" if order["channel"] == "request" else "") + order["status"]
                    statuses[label] = statuses.get(label, 0) + 1
        for key in ("spent", "paid", "refund_amount"):
            sonkwo[key] = str(money(sonkwo[key]))
        for key in ("gross", "fees", "sale_net", "request_net"):
            steampy[key] = str(money(steampy[key]))
        covered_sales = {order["order_id"]: ("AK" if order["channel"] == "request" else "K",
                                                 Decimal(order["net_amount"]))
                         for order in orders if order["platform"] == "steampy"
                         and order["status"] == "sold"}
        unlinked_credits = [bill for bill in wallet_credits
                            if Decimal(bill["amount"]) > 0
                            and (bill["tx_id"] not in covered_sales
                                 or bill["tx_type"] != covered_sales[bill["tx_id"]][0])]
        credited: dict[tuple[str, str], Decimal] = {}
        for bill in wallet_credits:
            if Decimal(bill["amount"]) > 0:
                key = (bill["tx_type"], bill["tx_id"])
                credited[key] = credited.get(key, Decimal("0")) + Decimal(bill["amount"])
        missing_credits = 0
        mismatched_credits = 0
        if wallet_row is not None:
            for order_id, (bill_type, net) in covered_sales.items():
                amount = credited.get((bill_type, order_id))
                if amount is None:
                    missing_credits += 1
                elif amount != net:
                    mismatched_credits += 1
        unlinked_types: dict[str, int] = {}
        for bill in unlinked_credits:
            unlinked_types[bill["tx_type"]] = unlinked_types.get(bill["tx_type"], 0) + 1
        other_movements = [bill for bill in wallet_movements
                           if movement_kind(bill["tx_type"], bill["cd_flag"], Decimal(bill["amount"]))
                           in {"other_credits", "other_debits"}]
        other_types: dict[str, int] = {}
        for bill in other_movements:
            other_types[bill["tx_type"]] = other_types.get(bill["tx_type"], 0) + 1
        return {"last_synced_at": row["completed_at"] if row else None,
                "source_coverage": row["source_coverage"].split(",") if row else [],
                "source_synced_at": (json.loads(row["source_synced_at"])
                                     or {"sonkwo": row["completed_at"],
                                         "ordinary": row["completed_at"]}) if row else {},
                "sync_warnings": json.loads(row["warnings"]) if row else [],
                "report_start": REPORT_START_CHINA.date().isoformat(),
                "sonkwo": sonkwo, "steampy": steampy,
                "refunds_pending_platform_verification": sum(
                    line["refunded_quantity"] > 0 and line["refund_source"] == "user_confirmed"
                    for line in purchase_lines),
                "wallet_synced_at": wallet_row["completed_at"] if wallet_row else None,
                "unlinked_wallet_credits": {
                    "count": len(unlinked_credits),
                    "amount": str(money(sum((Decimal(bill["amount"])
                                             for bill in unlinked_credits), Decimal("0")))),
                    "types": unlinked_types},
                "unclassified_wallet_movements": {
                    "count": len(other_movements),
                    "debits": str(money(sum((-Decimal(bill["amount"])
                                               for bill in other_movements
                                               if Decimal(bill["amount"]) < 0), Decimal("0")))),
                    "credits": str(money(sum((Decimal(bill["amount"])
                                                for bill in other_movements
                                                if Decimal(bill["amount"]) > 0), Decimal("0")))),
                    "types": other_types},
                "missing_wallet_credit_count": missing_credits,
                "mismatched_wallet_credit_count": mismatched_credits,
                "realized_profit": None,
                "profit_note": "SteamPy 普通与求购成功订单均按同款先购先销分配买入成本。求购记录只确认扣费后收入，成交原价和费用未从订单字段独立核实；提现到账尚未核对，订单价差不等于已实现利润。"}

    def account_orders(self, platform: str, limit: int = 100) -> list[dict]:
        if platform not in {"sonkwo", "steampy"}:
            raise ValueError("未知平台")
        with self._connect() as db:
            rows = db.execute(
                """SELECT platform,order_id,title,occurred_at,status,quantity,amount,fee,net_amount,channel
                   FROM account_orders WHERE platform=? AND """ + _order_scope_sql() +
                " ORDER BY occurred_at DESC LIMIT ?",
                (platform, min(max(limit, 1), 200)),
            ).fetchall()
            return [dict(row) for row in rows]

    def saved_account_orders(self, platform: str) -> list[AccountOrder]:
        """Read one platform's last complete source snapshot for partial refreshes."""
        if platform not in {"sonkwo", "steampy"}:
            raise ValueError("未知平台")
        with self._connect() as db:
            return self._read_account_orders(db, platform)

    def account_order_lines(self, platform: str, order_id: str) -> list[dict]:
        if platform not in {"sonkwo", "steampy"}:
            raise ValueError("未知平台")
        with self._connect() as db:
            rows = db.execute(
                """SELECT l.line_no,l.title,l.quantity,l.unit_cost,l.refunded_quantity,
                   l.refund_amount,l.refund_source FROM account_order_lines l
                   JOIN account_orders o ON o.platform=l.platform AND o.order_id=l.order_id
                   WHERE l.platform=? AND l.order_id=? AND """ + _order_scope_sql("o") +
                " ORDER BY l.line_no",
                (platform, order_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def account_reconciliations(self) -> dict:
        with self._connect() as db:
            meta = db.execute("SELECT completed_at FROM account_reconciliation_meta WHERE id=1").fetchone()
            rows = db.execute(
                """SELECT r.sale_order_id,r.status,r.reason,r.buy_order_id,r.buy_line_no,
                   r.buy_unit_no,r.buy_unit_cost,r.order_stage_spread,
                   sale.title AS sale_title,sale.occurred_at AS sale_time,sale.channel AS sale_channel,
                   sale.amount AS sale_gross,sale.fee AS sale_fee,sale.net_amount AS sale_net,
                   buy_line.title AS buy_title
                   FROM account_sale_reconciliation r
                   JOIN account_orders sale ON sale.platform='steampy' AND sale.order_id=r.sale_order_id
                   LEFT JOIN account_order_lines buy_line ON buy_line.platform='sonkwo'
                       AND buy_line.order_id=r.buy_order_id AND buy_line.line_no=r.buy_line_no
                   WHERE """ + _order_scope_sql("sale") +
                " ORDER BY sale.occurred_at DESC, r.sale_order_id DESC"
            ).fetchall()
        result = [dict(row) for row in rows]
        counts = {status: sum(item["status"] == status for item in result)
                  for status in ("candidate", "fifo", "review", "unmatched")}
        candidate_spread = sum((Decimal(item["order_stage_spread"]) for item in result
                                if item["status"] == "candidate"), Decimal("0"))
        fifo_spread = sum((Decimal(item["order_stage_spread"]) for item in result
                           if item["status"] == "fifo"), Decimal("0"))
        return {"ready": meta is not None, "completed_at": meta["completed_at"] if meta else None,
                "counts": counts, "matched_count": counts["candidate"] + counts["fifo"],
                "candidate_order_spread": str(money(candidate_spread)),
                "fifo_order_spread": str(money(fifo_spread)),
                "matched_order_spread": str(money(candidate_spread + fifo_spread)),
                "rows": result}

    def purchase_inventory(self, *, state: str = "all", query: str = "",
                           page: int = 1, page_size: int = 30) -> dict:
        """Show each in-scope Sonkwo purchase unit and its allocated sale."""
        if state not in {"all", "unsold", "loss", "sold"}:
            raise ValueError("未知进货状态筛选")
        if not 1 <= page or not 1 <= page_size <= 100 or len(query) > 100:
            raise ValueError("进货分页或搜索条件无效")
        snapshot = self._purchase_inventory()
        term = query.strip().casefold()
        filtered = [unit for unit in snapshot["rows"]
                    if (state == "all"
                        or state == "unsold" and unit["status"] == "unmatched"
                        or state == "sold" and unit["status"] == "sold"
                        or state == "loss" and unit["order_stage_spread"] is not None
                        and Decimal(unit["order_stage_spread"]) < 0)
                    and (not term or term in unit["title"].casefold()
                         or term in unit["purchase_order_id"].casefold()
                         or unit["sale_order_id"] is not None
                         and term in unit["sale_order_id"].casefold())]
        return {**snapshot, "page": page, "page_size": page_size,
                "total": len(filtered), "pages": max(1, (len(filtered) + page_size - 1) // page_size),
                "rows": filtered[(page - 1) * page_size:page * page_size]}

    def _purchase_inventory(self) -> dict:
        """One full unit ledger shared by pagination and turnover; no re-matching."""
        with self._connect() as db:
            synced = db.execute("SELECT * FROM account_order_sync WHERE id=1").fetchone()
            purchases = db.execute(
                """SELECT l.order_id,l.line_no,l.title,l.alternate_titles,l.product_id,
                   l.quantity-l.refunded_quantity AS quantity,l.unit_cost,
                   o.occurred_at AS purchase_time
                   FROM account_order_lines l
                   JOIN account_orders o ON o.platform=l.platform AND o.order_id=l.order_id
                   WHERE l.platform='sonkwo' AND o.status='completed'
                   AND l.quantity>l.refunded_quantity AND """ +
                _order_scope_sql("o") +
                " ORDER BY o.occurred_at,l.order_id,l.line_no").fetchall()
            matches = db.execute(
                """SELECT r.buy_order_id,r.buy_line_no,r.buy_unit_no,r.status,r.reason,
                   s.order_id AS sale_order_id,s.title AS sale_title,s.channel AS sale_channel,
                   s.occurred_at AS sale_time,s.amount AS sale_gross,
                   s.fee AS sale_fee,s.net_amount AS sale_net,s.product_id AS market_product_id,
                   s.stock_created_at
                   FROM account_sale_reconciliation r
                   JOIN account_orders s ON s.platform='steampy' AND s.order_id=r.sale_order_id
                   WHERE r.status IN ('candidate','fifo') AND """ +
                _order_scope_sql("s")).fetchall()
            unallocated_sales = db.execute(
                """SELECT COUNT(*) FROM account_sale_reconciliation r
                   JOIN account_orders s ON s.platform='steampy' AND s.order_id=r.sale_order_id
                   WHERE r.status IN ('review','unmatched') AND """ +
                _order_scope_sql("s")).fetchone()[0]
            wallet_ready = db.execute("SELECT 1 FROM wallet_sync WHERE id=1").fetchone() is not None
            credits: dict[tuple[str, str], Decimal] = {}
            if wallet_ready:
                for bill in db.execute("""SELECT tx_type,tx_id,amount FROM wallet_bills
                                          WHERE tx_type IN ('K','AK') AND cd_flag='D'"""):
                    key = (bill["tx_type"], bill["tx_id"])
                    credits[key] = credits.get(key, Decimal("0")) + Decimal(bill["amount"])
        allocated: dict[tuple[str, int, int], dict] = {}
        for row in matches:
            key = (row["buy_order_id"], row["buy_line_no"], row["buy_unit_no"])
            if None in key or key in allocated:
                raise ValueError("买入商品的成交分配不完整或重复，请重新核对订单")
            allocated[key] = dict(row)
        purchase_quantities: dict[tuple[str, str], int] = {}
        for line in purchases:
            purchase_key = (line["order_id"], line["title"])
            purchase_quantities[purchase_key] = (
                purchase_quantities.get(purchase_key, 0) + line["quantity"])
        purchase_unit_numbers: dict[tuple[str, str], int] = {}
        products: dict[str, dict] = {}
        seen_allocations: set[tuple[str, int, int]] = set()
        for line in purchases:
            title = line["title"]
            product = products.setdefault(title, {
                "title": title, "bought_units": 0, "sold_units": 0,
                "unsold_units": 0, "loss_units": 0,
                "purchase_total": Decimal("0"), "sold_cost": Decimal("0"),
                "unsold_cost": Decimal("0"), "sale_gross": Decimal("0"),
                "sale_fees": Decimal("0"), "sale_net": Decimal("0"),
                "order_stage_spread": Decimal("0"), "gross_unknown_sales": 0,
                "units": []})
            cost = Decimal(line["unit_cost"])
            for unit_no in range(1, line["quantity"] + 1):
                purchase_key = (line["order_id"], title)
                purchase_unit_numbers[purchase_key] = (
                    purchase_unit_numbers.get(purchase_key, 0) + 1)
                key = (line["order_id"], line["line_no"], unit_no)
                sale = allocated.get(key)
                product["bought_units"] += 1
                product["purchase_total"] += cost
                unit = {"purchase_order_id": line["order_id"],
                        "product_id": line["product_id"],
                        "alternate_titles": json.loads(line["alternate_titles"]),
                        "purchase_time": line["purchase_time"],
                        "line_no": line["line_no"], "unit_no": unit_no,
                        "purchase_quantity": purchase_quantities[purchase_key],
                        "purchase_unit_no": purchase_unit_numbers[purchase_key],
                        "unit_cost": str(money(cost)),
                        "status": "sold" if sale else "unmatched",
                        "sale_order_id": None, "sale_title": None, "sale_time": None,
                        "sale_channel": None,
                        "market_product_id": None, "stock_created_at": None,
                        "sale_gross": None, "sale_fee": None, "sale_net": None,
                        "order_stage_spread": None, "allocation": None,
                        "reason": None, "wallet_credit_matches_net": None}
                if sale:
                    seen_allocations.add(key)
                    gross = Decimal(sale["sale_gross"]) if sale["sale_gross"] is not None else None
                    fee = Decimal(sale["sale_fee"]) if sale["sale_fee"] is not None else None
                    net = Decimal(sale["sale_net"])
                    spread = money(net - cost)
                    product["sold_units"] += 1
                    product["sold_cost"] += cost
                    if gross is not None and fee is not None:
                        product["sale_gross"] += gross
                        product["sale_fees"] += fee
                    else:
                        product["gross_unknown_sales"] += 1
                    product["sale_net"] += net
                    product["order_stage_spread"] += spread
                    if spread < 0:
                        product["loss_units"] += 1
                    unit.update({"sale_order_id": sale["sale_order_id"],
                                 "sale_title": sale["sale_title"],
                                 "sale_time": sale["sale_time"],
                                 "sale_channel": sale["sale_channel"],
                                 "market_product_id": sale["market_product_id"],
                                 "stock_created_at": sale["stock_created_at"],
                                 "sale_gross": str(money(gross)) if gross is not None else None,
                                 "sale_fee": str(money(fee)) if fee is not None else None,
                                 "sale_net": str(money(net)),
                                 "order_stage_spread": str(spread),
                                 "allocation": sale["status"],
                                 "reason": sale["reason"],
                                 "wallet_credit_matches_net": (
                                     credits.get(("AK" if sale["sale_channel"] == "request" else "K",
                                                  sale["sale_order_id"])) == net
                                     if wallet_ready else None)})
                else:
                    product["unsold_units"] += 1
                    product["unsold_cost"] += cost
                product["units"].append(unit)
        if seen_allocations != set(allocated):
            raise ValueError("成交分配指向不存在的买入商品，请重新核对订单")
        totals = {name: sum((product[name] for product in products.values()), Decimal("0"))
                  for name in ("purchase_total", "sold_cost", "unsold_cost", "sale_gross",
                               "sale_fees", "sale_net", "order_stage_spread")}
        summary = {"product_count": len(products),
                   "bought_units": sum(p["bought_units"] for p in products.values()),
                   "sold_units": sum(p["sold_units"] for p in products.values()),
                   "unsold_units": sum(p["unsold_units"] for p in products.values()),
                   "loss_units": sum(p["loss_units"] for p in products.values()),
                   "gross_unknown_sales": sum(p["gross_unknown_sales"] for p in products.values()),
                   "unallocated_sales": unallocated_sales,
                   **{name: str(money(value)) for name, value in totals.items()}}
        units = [{"title": product["title"], **unit}
                 for product in products.values() for unit in product["units"]]
        units.sort(key=lambda unit: (unit["purchase_time"], unit["purchase_order_id"],
                                        unit["line_no"], unit["unit_no"]), reverse=True)
        return {"ready": synced is not None, "last_synced_at": synced["completed_at"] if synced else None,
                "source_synced_at": json.loads(synced["source_synced_at"]) if synced else {},
                "source_coverage": synced["source_coverage"].split(",") if synced else [],
                "sync_warnings": json.loads(synced["warnings"]) if synced else [],
                "report_start": REPORT_START_CHINA.date().isoformat(),
                "summary": summary, "rows": units}

    def turnover_report(self, catalog: TitleCatalog | None = None, *, now: datetime | None = None,
                        include_units: bool = False) -> dict:
        from .turnover import build_turnover_report, public_report
        report = build_turnover_report(self._purchase_inventory(), catalog or TitleCatalog(), now=now)
        return report if include_units else public_report(report)

    def replace_wallet_bills(self, bills: list[WalletBill], *, balance: Decimal | None = None,
                             pending_balance: Decimal | None = None, balance_at: str | None = None) -> None:
        """Replace only after all pages were validated; preserve bank confirmations."""
        with self._connect() as db:
            db.execute("DELETE FROM wallet_bills")
            db.executemany(
                """INSERT INTO wallet_bills(bill_id,occurred_at,amount,tx_type,tx_id,cd_flag)
                   VALUES(?,?,?,?,?,?)""",
                [(bill.bill_id, bill.occurred_at, str(bill.amount), bill.tx_type,
                  bill.tx_id, bill.cd_flag) for bill in bills],
            )
            db.execute("""INSERT INTO wallet_sync(id,completed_at,balance,pending_balance,balance_at)
                       VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET completed_at=excluded.completed_at,
                       balance=excluded.balance,pending_balance=excluded.pending_balance,
                       balance_at=excluded.balance_at""",
                       (utc_now(), str(money(balance)) if balance is not None else None,
                        str(money(pending_balance)) if pending_balance is not None else None,
                        balance_at if balance is not None else None))

    def wallet_reconciliation(self) -> dict:
        with self._connect() as db:
            meta = db.execute("SELECT completed_at,balance,pending_balance,balance_at FROM wallet_sync WHERE id=1").fetchone()
            bills = [dict(row) for row in db.execute("SELECT * FROM wallet_bills")]
        balance = meta["balance"] if meta else None
        return {"ready": meta is not None, "last_synced_at": meta["completed_at"] if meta else None,
                "available_balance": balance, "pending_balance": meta["pending_balance"] if meta else None,
                "balance_at": meta["balance_at"] if meta else None,
                **reconcile_wallet(bills, balance)}

    def record_bank_receipt(self, bill_id: str, amount: Decimal,
                            received_at: str, note: str = "") -> None:
        """Record the user's bank statement evidence separately from wallet debits."""
        if not bill_id or len(bill_id) > 120 or len(note) > 120:
            raise ValueError("提现编号或备注无效")
        try:
            receipt_date = date.fromisoformat(received_at)
        except ValueError as exc:
            raise ValueError("到账日期无效") from exc
        if receipt_date > datetime.now().date():
            raise ValueError("到账日期不能在未来")
        amount = money(amount)
        if amount <= 0:
            raise ValueError("到账金额必须大于零")
        with self._connect() as db:
            bill = db.execute("SELECT tx_type,cd_flag,amount FROM wallet_bills WHERE bill_id=?", (bill_id,)).fetchone()
            if bill is None or movement_kind(bill["tx_type"], bill["cd_flag"], Decimal(bill["amount"])) != "withdrawals":
                raise ValueError("没有这笔提现扣款记录")
            db.execute("""INSERT INTO bank_receipts(bill_id,received_at,amount,note,recorded_at)
                       VALUES(?,?,?,?,?) ON CONFLICT(bill_id) DO UPDATE SET
                       received_at=excluded.received_at,amount=excluded.amount,
                       note=excluded.note,recorded_at=excluded.recorded_at""",
                       (bill_id, received_at, str(amount), note, utc_now()))

    def clear_bank_receipt(self, bill_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM bank_receipts WHERE bill_id=?", (bill_id,))

    def wallet_payouts(self) -> dict:
        with self._connect() as db:
            meta = db.execute("SELECT completed_at FROM wallet_sync WHERE id=1").fetchone()
            bills = [dict(row) for row in db.execute(
                "SELECT bill_id,occurred_at,amount,tx_type,tx_id,cd_flag FROM wallet_bills")]
            receipts = {row["bill_id"]: dict(row) for row in db.execute(
                "SELECT bill_id,received_at,amount,note,recorded_at FROM bank_receipts")}
        cashouts = [bill for bill in bills if movement_kind(
            bill["tx_type"], bill["cd_flag"], Decimal(bill["amount"])) == "withdrawals"]
        fee_bills = [bill for bill in bills if movement_kind(
            bill["tx_type"], bill["cd_flag"], Decimal(bill["amount"])) == "withdrawal_fees"]
        rows = []
        used_fees: set[str] = set()
        for cashout in sorted(cashouts, key=lambda item: item["occurred_at"], reverse=True):
            debit = Decimal(cashout["amount"])
            if debit >= 0:
                raise ValueError("SteamPy 提现流水出现非扣款金额，需核对")
            linked = [bill for bill in fee_bills if bill["bill_id"] not in used_fees
                      and cashout["tx_id"] and bill["tx_id"] == cashout["tx_id"]]
            if not linked:
                linked = [bill for bill in fee_bills if bill["bill_id"] not in used_fees
                          and bill["occurred_at"] == cashout["occurred_at"]]
            fee = None
            if len(linked) == 1:
                used_fees.add(linked[0]["bill_id"])
                fee = abs(Decimal(linked[0]["amount"]))
            receipt = receipts.get(cashout["bill_id"])
            received = Decimal(receipt["amount"]) if receipt else None
            rows.append({"bill_id": cashout["bill_id"],
                         "occurred_at": cashout["occurred_at"],
                         "wallet_debit": str(money(abs(debit))),
                         "wallet_fee": str(money(fee)) if fee is not None else None,
                         "bank_received": str(money(received)) if received is not None else None,
                         "bank_received_at": receipt["received_at"] if receipt else None,
                         "bank_note": receipt["note"] if receipt else None,
                         "bank_difference": str(money(received - abs(debit))) if received is not None else None,
                         "status": "manual_receipt" if receipt else "bank_unverified"})
        return {"ready": meta is not None,
                "last_synced_at": meta["completed_at"] if meta else None,
                "wallet_bill_count": len(bills),
                "withdrawal_count": len(rows),
                "wallet_debits": str(money(sum((Decimal(row["wallet_debit"]) for row in rows), Decimal("0")))),
                "wallet_fees": str(money(sum((abs(Decimal(bill["amount"])) for bill in fee_bills), Decimal("0")))),
                "bank_confirmed": str(money(sum((Decimal(row["bank_received"]) for row in rows
                                                   if row["bank_received"] is not None), Decimal("0")))),
                "awaiting_bank_check": sum(row["status"] == "bank_unverified" for row in rows),
                "unlinked_fee_count": len(fee_bills) - len(used_fees),
                "rows": rows}

    def refunded_purchases(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute("""SELECT l.order_id,l.line_no,l.title,
                l.quantity,l.refunded_quantity,l.refund_amount,l.refund_source,
                o.occurred_at AS purchase_time,c.evidence,c.confirmed_at
                FROM account_order_lines l
                JOIN account_orders o ON o.platform=l.platform AND o.order_id=l.order_id
                LEFT JOIN purchase_refund_confirmations c ON c.order_id=l.order_id AND c.line_no=l.line_no
                WHERE l.platform='sonkwo' AND l.refunded_quantity>0 AND """ + _order_scope_sql("o") +
                " ORDER BY o.occurred_at DESC,l.order_id,l.line_no")]

    def financial_overview(self) -> dict:
        """Keep purchase spending, seller credits and verified cash receipts separate."""
        orders = self.account_order_summary()
        payouts = self.wallet_payouts()
        wallet = self.wallet_reconciliation()
        wallet["sale_credit_difference"] = str(money(Decimal(orders["steampy"]["sale_net"])
                                                     - Decimal(wallet["sale_credits"])))
        wallet["sale_credits_verified"] = (
            wallet["ready"] and orders["last_synced_at"] is not None
            and {"ordinary", "request"}.issubset(orders["source_coverage"])
            and not orders["missing_wallet_credit_count"] and not orders["mismatched_wallet_credit_count"]
            and not orders["unlinked_wallet_credits"]["count"])
        withdrawals = [row for row in payouts["rows"] if in_report_period(row["occurred_at"])]
        confirmed = [row for row in withdrawals if row["bank_received"] is not None]
        return {
            "orders_ready": orders["last_synced_at"] is not None, "payouts_ready": payouts["ready"],
            "report_start": orders["report_start"],
            "purchase_paid": orders["sonkwo"]["paid"],
            "purchase_refunds": orders["sonkwo"]["refund_amount"],
            "purchase_spent": orders["sonkwo"]["spent"],
            "purchase_units": orders["sonkwo"]["units"],
            "refunded_units": orders["sonkwo"]["refunded_units"],
            "sale_net": orders["steampy"]["sale_net"], "sale_count": orders["steampy"]["sold"],
            "withdrawal_debits": wallet["withdrawals"],
            "withdrawal_fees": wallet["withdrawal_fees"],
            "withdrawal_count": len(withdrawals), "bank_confirmed_count": len(confirmed),
            "bank_pending_count": len(withdrawals) - len(confirmed),
            "bank_confirmed": (str(money(sum((Decimal(row["bank_received"]) for row in confirmed), Decimal("0"))))
                               if confirmed else None),
            "refunds_pending_platform_verification": orders["refunds_pending_platform_verification"],
            "source_warnings": orders["sync_warnings"],
            "wallet": wallet,
        }

    def account_order_ledger(self, *, platform: str = "all", state: str = "all",
                             query: str = "", page: int = 1, page_size: int = 50,
                             catalog: TitleCatalog | None = None) -> dict:
        """Return every account order through bounded, searchable pages."""
        if platform not in {"all", "sonkwo", "steampy"}:
            raise ValueError("未知平台筛选")
        if state not in {"all", "completed_buy", "sold", "not_counted"}:
            raise ValueError("未知订单状态筛选")
        if not 1 <= page or not 1 <= page_size <= 100 or len(query) > 100:
            raise ValueError("订单分页或搜索条件无效")
        conditions = [_order_scope_sql()]
        parameters: list[object] = []
        if platform != "all":
            conditions.append("platform=?")
            parameters.append(platform)
        if state == "completed_buy":
            conditions.append("""platform='sonkwo' AND status='completed' AND
                (NOT EXISTS(SELECT 1 FROM account_order_lines l WHERE l.platform='sonkwo'
                    AND l.order_id=account_orders.order_id)
                 OR EXISTS(SELECT 1 FROM account_order_lines l WHERE l.platform='sonkwo'
                    AND l.order_id=account_orders.order_id AND l.quantity>l.refunded_quantity))""")
        elif state == "sold":
            conditions.append("platform='steampy' AND status='sold'")
        elif state == "not_counted":
            conditions.append("platform='steampy' AND status!='sold'")
        if query.strip():
            conditions.append("""(instr(lower(title),lower(?))>0
                OR instr(lower(order_id),lower(?))>0
                OR instr(lower(alternate_titles),lower(?))>0
                OR EXISTS (
                    SELECT 1 FROM account_order_lines l
                    WHERE l.platform=account_orders.platform AND l.order_id=account_orders.order_id
                    AND (instr(lower(l.title),lower(?))>0
                         OR instr(lower(l.alternate_titles),lower(?))>0)))""")
            parameters.extend([query.strip()] * 5)
        where = " WHERE " + " AND ".join(f"({condition})" for condition in conditions) if conditions else ""
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM account_orders" + where, parameters).fetchone()[0]
            rows = [dict(row) for row in db.execute(
                """SELECT platform,order_id,title,occurred_at,status,quantity,amount,fee,net_amount,
                   alternate_titles,channel
                   FROM account_orders""" + where +
                " ORDER BY substr(occurred_at,1,10) DESC, platform, occurred_at DESC, order_id DESC"
                " LIMIT ? OFFSET ?",
                [*parameters, page_size, (page - 1) * page_size],
            )]
            purchase_lines: dict[str, list[dict]] = {}
            purchase_ids = [row["order_id"] for row in rows if row["platform"] == "sonkwo"]
            if purchase_ids:
                placeholders = ",".join("?" for _ in purchase_ids)
                for line in db.execute(
                    """SELECT order_id,line_no,title,quantity,unit_cost,
                       refunded_quantity,refund_amount,refund_source FROM account_order_lines
                       WHERE platform='sonkwo' AND order_id IN (""" + placeholders +
                    ") ORDER BY order_id,line_no", purchase_ids,
                ):
                    item = dict(line)
                    purchase_lines.setdefault(item.pop("order_id"), []).append(item)
            sold = [dict(row) for row in db.execute(
                """SELECT order_id,title,occurred_at,amount,net_amount,alternate_titles
                   FROM account_orders WHERE platform='steampy' AND status='sold' AND """ +
                _order_scope_sql() + " ORDER BY occurred_at,order_id")]
            wallet_ready = db.execute("SELECT 1 FROM wallet_sync WHERE id=1").fetchone() is not None
            credits: dict[tuple[str, str], Decimal] = {}
            if wallet_ready:
                for bill in db.execute("""SELECT tx_type,tx_id,amount FROM wallet_bills
                                          WHERE tx_type IN ('K','AK') AND cd_flag='D'"""):
                    key = (bill["tx_type"], bill["tx_id"])
                    credits[key] = credits.get(key, Decimal("0")) + Decimal(bill["amount"])
        catalog = catalog or TitleCatalog()
        for row in rows:
            alternatives = tuple(json.loads(row.pop("alternate_titles")))
            row["purchase_lines"] = purchase_lines.get(row["order_id"], []) if row["platform"] == "sonkwo" else []
            if row["platform"] == "sonkwo":
                refunded_quantity = sum(line["refunded_quantity"] for line in row["purchase_lines"])
                refunded_amount = sum((Decimal(line["refund_amount"]) for line in row["purchase_lines"]), Decimal("0"))
                if row["status"] == "refunded":
                    refunded_quantity, refunded_amount = row["quantity"], Decimal(row["amount"] or "0")
                row["active_purchase_quantity"] = row["quantity"] - refunded_quantity
                row["purchase_refund_amount"] = str(money(refunded_amount))
                row["purchase_net_spent"] = str(money(Decimal(row["amount"] or "0") - refunded_amount))
                row["purchase_display_status"] = ("refunded" if refunded_quantity == row["quantity"] else
                                                  "partially_refunded" if refunded_quantity else row["status"])
            row["wallet_checked"] = wallet_ready
            credit = (credits.get(("AK" if row["channel"] == "request" else "K",
                                   row["order_id"])) if row["platform"] == "steampy" else None)
            row["wallet_credit_amount"] = str(money(credit)) if credit is not None else None
            row["wallet_credit_matches_net"] = (
                credit == Decimal(row["net_amount"]) if credit is not None and row["net_amount"] is not None
                else None)
            row["later_same_product_sales"] = []
            row["later_same_product_sale_count"] = 0
            if row["platform"] != "steampy" or row["status"] == "sold":
                continue
            for sale in sold:
                if sale["occurred_at"][:19].replace("T", " ") < row["occurred_at"][:19].replace("T", " "):
                    continue
                matched, _ = catalog.compare_historical_products(
                    row["title"], alternatives, sale["title"],
                    tuple(json.loads(sale["alternate_titles"])))
                if not matched:
                    continue
                row["later_same_product_sale_count"] += 1
                if len(row["later_same_product_sales"]) < 3:
                    row["later_same_product_sales"].append({
                        "order_id": sale["order_id"], "occurred_at": sale["occurred_at"],
                        "amount": sale["amount"], "net_amount": sale["net_amount"]})
        return {"page": page, "page_size": page_size, "total": total,
                "pages": max(1, (total + page_size - 1) // page_size),
                "rows": rows}

    def scan_statistics(self) -> dict:
        """Summarize sightings without adding repeated scans as unique profit."""
        with self._connect() as db:
            run_counts = {
                row["status"]: row["count"]
                for row in db.execute("SELECT status, COUNT(*) AS count FROM scan_runs GROUP BY status")
            }
            unique_offers = db.execute(
                "SELECT COUNT(DISTINCT offer_url) AS count FROM assessments"
            ).fetchone()["count"]
            latest = db.execute("SELECT * FROM scan_runs ORDER BY rowid DESC LIMIT 1").fetchone()
            if latest is None:
                return {"run_counts": run_counts, "unique_offers": unique_offers,
                        "latest_run": None, "latest_completed_run": None,
                        "latest_verdicts": {},
                        "latest_estimated_profit": "0.00", "daily": []}
            completed = db.execute(
                """SELECT * FROM scan_runs WHERE status IN ('completed','completed_with_warnings')
                   ORDER BY rowid DESC LIMIT 1"""
            ).fetchone()
            rows = db.execute(
                "SELECT verdict, net_profit, liquidity FROM assessments WHERE run_id=?",
                (completed["id"] if completed is not None else "",),
            ).fetchall()
            verdicts: dict[str, int] = {}
            estimate = Decimal("0")
            asking_estimate = Decimal("0")
            for row in rows:
                verdicts[row["verdict"]] = verdicts.get(row["verdict"], 0) + 1
                liquidity = json.loads(row["liquidity"]) if row["liquidity"] else {}
                if row["verdict"] == "opportunity" and liquidity.get("status") == "request_viable":
                    estimate += Decimal(liquidity["request_profit"])
                if row["verdict"] == "price_only" and row["net_profit"] is not None:
                    asking_estimate += Decimal(row["net_profit"])
            daily = [dict(row) for row in db.execute(
                """SELECT substr(started_at,1,10) AS day, COUNT(*) AS runs,
                   SUM(processed) AS processed, SUM(opportunities) AS opportunities
                   FROM scan_runs GROUP BY substr(started_at,1,10)
                   ORDER BY day DESC LIMIT 7"""
            )]
            return {
                "run_counts": run_counts, "unique_offers": unique_offers,
                "latest_run": dict(latest),
                "latest_completed_run": dict(completed) if completed is not None else None,
                "latest_verdicts": verdicts,
                "latest_estimated_profit": str(money(estimate)),
                "latest_asking_profit": str(money(asking_estimate)), "daily": daily,
            }
