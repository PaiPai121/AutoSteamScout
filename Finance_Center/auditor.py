import json
import os
import re
import datetime

class FinanceAuditor:
    def __init__(self):
        self.sonkwo_file = "data/purchase_ledger.json"
        self.steampy_file = "data/steampy_sales.json"
        self.report_file = "data/finance_summary.json"
        self.PAYOUT_RATE = 0.95 

        # 🚫 异常订单黑名单 (基于 order_time 唯一标识)
        self.blacklist_times = [
            "2026-02-18 20:27:04", # 异形工厂2 异常关闭单
            "2026-02-18 17:57:04"  # 异形工厂2 异常关闭单
        ]

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

    def run_detailed_audit(self):
        sonkwo_data = self._load_json(self.sonkwo_file)
        steampy_data = self._load_json(self.steampy_file)
        now = datetime.datetime.now()
        
        # --- 第一部分：库存对账与账龄分析 ---
        active_items = []
        sold_names = []
        for s in steampy_data:
            name = s.get("name", "")
            status = s.get("status", "")
            stock_str = s.get("stock", "1/1")
            
            # 记录所有已触碰过的游戏名（用于查漏）
            sold_names.append(name.lower())

            # 提取真正“在架”的单子算账龄
            try:
                curr_stk = int(re.findall(r'(\d+)\s*/', stock_str)[0])
            except: curr_stk = 1

            if "出售" in status and curr_stk > 0:
                try:
                    start_time = datetime.datetime.strptime(s.get("order_time"), "%Y-%m-%d %H:%M:%S")
                    days_on_shelf = (now - start_time).days
                except: days_on_shelf = 0
                
                active_items.append({
                    "name": name,
                    "price": s.get("my_price"),
                    "days": days_on_shelf
                })

        # 查漏逻辑
        missing_inventory = []
        for p in sonkwo_data:
            p_name = p.get("name", "").lower()
            if not any(p_name in s_name or s_name in p_name for s_name in sold_names):
                missing_inventory.append(p.get("name"))

        # --- 第二部分：资金总量审计 (保持你刚才的稳健逻辑) ---
        total_investment = sum(self._clean_price(item.get("cost", 0)) for item in sonkwo_data)
        funds = {"cash_in_pocket": 0.0, "on_sale_value": 0.0}
        counts = {"sold": 0, "active": 0, "closed": 0, "blacklisted": 0}

        for entry in steampy_data:
            order_time = entry.get("order_time", "")
            if order_time in self.blacklist_times:
                counts["blacklisted"] += 1
                continue

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

        # --- 第三部分：构建最终报表 (包含明细) ---
        report = {
            "update_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {
                "total_investment": round(total_investment, 2),
                "realized_cash": round(funds["cash_in_pocket"], 2),
                "floating_asset": round(funds["on_sale_value"], 2),
                "recovery_rate": round((funds["cash_in_pocket"] / total_investment * 100) if total_investment > 0 else 0, 2),
                "stats": counts
            },
            # 🚀 这就是你要的“详细”：明细列表
            "details": {
                "on_shelf_aging": sorted(active_items, key=lambda x: x['days'], reverse=True),
                "missing_from_steampy": list(set(missing_inventory))
            }
        }

        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=4)
        
        self._print_terminal_dashboard(report)
        return report

    def _print_terminal_dashboard(self, r):
        summary = r['summary']
        details = r['details']
        
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

        # 3. 🛡️ 库存漏损区 (买了但没上架)
        missing = details['missing_from_steampy']
        print(f" ⚠️ 【仓库遗珠检测】 (未上架: {len(missing)} 笔)")
        if missing:
            # 仅列出前5个，防止刷屏
            for name in missing[:5]:
                print(f"    ❓ 未上架: {name}")
            if len(missing) > 5:
                print(f"    ... 等共 {len(missing)} 件资产尚未进入销售终端")
        else:
            print("    ✨ 完美对账：所有采购资产均已录入销售系统")

        print("-" * 55)
        print(f" 📦 统计: 已售({summary['stats']['sold']}) | 在售({summary['stats']['active']}) | 关闭({summary['stats']['closed']}) | 拦截({summary['stats']['blacklisted']})")
        print("🚀 " * 15 + "\n")
if __name__ == "__main__":
    FinanceAuditor().run_detailed_audit()