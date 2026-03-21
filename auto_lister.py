"""
┌─────────────────────────────────────────────────────────────────────────────┐
│                           AutoLister 自动上架引擎                            │
│                                                                             │
│  职责：基于 SteamPy 市场价格，自动为"待售商品"定价并上架                         │
│  核心逻辑：查询竞品价格 → 智能定价 (略低于市场) → 利润校验 → 自动上架          │
│                                                                             │
│  设计原则：高内聚、低耦合、模块化、可扩展                                     │
└─────────────────────────────────────────────────────────────────────────────┘
"""

import asyncio
import re
import logging
import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime

# 导入配置文件
import config

# 确保能找到项目根目录的模块
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import PAYOUT_RATE, AUDIT_CONFIG


class ListingStatus(Enum):
    """上架结果状态枚举"""
    SUCCESS = "success"              # 上架成功
    FAILED = "failed"                # 上架失败
    SKIPPED_LOW_PROFIT = "skipped_low_profit"   # 跳过：利润不足
    SKIPPED_LOSS = "skipped_loss"    # 跳过：会亏本
    SKIPPED_NO_MARKET = "skipped_no_market"     # 跳过：SteamPy 无市场数据
    SKIPPED_ALREADY_LISTED = "skipped_already_listed"  # 跳过：已在售
    MARKED_AS_DAMAGED = "marked_as_damaged"  # 🚫 标记为损毁：仅核算成本，禁售
    ERROR = "error"                  # 异常错误


@dataclass
class MarketData:
    """SteamPy 市场数据"""
    game_name: str           # 匹配到的游戏名
    lowest_price: float      # 市场最低价
    top5_prices: List[float] # Top5 价格阵列
    average_price: float     # 平均价格


@dataclass
class PricingDecision:
    """定价决策结果"""
    target_price: float      # 建议上架价格
    undercut_amount: float   # 比市场最低价低多少
    expected_revenue: float  # 预期收入 (扣除手续费后)
    expected_profit: float   # 预期利润
    roi: float               # 投资回报率
    is_profitable: bool      # 是否有利可图
    reason: str              # 决策理由


@dataclass
class ListingResult:
    """单次上架操作结果"""
    status: ListingStatus
    purchase_name: str       # 采购端游戏名
    purchase_cost: float     # 采购成本
    cd_key: str              # 激活码
    market_name: Optional[str] = None      # SteamPy 匹配名
    listing_price: Optional[float] = None  # 上架价格
    profit: Optional[float] = None         # 预期利润
    message: str = ""        # 详细消息


class AutoLister:
    """
    自动上架引擎

    核心职责：
    1. 查询 SteamPy 市场价格
    2. 智能定价 (略低于市场均价)
    3. 利润校验 (扣除手续费后仍高于成本)
    4. 执行自动上架
    5. 发送通知反馈
    """

    def __init__(self, steampy_monitor, notifier=None):
        """
        初始化自动上架引擎

        Args:
            steampy_monitor: SteamPyMonitor 实例，用于查询价格和上架
            notifier: FeishuNotifier 实例，用于发送通知 (可选)
        """
        self.steampy = steampy_monitor
        self.notifier = notifier

        # 🚀 从配置文件读取参数（不再硬编码）
        self.UNDERCUT_AMOUNT = config.AUTO_LISTER_CONFIG["UNDERCUT_AMOUNT"]
        self.MIN_PROFIT_MARGIN = config.AUTO_LISTER_CONFIG["MIN_PROFIT_MARGIN"]
        self.MIN_ROI = config.AUTO_LISTER_CONFIG["MIN_ROI"]

        # 日志记录器
        self.logger = logging.getLogger("AutoLister")

    def is_damaged(self, cd_key: str) -> bool:
        """
        🚫 检查 CDKey 是否在损毁黑名单中

        Args:
            cd_key: 激活码

        Returns:
            bool: 是否已损毁
        """
        damaged_file = "data/damaged_items.json"
        if not os.path.exists(damaged_file):
            return False

        try:
            with open(damaged_file, "r", encoding="utf-8") as f:
                damaged_items = json.load(f)

            # 检查 CDKey 是否在损毁列表中
            cd_key_upper = cd_key.strip().upper()
            for item in damaged_items:
                # 可能通过 name 或 cd_key 标记
                if item.get("cd_key", "").strip().upper() == cd_key_upper:
                    return True
                # 也检查 name（如果没有 cd_key）
                if item.get("name") and item.get("name") == self._get_name_by_cdkey(cd_key):
                    return True

            return False
        except:
            return False

    def _get_name_by_cdkey(self, cd_key: str) -> Optional[str]:
        """根据 CDKey 获取商品名称（用于损毁检查）"""
        ledger_file = "data/purchase_ledger.json"
        if not os.path.exists(ledger_file):
            return None

        try:
            with open(ledger_file, "r", encoding="utf-8") as f:
                purchase_data = json.load(f)

            cd_key_upper = cd_key.strip().upper()
            for item in purchase_data:
                if item.get("cd_key", "").strip().upper() == cd_key_upper:
                    return item.get("name")
            return None
        except:
            return None
    
    async def query_market_price(self, game_name: str) -> Optional[MarketData]:
        """
        查询 SteamPy 市场价格

        Args:
            game_name: 游戏名称

        Returns:
            MarketData 对象，如果查询失败返回 None
        """
        try:
            print(f"🔍 [市场价格查询] 目标：{game_name}")
            print(f"\n{'='*60}")
            print(f"🔍 [Step 1] 查询 SteamPy 市场价格")
            print(f"   游戏名称：{game_name}")

            # 调用 SteamPy 的搜索接口
            result = await self.steampy.get_game_market_price_with_name(game_name)

            if not result or len(result) < 3:
                self.logger.warning(f"⚠️ [市场价格查询] 未找到匹配：{game_name}")
                print(f"   ❌ 未找到市场数据")
                print(f"{'='*60}\n")
                return None

            py_price, py_match_name, top5_list = result

            # 计算平均价格
            average_price = sum(top5_list) / len(top5_list) if top5_list else py_price

            market_data = MarketData(
                game_name=py_match_name,
                lowest_price=py_price,
                top5_prices=top5_list,
                average_price=average_price
            )

            print(
                f"✅ [市场价格查询] 成功 | "
                f"匹配名：{py_match_name} | "
                f"最低价：¥{py_price} | "
                f"Top5: {top5_list}"
            )
            print(f"   ✅ 匹配名称：{py_match_name}")
            print(f"   💰 市场最低价：¥{py_price}")
            print(f"   📊 Top5 价格：{top5_list}")
            print(f"   📈 平均价格：¥{average_price:.2f}")
            print(f"{'='*60}\n")

            return market_data

        except Exception as e:
            self.logger.error(f"🚨 [市场价格查询] 异常：{e}")
            print(f"   🚨 查询异常：{e}")
            print(f"{'='*60}\n")
            return None
    
    def calculate_pricing(
        self,
        market_data: MarketData,
        purchase_cost: float
    ) -> PricingDecision:
        """
        计算最优定价策略

        Args:
            market_data: 市场数据
            purchase_cost: 采购成本

        Returns:
            PricingDecision 定价决策
        """
        print(f"\n{'='*60}")
        print(f"🧮 [Step 2] 计算最优定价")
        print(f"   市场最低价：¥{market_data.SKC-sku-label.lowest_price}")
        print(f"   采购成本：¥{purchase_cost}")
        print(f"   自动 undercut：¥{self.UNDERCUT_AMOUNT}")

        # 策略：比市场最低价再低一点，确保竞争力
        target_price = max(
            market_data.SKC-sku-label.lowest_price - self.UNDERCUT_AMOUNT,
            0.01  # 确保价格为正
        )

        # 🚀 保留两位小数，避免浮点数精度问题
        target_price = round(target_price, 2)

        # 计算预期收入 (扣除 3% 手续费)
        expected_revenue = round(target_price * PAYOUT_RATE, 2)

        # 计算预期利润
        expected_profit = round(expected_revenue - purchase_cost, 2)

        # 计算 ROI（百分比形式，如 12.92 表示 12.92%）
        roi = round((expected_profit / purchase_cost) * 100, 2) if purchase_cost > 0 else 0

        # 判定是否有利可图
        # MIN_ROI 是 0.05，表示 5%，需要乘以 100 变成 5.0 来和 roi 比较
        is_profitable = (
            expected_profit >= self.MIN_PROFIT_MARGIN and
            roi >= self.MIN_ROI * 100  # 0.05 * 100 = 5.0%
        )

        print(f"   上架价格：¥{target_price:.2f}")
        print(f"   预期收入：¥{expected_revenue:.2f} (扣除 3% 手续费)")
        print(f"   预期利润：¥{expected_profit:.2f}")
        print(f"   ROI: {roi:.1f}%")
        print(f"   最低利润要求：¥{self.MIN_PROFIT_MARGIN}")
        print(f"   最低 ROI 要求：{self.MIN_ROI * 100:.1f}%")

        # 生成决策理由
        if is_profitable:
            reason = f"定价 ¥{target_price:.2f}，预计利润 ¥{expected_profit:.2f} (ROI: {roi:.1f}%)"
            print(f"   ✅ 利润校验通过")
        else:
            if expected_profit < 0:
                reason = f"定价 ¥{target_price:.2f} 将亏损 ¥{abs(expected_profit):.2f}"
                print(f"   ❌ 会亏本，将跳过")
            else:
                reason = f"利润 ¥{expected_profit:.2f} 低于最低要求 ¥{self.MIN_PROFIT_MARGIN}"
                print(f"   ❌ 利润不足，将跳过")

        print(f"{'='*60}\n")

        return PricingDecision(
            target_price=target_price,
            undercut_amount=self.UNDERCUT_AMOUNT,
            expected_revenue=expected_revenue,
            expected_profit=expected_profit,
            roi=roi,
            is_profitable=is_profitable,
            reason=reason
        )
    
    async def execute_listing(
        self,
        game_name: str,
        cd_key: str,
        price: float
    ) -> Tuple[bool, str]:
        """
        执行上架操作

        Args:
            game_name: 游戏名称 (使用 SteamPy 匹配名)
            cd_key: 激活码
            price: 上架价格

        Returns:
            (success, message) 元组
        """
        try:
            # 🚀 确保价格保留两位小数
            price = round(float(price), 2)

            # 🛡️ [关键修复] 确保页面在卖家中心
            print("🏥 [页面检查] 确保页面在卖家中心...")
            if hasattr(self.steampy, 'action_goto_seller_post'):
                current_url = self.steampy.page.url
                if "sellerCenterCDK" not in current_url and "sellerCDKey" not in current_url:
                    print(f"   ⚠️ 当前 URL: {current_url}")
                    print("   🧭 正在导航到卖家中心...")
                    nav_success = await self.steampy.action_goto_seller_post()
                    if not nav_success:
                        return False, "🚨 上架失败：无法导航到卖家中心"
                    await asyncio.sleep(1.0)  # 等待页面加载
                    print("   ✅ 已到达卖家中心")
                else:
                    print(f"   ✅ 已在卖家中心：{current_url}")

            print(f"\n{'='*60}")
            print(f"🚀 [Step 3] 执行上架操作")
            print(f"   游戏名称：{game_name}")
            print(f"   激活码：{cd_key[:5]}***{cd_key[-3:] if len(cd_key) > 5 else ''}")
            print(f"   上架价格：¥{price:.2f}")
            self.logger.info(f"🚀 [执行上架] {game_name} | 价格：¥{price} | Key: {cd_key[:5]}***")

            # 调用 SteamPy 的上架接口
            # 构造 post 指令格式：游戏名|Key|价格
            post_arg = f"{game_name}|{cd_key}|{price}"
            print(f"   📝 POST 参数：{post_arg}")

            success, message = await self.steampy.action_fill_post_form(
                game_name=game_name,
                key_code=cd_key,
                price=price,
                auto_confirm=True  # 自动模式，跳过人工确认
            )

            if success:
                print(f"✅ [执行上架] 成功：{message}")
                print(f"   ✅ 上架成功")
                print(f"   消息：{message}")

                # 🚀 [关键优化] 上架成功后立即写入 steampy_sales.json，避免重新爬取
                print(f"\n💾 [数据同步] 正在更新 steampy_sales.json...")
                await self._sync_to_steampy_sales(game_name, cd_key, price)
            else:
                self.logger.warning(f"⚠️ [执行上架] 失败：{message}")
                print(f"   ❌ 上架失败")
                print(f"   消息：{message}")

            print(f"{'='*60}\n")

            return success, message

        except Exception as e:
            error_msg = f"上架异常：{str(e)}"
            self.logger.error(f"🚨 [执行上架] 异常：{error_msg}")
            print(f"   🚨 上架异常：{e}")
            print(f"{'='*60}\n")
            return False, error_msg

    async def _sync_to_steampy_sales(self, game_name: str, cd_key: str, price: float):
        """
        🚀 上架成功后立即同步到 steampy_sales.json

        Args:
            game_name: 游戏名称
            cd_key: 激活码
            price: 上架价格
        """
        import json
        import os
        from datetime import datetime

        sales_file = "data/steampy_sales.json"

        # 加载现有数据
        sales_data = []
        if os.path.exists(sales_file):
            try:
                with open(sales_file, "r", encoding="utf-8") as f:
                    sales_data = json.load(f)
            except:
                sales_data = []

        # 创建新条目
        now = datetime.now()
        new_entry = {
            "order_id": f"SPY_{now.strftime('%Y%m%d%H%M%S')}",
            "name": game_name,
            "price": price,
            "cd_key": cd_key,
            "status": "未出库",  # 刚上架，还未卖出
            "order_tag": "STOCK",  # 库存状态
            "order_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "sync_at": now.strftime("%Y-%m-%d %H:%M:%S")
        }

        # 检查是否已存在（避免重复添加）
        cd_key_upper = cd_key.strip().upper()
        exists = any(item.get("cd_key", "").strip().upper() == cd_key_upper for item in sales_data)

        if not exists:
            sales_data.append(new_entry)

            # 保存数据
            with open(sales_file, "w", encoding="utf-8") as f:
                json.dump(sales_data, f, ensure_ascii=False, indent=2)

            print(f"   ✅ 已同步到 steampy_sales.json")
            print(f"   📝 游戏：{game_name} | 价格：¥{price} | 状态：未出库")
        else:
            print(f"   ⚠️ 该商品已在 steampy_sales.json 中，跳过同步")
    
    async def list_single_item(
        self,
        purchase_name: str,
        cd_key: str,
        purchase_cost: float
    ) -> ListingResult:
        """
        上架单个商品 (核心入口函数)

        Args:
            purchase_name: 采购端游戏名
            cd_key: 激活码
            purchase_cost: 采购成本

        Returns:
            ListingResult 上架结果
        """
        try:
            print(f"\n{'#'*60}")
            print(f"# 🎯 [商品上架] 开始处理")
            print(f"#   采购名称：{purchase_name}")
            print(f"#   采购成本：¥{purchase_cost}")
            print(f"#   CDKey: {cd_key[:5]}***{cd_key[-3:] if len(cd_key) > 5 else ''}")
            print(f"{'#'*60}\n")

            # 🛡️ [风险修复 5] 检查是否已在售（避免重复上架）
            # 如果 steampy 有 current_active_keys 属性，直接检查
            if hasattr(self.steampy, 'current_active_keys'):
                print(f"🔍 [检查] 验证是否已在售...")
                if cd_key.strip().upper() in [k.upper() for k in self.steampy.current_active_keys]:
                    print(f"   ⏭️  该商品已在售，跳过上架")
                    return ListingResult(
                        status=ListingStatus.SKIPPED_ALREADY_LISTED,
                        purchase_name=purchase_name,
                        purchase_cost=purchase_cost,
                        cd_key=cd_key,
                        message=f"该商品已在售，跳过上架"
                    )

            # 🚫 [政治审查] 检查是否已标记为损毁（推荐方案）
            print(f"🚫 [审查] 检查是否已标记为损毁...")
            if self.is_damaged(cd_key):
                print(f"   🚫 该商品已标记为损毁，严禁上架")
                self.logger.warning(f"🚫 [损毁拦截] {purchase_name} 已标记为损毁，严禁上架。")
                return ListingResult(
                    status=ListingStatus.MARKED_AS_DAMAGED,
                    purchase_name=purchase_name,
                    purchase_cost=purchase_cost,
                    cd_key=cd_key,
                    message="该项已标记为损毁，成本已计入财务报表，严禁上架。"
                )

            # Step 1: 查询 SteamPy 市场价格
            market_data = await self.query_market_price(purchase_name)

            if not market_data:
                return ListingResult(
                    status=ListingStatus.SKIPPED_NO_MARKET,
                    purchase_name=purchase_name,
                    purchase_cost=purchase_cost,
                    cd_key=cd_key,
                    message=f"SteamPy 无市场数据，无法定价"
                )

            # Step 2: 计算最优定价
            pricing = self.calculate_pricing(market_data, purchase_cost)

            print(
                f"📊 [定价决策] {purchase_name} | "
                f"成本：¥{purchase_cost} | "
                f"目标价：¥{pricing.target_price} | "
                f"预期利润：¥{pricing.expected_profit} | "
                f"{pricing.reason}"
            )

            # Step 3: 利润校验
            if not pricing.is_profitable:
                status = (
                    ListingStatus.SKIPPED_LOSS
                    if pricing.expected_profit < 0
                    else ListingStatus.SKIPPED_LOW_PROFIT
                )

                print(f"\n{'#'*60}")
                print(f"# 🚫 [跳过] 利润校验未通过")
                print(f"#   状态：{status.value}")
                print(f"#   原因：{pricing.reason}")
                print(f"{'#'*60}\n")

                return ListingResult(
                    status=status,
                    purchase_name=purchase_name,
                    purchase_cost=purchase_cost,
                    cd_key=cd_key,
                    message=pricing.reason
                )

            # Step 4: 执行上架
            print(f"\n{'#'*60}")
            print(f"# ✅ [通过] 利润校验通过，准备上架")
            print(f"{'#'*60}\n")

            success, message = await self.execute_listing(
                game_name=market_data.game_name,  # 使用 SteamPy 匹配名
                cd_key=cd_key,
                price=pricing.target_price
            )

            if success:
                return ListingResult(
                    status=ListingStatus.SUCCESS,
                    purchase_name=purchase_name,
                    purchase_cost=purchase_cost,
                    cd_key=cd_key,
                    market_name=market_data.game_name,
                    listing_price=pricing.target_price,
                    profit=pricing.expected_profit,
                    message=f"上架成功 | 价格：¥{pricing.target_price} | 预期利润：¥{pricing.expected_profit}"
                )
            else:
                return ListingResult(
                    status=ListingStatus.FAILED,
                    purchase_name=purchase_name,
                    purchase_cost=purchase_cost,
                    cd_key=cd_key,
                    market_name=market_data.game_name,
                    listing_price=pricing.target_price,
                    message=f"上架失败：{message}"
                )

        except Exception as e:
            self.logger.error(f"🚨 [上架流程] 异常：{e}")
            print(f"\n{'#'*60}")
            print(f"# 🚨 [异常] 上架流程崩溃")
            print(f"#   错误：{e}")
            print(f"{'#'*60}\n")
            return ListingResult(
                status=ListingStatus.ERROR,
                purchase_name=purchase_name,
                purchase_cost=purchase_cost,
                cd_key=cd_key,
                message=f"上架异常：{str(e)}"
            )
    
    async def list_missing_items(
        self,
        missing_items: List[Dict[str, Any]]
    ) -> List[ListingResult]:
        """
        批量上架待售商品

        Args:
            missing_items: 待售商品列表，每项包含：
                - name: 游戏名
                - cd_key: 激活码
                - cost: 采购成本

        Returns:
            List[ListingResult] 上架结果列表
        """
        results = []

        print(f"\n{'='*80}")
        print(f"="*80)
        print(f"📦 [批量上架] 开始处理 {len(missing_items)} 个待售商品")
        print(f"⚠️  [警告] 此过程将占用浏览器，巡航任务将等待...")
        print(f"="*80)
        print(f"{'='*80}\n")

        print(f"📦 [批量上架] 开始处理 {len(missing_items)} 个待售商品")

        for i, item in enumerate(missing_items, 1):
            print(f"\n{'='*80}")
            print(f"[{i}/{len(missing_items)}] 正在处理：{item.get('name', 'Unknown')}")
            print(f"{'='*80}\n")

            print(f"\n{'='*60}")
            print(f"[{i}/{len(missing_items)}] 正在处理：{item.get('name', 'Unknown')}")
            print(f"{'='*60}")

            result = await self.list_single_item(
                purchase_name=item.get('name', 'Unknown'),
                cd_key=item.get('cd_key', ''),
                purchase_cost=float(item.get('cost', 0))
            )

            results.append(result)

            # 发送飞书通知
            await self._send_notification(result)

            # 频率控制，防止请求过快
            print(f"\n⏳ 等待 1 秒，防止请求过快...\n")
            await asyncio.sleep(1.0)

        # 发送汇总报告
        await self._send_summary_report(results)

        print(f"\n{'='*80}")
        print(f"✅ [批量上架] 完成！共处理 {len(results)} 个商品")
        print(f"🔓 浏览器已释放，巡航任务可继续")
        print(f"{'='*80}\n")

        return results
    
    async def _send_notification(self, result: ListingResult):
        """
        发送单条上架通知（详细完整版）
        每条通知都包含所有字段，不省略任何信息
        ⚠️ 安全原则：不显示 CDKey 明文，使用订单号标识
        """
        if not self.notifier:
            return

        # 根据状态生成不同的通知内容
        status_emoji = {
            ListingStatus.SUCCESS: "✅",
            ListingStatus.FAILED: "❌",
            ListingStatus.SKIPPED_LOW_PROFIT: "📉",
            ListingStatus.SKIPPED_LOSS: "💸",
            ListingStatus.SKIPPED_NO_MARKET: "📭",
            ListingStatus.SKIPPED_ALREADY_LISTED: "⏭️",
            ListingStatus.ERROR: "🚨"
        }

        emoji = status_emoji.get(result.status, "⚪")

        # 状态文本映射
        status_text_map = {
            ListingStatus.SUCCESS: "上架成功",
            ListingStatus.FAILED: "上架失败",
            ListingStatus.SKIPPED_LOW_PROFIT: "跳过 - 利润不足",
            ListingStatus.SKIPPED_LOSS: "跳过 - 会亏本",
            ListingStatus.SKIPPED_NO_MARKET: "跳过 - 无市场数据",
            ListingStatus.SKIPPED_ALREADY_LISTED: "跳过 - 已在售",
            ListingStatus.ERROR: "异常错误"
        }
        status_text = status_text_map.get(result.status, "未知状态")

        # 生成订单号（用于标识，不暴露敏感信息）
        order_id = f"ORD_{datetime.now().strftime('%H%M%S')}_{hash(result.purchase_name) % 1000:03d}"

        # 构建通知内容（完整详细版）
        content = (
            f"{emoji} [自动上架反馈 - 详细报告]\n"
            f"{'═'*50}\n"
            f"📌 基本信息\n"
            f"{'─'*50}\n"
            f"🎮 游戏名称：{result.purchase_name}\n"
            f"📋 订单编号：{order_id}\n"
            f"💰 采购成本：¥{result.purchase_cost:.2f}\n"
            f"📊 上架状态：{status_text}\n"
        )

        # 添加市场信息（如果有）
        if result.market_name:
            content += (
                f"\n"
                f"📌 市场信息\n"
                f"{'─'*50}\n"
                f"🏷️ SteamPy 匹配名：{result.market_name}\n"
            )

        # 添加定价信息（如果有）
        if result.listing_price is not None:
            # 计算手续费和预期收入
            service_fee = result.listing_price * 0.03  # 3% 手续费
            expected_revenue = result.listing_price * 0.97  # 扣除手续费后

            content += (
                f"\n"
                f"📌 定价详情\n"
                f"{'─'*50}\n"
                f"💵 上架价格：¥{result.listing_price:.2f}\n"
                f"🧾 平台手续费 (3%): ¥{service_fee:.2f}\n"
                f"💰 预期收入：¥{expected_revenue:.2f}\n"
            )

        # 添加利润信息（如果有）
        if result.profit is not None:
            # 计算 ROI
            roi = (result.profit / result.purchase_cost * 100) if result.purchase_cost > 0 else 0
            profit_emoji = "🟢" if result.profit > 0 else ("🔴" if result.profit < 0 else "⚪")

            content += (
                f"\n"
                f"📌 利润分析\n"
                f"{'─'*50}\n"
                f"{profit_emoji} 预期利润：¥{result.profit:.2f}\n"
                f"📈 投资回报率：{roi:.1f}%\n"
            )

        # 添加详细消息
        content += (
            f"\n"
            f"📌 详细说明\n"
            f"{'─'*50}\n"
            f"💬 {result.message}\n"
        )

        # 根据不同状态添加额外提示
        if result.status == ListingStatus.SUCCESS:
            content += (
                f"\n"
                f"{'═'*50}\n"
                f"✨ 商品已成功上架到 SteamPy 平台\n"
                f"💡 请定期检查销售情况，如有需要可调整价格\n"
            )
        elif result.status == ListingStatus.SKIPPED_LOSS:
            content += (
                f"\n"
                f"{'═'*50}\n"
                f"⚠️ 该商品上架后会亏损，已自动跳过\n"
                f"💡 建议：考虑提高售价或等待市场价格回升\n"
            )
        elif result.status == ListingStatus.SKIPPED_LOW_PROFIT:
            content += (
                f"\n"
                f"{'═'*50}\n"
                f"⚠️ 该商品利润过低，已自动跳过\n"
                f"💡 建议：考虑提高售价或等待市场价格回升\n"
            )
        elif result.status == ListingStatus.SKIPPED_NO_MARKET:
            content += (
                f"\n"
                f"{'═'*50}\n"
                f"⚠️ SteamPy 平台暂无该游戏市场数据\n"
                f"💡 建议：手动在 SteamPy 搜索确认是否有市场需求\n"
            )
        elif result.status == ListingStatus.FAILED:
            content += (
                f"\n"
                f"{'═'*50}\n"
                f"🚨 上架失败，请检查原因\n"
                f"💡 建议：查看系统日志获取详细错误信息\n"
            )
        elif result.status == ListingStatus.ERROR:
            content += (
                f"\n"
                f"{'═'*50}\n"
                f"🚨 发生异常错误\n"
                f"💡 建议：检查系统日志并联系管理员\n"
            )

        content += f"{'═'*50}"

        await self.notifier.send_text(content)
    
    async def _send_summary_report(self, results: List[ListingResult]):
        """发送批量上架汇总报告"""
        if not self.notifier:
            return

        # 统计各项数据
        total = len(results)
        success_count = sum(1 for r in results if r.status == ListingStatus.SUCCESS)
        failed_count = sum(1 for r in results if r.status == ListingStatus.FAILED)
        skipped_low_profit = sum(1 for r in results if r.status == ListingStatus.SKIPPED_LOW_PROFIT)
        skipped_loss = sum(1 for r in results if r.status == ListingStatus.SKIPPED_LOSS)
        skipped_no_market = sum(1 for r in results if r.status == ListingStatus.SKIPPED_NO_MARKET)

        # 计算总预期利润
        total_expected_profit = sum(
            r.profit for r in results
            if r.status == ListingStatus.SUCCESS and r.profit
        )

        content = (
            f"📊 [自动上架汇总报告]\n"
            f"{'═'*50}\n"
            f"📦 总处理：{total} 个\n"
            f"✅ 成功上架：{success_count} 个\n"
            f"❌ 上架失败：{failed_count} 个\n"
            f"📉 利润不足：{skipped_low_profit} 个\n"
            f"💸 会亏本：{skipped_loss} 个\n"
            f"📭 无市场：{skipped_no_market} 个\n"
            f"{'─'*50}\n"
            f"💰 总预期利润：¥{total_expected_profit:.2f}\n"
            f"⏰ 完成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'═'*50}"
        )

        await self.notifier.send_text(content)


# ==========================================
# 🚀 独立测试入口
# ==========================================
if __name__ == "__main__":
    # 这里可以添加独立的测试代码
    print("AutoLister 模块已加载")
    print("请在 arbitrage_commander.py 中集成使用")
