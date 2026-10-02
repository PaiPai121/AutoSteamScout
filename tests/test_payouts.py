"""Regression fixtures based on the SteamPy wallet bill field contract."""

from decimal import Decimal

import pytest

from autoscout.payouts import WalletBill, parse_wallet_bills
from autoscout.ports import SourceError
from autoscout.repository import Repository


def test_wallet_cashout_stays_distinct_from_bank_receipt(tmp_path):
    bills = parse_wallet_bills([
        {"id": "fee-1", "createTime": "2026-08-30 13:35:27", "amount": -3.01,
         "txType": "CashOutFee", "txId": "withdrawal-1", "cdFlag": "C",
         "userId": "must-not-be-retained"},
        {"id": "cash-1", "createTime": "2026-08-30 13:35:27", "amount": -301.0,
         "txType": "CashOut", "txId": "withdrawal-1", "cdFlag": "C"},
    ])
    assert bills[0].amount == Decimal("-3.01")
    assert not hasattr(bills[0], "userId")
    repo = Repository(tmp_path / "account.sqlite3")
    repo.replace_wallet_bills(bills)
    summary = repo.wallet_payouts()
    assert summary["withdrawal_count"] == 1
    assert summary["wallet_debits"] == "301.00"
    assert summary["wallet_fees"] == "3.01"
    assert summary["bank_confirmed"] == "0.00"
    assert summary["awaiting_bank_check"] == 1
    assert summary["rows"][0]["status"] == "bank_unverified"

    repo.record_bank_receipt("cash-1", Decimal("301.00"), "2026-08-31", "银行账单已核对")
    repo.replace_wallet_bills(bills)
    summary = repo.wallet_payouts()
    assert summary["bank_confirmed"] == "301.00"
    assert summary["awaiting_bank_check"] == 0
    assert summary["rows"][0]["bank_difference"] == "0.00"
    assert summary["rows"][0]["status"] == "manual_receipt"


def test_wallet_rejects_duplicate_or_invalid_amount():
    row = {"id": "same", "createTime": "2026-08-30 13:35:27",
           "amount": -301.0, "txType": "CashOut", "txId": "withdrawal-1"}
    with pytest.raises(SourceError, match="重复"):
        parse_wallet_bills([row, row])
    with pytest.raises(SourceError, match="金额"):
        parse_wallet_bills([{**row, "amount": "not-a-number"}])


def test_bank_receipt_cannot_attach_to_fee(tmp_path):
    repo = Repository(tmp_path / "account.sqlite3")
    repo.replace_wallet_bills([WalletBill("fee-1", "2026-08-30 13:35:27",
                                         Decimal("-3.01"), "CashOutFee", "one", "C")])
    with pytest.raises(ValueError, match="没有这笔提现"):
        repo.record_bank_receipt("fee-1", Decimal("301"), "2026-08-31")


def test_bank_confirmation_api_requires_local_session_token(tmp_path):
    from fastapi.testclient import TestClient

    from autoscout.settings import Settings
    from autoscout.web import create_app

    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    repo.replace_wallet_bills([WalletBill("cash-1", "2026-08-30 13:35:27",
                                         Decimal("-301.00"), "CashOut", "one", "C")])
    with TestClient(create_app(settings, repo)) as client:
        url = "/api/payouts/cash-1/receipt"
        payload = {"amount": "301.00", "received_at": "2026-08-31", "note": "已核对"}
        assert client.post(url, json=payload).status_code == 403
        token = client.get("/api/session").json()["token"]
        response = client.post(url, headers={"X-Scout-Token": token}, json=payload)
        assert response.status_code == 200
        assert response.json()["bank_confirmed"] == "301.00"
        cleared = client.delete(url, headers={"X-Scout-Token": token})
        assert cleared.status_code == 200
        assert cleared.json()["awaiting_bank_check"] == 1
