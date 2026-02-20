import asyncio
import os
import sys

# 确保能找到上级目录的模块
current_dir = os.path.dirname(os.path.abspath(__file__))
root_path = os.path.dirname(current_dir)
if root_path not in sys.path:
    sys.path.append(root_path)

try:
    from .LocalGameMatcher import SpyGameMatcher
    from .AssetAuditor import AssetAuditor
except ImportError:
    from LocalGameMatcher import SpyGameMatcher
    from AssetAuditor import AssetAuditor

class GameRatingManager:
    def __init__(self, ai_handler=None):
        # 1. 初始化内部组件（AI 实例会自动在 Matcher 内部按需创建）
        self.matcher = SpyGameMatcher(ai_handler=ai_handler)
        self.auditor = AssetAuditor(ai_handler=self.matcher.ai)
        self.is_ready = False

    def initialize(self):
        """一次性加载 6 万条索引，主程序启动时调用一次即可"""
        if self.matcher.initialize():
            self.is_ready = True
            return True
        return False

    async def get_rating_and_id(self, chinese_name):
        """
        [总控函数] 输入中文名，直接输出 AppID 和 评价明细
        """
        if not self.is_ready:
            return None, "引擎未初始化", "ERROR"

        # Step 1: 捞鱼 (广撒网)
        candidates = await self.matcher.fetch_candidates(chinese_name)
        if not candidates:
            return None, "未找到候选资产", "MISSING"

        # Step 2: 审计 (精判别)
        final_id, reason = await self.auditor.audit(chinese_name, candidates)

        if final_id == "NONE" or not final_id:
            return None, f"识别弃权: {reason}", "UNCERTAIN"

        # Step 3: 数据提纯
        target_info = next((c for c in candidates if str(c['appid']) == str(final_id)), None)
        
        if target_info:
            return final_id, target_info, "SUCCESS"
        
        # 🛡️ 修正：如果 ID 不在候选列表里，说明 AI 抄错了或识别失败，返回 UNCERTAIN
        return None, f"审计锁定 ID ({final_id}) 在候选库中不存在", "UNCERTAIN"

# ==========================================
# 🚀 模块化测试入口
# ==========================================
if __name__ == "__main__":
    async def test_suite():
        print("🛠️ 正在启动 GameRatingCenter 总控测试...")
        
        # 1. 初始化
        manager = GameRatingManager()
        if not manager.initialize():
            print("❌ 初始化失败，请检查数据文件。")
            return

        # 2. 准备各种“奇葩”和“正经”的测试用例
        test_cases = [
            "人中之龙7",          # 无数字匹配挑战
            "生化危机4 重制版",    # 版本干扰挑战
            "绝地潜兵 2",         # 翻译挑战
            "使命召唤：现代战争",   # 极度混淆挑战
            "一个根本不存在的游戏"   # 弃权机制测试
        ]

        print("\n" + "═"*60)
        print(f"{'测试目标':<15} | {'状态':<10} | {'AppID':<10} | {'评价摘要'}")
        print("─"*60)

        for name in test_cases:
            appid, data, status = await manager.get_rating_and_id(name)
            
            if status == "SUCCESS":
                # 提取评分和评论数
                rating_detail = data.get('info', '无数据')
                print(f"{name:<18} | ✅ 成功    | {appid:<10} | {rating_detail}")
            elif status == "UNCERTAIN":
                print(f"{name:<18} | ⚠️ 弃权    | {'-':<10} | {data}")
            else:
                print(f"{name:<18} | ❌ 失败    | {'-':<10} | {data}")

        print("═"*60)

    asyncio.run(test_suite())