import asyncio
import json
import os
import datetime

class SyncManager:
    def __init__(self, commander):
        self.commander = commander
        self.sonkwo = commander.finance
        self.steampy = commander.steampy_center

    async def run_full_sync(self):
        """🚀 执行全量同步任务"""
        print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 🔄 启动跨平台一键同步...")
        
        # 建立一个专用的工作页面，不干扰主页面的逻辑
        # 我们使用 commander 的 context，因为它持有所有登录 Cookie
        sync_page = await self.commander.sonkwo.context.new_page()
        
        try:
            # --- 阶段 A: 杉果订单同步 ---
            print("📍 [1/2] 正在提取杉果采购成本...")
            await self.sonkwo.action_fetch_ledger(sync_page)
            
            await asyncio.sleep(3) # 缓冲间隔

            # --- 阶段 B: SteamPY 挂单同步 ---
            print("📍 [2/2] 正在扫描 SteamPY 货架状态...")
            await self.steampy.action_fetch_seller_ledger(sync_page)

            print("✨ 同步任务圆满完成！数据已更新。")
            return {"status": "success", "msg": "同步完成"}
        
        except Exception as e:
            print(f"❌ 同步失败: {e}")
            return {"status": "error", "msg": str(e)}
        finally:
            if not sync_page.is_closed():
                await sync_page.close()

    def get_summary_report(self):
        """📊 生成汇总对账数据 (用于前端展示)"""
        # 读取两份 JSON 并根据游戏名匹配计算利润
        # 这一部分可以在后续专门写对账逻辑时细化
        pass