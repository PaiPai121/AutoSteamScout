"""Runtime configuration; no account data or credentials belong in source control."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os
from pathlib import Path

from dotenv import load_dotenv

from .domain import PricingPolicy


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    headless: bool = True
    show_login_input: bool = False
    browser_channel: str | None = "auto"
    sonkwo_profile: Path | None = None
    steampy_profile: Path | None = None
    auto_scan: bool = False
    scan_interval: float = 1800.0
    scan_keywords: tuple[str, ...] = ("",)
    fee_rate: Decimal = Decimal("0.03")
    payout_fee_rate: Decimal = Decimal("0.01")
    min_profit: Decimal = Decimal("0.50")
    min_roi: Decimal = Decimal("0.05")
    undercut: Decimal = Decimal("0.01")
    max_pages: int = 2
    max_offers: int = 100
    operation_timeout: float = 30.0
    run_timeout: float = 900.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        raw_data = os.getenv("AUTOSCOUT_DATA_DIR")
        data_dir = Path(raw_data).expanduser().resolve() if raw_data else Path.home() / ".autoscout"
        settings = cls(
            data_dir=data_dir,
            headless=os.getenv("AUTOSCOUT_HEADLESS", "true").lower() not in {"0", "false", "no"},
            show_login_input=os.getenv("AUTOSCOUT_SHOW_LOGIN_INPUT", "false").lower() in {"1", "true", "yes"},
            browser_channel=os.getenv("AUTOSCOUT_BROWSER_CHANNEL", "auto") or None,
            sonkwo_profile=(Path(os.environ["AUTOSCOUT_SONKWO_PROFILE"]).expanduser().resolve()
                             if os.getenv("AUTOSCOUT_SONKWO_PROFILE") else None),
            steampy_profile=(Path(os.environ["AUTOSCOUT_STEAMPY_PROFILE"]).expanduser().resolve()
                             if os.getenv("AUTOSCOUT_STEAMPY_PROFILE") else None),
            auto_scan=os.getenv("AUTOSCOUT_AUTO_SCAN", "true").lower() not in {"0", "false", "no"},
            scan_interval=float(os.getenv("AUTOSCOUT_SCAN_INTERVAL", "1800")),
            scan_keywords=tuple(os.getenv("AUTOSCOUT_SCAN_KEYWORDS", "").split(",")),
            fee_rate=Decimal(os.getenv("AUTOSCOUT_SELL_FEE_RATE", "0.03")),
            payout_fee_rate=Decimal(os.getenv("AUTOSCOUT_PAYOUT_FEE_RATE", "0.01")),
            min_profit=Decimal(os.getenv("AUTOSCOUT_MIN_PROFIT", "0.50")),
            min_roi=Decimal(os.getenv("AUTOSCOUT_MIN_ROI", "0.05")),
            undercut=Decimal(os.getenv("AUTOSCOUT_UNDERCUT", "0.01")),
            max_pages=int(os.getenv("AUTOSCOUT_MAX_PAGES", "2")),
            max_offers=int(os.getenv("AUTOSCOUT_MAX_OFFERS", "100")),
            operation_timeout=float(os.getenv("AUTOSCOUT_OPERATION_TIMEOUT", "30")),
            run_timeout=float(os.getenv("AUTOSCOUT_RUN_TIMEOUT", "900")),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        self.policy
        if self.max_pages < 1 or self.max_offers < 1:
            raise ValueError("扫描页数和商品上限必须大于零")
        if self.operation_timeout <= 0 or self.run_timeout <= 0:
            raise ValueError("超时时间必须大于零")
        if self.scan_interval <= 0:
            raise ValueError("自动巡航间隔必须大于零")
        if not self.scan_keywords or any(len(word.strip()) > 80 for word in self.scan_keywords):
            raise ValueError("自动巡航关键词无效")

    @property
    def policy(self) -> PricingPolicy:
        return PricingPolicy(self.fee_rate, self.min_profit, self.min_roi, self.undercut,
                             self.payout_fee_rate)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "autoscout.sqlite3"

    @property
    def aliases_path(self) -> Path:
        return self.data_dir / "aliases.json"

    def profile_path(self, platform: str) -> Path:
        if platform not in {"sonkwo", "steampy"}:
            raise ValueError("未知平台")
        override = self.sonkwo_profile if platform == "sonkwo" else self.steampy_profile
        return override or self.data_dir / "profiles" / platform
