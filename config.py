# config.py
from pathlib import Path
import os

# 这行代码会自动获取 config.py 所在的文件夹路径
PROJECT_ROOT = Path(__file__).resolve().parent
# --- 资源与调试控制 ---
DEBUG_MODE = True           # 总开关
ENABLE_SCREENSHOTS = True   # 截图开关（建议默认关闭，爆内存元凶）
SINGLE_PROCESS_MODE = True    # 强制 Chromium 单进程（节省内存神器）

# --- 财务费率配置 ---
# SteamPY 实际到手费率（扣除 3% 手续费后为 0.97）
# 如果未来平台费率变动，只需修改此处
PAYOUT_RATE = 0.97

# 专门用于控制后台定时触发 get_audit_stats 的频率
RECON_INTERVAL = 3600 * 6

# --- 审计与熔断阈值 ---
AUDIT_CONFIG = {
    "MIN_SCORE": 75,             # 最低好评率要求 (百分比)
    "MIN_REVIEWS": 500,          # 自动巡航时的最低评论样本数
    "MIN_PROFIT": 0.5,           # 触发推送的最低利润门槛 (元)
    "ROI_THRESHOLD": 0.05,       # ROI 关注阈值 (5%)
}

# --- 🚀 自动上架配置 ---
AUTO_LISTER_CONFIG = {
    "UNDERCUT_AMOUNT": 0.05,     # 比市场最低价低多少元
    "MIN_PROFIT_MARGIN": 0.10,   # 最低利润要求 (元)
    "MIN_ROI": 0.05,             # 最低 ROI 要求 (5%)
}

# --- 爬虫与性能设置 ---
SCOUT_CONFIG = {
    "SLEEP_INTERVAL": 1.0,       # 巡航项之间的间隔 (秒)
    "MAX_HISTORY": 100,          # Web 历史记录保存上限
    "RETRY_COUNT": 3,            # AI 接口或网络请求重试次数
    # 💡 [新增]：巡航分类词任务清单
    "SEARCH_TASKS": [
        "", "steam", "action", "rpg", "strategy", 
        "adventure", "indie", "capcom", "bandai"
    ],
    # 💡 [新增]：单任务扫描深度 (页数)
    "MAX_PAGES_PER_TASK": 3,
    "BASE_CYCLE_TIME": 6000,      # 基础巡航周期 (秒)
    "JITTER_RANGE": 600,          # 随机抖动范围 (秒)，即基础值 ±600s
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

# --- Web 界面控制 ---
WEB_CONFIG = {
    "HOST": "0.0.0.0",
    "PORT": 8000,
    "REFRESH_INTERVAL": 5,        # 前端雷达刷新频率 (秒)
    "STATIC_DIR": "web/static",   # 静态资源目录
    "TEMPLATE_DIR": "web/templates", # 模板目录
}

# --- 🔐 API 安全认证 ---
# 方式 1：环境变量（推荐，更安全）
#   export SENTINEL_API_TOKEN="888888"
# 方式 2：直接写在这里（方便，但不要推送到 GitHub）
API_TOKEN = os.getenv("SENTINEL_API_TOKEN", "niya123")  # 默认密码 888888
# ⚠️ 警告：如果修改了密码，请不要推送到 GitHub！
# 建议：使用环境变量，或者在这里设置后把 config.py 加入 .gitignore

PATH_CONFIG = {
    "DB_NAME": "steamspy_all.json",
    "HISTORY_FILE": os.path.join(PROJECT_ROOT, "arbitrage_history.json"),
    "LOG_DIR": os.path.join(PROJECT_ROOT, "logs"),
}