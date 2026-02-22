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

    def _calculate_profit_shadow(self, sonkwo_valid, realized_cash, floating_asset, total_investment, active_items=None, sold_items=None, name_mapping=None, ghost_names=None):
        """
        🎯 终极全息穿透审计引擎
        
        逻辑原则：实物 > 映射 > 遗珠
        盈亏核算：双 FIFO 价格池（在售/已售）
        
        🚀 返回：{current_profit, expected_profit, trace_details: [含采购 + 幽灵]}
        """
        try:
            active_items = active_items or []
            sold_items = sold_items or []
            name_mapping = name_mapping or {}
            ghost_names = ghost_names or []
            
            # 1. 预建索引：销售全量价格表（用于幽灵资产）
            all_sales_items = active_items + sold_items
            price_map = {i['name']: self._clean_price(i.get('price', 0)) for i in all_sales_items}
            
            # 2. 建立双重 FIFO 价格池：分别对应"在售"和"已售"
            active_price_pools = {}
            for item in active_items:
                name = item['name']
                price = self._clean_price(item.get('price', 0))
                active_price_pools.setdefault(name, []).append(price)

            sold_price_pools = {}
            for item in sold_items:
                name = item['name']
                price = self._clean_price(item.get('price', 0))
                sold_price_pools.setdefault(name, []).append(price)

            # 3. 建立状态计数器
            active_counter = Counter([i['name'] for i in active_items])
            sold_counter = Counter([i['name'] for i in sold_items])

            sold_cost = 0.0
            on_shelf_cost = 0.0
            missing_cost = 0.0
            trace_details = []

            # 4. 判定采购流 (判决每一笔钱的归宿)
            for p in sonkwo_valid:
                p_cost = self._clean_price(p.get("cost", 0))
                p_name = p.get("name", "").strip()
                p_uid = p.get("uid", "Unknown")
                
                target_name = name_mapping.get(p_name)

                if target_name and active_counter[target_name] > 0:
                    # ✅ [在售]：匹配到货架实物
                    tag = "在售"
                    on_shelf_cost += p_cost
                    active_counter[target_name] -= 1
                    # 消费"在售价格池"
                    price_val = active_price_pools[target_name].pop(0) if active_price_pools[target_name] else 0
                    est_revenue = price_val * self.PAYOUT_RATE
                    profit_val = round(est_revenue - p_cost, 2)
                    
                elif target_name and sold_counter[target_name] > 0:
                    # ✅ [已售]：匹配到历史销售存根
                    tag = "已售"
                    sold_cost += p_cost
                    sold_counter[target_name] -= 1
                    # 🚀 消费"已售价格池"，找回历史成交价
                    price_val = sold_price_pools[target_name].pop(0) if sold_price_pools[target_name] else 0
                    est_revenue = price_val * self.PAYOUT_RATE
                    profit_val = round(est_revenue - p_cost, 2)
                    
                else:
                    # 🟡 [遗珠]：无映射或无坑位
                    tag = "遗珠"
                    missing_cost += p_cost
                    est_revenue = 0.0
                    profit_val = round(est_revenue - p_cost, 2)

                trace_details.append({
                    "source_name": p_name,
                    "uid": p_uid,
                    "mapped_name": target_name or "-",
                    "tag": tag,
                    "cost": p_cost,
                    "est_revenue": round(est_revenue, 2),
                    "profit": profit_val
                })

            # 5. 判定幽灵流 (合并入全息视图)
            for g_name in ghost_names:
                rev = price_map.get(g_name, 0) * self.PAYOUT_RATE
                trace_details.append({
                    "source_name": g_name,
                    "uid": "GHOST",
                    "mapped_name": g_name,
                    "tag": "来源不明",
                    "cost": 0.0,
                    "est_revenue": round(rev, 2),
                    "profit": round(rev, 2)
                })

            # 6. 财务汇总校验
            current_profit = round(realized_cash - sold_cost, 2)
            expected_profit = round((realized_cash + floating_asset) - total_investment, 2)

            return {
                "current_profit": current_profit,
                "expected_profit": expected_profit,
                "trace_details": trace_details
            }

        except Exception as e:
            import traceback
            print(f"🚨 [全息审计崩溃]: {e}\n{traceback.format_exc()[-200:]}")
            return {"current_profit": 0.0, "expected_profit": 0.0, "trace_details": []}

    async def run_detailed_audit(self, silent=True):
        """
        🚀 流程编排器：指挥官只需看这里的流程
        
        Args:
            silent: 是否静默模式。False 时会在终端打印完整详细报告
        """
        # 1. 准备数据 (清洗与黑名单)
        sonkwo_valid, steampy_valid = self._prepare_data()

        # 2. 核心对账 (双向穿透) - 只生成 name_mapping 和 active_items
        inventory_report = await self._reconcile_inventory(sonkwo_valid, steampy_valid)

        # 3. 财务分析 (收入、在售、利润溯源) - 单向流：财务层统一判定状态
        financial_summary = self._analyze_finances(
            sonkwo_valid,
            inventory_report['active_items'],
            inventory_report['sold_items'],
            inventory_report['closed_count'],
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
        🎯 终极脱水版：只提供翻译字典和实物清单
        
        职责：
        1. 盘点货架实物 (Active)
        2. 整理历史存根 (Sold)
        3. 建立语义映射 (Mapping)
        4. 标记未知来源 (Ghost)
        
        🚀 原则：只搬运数据，不判定状态
        """
        now = datetime.datetime.now()
        
        # 1. 物理层：扫描销售端，划分"货架"与"历史"
        active_items = []
        sold_items = []
        closed_count = 0
        
        for s in steampy_valid:
            name = s.get("name", "")
            status = s.get("status", "")
            # 简单的库存判定
            try:
                curr_stk = int(re.findall(r'(\d+)\s*/', s.get("stock", "1/1"))[0])
            except:
                curr_stk = 1

            if "出售" in status and curr_stk > 0:
                # 记录在售实物
                try:
                    start_time = datetime.datetime.strptime(s.get("order_time"), "%Y-%m-%d %H:%M:%S")
                    days_on_shelf = (now - start_time).days
                except:
                    days_on_shelf = 0
                    
                active_items.append({
                    "name": name, 
                    "price": s.get("my_price"), 
                    "days": days_on_shelf
                })
            elif (("出售" in status and curr_stk == 0) or 
                  (("关闭" in status or "下架" in status) and curr_stk == 0)):
                # 记录已售存根
                sold_items.append({"name": name, "price": s.get("my_price")})
            else:
                closed_count += 1

        # 2. 语义层：建立采购名与销售名的映射（纯翻译，不带状态）
        name_mapping = {}
        all_sales_names = list(set([s.get("name", "") for s in steampy_valid]))
        
        # 建立临时计数器，仅用于分配映射关系（防止多笔同名采购抢占）
        temp_pool = Counter([s.get("name", "") for s in steampy_valid])
        
        for p in sorted(sonkwo_valid, key=lambda x: len(x.get("name", "")), reverse=True):
            p_name = p.get("name", "")
            # 只管找不找得到翻译，不管它卖没卖掉
            matched_name = await self._is_same_game(p_name, all_sales_names)
            if matched_name and temp_pool[matched_name] > 0:
                name_mapping[p_name] = matched_name
                temp_pool[matched_name] -= 1

        # 3. 补遗层：找出哪些销售项是"石头里蹦出来的" (Ghost)
        matched_sales_set = set(name_mapping.values())
        ghost_names = [name for name in all_sales_names if name not in matched_sales_set]

        return {
            "active_items": active_items,
            "sold_items": sold_items,
            "closed_count": closed_count,
            "name_mapping": name_mapping,
            "ghost_names": ghost_names
        }

    def _analyze_finances(self, sonkwo_valid, active_items, sold_items, closed_count, name_mapping=None, ghost_names=None):
        """
        🎯 财务分析层：资金总量统计 + 影子利润核算
        
        🚀 终极单向流：财务层基于实物证据独立判定（实物 > 映射 > 遗珠）
        """
        name_mapping = name_mapping or {}
        ghost_names = ghost_names or []

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

        # 3. 穿透利润溯源 (终极单向流：财务层独立判定)
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
        # 从 trace_details 中提取"已售"总成本（财务层精准分流）
        sold_cost = sum(t['cost'] for t in profit_result['trace_details'] if t['tag'] == '已售')
        
        # 计算已售部分的 ROI (实利 / 已售成本)
        sold_roi = (profit_result["current_profit"] / sold_cost * 100) if sold_cost > 0 else 0
        
        # 计算全盘预期 ROI (预期总利 / 总投入)
        total_exp_roi = (profit_result["expected_profit"] / total_investment * 100) if total_investment > 0 else 0

        return {
            "total_investment": round(total_investment, 2),
            "realized_cash": round(realized_cash, 2),
            "floating_asset": round(floating_asset, 2),
            "current_profit": profit_result["current_profit"],
            "expected_profit": profit_result["expected_profit"],
            "sold_roi": round(sold_roi, 2),  # 🟢 新增：已售 ROI
            "total_expected_roi": round(total_exp_roi, 2),  # 🔵 新增：全盘预期 ROI
            "trace_details": profit_result["trace_details"],  # 🚀 透传交易明细
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
            f"{t['source_name']} ({t['uid']})"
            for t in trace_details
            if t['tag'] == '遗珠'
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
