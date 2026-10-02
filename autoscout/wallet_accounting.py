"""Classify every signed wallet movement without guessing its business purpose."""

from decimal import Decimal

from .domain import money
from .reconcile import in_report_period


def movement_kind(tx_type: str, cd_flag: str, amount: Decimal) -> str:
    if cd_flag == "D" and amount > 0 and tx_type in {"K", "AK"}:
        return "sale_credits"
    if cd_flag == "C" and amount < 0:
        if tx_type == "CashOut":
            return "withdrawals"
        if tx_type == "CashOutFee":
            return "withdrawal_fees"
    return "other_credits" if amount > 0 else "other_debits"


def reconcile_wallet(bills: list[dict], balance: str | None) -> dict:
    """Keep the reporting period separate from the full-history balance comparison.

    The public SteamPy wallet page maps K/w-K to CDKey and AK/w-AK to
    CDK求购. These are categories, not proof that a debit is a purchase.
    Any unrecognized transaction remains visible in other movements.
    """
    totals = {kind: Decimal("0") for kind in
              ("sale_credits", "withdrawals", "withdrawal_fees", "other_credits", "other_debits")}
    history_net = Decimal("0")
    period_net = Decimal("0")
    other = []
    labels = {"K": "CDKey", "w-K": "CDKey", "AK": "CDK求购", "w-AK": "CDK求购"}
    for bill in bills:
        amount = Decimal(bill["amount"])
        history_net += amount
        if not in_report_period(bill["occurred_at"]):
            continue
        period_net += amount
        kind = movement_kind(bill["tx_type"], bill["cd_flag"], amount)
        totals[kind] += abs(amount)
        if kind.startswith("other_"):
            other.append({**bill, "label": labels.get(bill["tx_type"], "平台类型 " + bill["tx_type"]),
                          "amount": str(money(amount)), "direction": "credit" if amount > 0 else "debit"})
    return {
        **{key: str(money(value)) for key, value in totals.items()},
        "period_net": str(money(period_net)), "history_net": str(money(history_net)),
        "pre_period_net": str(money(history_net - period_net)),
        "balance_difference": str(money(Decimal(balance) - history_net)) if balance is not None else None,
        "other_movements": sorted(other, key=lambda row: (row["occurred_at"], row["bill_id"]), reverse=True),
    }
