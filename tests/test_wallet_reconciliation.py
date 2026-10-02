"""Regressions for the missing wallet debit, with anonymized source metadata."""

from decimal import Decimal

import pytest

from autoscout.orders import AccountOrder
from autoscout.payouts import PayoutSyncService, WalletBill, parse_wallet_balance, parse_wallet_bills
from autoscout.ports import SourceError
from autoscout.repository import Repository
from autoscout.settings import Settings


def bill(number, amount, tx_type, tx_id="", flag="C", time="2026-02-19 20:36:41"):
    return WalletBill(str(number), time, Decimal(str(amount)), tx_type, tx_id, flag)


def test_sales_withdrawals_other_debit_and_actual_balance_reconcile(tmp_path):
    repo = Repository(tmp_path / "account.sqlite3")
    nets = [Decimal("25.00")] * 31 + [Decimal("48.42"), Decimal("5.58"), Decimal("9.70")]
    orders, bills = [], []
    for index, net in enumerate(nets):
        request = index >= 32
        fee = None if request else Decimal("0.77") if index < 31 else Decimal("1.61")
        orders.append(AccountOrder("steampy", str(index), "游戏", "2026-02-19 20:00:00", "sold", 1,
                                   net + fee if fee is not None else None, fee, net,
                                   channel="request" if request else "ordinary"))
        bills.append(bill(index, net, "AK" if request else "K", str(index), "D"))
    repo.replace_account_orders(orders, source_coverage=("ordinary", "request"))
    bills += [bill("other", "-69.51", "w-K", "related-order"),
              bill("w1", "-460", "CashOut", "withdrawal-1"),
              bill("f1", "-4.60", "CashOutFee", "withdrawal-1"),
              bill("w2", "-301", "CashOut", "withdrawal-2"),
              bill("f2", "-3.01", "CashOutFee", "withdrawal-2")]
    repo.replace_wallet_bills(bills, balance=Decimal("0.58"), pending_balance=Decimal("0"),
                              balance_at="2026-10-02T02:30:50+00:00")
    finance = repo.financial_overview()
    wallet = finance["wallet"]
    assert finance["sale_net"] == wallet["sale_credits"] == "838.70"
    assert finance["withdrawal_debits"] == wallet["withdrawals"] == "761.00"
    assert finance["withdrawal_fees"] == wallet["withdrawal_fees"] == "7.61"
    assert wallet["other_debits"] == "69.51" and wallet["other_credits"] == "0.00"
    assert wallet["period_net"] == wallet["history_net"] == wallet["available_balance"] == "0.58"
    assert wallet["balance_difference"] == wallet["sale_credit_difference"] == "0.00"
    assert wallet["sale_credits_verified"] is True
    assert len(wallet["other_movements"]) == 1
    assert wallet["other_movements"][0]["label"] == "CDKey"
    assert wallet["other_movements"][0]["tx_id"] == "related-order"
    assert finance["bank_confirmed"] is None  # Platform money balance is not bank evidence.


def test_cutoff_other_credits_and_reversals_are_not_misclassified(tmp_path):
    repo = Repository(tmp_path / "account.sqlite3")
    bills = [bill("old", 50, "Deposit", flag="D", time="2024-12-31 23:59:59"),
             bill("new-sale", 100, "K", "sale", "D", "2024-12-31T16:00:00+00:00"),
             bill("refund", 4, "Refund", flag="D"),
             bill("new-other", -5, "w-K"), bill("unknown-debit", -2, "NEW-TYPE"),
             bill("withdrawal", -60, "CashOut", "withdrawal"),
             bill("fee", "-0.60", "CashOutFee", "withdrawal"),
             bill("fee-reversal", "0.10", "CashOutFee", "reversal", "D"),
             bill("withdrawal-reversal", 3, "CashOut", "reversal", "D")]
    repo.replace_wallet_bills(bills, balance=Decimal("89.50"), pending_balance=Decimal("12"))
    wallet = repo.financial_overview()["wallet"]
    assert wallet["sale_credits"] == "100.00"
    assert wallet["other_credits"] == "7.10" and wallet["other_debits"] == "7.00"
    assert wallet["period_net"] == "39.50" and wallet["pre_period_net"] == "50.00"
    assert wallet["history_net"] == "89.50" and wallet["balance_difference"] == "0.00"
    assert wallet["pending_balance"] == "12.00"  # Kept separate from the available balance.
    assert len(wallet["other_movements"]) == 5
    assert all(row["bill_id"] != "old" for row in wallet["other_movements"])
    assert any(row["label"] == "平台类型 NEW-TYPE" for row in wallet["other_movements"])
    assert repo.wallet_payouts()["wallet_debits"] == "60.00"
    assert repo.wallet_payouts()["wallet_fees"] == "0.60"
    with pytest.raises(ValueError, match="没有这笔提现"):
        repo.record_bank_receipt("withdrawal-reversal", Decimal("3"), "2026-02-20")


def test_balance_gap_and_missing_per_sale_link_are_visible(tmp_path):
    repo = Repository(tmp_path / "account.sqlite3")
    repo.replace_account_orders([
        AccountOrder("steampy", "sale", "游戏", "2026-02-19 20:00:00", "sold", 1,
                     Decimal("10"), Decimal("0"), Decimal("10"))], source_coverage=("ordinary", "request"))
    repo.replace_wallet_bills([bill("credit", 10, "K", "different-sale", "D")],
                              balance=Decimal("15"), pending_balance=Decimal("0"))
    wallet = repo.financial_overview()["wallet"]
    assert wallet["sale_credit_difference"] == "0.00"
    assert wallet["sale_credits_verified"] is False  # Equal totals alone do not prove correctness.
    assert wallet["balance_difference"] == "5.00"
    repo.replace_wallet_bills([bill("credit", 10, "K", "different-sale", "D")])
    wallet = repo.wallet_reconciliation()
    assert wallet["available_balance"] is None and wallet["balance_difference"] is None


@pytest.mark.parametrize("value", [None, "NaN", "Infinity", "invalid"])
def test_invalid_balance_never_becomes_zero(value):
    with pytest.raises(SourceError, match="余额金额无效"):
        parse_wallet_balance({"success": True, "result": {"balance": value, "pendingBalance": "0"}})


@pytest.mark.parametrize("value", ["NaN", "Infinity"])
def test_nonfinite_bill_amount_is_rejected(value):
    with pytest.raises(SourceError, match="金额无效"):
        parse_wallet_bills([{"id": "bill", "createTime": "2026-02-19 20:36:41", "amount": value, "txType": "K"}])


async def test_failed_balance_fetch_retains_previous_whole_snapshot(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    repo.replace_wallet_bills([bill("saved", 10, "K", flag="D")],
                              balance=Decimal("10"), pending_balance=Decimal("0"))
    before = repo.wallet_reconciliation()

    async def failed_fetch(*_args):
        raise SourceError("SteamPy 钱包余额返回 HTTP 503，保留上次完整快照")

    monkeypatch.setattr("autoscout.payouts.fetch_wallet_snapshot", failed_fetch)
    service = PayoutSyncService(settings, repo)
    service.start()
    status = await service.wait()
    assert status["status"] == "failed" and "503" in status["error"]
    assert repo.wallet_reconciliation() == before
