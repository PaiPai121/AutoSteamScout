import json
import os
import re
import datetime
import config

class FinanceAuditor:
    def __init__(self, ai_engine=None):
        self.sonkwo_file = "data/purchase_ledger.json"
        self.steampy_file = "data/steampy_sales.json"
        self.report_file = "data/finance_summary.json"
        self.alias_cache_file = "data/alias_cache.json"
        self.blacklist_file = "data/finance_blacklist.json"  # 🆕 黑名单配置文件
        self.PAYOUT_RATE = getattr(config, 'PAYOUT_RATE', 0.97)
        if ai_engine:
            self.ai_engine = ai_engine
        else:
            try:
                from arbitrage_commander import ArbitrageAI
                self.ai_engine = ArbitrageAI()
            except:
                print("⚠️ [Auditor] AI 引擎实例化失败，将仅使用硬核匹配。")
                self.ai_engine = None
        self.alias_cache = self._load_json(self.alias_cache_file)
        if not isinstance(self.alias_cache, dict):
            self.alias_cache = {}
        
        # 🆕 加载黑名单配置（Key 统一标识，区分采购/销售端）
        blacklist_config = self._load_json(self.blacklist_file)
        self.blacklist_purchase_keys = []  # 采购端排除的 Key
        self.blacklist_sales_keys = []     # 销售端排除的 Key
        
        if isinstance(blacklist_config, dict):
            for item in blacklist_config.get("blacklist", []):
                key = item.get("cd_key", "")
                side = item.get("side", "")
                if key:
                    if side == "purchase":
                        self.blacklist_purchase_keys.append(key)
                    elif side == "sales":
                        self.blacklist_sales_keys.append(key)
                    else:
                        # 兼容旧格式：没有 side 字段，默认同时排除
                        self.blacklist_purchase_keys.append(key)
                        self.blacklist_sales_keys.append(key)
        elif isinstance(blacklist_config, list):
            # 兼容更旧的纯列表格式
            self.blacklist_purchase_keys = list(blacklist_config)
            self.blacklist_sales_keys = list(blacklist_config)

    def _load_json(self, path):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return []
        return []

    def _clean_price(self, price_str):
        if not price_str:
            return 0.0
        try:
            cleaned = re.sub(r'[^\d.]', '', str(price_str))
            return float(cleaned) if cleaned else 0.0
        except:
            return 0.0

    def _calculate_profit_shadow(self, sonkwo_valid, realized_cash, floating_asset, total_investment, active_items=None, sold_items=None, name_mapping=None, ghost_names=None):
        """
        🎯 基于实物指纹的利润核算 (替代 FIFO)

        逻辑原则：
        - Key 精准匹配，废弃 FIFO 价格池
        - 直接"成本 A - 售价 B"，不再需要语义猜测

        🚀 真实价值算法：
        - 已售：收益 = (售价 * 0.97) - 成本 → 实际利润
        - 在售：收益 = (挂价 * 0.97) - 成本 → 账面浮盈
        - 遗珠：收益 = 0 - 成本 → 沉淀亏损 (提醒尽快上架)
        """
        active_map = {it['key']: it for it in (active_items or [])}
        sold_map = {it['key']: it for it in (sold_items or [])}

        trace_details = []
        total_realized_cost = 0.0

        for idx, p in enumerate(sonkwo_valid):
            p_key = p.get("cd_key", "").strip().upper()
            p_cost = self._clean_price(p.get("cost", 0))
            p_name = p.get("name")

            if p_key in sold_map:
                # 状态 A：已变现
                tag = "已售"
                s = sold_map[p_key]
                revenue = self._clean_price(s['price']) * self.PAYOUT_RATE
                total_realized_cost += p_cost
            elif p_key in active_map:
                # 状态 B：已上架在售 → 计算账面浮盈
                tag = "在售"
                s = active_map[p_key]
                revenue = self._clean_price(s['price']) * self.PAYOUT_RATE
            else:
                # 状态 C：🛡️ 遗珠（买了还没卖/没上架）→ 沉淀亏损
                tag = "遗珠"
                revenue = 0.0

            trace_details.append({
                "source_name": p_name,
                "tag": tag,
                "cost": p_cost,
                "est_revenue": round(revenue, 2),
                "profit": round(revenue - p_cost, 2),  # 🚀 所有状态都计算真实盈亏
                "mapped_name": active_map.get(p_key, {}).get('name') or sold_map.get(p_key, {}).get('name') or '-',  # 🆕 映射销售名
                "uid": p.get("uid", f"{p_name}_{idx}"),  # 🆕 使用账本原有的 uid
                # 🚨 CDKey 不返回给前端，保护敏感信息
                # "cd_key": p.get("cd_key", ""),  ← 已移除
                "damaged": p.get("damaged", False)  # 🚀 返回损毁标记
            })

        # 合并幽灵资产 (为了报表完整性)
        for g_name in (ghost_names or []):
            trace_details.append({
                "source_name": g_name,
                "uid": "GHOST",
                "tag": "来源不明",
                "cost": 0.0,
                "est_revenue": 0.0,
                "profit": 0.0
            })

        return {
            "current_profit": round(realized_cash - total_realized_cost, 2),
            "expected_profit": round((realized_cash + floating_asset) - total_investment, 2),
            "trace_details": trace_details
        }

    async def run_detailed_audit(self, silent=True):
        """
        🚀 流程编排器：指挥官只需看这里的流程

        Args:
            silent: 是否静默模式。False 时会在终端打印完整详细报告
        """
        # 1. 准备数据 (清洗与黑名单)
        sonkwo_valid, steampy_valid = self._prepare_data()

        # 2. 核心对账 (双向穿透)
        inventory_report = await self._reconcile_inventory(sonkwo_valid, steampy_valid)

        # 3. 财务分析
        financial_summary = self._analyze_finances(
            sonkwo_valid,
            inventory_report['active_items'],
            inventory_report['sold_items'],
            inventory_report['name_mapping'],
            inventory_report['ghost_names']
        )

        # 4. 封装报告
        final_report = self._build_report(
            inventory_report,
            financial_summary
        )

        # 5. 持久化与展示
        self._save_and_display(final_report, silent=silent)
        return final_report

    def _prepare_data(self):
        """
        🎯 数据准备层：加载并清洗原始数据
        返回：(sonkwo_valid, steampy_valid)
        """
        sonkwo_data = self._load_json(self.sonkwo_file)
        steampy_data = self._load_json(self.steampy_file)

        # 加载损毁列表
        damaged_file = "data/damaged_items.json"
        damaged_keys = set()
        if os.path.exists(damaged_file):
            try:
                with open(damaged_file, "r", encoding="utf-8") as f:
                    damaged_items = json.load(f)
                damaged_keys = {item.get("cd_key", "").strip().upper() for item in damaged_items if item.get("cd_key")}
            except:
                pass

        # 采购端：排除退款单 + 黑名单 Key
        sonkwo_valid = [
            p for p in sonkwo_data
            if "退款" not in p.get("status", "")
            and "REFUN" not in p.get("cd_key", "").upper()  # 排除退款占位符
            and p.get("cd_key")  # 确保有 Key
            and p.get("cd_key") not in self.blacklist_purchase_keys  # 排除采购端黑名单 Key
        ]

        # 销售端：排除黑名单 Key
        steampy_valid = [
            s for s in steampy_data
            if s.get("cd_key") not in self.blacklist_sales_keys
        ]

        print(f"📦 [数据准备] 采购有效：{len(sonkwo_valid)} 笔 | 销售有效：{len(steampy_valid)} 笔")
        print(f"   - 采购端黑名单 Key: {len(self.blacklist_purchase_keys)} 笔")
        print(f"   - 销售端黑名单 Key: {len(self.blacklist_sales_keys)} 笔")
        print(f"   - 损毁商品：{len(damaged_keys)} 笔")
        return sonkwo_valid, steampy_valid

    async def _reconcile_inventory(self, sonkwo_valid, steampy_valid):
        """
        🎯 终极 Key 碰撞审计 (银行对账模式)

        职责：
        1. 建立双索引，实现 100% 精准对接
        2. 自动识别：[在售]、[已售]、[遗珠：未上架]、[幽灵：货源不明]
        3. 计算货架账龄

        🚀 原则：Key 是唯一真理，废弃语义匹配
        """
        now = datetime.datetime.now()
        
        # A. 建立采购端索引 (以 Key 为准)
        purchase_map = {p.get("cd_key", "").strip().upper(): p for p in sonkwo_valid if p.get("cd_key")}
        
        # B. 建立销售端索引 (以 Key 为准)
        sales_map = {s.get("cd_key", "").strip().upper(): s for s in steampy_valid if s.get("cd_key")}

        active_items = []
        sold_items = []
        name_mapping = {} 
        matched_purchase_keys = set()
        
        # 1. 遍历 SteamPY 销售端（看看上架了什么）
        for s_key, s_item in sales_map.items():
            s_name = s_item.get("name", "")
            s_status = s_item.get("status", "")
            s_price = s_item.get("price", 0)
            
            if s_key in purchase_map:
                # ✅ 匹配成功：这是正规军，找到了货源
                p_item = purchase_map[s_key]
                matched_purchase_keys.add(s_key)
                name_mapping[p_item.get("name")] = s_name  # 维持名字映射缓存
                
                # 计算账龄 (从下单时间开始)
                try:
                    start_time = datetime.datetime.strptime(s_item.get("order_time", ""), "%Y-%m-%d %H:%M:%S")
                    days_on_shelf = (now - start_time).days
                except:
                    days_on_shelf = 0
                
                item_data = {
                    "name": s_name,
                    "price": s_price,
                    "key": s_key,
                    "cost": p_item.get("cost"),
                    "days": days_on_shelf  # 💡 保留账龄字段
                }

                # 💡 精确匹配状态，防止 "未出库" 被误认为 "已售"
                if s_status.strip() == "出库":
                    sold_items.append(item_data)
                else:
                    # 只要不是 "出库"，都视为在架资产 (包括 "未出库")
                    active_items.append(item_data)
            else:
                # 👻 幽灵资产：上架了，但衫果采购单里没有这个 Key
                # 这可能是你从其他平台买的，或者以前手动录入的
                pass 

        # 2. 识别遗珠 (买了但没上架)
        # 排除掉已经匹配成功的 Key，剩下的就是仓库里的资产
        ghost_names = []  # 记录货源不明
        for s_key, s_item in sales_map.items():
            if s_key not in matched_purchase_keys:
                ghost_names.append(s_item.get("name"))

        return {
            "active_items": active_items,
            "sold_items": sold_items,
            "closed_count": 0,
            "name_mapping": name_mapping,
            "ghost_names": ghost_names  # 这里的 ghost 指的是"货源不明的上架商品"
        }

    def _analyze_finances(self, sonkwo_valid, active_items, sold_items, name_mapping=None, ghost_names=None):
        """
        🎯 财务分析层：资金总量统计 + 影子利润核算

        🚀 Key-Based 精准核算：基于实物证据独立判定
        """
        name_mapping = name_mapping or {}
        ghost_names = ghost_names or []

        # 1. 投资总额
        total_investment = sum(
            self._clean_price(p.get("cost", 0)) for p in sonkwo_valid
        )

        # 2. 基于对账结果统计资金
        realized_cash = 0.0
        print("\n💰 [已售商品收入明细]")
        print("-" * 55)
        for item in sold_items:
            price = self._clean_price(item.get("price", "0"))
            income = price * self.PAYOUT_RATE
            realized_cash += income
            print(f"  {item.get('name', 'Unknown'):<25} ¥{price:>7.2f} → ¥{income:.2f}")
        print(f"  合计回笼：¥{realized_cash:.2f}")

        floating_asset = sum(
            self._clean_price(item.get("price", "0")) * self.PAYOUT_RATE
            for item in active_items
        )

        # 统计各状态数量
        counts = {
            "sold": len(sold_items),
            "active": len(active_items),
            "blacklisted": len(self.blacklist_sales_keys) + len(self.blacklist_purchase_keys)
        }

        # 3. 穿透利润溯源
        profit_result = self._calculate_profit_shadow(
            sonkwo_valid,
            realized_cash,
            floating_asset,
            total_investment,
            active_items,
            sold_items,
            name_mapping,
            ghost_names
        )

        # 🚀 4. 战略级 ROI 核算
        sold_cost = sum(t['cost'] for t in profit_result['trace_details'] if t['tag'] == '已售')
        sold_roi = (profit_result["current_profit"] / sold_cost * 100) if sold_cost > 0 else 0
        total_exp_roi = (profit_result["expected_profit"] / total_investment * 100) if total_investment > 0 else 0

        return {
            "total_investment": round(total_investment, 2),
            "realized_cash": round(realized_cash, 2),
            "floating_asset": round(floating_asset, 2),
            "current_profit": profit_result["current_profit"],
            "expected_profit": profit_result["expected_profit"],
            "sold_roi": round(sold_roi, 2),
            "total_expected_roi": round(total_exp_roi, 2),
            "trace_details": profit_result["trace_details"],
            "stats": counts
        }

    def _build_report(self, inventory_report, financial_summary):
        """
        🎯 报告封装层：组装最终审计报告结构

        🚀 单一数据源原则：
        - missing_from_steampy：从 trace_details 中提取"遗珠"状态
        - trace_details：包含采购交易 + 幽灵资产的完整溯源
        """
        now = datetime.datetime.now()
        total_investment = financial_summary["total_investment"]
        realized_cash = financial_summary["realized_cash"]

        # 🚀 从 trace_details 中提取真正的"遗珠"清单（单一事实源）
        trace_details = financial_summary["trace_details"]
        missing_from_trace = [
            t['source_name']
            for t in trace_details
            if t['tag'] == '遗珠' and not t.get('damaged', False)  # 🚀 排除损毁商品   
        ]

        return {
            "update_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_investment": total_investment,
                "realized_cash": realized_cash,
                "floating_asset": financial_summary["floating_asset"],
                "current_profit": financial_summary["current_profit"],
                "expected_profit": financial_summary["expected_profit"],
                "sold_roi": financial_summary["sold_roi"],  # 🚀 新增：已售 ROI
                "total_expected_roi": financial_summary["total_expected_roi"],  # 🚀 新增：全盘预期 ROI
                "recovery_rate": round(
                    (realized_cash / total_investment * 100) if total_investment > 0 else 0,
                    2
                ),
                "stats": financial_summary["stats"]
            },
            "details": {
                "on_shelf_aging": sorted(
                    inventory_report['active_items'],
                    key=lambda x: x['days'],
                    reverse=True
                ),
                "missing_from_steampy": missing_from_trace,
                "ghost_inventory": inventory_report['ghost_names'],
                "trace_details": trace_details  # 🚀 包含幽灵资产的完整交易明细
            }
        }

    def _save_and_display(self, report, silent = True):
        """
        🎯 持久化层：保存报告并打印终端仪表盘
        """
        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=4)

        # 💡 只有当你显式要求显示（比如手动调试）时才打印
        if not silent:
            self._print_terminal_dashboard(report)
        else:
            # 生产环境只留一条极简的成功记录到日志
            print(f"✅ [审计同步] {report['update_at']} | 利润: {report['summary']['current_profit']}")

    def _print_terminal_dashboard(self, r):
        summary = r['summary']
        details = r['details']
        print(
            f"📊 [财务快照] 投入：{summary['total_investment']} | "
            f"回笼：{summary['realized_cash']} | "
            f"实利：{summary.get('current_profit', 'N/A')} | "
            f"预利：{summary.get('expected_profit', 'N/A')} | "
            f"进度：{summary['recovery_rate']}%"
        )
        print("\n" + "🚀 " * 15)
        print(f"   【母舰全息资产审计】 {r['update_at']}")
        print("-" * 55)

        # 1. 资金核心区
        print(f" 💰 采购总成本：   ¥ {summary['total_investment']:.2f}")
        print(f" ✅ 已收回现金：   ¥ {summary['realized_cash']:.2f}")
        print(f" ⏳ 货架在售资产： ¥ {summary['floating_asset']:.2f}")

        rate = summary['recovery_rate']
        blocks = int(rate / 5)
        bar = "█" * blocks + "░" * (20 - blocks)
        print(f" 📊 回本进度：[{bar}] {rate:.1f}%")
        print("-" * 55)

        # 2. 🧊 货架账龄区 (展示卖得最慢的前 3 名)
        print(" 🕒【货架账龄警报】 (最陈旧挂单)")
        if details['on_shelf_aging']:
            for item in details['on_shelf_aging'][:3]:
                mood = "🔴" if item['days'] > 7 else "🟡" if item['days'] > 3 else "🟢"
                print(f"    {mood} {item['days']:>2}天 | {item['price']:<8} | {item['name']}")
        else:
            print("    ✅ 货架空空如也，请尽快补货")
        print("-" * 55)

        # 3. 🛡️ 库存漏损区 (仓库遗珠)
        missing = r['details']['missing_from_steampy']
        print(f" ⚠️【仓库遗珠检测】 (未上架：{len(missing)} 笔)")
        if missing:
            for name in missing[:10]:
                print(f"    ❓ 漏挂：{name}")
        else:
            print("    ✨ 完美对账：所有采购均已进入销售终端")
        print("-" * 55)

        # 4. 🚩 幽灵资产区 (上了但没买)
        ghosts = r['details']['ghost_inventory']
        print(f" 💀【幽灵资产警告】 (来源不明：{len(ghosts)} 笔)")
        if ghosts:
            for name in ghosts:
                print(f"    🚩 未知资产：{name}")
        else:
            print("    ✅ 账目清爽：无未知来源挂单")

        print("-" * 55)

        # 5. 📋 全息资产溯源清单 (新增：逐笔交易盈亏 + 幽灵资产)
        trace = r['details'].get('trace_details', [])
        print(f" 📋【全息资产溯源清单】 (共 {len(trace)} 笔交易，含幽灵资产)")
        if trace:
            # 按状态分组统计
            from collections import defaultdict
            grouped = defaultdict(list)
            for item in trace:
                grouped[item['tag']].append(item)

            # 打印每组详情
            for tag in ["已售", "在售", "遗珠", "来源不明"]:
                items = grouped.get(tag, [])
                if items:
                    tag_ico = {"已售": "✅", "在售": "🔵", "遗珠": "🟡", "来源不明": "👻"}.get(tag, "⚪")
                    print(f"\n    {tag_ico} ── {tag} 商品 ({len(items)} 笔) ──")
                    for it in items[:15]:  # 每组最多显示 15 笔
                        # 🚀 profit 永远是数字
                        profit_val = float(it['profit']) if isinstance(it['profit'], (int, float)) else 0
                        profit_color = "🟢" if profit_val > 0 else ("🔴" if profit_val < 0 else "⚪")
                        print(f"       {it['source_name']:<25} 成本¥{it['cost']:.2f} → 收入¥{it['est_revenue']:.2f} | 盈亏：{profit_color} ¥{profit_val:.2f}")
                    if len(items) > 15:
                        print(f"       ... 还有 {len(items) - 15} 笔，请在 Web 端查看完整清单")
        else:
            print("    📊 暂无交易记录")

        print("=" * 55)

if __name__ == "__main__":
    import asyncio
    auditor = FinanceAuditor()
    asyncio.run(auditor.run_detailed_audit(silent=False))
