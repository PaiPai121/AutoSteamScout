# config.py
from pathlib import Path
import os

# 这行代码会自动获取 config.py 所在的文件夹路径
PROJECT_ROOT = Path(__file__).resolve().parent
# --- 资源与调试控制 ---
DEBUG_MODE = False           # 总开关
ENABLE_SCREENSHOTS = True   # 截图开关（建议默认关闭，爆内存元凶）
SINGLE_PROCESS_MODE = True    # 强制 Chromium 单进程（节省内存神器）

# --- 审计与熔断阈值 ---
AUDIT_CONFIG = {
    "MIN_SCORE": 75,             # 最低好评率要求 (百分比)
    "MIN_REVIEWS": 500,          # 自动巡航时的最低评论样本数
    "MIN_PROFIT": 0.5,           # 触发推送的最低利润门槛 (元)
    "ROI_THRESHOLD": 0.05,       # ROI 关注阈值 (5%)
}

# --- 爬虫与性能设置 ---
SCOUT_CONFIG = {
    "SLEEP_INTERVAL": 1.0,       # 巡航项之间的间隔 (秒)
    "MAX_HISTORY": 100,          # Web 历史记录保存上限
    "RETRY_COUNT": 3,            # AI 接口或网络请求重试次数
}

# --- 飞书通知 ---
NOTIFIER_CONFIG = {
    "WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/70423ec9-8744-40c2-a3af-c94bbbd0990a",
    "REPORT_TITLE": "🛰️ Arbitrage Sentinel 实时战报",
}

# --- 路径配置 ---
PATH_CONFIG = {
    "DB_NAME": "steamspy_all.json",
}