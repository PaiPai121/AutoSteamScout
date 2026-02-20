import sys
import os

# 路径修复
root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_path not in sys.path:
    sys.path.append(root_path)

class AssetAuditor:
    def __init__(self, ai_handler=None):
        if ai_handler is None:
            from arbitrage_commander import ArbitrageAI
            self.ai = ArbitrageAI()
        else:
            self.ai = ai_handler

    async def audit(self, query_name, candidates):
        """
        [精密审计] 拿着 ITAD 的中文名，在 SteamSpy 的鱼群里选出唯一的真神
        """
        if not candidates:
            return "NONE", "未发现任何候选资产"

        # 格式化候选名单供 AI 参考
        # 我们把好评率和评论数也喂给 AI，它会自动识别哪个是“主版本”
        candidate_str = ""
        for i, c in enumerate(candidates):
            candidate_str += f"[{i}] ID: {c['appid']} | Name: {c['name']} | Stats: {c['info']}\n"

        prompt = f"""
        你现在是 Steam 资产核数师。你的首要准则是：【绝对准确，拒绝脑补】。
        
        待核实目标：'{query_name}'
        候选列表：
        {candidate_str}

        ⚠️ 审计过滤准则（严格执行）：
        1. **副标题冲突原则**：如果候选项的名字中包含明显的额外副标题（如 Pirate, Gaiden, Revelations, Spin-off, Expansion），而原始目标 '{query_name}' 中并没有对应的含义，必须剔除。
        2. **数字刚性原则**：如果目标含有数字（如 7），优先寻找含有该数字或对应罗马数字（VII）的项。如果列表中某项完全没有数字，只有在其评价数（Reviews）远超其他项且名字核心词高度一致时，才考虑它作为“无数字副标题”的正传。
        3. **排除非完整版**：坚决排除 Upgrade, DLC, Soundtrack, Pack, Bundle。
        4. **疑罪从无**：如果在多个项之间存在明显歧义（例如无法确定哪个是正传），或者没有一个项能 90% 匹配语义，必须输出 ID: NONE。

        输出格式：
        ID: [AppID 或 NONE] | Reason: [简述你如何根据“副标题”或“数字”逻辑排除干扰项的]
        """
        try:
            response = self.ai._call_with_retry(prompt)
            if "ID:" in response:
                # 提取 ID 和 理由
                parts = response.split("|")
                final_id = parts[0].replace("ID:", "").strip()
                reason = parts[1].replace("Reason:", "").strip() if len(parts) > 1 else "语义锁定"
                return final_id, reason
            return "NONE", "AI 未能锁定唯一资产"
        except Exception as e:
            return "NONE", f"审计过程异常: {str(e)}"

# ==========================================
# 🚀 集成测试：Matcher + Auditor 联动
# ==========================================
if __name__ == "__main__":
    from game_rating.LocalGameMatcher import SpyGameMatcher
    import asyncio

    async def run_full_test():
        matcher = SpyGameMatcher()
        auditor = AssetAuditor(ai_handler=matcher.ai) # 复用 AI
        
        if not matcher.initialize(): return

        target = "人中之龙7"
        print(f"\n⚡ 正在对 [{target}] 进行全链路识别...")
        
        # 1. 捞鱼
        fish = await matcher.fetch_candidates(target)
        # 2. 判决
        final_id, reason = await auditor.audit(target, fish)
        
        print(f"🎯 最终锁定 AppID: {final_id}")
        print(f"📖 判定理由: {reason}")

    asyncio.run(run_full_test())