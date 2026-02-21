import json
import os
import re
import datetime
from collections import Counter

class FinanceAuditor:
    def __init__(self, ai_engine=None):
        self.sonkwo_file = "data/purchase_ledger.json"
        self.steampy_file = "data/steampy_sales.json"
        self.report_file = "data/finance_summary.json"
        self.alias_cache_file = "data/alias_cache.json" # 🚀 别名缓存
        self.PAYOUT_RATE = 0.95 
        if ai_engine:
            self.ai_engine = ai_engine
        else:
            try:
                from arbitrage_commander import ArbitrageAI
                self.ai_engine  = ArbitrageAI()
            except:
                print("⚠️ [Auditor] AI 引擎实例化失败，将仅使用硬核匹配。")
                self.ai_engine = None
        self.alias_cache = self._load_json(self.alias_cache_file)
        if not isinstance(self.alias_cache, dict): self.alias_cache = {}
        # 🚫 异常订单黑名单 (基于 order_time 唯一标识)
        self.blacklist_times = [
            "2026-02-18 20:27:04", # 异形工厂2 异常关闭单
            "2026-02-18 17:57:04"  # 异形工厂2 异常关闭单
        ]
    
    async def _is_same_game(self, p_name, s_names_list):
        p_name_clean = p_name.strip()
        
        # 0. 定义超强清洗函数
        def super_clean(text):
            return re.sub(r'[^\w\u4e00-\u9fa5]', '', text).lower()

        p_val = super_clean(p_name_clean)

        # 1. Level 1: 缓存判定
        if p_name_clean in self.alias_cache:
            print(f"🔁 [缓存命中] 采购名 <{p_name_clean}> 已缓存对应销售名 <{self.alias_cache[p_name_clean]}>")
            target_py_name = self.alias_cache[p_name_clean]
            target_val = super_clean(target_py_name)
            for s_name in s_names_list:
                if super_clean(s_name) == target_val:
                    return s_name
            print(f"⚠️ [缓存失效] 虽然 <{p_name_clean}> 在缓存中，但对应的销售名 <{target_py_name}> 未在当前销售列表中找到。")
            # return None

        # 2. Level 2: 物理层匹配 + 深度诊断 Log
        for s_name in s_names_list:
            s_val = super_clean(s_name)
            
            # 🔍 【深度诊断埋点】：当名字中包含“全网公敌”时触发
            # if "全网公敌" in p_name and "全网公敌" in s_name:
            #     print(f"\n🕵️ [诊断日志] 发现潜在匹配项:")
            #     print(f"   - 采购名: [{p_name_clean}] (Hex: {' '.join(hex(ord(c)) for c in p_name_clean)})")
            #     print(f"   - 销售名: [{s_name}] (Hex: {' '.join(hex(ord(c)) for c in s_name)})")
            #     print(f"   - 清洗后对比: [{p_val}] vs [{s_val}] | 结果: {p_val == s_val}")

            if s_val == p_val:
                return s_name

        # 3. Level 3: AI 判定
        if self.ai_engine:
            # 在进入 AI 前也打个 Log
            if "全网公敌" in p_name:
                print(f"   📡 [AI 决策前路] 物理匹配失败，申请 AI 对抗: {p_name_clean}")
            if len(p_name_clean) < 2: return None

            print(f"  📡 [AI 雷达启动] 正在为 <{p_name_clean}> 检索语义匹配项...")
            potential_candidates = [s for s in s_names_list if abs(len(p_name_clean) - len(s)) <= 15]
            
            for s_name in potential_candidates:
                try:
                    if self.ai_engine.verify_version(p_name_clean, s_name):
                        print(f"  ✅ [AI 命中] 语义识别成功: <{p_name_clean}> == <{s_name}>")
                        
                        # 💡 核心保护：只有当缓存里没有这一项时，才允许 AI 写入
                        if p_name_clean not in self.alias_cache:
                            self.alias_cache[p_name_clean] = s_name
                            with open(self.alias_cache_file, "w", encoding="utf-8") as f:
                                json.dump(self.alias_cache, f, ensure_ascii=False, indent=4)
                        return s_name
                except: continue
        return None


    def _load_json(self, path):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except: return []
        return []

    def _clean_price(self, price_str):
        if not price_str: return 0.0
        try:
            cleaned = re.sub(r'[^\d.]', '', str(price_str))
            return float(cleaned) if cleaned else 0.0
        except: return 0.0

    def _calculate_profit_shadow(self, sonkwo_valid, missing_inventory, realized_cash, floating_asset, total_investment, active_items=None):
        """
        🎯 穿透式成本溯源引擎
        逻辑原则：采购总额 = 已售成本 + 在售成本 + 遗珠成本
        """
        try:
            # --- 1. 数据准备与归一化 ---
            active_items = active_items or []
            missing_inventory = missing_inventory or []
            
            # 使用 Counter 精确处理多份拷贝的计数
            # 遗珠计数器：买了但没上架的
            missing_raw_names = [re.sub(r'\s\(.*\)', '', m).strip() for m in missing_inventory]
            missing_counter = Counter(missing_raw_names)
            
            # 在售计数器：已经上架且正在卖的
            active_counter = Counter([item.get('name', '').strip() for item in active_items])
            
            # 成本桶
            sold_cost = 0.0      # 对应已回笼资金的本金
            on_shelf_cost = 0.0  # 对应货架资产的本金
            missing_cost = 0.0   # 对应吃灰资产的本金

            # --- 2. 物理分流循环 (严格遵循优先级) ---
            # 按采购时间或价格排序不影响总量，但为了逻辑一致性，保持原始顺序
            for p in sonkwo_valid:
                p_cost = self._clean_price(p.get("cost", 0))
                p_name = p.get("name", "").strip()
                
                # 判定优先级 A：是否在“遗珠清单”中？
                if missing_counter[p_name] > 0:
                    missing_cost += p_cost
                    missing_counter[p_name] -= 1
                    
                # 判定优先级 B：是否在“在售清单”中？
                elif active_counter[p_name] > 0:
                    on_shelf_cost += p_cost
                    active_counter[p_name] -= 1
                    
                # 判定优先级 C：若既不在仓库也不在货架，根据逻辑严密性，它必然已售
                else:
                    sold_cost += p_cost

            # --- 3. 财务校验 (严谨性检查) ---
            # 校验公式：各部分成本之和必须等于总投入（允许极小浮点误差）
            calculated_total = sold_cost + on_shelf_cost + missing_cost
            if abs(calculated_total - total_investment) > 0.01:
                print(f"⚠️ [审计预警] 成本分流不平衡！差额: {calculated_total - total_investment:.2f}")

            # --- 4. 利润计算 (基于物理溯源结果) ---
            # 当前已实现利润 = 实际回笼现金 - 对应这些现金的物理采购成本
            current_profit = round(realized_cash - sold_cost, 2)
            
            # 最终预期总利润 = (实际现金 + 货架预期回收) - 采购总投入
            # 这是最严谨的全局指标，不受对账匹配细微误差影响
            expected_profit = round((realized_cash + floating_asset) - total_investment, 2)

            return current_profit, expected_profit
            
        except Exception as e:
            import traceback
            print(f"🚨 [财务溯源崩溃] 错误: {e}\n{traceback.format_exc()[-200:]}")
            return 0.0, 0.0


    async def run_detailed_audit(self):
        sonkwo_data = self._load_json(self.sonkwo_file)
        steampy_data = self._load_json(self.steampy_file)
        now = datetime.datetime.now()

        # --- 🚀 1. 有效性过滤 & 黑名单清洗 ---
        # 采购端：排除退款单
        sonkwo_valid = [p for p in sonkwo_data if "退款" not in p.get("status", "")]
        
        # 销售端：排除黑名单中的“干扰订单”（如异形工厂2的关闭单）
        # 这样统计和对账时就不会受到这部分干扰
        steampy_valid = [
            s for s in steampy_data 
            if s.get("order_time") not in self.blacklist_times
        ]

        # --- 🚀 2. 建立双向计数池 ---
        # 销售池：当前销售端存在的“坑位”计数
        py_sales_pool = Counter([s.get("name", "") for s in steampy_valid])
        # 记录所有在 SteamPY 出现的原始名字，用于“反向查幽灵”
        unmatched_py_names = [s.get("name", "") for s in steampy_valid]

        # --- 🚀 3. 第一阶段：库存对账与账龄分析 (保持现有逻辑) ---
        active_items = []
        for s in steampy_valid:
            name = s.get("name", "")
            status = s.get("status", "")
            stock_str = s.get("stock", "1/1")
            
            try:
                curr_stk = int(re.findall(r'(\d+)\s*/', stock_str)[0])
            except: curr_stk = 1

            if "出售" in status and curr_stk > 0:
                try:
                    start_time = datetime.datetime.strptime(s.get("order_time"), "%Y-%m-%d %H:%M:%S")
                    days_on_shelf = (now - start_time).days
                except: days_on_shelf = 0
                
                active_items.append({
                    "name": name, "price": s.get("my_price"), "days": days_on_shelf
                })

        # --- 🚀 4. 第二阶段：双向穿透查漏 (解决全网公敌2误报) ---
        print(f"📡 [审计中] 正在执行双向穿透对账 (采购:{len(sonkwo_valid)} 笔 vs 销售:{len(steampy_valid)} 笔)...")
        missing_inventory = [] # 仓库遗珠 (买了没上)
        
        # 建立对账副本
        match_pool = Counter(py_sales_pool)

        # 排序：名字长的（通常是 DLC 或长名中文）先匹配，防止短名抢坑
        for p in sorted(sonkwo_valid, key=lambda x: len(x.get("name", "")), reverse=True):
            p_name = p.get("name", "")
            uid = p.get("uid", "Unknown")
            
            # 这里的 _is_same_game 内部已按照：手动缓存 > 硬核匹配 > AI 判定 排序
            matched_name = await self._is_same_game(p_name, list(match_pool.keys()))
            
            if matched_name and match_pool[matched_name] > 0:
                # ✅ 匹配成功，消耗销售池一个名额
                match_pool[matched_name] -= 1
                # 同时也从“反向名单”中划掉一个（只划掉一个实例）
                if matched_name in unmatched_py_names:
                    unmatched_py_names.remove(matched_name)
            else:
                # ❌ 采购单在销售端找不到，存入遗珠
                missing_inventory.append(f"{p_name} ({uid})")

        # --- 🚀 5. 第三阶段：资金总量统计 ---
        total_investment = sum(self._clean_price(item.get("cost", 0)) for item in sonkwo_valid)
        funds = {"cash_in_pocket": 0.0, "on_sale_value": 0.0}
        counts = {"sold": 0, "active": 0, "closed": 0, "blacklisted": len(self.blacklist_times)}

        for entry in steampy_valid:
            price = self._clean_price(entry.get("my_price", "0"))
            net_income = price * self.PAYOUT_RATE
            status = entry.get("status", "")
            
            try:
                current_stock = int(re.findall(r'(\d+)\s*/', entry.get("stock", "1/1"))[0])
            except: current_stock = 1

            if "出售" in status:
                if current_stock > 0:
                    funds["on_sale_value"] += net_income
                    counts["active"] += 1
                else:
                    funds["cash_in_pocket"] += net_income
                    counts["sold"] += 1
            elif "关闭" in status or "下架" in status:
                counts["closed"] += 1
            else:
                funds["cash_in_pocket"] += net_income
                counts["sold"] += 1

        current_profit, expected_profit = self._calculate_profit_shadow(
            sonkwo_valid, missing_inventory, 
            funds["cash_in_pocket"], funds["on_sale_value"], 
            total_investment, active_items
        )

        report = {
            "update_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_investment": round(total_investment, 2),
                "realized_cash": round(funds["cash_in_pocket"], 2),
                "floating_asset": round(funds["on_sale_value"], 2),
                "current_profit": current_profit,   
                "expected_profit": expected_profit,     
                "recovery_rate": round((funds["cash_in_pocket"] / total_investment * 100) if total_investment > 0 else 0, 2),
                "stats": counts
            },
            "details": {
                "on_shelf_aging": sorted(active_items, key=lambda x: x['days'], reverse=True),
                "missing_from_steampy": missing_inventory, # 买了没上
                "ghost_inventory": unmatched_py_names      # 上了没买 (幽灵资产)
            }
        }

        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=4)
        
        self._print_terminal_dashboard(report)
        return report

    def _print_terminal_dashboard(self, r):
        summary = r['summary']
        details = r['details']
        print(
            f"📊 [财务快照] 投入: {summary['total_investment']} | "
            f"回笼: {summary['realized_cash']} | "
            f"实利: {summary.get('current_profit', 'N/A')} | "
            f"预利: {summary.get('expected_profit', 'N/A')} | "
            f"进度: {summary['recovery_rate']}%"
        )
        print("\n" + "🚀 " * 15)
        print(f"   【母舰全息资产审计】 {r['update_at']}")
        print("-" * 55)
        
        # 1. 资金核心区
        print(f" 💰 采购总成本:    ¥ {summary['total_investment']:.2f}")
        print(f" ✅ 已收回现金:    ¥ {summary['realized_cash']:.2f}")
        print(f" ⏳ 货架在售资产:  ¥ {summary['floating_asset']:.2f}")
        
        rate = summary['recovery_rate']
        blocks = int(rate / 5)
        bar = "█" * blocks + "░" * (20 - blocks)
        print(f" 📊 回本进度: [{bar}] {rate:.1f}%")
        print("-" * 55)

        # 2. 🧊 货架账龄区 (展示卖得最慢的前3名)
        print(" 🕒 【货架账龄警报】 (最陈旧挂单)")
        if details['on_shelf_aging']:
            for item in details['on_shelf_aging'][:3]: # 仅列出最久的前3个
                # 根据天数显示不同情绪图标
                mood = "🔴" if item['days'] > 7 else "🟡" if item['days'] > 3 else "🟢"
                print(f"    {mood} {item['days']:>2}天 | {item['price']:<8} | {item['name']}")
        else:
            print("    ✅ 货架空空如也，请尽快补货")
        print("-" * 55)

        # 3. 🛡️ 库存漏损区 (仓库遗珠)
        missing = r['details']['missing_from_steampy']
        print(f" ⚠️ 【仓库遗珠检测】 (未上架: {len(missing)} 笔)")
        if missing:
            for name in missing[:10]:
                print(f"    ❓ 漏挂: {name}")
        else:
            print("    ✨ 完美对账：所有采购均已进入销售终端")
        print("-" * 55)

        # 4. 🚩 幽灵资产区 (上了但没买)
        ghosts = r['details']['ghost_inventory']
        print(f" 💀 【幽灵资产警告】 (来源不明: {len(ghosts)} 笔)")
        if ghosts:
            for name in ghosts:
                print(f"    🚩 未知资产: {name}")
        else:
            print("    ✅ 账目清爽：无未知来源挂单")

        print("-" * 55)

if __name__ == "__main__":
    import asyncio
    auditor = FinanceAuditor()
    asyncio.run(auditor.run_detailed_audit())