# SteamCN 哨兵

SteamCN 哨兵是一个高级自动化检测系统，旨在识别 Steam 和 Sonkwo（杉果）游戏平台之间的盈利机会。该工具利用浏览器自动化监控价格差异，并实时识别潜在的机会。

## 🚀 快速开始

### 1️⃣ 复制配置文件

```bash
cp config.example.py config.py
cp .env.example .env
```

### 2️⃣ 设置访问密码

编辑 `config.py` 第 68 行：
```python
API_TOKEN = os.getenv("SENTINEL_API_TOKEN", "888888")
#                                   ↑ 改成你想要的密码
```

### 3️⃣ 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4️⃣ 启动服务

```bash
python web_dashboard.py
```

### 5️⃣ 手机/浏览器访问

```
http://你的服务器 IP:8000/audit
```

首次访问需要输入密码（默认：`888888`），之后自动登录。

---

## 概述

系统包含两个主要模块：
- **SteamPY-Scout**: 监控 Steam 市场价格和库存
- **Sonkwo-Scout**: 监控 Sonkwo（杉果）游戏平台价格

两个模块都使用 Playwright 进行浏览器自动化，并维护持久会话以保持登录到相应平台。

## 功能特性

### SteamPY-Scout
- 自动导航到 Steam CDKey 市场
- 游戏搜索和详情页导航
- 价格扫描和比较
- 库存监控
- 持久会话管理
- 实时状态监控

### Sonkwo-Scout
- 自动导航到杉果游戏平台
- 带史低（历史最低价）过滤的 Steam 密钥搜索
- 包含优惠券信息的详细价格分析
- 跨区域风险评估
- 订单确认处理
- 持久会话管理

## 架构

```
SteamCN 哨兵/
├── SteamPY-Scout/
│   ├── steampy_scout_core.py    # 核心 Steam 浏览器自动化
│   ├── steampy_hunter.py        # 高级 Steam 监控功能
│   ├── save_session.py          # Steam 会话保存工具
│   └── steampy_data/            # Steam 持久会话数据
└── Sonkwo-Scout/
    ├── sonkwo_scout_core.py     # 核心杉果浏览器自动化
    ├── sonkwo_hunter.py         # 高级杉果监控功能
    ├── save_sonkwo_session.py   # 杉果会话保存工具
    └── sonkwo_data/             # 杉果持久会话数据
```

## 先决条件

- Python 3.7+
- Playwright
- Tabulate

安装依赖：
```bash
pip install playwright tabulate
playwright install chromium
```

## 设置

### 1. Steam 账户设置
```bash
cd SteamPY-Scout
python save_session.py
```
按照提示在打开的浏览器窗口中登录您的 Steam 账户。

### 2. 杉果账户设置
```bash
cd ../Sonkwo-Scout
python save_sonkwo_session.py
```
按照提示在打开的浏览器窗口中登录您的杉果账户。

## 使用方法

### Steam 监控
```bash
cd SteamPY-Scout
python steampy_hunter.py
```

交互界面中的可用命令：
- `search [游戏名称]` - 搜索游戏并导航到详情页
- `scan [游戏名称]` - 搜索游戏并扫描其价格详情
- `scan` - 扫描当前页面的价格详情
- `goto` - 导航到 CDKey 市场
- `exit` - 退出程序

### 杉果监控
```bash
cd Sonkwo-Scout
python sonkwo_hunter.py
```

交互界面中的可用命令：
- `search [游戏名称]` - 搜索带有 Steam 密钥和历史最低价的游戏
- `scan` 或 `s` - 扫描当前页面（详情或确认页）
- `buy` 或 `submit` - 处理购买（详情 → 确认或提交订单）
- `[数字]` - 按索引导航到特定搜索结果
- `exit` - 退出程序

## 关键功能详解

### 检测
系统识别以下情况的机会：
- 游戏在杉果上的价格低于 Steam 上的当前市场价格
- 历史最低价指标帮助识别最佳交易
- 优惠券和折扣信息被纳入盈利能力计算

### 风险评估
- 跨区域兼容性检查
- 订单验证和确认
- 自动优惠券应用检测

### 持久会话
两个模块都维护浏览器会话以避免重复登录，将会话数据存储在专用目录中。

## 技术细节

系统使用 Playwright 的持久上下文在运行之间维护浏览器状态。这允许工具：
- 保持登录到平台
- 维护 cookies 和本地存储
- 准确模拟真实用户行为

每个模块都实现了跟踪以下内容的状态监控系统：
- 当前页面类型
- 登录状态
- 导航状态
- 区域特定设置

## 限制和注意事项

- 两个平台都可能实施反机器人措施，影响可靠性
- 价格动态变化，因此机会可能是短暂的
- 某些游戏可能适用区域限制
- 互联网连接稳定性影响性能
- 随着网站更新其界面，可能需要定期维护

## 贡献

欢迎贡献！请随时提交拉取请求。对于重大更改，请先开 issue 讨论您想要更改的内容。

## 许可证

该项目根据 MIT 许可证授权 - 详见 LICENSE 文件。