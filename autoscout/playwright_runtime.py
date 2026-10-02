"""Start Playwright even when its installed Node driver is outside the readable workspace."""

from __future__ import annotations

from contextlib import asynccontextmanager
import importlib.metadata
import inspect
import logging
import os
from pathlib import Path
import shutil
import subprocess

import playwright
from playwright.async_api import async_playwright
from playwright._impl import _transport


log = logging.getLogger(__name__)
_prepared = False


def _driver_can_start(node: str, cli: str) -> bool:
    try:
        check = subprocess.run([node, "--check", cli], capture_output=True,
                               timeout=8, check=False)
        return check.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def prepare_playwright_driver(data_dir: Path) -> None:
    """Use the installed driver normally; copy it locally only if access is denied."""
    global _prepared
    if _prepared:
        return
    original_node, original_cli = _transport.compute_driver_executable()
    if _driver_can_start(original_node, original_cli):
        _prepared = True
        return
    node = os.getenv("PLAYWRIGHT_NODEJS_PATH") or shutil.which("node")
    version = importlib.metadata.version("playwright")
    source = Path(inspect.getfile(playwright)).parent / "driver" / "package"
    target = data_dir / "runtime" / f"playwright-driver-{version}"
    marker = target / ".copy-complete"
    if not marker.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, dirs_exist_ok=True)
        marker.write_text(version, encoding="utf-8")
    if not node:
        bundled_node = target.parent / Path(original_node).name
        try:
            if not bundled_node.is_file():
                shutil.copy2(original_node, bundled_node)
        except OSError as exc:
            raise RuntimeError(
                "Playwright 驱动无法启动，且无法复制随包提供的 Node.js；"
                "请检查安装目录权限或设置 PLAYWRIGHT_NODEJS_PATH"
            ) from exc
        node = str(bundled_node)
    cli = target / "cli.js"
    if not _driver_can_start(node, str(cli)):
        raise RuntimeError("Playwright 驱动在安装目录和本地副本均无法启动；请检查运行账号的文件访问权限")
    _transport.compute_driver_executable = lambda: (node, str(cli))
    log.warning("Playwright 安装目录不可访问，已使用数据目录内的驱动副本：%s", target)
    _prepared = True


@asynccontextmanager
async def playwright_session(data_dir: Path):
    prepare_playwright_driver(data_dir)
    instance = await async_playwright().start()
    try:
        yield instance
    finally:
        await instance.stop()
