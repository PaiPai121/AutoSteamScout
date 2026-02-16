# AutoSteamScout 安装指南

## 系统要求

- Python 3.8 或更高版本
- 支持的操作系统：
  - Linux (Ubuntu, CentOS, Fedora 等)
  - macOS
  - Windows 10/11

## 自动安装（推荐）

我们提供了自动化安装脚本，支持多种操作系统：

### Linux/macOS

1. 给安装脚本添加执行权限：
   ```bash
   chmod +x install_linux.sh
   ```

2. 运行安装脚本：
   ```bash
   ./install_linux.sh
   ```

### Windows

双击运行 `install_windows.bat` 文件。

## 手动安装

如果你更喜欢手动安装，请按照以下步骤操作：

### 1. 克隆项目

```bash
git clone https://github.com/PaiPai121/AutoSteamScout.git
cd AutoSteamScout
```

### 2. 创建虚拟环境

```bash
python3 -m venv venv
```

### 3. 激活虚拟环境

- Linux/macOS:
  ```bash
  source venv/bin/activate
  ```

- Windows:
  ```bash
  venv\Scripts\activate
  ```

### 4. 升级 pip

```bash
python -m pip install --upgrade pip
```

### 5. 安装依赖

```bash
pip install -r requirements.txt
```

### 6. 安装 Playwright 及其浏览器

```bash
playwright install chromium
```

### 7. 安装额外依赖（如果需要）

```bash
pip install asyncio aiofiles
```

## 配置环境变量

复制 `.env.example` 文件并重命名为 `.env`，然后填入相应的配置信息：

```bash
cp .env.example .env
```

编辑 `.env` 文件，填入你的配置信息。

## 验证安装

安装完成后，可以通过以下命令验证 Playwright 是否正确安装：

```bash
playwright --version
```

## 运行项目

激活虚拟环境后，即可运行项目中的 Python 脚本：

```bash
python your_script.py
```

## 故障排除

### 在 Linux 系统上缺少依赖

如果你在 Linux 系统上遇到问题，可能需要安装一些系统依赖：

#### Ubuntu/Debian:
```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-dev
```

#### CentOS/RHEL/Fedora:
```bash
sudo yum install python3 python3-pip python3-devel
# 或者对于较新版本
sudo dnf install python3 python3-pip python3-devel
```

### Playwright 依赖问题

如果 Playwright 安装后运行有问题，可以尝试安装系统依赖：

```bash
playwright install-deps chromium
```

## 更新项目

当项目更新时，可以使用以下命令更新依赖：

```bash
git pull origin main
source venv/bin/activate  # 或 venv\Scripts\activate
pip install -r requirements.txt --upgrade
```

## 卸载

如需卸载项目，只需删除项目目录和虚拟环境：

```bash
rm -rf venv/
rm -rf AutoSteamScout/
```

---

祝你使用愉快！如有问题，请查看项目文档或提交 issue。