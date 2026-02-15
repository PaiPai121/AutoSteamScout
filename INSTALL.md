# 安装说明

## 1. 克隆项目

```bash
git clone <repository-url>
cd SteamCN-Arbitrage-Sentinel
```

## 2. 创建虚拟环境（推荐）

```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# 或
venv\Scripts\activate  # Windows
```

## 3. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

## 4. 配置环境变量

创建 `.env` 文件并添加以下内容：

```env
ZHIPU_API_KEY=your_zhipu_api_key_here
ZHIPU_MODEL=glm-4-flash
```

## 5. 设置浏览器会话

### 设置 Steam 会话
```bash
cd SteamPY-Scout
python save_session.py
```

### 设置 Sonkwo 会话
```bash
cd ../Sonkwo-Scout
python save_sonkwo_session.py
```

## 6. 运行项目

### 运行套利监控
```bash
cd ..
python arbitrage_commander.py
```

### 运行 Web 仪表板
```bash
python web_dashboard.py
```

### 运行特定游戏搜索
```bash
python arbitrage_commander.py "游戏名称"
```

## 7. 安装 Playwright 浏览器驱动

如果遇到浏览器相关错误，请运行：
```bash
playwright install
```

这将安装所有支持的浏览器驱动。