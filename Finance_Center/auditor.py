import json
import os
import re
import datetime
from collections import Counter
import config

class FinanceAuditor:
    def __init__(self, ai_engine=None):
        self.sonkwo_file = "data/purchase_ledger.json"
        self.steampy_file = "data/steampy_sales.json"
        self.report_file = "data/finance_summary.json"
        self.alias_cache_file = "data/alias_cache.json"
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
        self.blacklist_times = [
            "2026-02-18 20:27:04",
            "2026-02-18 17:57:04"
        ]

    async def _is_same_game(self, p_name, s_names_list):
        """判断采购名与销售名是否对应同一游戏（缓存 > 硬核匹配 > AI）"""
        p_name_clean = p_name.strip()

        def super_clean(text):
            return re.sub(r'[^\w\u4e00-\u9fa5]', '', text).lower()

        p_val = super_clean(p_name_clean)

        # Level 1: 缓存判定
        if p_name_clean in self.alias_cache:
            # print(f"🔁 [缓存命中] 采购名 <{p_name_clean}> 已缓存对应销售名 <{self.alias_cache[p_name_clean]}>")
            target_py_name = self.alias_cache[p_name_clean]
            target_val = super_clean(target_py_name)
            for s_name in s_names_list:
                if super_clean(s_name) == target_val:
                    return s_name
            print(f"⚠️ [缓存失效] 虽然 <{p_name_clean}> 在缓存中，但对应的销售名 <{target_py_name}> 未在当前销售列表中找到。")

        # Level 2: 物理层匹配
        for s_name in s_names_list:
            s_val = super_clean(s_name)
            if s_val == p_val:
                return s_name

        # Level 3: AI 判定
        if self.ai_engine:
            if len(p_name_clean) < 2:
                return None

            # print(f"  📡 [AI 雷达启动] 正在为 <{p_name_clean}> 检索语义匹配项...")
            potential_candidates = [s for s in s_names_list if abs(len(p_name_clean) - len(s)) <= 15]

            for s_name in potential_candidates:
                try:
                    if self.ai_engine.verify_version(p_name_clean, s_name):
                        # print(f"  ✅ [AI 命中] 语义识别成功：<{p_name_clean}> == <{s_name}>")
                        if p_name_clean not in self.alias_cache:
                            self.alias_cache[p_name_clean] = s_name
                            with open(self.alias_cache_file, "w", encoding="utf-8") as f:
                                json.dump(self.alias_cache, f, ensure_ascii=False, indent=4)
                        return s_name
                except:
                    continue
        return None

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

    def _calculate_profit_shadow(self, sonkwo_valid, missing_inventory, realized_cash, floating_asset, total_investment, active_items=None, name_mapping=None):
        """
        🎯 穿透式成本溯源引擎
        逻辑原则：采购总额 = 已售成本 + 在售成本 + 遗珠成本

        💡 关键修复：使用名称映射表解决中英文命名差异
        
        🚀 价格池机制：为每个游戏建立 FIFO 价格队列，确保多价格场景下盈亏精确对应

        🚀 返回：{current_profit, expected_profit, trace_details: [每笔交易明细]}
        """
        try:
            active_items = active_items or []
            missing_inventory = missing_inventory or []
            name_mapping = name_mapping or {}

            # 清洗遗珠名称（移除 UID 后缀）
            missing_raw_names = [re.sub(r'\s\(.*\)', '', m).strip() for m in missing_inventory]
            missing_counter = Counter(missing_raw_names)

            # 💡 使用映射后的销售名构建在售计数器
            active_counter = Counter()
            for item in active_items:
                sale_name = item.get('name', '').strip()
                active_counter[sale_name] += 1

            # 🚀 新增：建立价格池 { "游戏名": [价格 1, 价格 2, ...] }
            # 目的：解决同名商品挂多个不同价格时，next() 盲抓导致的统计偏移
            price_pools = {}
            for item in active_items:
                name = item.get('name', '').strip()
                price = self._clean_price(item.get('price', 0))
                if name not in price_pools:
                    price_pools[name] = []
                price_pools[name].append(price)

            sold_cost = 0.0
            on_shelf_cost = 0.0
            missing_cost = 0.0

            # 诊断计数器
            unassigned_count = 0

            # 🚀 新增：交易溯源流水（每笔采购的状态 + 盈亏）
            trace_details = []

            for p in sonkwo_valid:
                p_cost = self._clean_price(p.get("cost", 0))
                p_name = p.get("name", "").strip()
                p_uid = p.get("uid", "Unknown")

                # 💡 使用映射后的销售名进行匹配
                mapped_name = name_mapping.get(p_name, p_name)

                # 判定优先级 A：是否在"遗珠清单"中？（使用映射名）
                if missing_counter[mapped_name] > 0:
                    missing_cost += p_cost
                    missing_counter[mapped_name] -= 1
                    tag = "遗珠"
                    # 遗珠：尚未产生任何收入
                    est_revenue = 0.0

                # 判定优先级 B：是否在"在售清单"中？（使用映射名）
                elif active_counter[mapped_name] > 0:
                    on_shelf_cost += p_cost
                    active_counter[mapped_name] -= 1
                    tag = "在售"
                    # 🚀 从价格池中按顺序"消费"一个价格 (FIFO 先进先出)
                    # 确保第一笔采购对应第一个挂单价格，物理同步
                    if mapped_name in price_pools and price_pools[mapped_name]:
                        price_val = price_pools[mapped_name].pop(0)
                        est_revenue = price_val * self.PAYOUT_RATE
                    else:
                        est_revenue = 0.0

                # 判定优先级 C：若既不在仓库也不在货架，必然已售
                else:
                    sold_cost += p_cost
                    unassigned_count += 1
                    tag = "已售"
                    # 已售：收入已计入 realized_cash，这里标记为"已核销"
                    est_revenue = 0.0

                # 🚀 记录这笔交易的完整溯源信息
                trace_details.append({
                    "source_name": p_name,
                    "uid": p_uid,
                    "mapped_name": mapped_name,
                    "tag": tag,
                    "cost": p_cost,
                    "est_revenue": round(est_revenue, 2),
                    "profit": round(est_revenue - p_cost, 2) if tag != "已售" else "已核销"
                })

            # 财务校验：各部分成本之和必须等于总投入
            calculated_total = sold_cost + on_shelf_cost + missing_cost
            if abs(calculated_total - total_investment) > 0.01:
                print(f"⚠️ [审计预警] 成本分流不平衡！差额：{calculated_total - total_investment:.2f}")

            # 诊断日志：如果有未分配成本，打印详情
            if unassigned_count > 0:
                print(f"\n🔍 [成本溯源] {unassigned_count} 笔采购归为'已售成本' ¥{sold_cost:.2f}")

            current_profit = round(realized_cash - sold_cost, 2)
            expected_profit = round((realized_cash + floating_asset) - total_investment, 2)

            return {
                "current_profit": current_profit,
                "expected_profit": expected_profit,
                "trace_details": trace_details
            }

        except Exception as e:
            import traceback
            print(f"🚨 [财务溯源崩溃] 错误：{e}\n{traceback.format_exc()[-200:]}")
            # 🚀 返回完整骨架结构，防止前端崩溃
            return {
                "current_profit": 0.0,
                "expected_profit": 0.0,
                "trace_details": []
            }

    async def run_detailed_audit(self):
        """
        🚀 流程编排器：指挥官只需看这里的流程
        """
        # 1. 准备数据 (清洗与黑名单)
        sonkwo_valid, steampy_valid = self._prepare_data()

        # 2. 核心对账 (双向穿透)
        inventory_report = await self._reconcile_inventory(sonkwo_valid, steampy_valid)
        
        # 3. 财务分析 (收入、在售、利润溯源)
        financial_summary = self._analyze_finances(
            sonkwo_valid,
            inventory_report['missing_inventory'],
            inventory_report['active_items'],
            inventory_report['sold_items'],
            inventory_report['closed_count'],
            inventory_report['name_mapping']
        )

        # 4. 封装报告
        final_report = self._build_report(
            inventory_report, 
            financial_summary
        )

        # 5. 持久化与展示
        self._save_and_display(final_report)
        return final_report

    def _prepare_data(self):
        """
        🎯 数据准备层：加载并清洗原始数据
        返回：(sonkwo_valid, steampy_valid)
        """
        sonkwo_data = self._load_json(self.sonkwo_file)
        steampy_data = self._load_json(self.steampy_file)

        # 采购端：排除退款单
        sonkwo_valid = [
            p for p in sonkwo_data 
            if "退款" not in p.get("status", "")
        ]

        # 销售端：排除黑名单中的"干扰订单"
        steampy_valid = [
            s for s in steampy_data
            if s.get("order_time") not in self.blacklist_times
        ]

        print(f"📦 [数据准备] 采购有效：{len(sonkwo_valid)} 笔 | 销售有效：{len(steampy_valid)} 笔")
        return sonkwo_valid, steampy_valid

    async def _reconcile_inventory(self, sonkwo_valid, steampy_valid):
        """
        🎯 核心对账层：双向穿透查漏 + 账龄分析
        返回：{active_items, sold_items, closed_count, missing_inventory, ghost_inventory, name_mapping}
        """
        now = datetime.datetime.now()

        # 1. 建立销售池计数（使用 Counter 处理多份拷贝）
        py_sales_pool = Counter([s.get("name", "") for s in steampy_valid])
        # 使用 Counter 记录未匹配的销售名（避免重复）
        unmatched_py_names = Counter([s.get("name", "") for s in steampy_valid])

        # 2. 账龄分析：提取在售商品 + 已售商品 + 关闭数量
        active_items = []
        sold_items = []
        closed_count = 0

        for s in steampy_valid:
            name = s.get("name", "")
            status = s.get("status", "")
            stock_str = s.get("stock", "1/1")

            try:
                curr_stk = int(re.findall(r'(\d+)\s*/', stock_str)[0])
            except:
                curr_stk = 1

            if "出售" in status:
                if curr_stk > 0:
                    try:
                        start_time = datetime.datetime.strptime(
                            s.get("order_time"), "%Y-%m-%d %H:%M:%S"
                        )
                        days_on_shelf = (now - start_time).days
                    except:
                        days_on_shelf = 0

                    active_items.append({
                        "name": name,
                        "price": s.get("my_price"),
                        "days": days_on_shelf
                    })
                else:
                    sold_items.append({
                        "name": name,
                        "price": s.get("my_price")
                    })
            # 💡 修复：关闭/下架状态且库存为 0，视为已售（非退款关闭）
            elif ("关闭" in status or "下架" in status) and curr_stk == 0:
                sold_items.append({
                    "name": name,
                    "price": s.get("my_price")
                })
                closed_count += 1
            elif "关闭" in status or "下架" in status:
                closed_count += 1

        # 3. 双向穿透对账 + 名称映射
        # print(f"📡 [审计中] 正在执行双向穿透对账...")
        missing_inventory = []
        match_pool = Counter(py_sales_pool)

        # 💡 名称映射表：采购名 -> 销售名 (用于财务分析时的成本溯源)
        name_mapping = {}

        # 排序：长名优先，防止短名抢坑
        for p in sorted(sonkwo_valid, key=lambda x: len(x.get("name", "")), reverse=True):
            p_name = p.get("name", "")
            uid = p.get("uid", "Unknown")

            matched_name = await self._is_same_game(p_name, list(match_pool.keys()))

            if matched_name and match_pool[matched_name] > 0:
                match_pool[matched_name] -= 1
                # 从 Counter 中减掉 1，如果减到 0 则自动移除
                unmatched_py_names[matched_name] -= 1
                if unmatched_py_names[matched_name] <= 0:
                    del unmatched_py_names[matched_name]

                # 🎯 记录名称映射，用于财务分析
                name_mapping[p_name] = matched_name
            else:
                missing_inventory.append(f"{p_name} ({uid})")

        # 4. 生成幽灵资产列表（去重后的未匹配销售名）
        ghost_inventory = list(unmatched_py_names.elements())

        return {
            "active_items": active_items,
            "sold_items": sold_items,
            "closed_count": closed_count,
            "missing_inventory": missing_inventory,
            "ghost_inventory": ghost_inventory,
            "name_mapping": name_mapping
        }

    def _analyze_finances(self, sonkwo_valid, missing_inventory, active_items, sold_items, closed_count, name_mapping=None):
        """
        🎯 财务分析层：资金总量统计 + 影子利润核算
        复用 inventory_report 中的 active_items 和 sold_items，确保数据一致性

        关键修复：使用名称映射表将采购名映射到销售名，解决中英文命名差异
        """
        name_mapping = name_mapping or {}

        # 1. 投资总额
        total_investment = sum(
            self._clean_price(p.get("cost", 0)) for p in sonkwo_valid
        )

        # 2. 基于对账结果统计资金 (不再遍历原始 steampy_valid)
        # 💡 注意：_reconcile_inventory 返回的 item 使用 "price" 字段存储价格
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
            "closed": closed_count,
            "blacklisted": len(self.blacklist_times)
        }

        # 3. 穿透利润溯源 (传入名称映射表)
        # 🚀 现在返回的是字典，包含 trace_details 交易明细
        profit_result = self._calculate_profit_shadow(
            sonkwo_valid,
            missing_inventory,
            realized_cash,
            floating_asset,
            total_investment,
            active_items,
            name_mapping
        )

        return {
            "total_investment": round(total_investment, 2),
            "realized_cash": round(realized_cash, 2),
            "floating_asset": round(floating_asset, 2),
            "current_profit": profit_result["current_profit"],
            "expected_profit": profit_result["expected_profit"],
            "trace_details": profit_result["trace_details"],  # 🚀 透传交易明细
            "stats": counts
        }

    def _build_report(self, inventory_report, financial_summary):
        """
        🎯 报告封装层：组装最终审计报告结构
        """
        now = datetime.datetime.now()
        total_investment = financial_summary["total_investment"]
        realized_cash = financial_summary["realized_cash"]

        return {
            "update_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_investment": total_investment,
                "realized_cash": realized_cash,
                "floating_asset": financial_summary["floating_asset"],
                "current_profit": financial_summary["current_profit"],
                "expected_profit": financial_summary["expected_profit"],
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
                "missing_from_steampy": inventory_report['missing_inventory'],
                "ghost_inventory": inventory_report['ghost_inventory'],
                "trace_details": financial_summary["trace_details"]  # 🚀 透传交易明细到 Web
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

if __name__ == "__main__":
    import asyncio
    auditor = FinanceAuditor()
    asyncio.run(auditor.run_detailed_audit())
