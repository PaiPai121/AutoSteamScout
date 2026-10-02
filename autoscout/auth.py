"""One-time headless account bootstrap; scheduled scans reuse the saved sessions."""

from __future__ import annotations

import asyncio
from getpass import getpass
import os
import queue
import re
import sys
import threading
import time
from typing import Awaitable, Callable
from urllib.parse import urlparse

from .browser import SONKWO_ORIGIN, STEAMPY_HOME, browser_launch_options
from .auth_monitor import AuthMonitor
from .playwright_runtime import playwright_session
from .ports import SourceError
from .settings import Settings
from .session_restore import (ensure_sonkwo_session, ensure_steampy_session,
                              save_sonkwo_session, sonkwo_session_valid)


LoginInputReader = Callable[[str, float, asyncio.Event], Awaitable[str]]


async def _secret_windows(prompt: str, timeout: float,
                          cancel: asyncio.Event | None, show_input: bool = False) -> str:
    """Read Windows console keys without a blocking reader thread."""
    import msvcrt

    print(prompt, end="", flush=True)
    chars: list[str] = []
    started = time.monotonic()
    next_report = 30
    while True:
        if cancel is not None and cancel.is_set():
            print(flush=True)
            raise SourceError("登录已取消")
        elapsed = int(time.monotonic() - started)
        if elapsed >= timeout:
            print(flush=True)
            raise SourceError("登录输入等待超时；请重新运行登录命令")
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in {"\r", "\n"}:
                print(flush=True)
                return "".join(chars).strip()
            if key == "\x03":
                print(flush=True)
                raise SourceError("登录输入已由 Ctrl+C 中断")
            if key == "\b":
                if chars:
                    chars.pop()
                    if show_input:
                        print("\b \b", end="", flush=True)
            elif key in {"\x00", "\xe0"}:
                msvcrt.getwch()
            elif key.isprintable():
                chars.append(key)
                if show_input:
                    print(key, end="", flush=True)
        if elapsed >= next_report:
            print(f"\n仍在等待终端输入，已过 {elapsed} 秒。", flush=True)
            next_report += 30
        await asyncio.sleep(0.1)


async def _secret(prompt: str, timeout: float,
                  cancel: asyncio.Event | None = None, show_input: bool = False) -> str:
    """Read terminal input with a deadline; optionally echo it for local debugging."""
    if not sys.stdin.isatty():
        raise SourceError("登录需要交互式终端提供手机号和短信验证码；扫描服务本身可无人值守运行")
    if os.name == "nt":
        return await _secret_windows(prompt, timeout, cancel, show_input)
    result: queue.Queue[object] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(input(prompt) if show_input else getpass(prompt))
        except (EOFError, KeyboardInterrupt, OSError) as exc:
            result.put(exc)

    threading.Thread(target=read, daemon=True).start()
    started = time.monotonic()
    next_report = 30
    while True:
        if cancel is not None and cancel.is_set():
            raise SourceError("登录已取消")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise SourceError("登录输入等待超时；请重新运行登录命令")
        try:
            value = result.get_nowait()
            break
        except queue.Empty:
            elapsed = int(time.monotonic() - started)
            if elapsed >= next_report:
                print(f"仍在等待终端输入，已过 {elapsed} 秒。", flush=True)
                next_report += 30
            await asyncio.sleep(min(0.5, remaining))
    if isinstance(value, BaseException):
        raise SourceError("登录输入已中断") from value
    return str(value).strip()


async def _authenticated(page, platform: str) -> bool:
    if platform == "sonkwo":
        return await sonkwo_session_valid(page.context, 5000)
    return ("/login" not in page.url
            and await page.locator("li:has-text('退出登录'), "
                                   ".ivu-menu-submenu:has-text('卖家中心')").count() > 0)


async def _wait_authenticated(page, platform: str, seconds: float = 30,
                              cancel: asyncio.Event | None = None) -> bool:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if cancel is not None and cancel.is_set():
            raise SourceError("登录已取消")
        if await _authenticated(page, platform):
            return True
        await asyncio.sleep(1)
    return False


async def _challenge_visible(page) -> bool:
    rendered = """element => {
        let node = element;
        let visible = element.getBoundingClientRect();
        while (node && node.nodeType === 1) {
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility !== 'visible'
                    || Number(style.opacity) < 0.05) return false;
            if (node !== element && /hidden|clip|scroll|auto/.test(
                    style.overflow + style.overflowX + style.overflowY)) {
                const clip = node.getBoundingClientRect();
                visible = {
                    left: Math.max(visible.left, clip.left),
                    top: Math.max(visible.top, clip.top),
                    right: Math.min(visible.right, clip.right),
                    bottom: Math.min(visible.bottom, clip.bottom)
                };
            }
            node = node.parentElement;
        }
        const left = Math.max(0, visible.left);
        const top = Math.max(0, visible.top);
        const right = Math.min(innerWidth, visible.right);
        const bottom = Math.min(innerHeight, visible.bottom);
        if (right <= left || bottom <= top) return false;
        const front = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
        return front === element || element.contains(front);
    }"""
    hints = ("拖动下方滑块完成拼图", "向右滑动完成验证", "请按住滑块")
    for frame in page.frames:
        for hint in hints:
            try:
                locator = frame.get_by_text(hint).first
                if not await locator.is_visible() or not await locator.evaluate(rendered):
                    continue
                ancestor = frame
                while ancestor.parent_frame is not None:
                    element = await ancestor.frame_element()
                    if not await element.evaluate(rendered):
                        break
                    ancestor = ancestor.parent_frame
                else:
                    return True
            except Exception:
                continue
    return False


async def _sonkwo_sms_timer_visible(page) -> bool:
    return await page.get_by_text(re.compile(r"\d+\s*秒后重发")).is_visible()


async def _sonkwo_sms_confirmed(page) -> bool:
    """A 200 response alone does not prove that Sonkwo started its SMS timer."""
    for _ in range(16):
        if await _sonkwo_sms_timer_visible(page):
            return True
        await asyncio.sleep(0.5)
    return False


async def _sms_login(page, platform: str, timeout_ms: int, monitor: AuthMonitor,
                     request_sms: bool = True, show_input: bool = False,
                     input_reader: LoginInputReader | None = None) -> None:
    monitor.stage("opening_login", "打开平台登录页面")
    if platform == "sonkwo":
        await page.goto(f"{SONKWO_ORIGIN}/sign_in", wait_until="domcontentloaded", timeout=timeout_ms)
        await page.locator(".login-tab-button").nth(1).click(timeout=timeout_ms)
        phone_input = page.locator("#phone_number")
        code_input = page.locator("#pending_phone_number_token")
        submit = page.locator("button.new_orange")
    else:
        await page.goto("https://steampy.com/login", wait_until="domcontentloaded", timeout=timeout_ms)
        await page.locator(".ivu-tabs-tab").nth(1).click(timeout=timeout_ms)
        phone_input = page.locator("input[placeholder='请输入手机号']")
        code_input = page.locator("input[placeholder='请输入短信验证码']")
        for label in ("自动登录", "我已阅读"):
            checkbox = page.locator("label").filter(has_text=label).locator("input[type=checkbox]").first
            await checkbox.check(force=True, timeout=timeout_ms)
        submit = page.locator("button.login-btn")

    monitor.stage("awaiting_phone", "请在本页输入手机号" if input_reader else "等待在终端输入手机号")
    phone = (await input_reader("phone", 600, monitor.cancel_requested)
             if input_reader else await _secret(
                 f"{platform} 手机号（仅在本机输入，不写入日志）：", 120,
                 monitor.cancel_requested, show_input))
    if not re.fullmatch(r"1\d{10}", phone):
        raise SourceError("手机号格式应为中国大陆 11 位号码；未发送验证码")
    await phone_input.fill(phone, timeout=timeout_ms)
    if await phone_input.input_value(timeout=timeout_ms) != phone:
        raise SourceError("手机号未写入登录页面；未请求短信验证码")
    phone = ""
    monitor.phone_filled()
    print(f"{platform}：手机号已写入登录页面。", flush=True)
    if request_sms:
        monitor.stage("requesting_sms", "点击获取验证码；等待平台页面反馈")
        print(f"{platform}：正在请求短信验证码…", flush=True)
        responses: list[str] = []

        def observe(response) -> None:
            parsed = urlparse(response.url)
            host = parsed.hostname or ""
            if (response.request.method != "GET"
                    and host.endswith(("sonkwo.cn", "steampy.com"))
                    and len(responses) < 5):
                safe_path = re.sub(r"\d{6,}", "[redacted]", parsed.path)
                responses.append(f"{response.request.method} {safe_path} HTTP {response.status}")

        page.on("response", observe)
        sms_timer_seen = False
        try:
            await page.get_by_role("button", name="获取验证码").click(timeout=timeout_ms)
            await asyncio.sleep(1.5)
            if platform == "sonkwo":
                sms_timer_seen = await _sonkwo_sms_timer_visible(page)
            if await _challenge_visible(page):
                monitor.stage("awaiting_challenge", "平台要求拼图验证；按住画面中拼图弹窗底部左侧的白色滑块向右拖")
                print(f"{platform}：出现拼图验证。在 /auth 预览画面中按住弹窗底部左侧带 » 的白色滑块，向右拖到缺口后松开。", flush=True)
                print(f"{platform}：程序会自动检测拼图消失；若检测卡住，可在预览页使用备用继续按钮。", flush=True)
                print(f"{platform}：要结束本次登录，可点预览页的“取消登录”。", flush=True)
                started_challenge = time.monotonic()
                next_report = 30
                hidden_checks = 0
                while not monitor.continue_requested.is_set():
                    if monitor.cancel_requested.is_set():
                        raise SourceError("登录已取消")
                    if platform == "sonkwo" and not sms_timer_seen:
                        sms_timer_seen = await _sonkwo_sms_timer_visible(page)
                    hidden_checks = 0 if await _challenge_visible(page) else hidden_checks + 1
                    if hidden_checks >= 3:
                        monitor.stage("challenge_confirmed", "已自动识别拼图画面消失，正在确认验证码请求")
                        print(f"{platform}：已自动识别拼图画面消失。", flush=True)
                        break
                    elapsed = int(time.monotonic() - started_challenge)
                    if elapsed >= 300:
                        raise SourceError("拼图验证等待超时；尚不能确认短信已发送")
                    if elapsed >= next_report:
                        print(f"{platform}：仍在等待手动拼图验证，已过 {elapsed} 秒。", flush=True)
                        next_report += 30
                    await asyncio.sleep(0.5)
                await asyncio.sleep(2)
        finally:
            page.remove_listener("response", observe)
        receipt = "；".join(responses) if responses else "未观察到平台 POST 回应"
        if platform == "sonkwo":
            if not (sms_timer_seen or await _sonkwo_sms_confirmed(page)):
                raise SourceError(
                    f"杉果没有显示短信重发倒计时，无法确认验证码已发送（{receipt}）；"
                    "请查看登录预览中的平台提示；若短信随后到达，可在 /auth 勾选复用验证码重新登录"
                )
            confirmation = "网页已出现短信重发倒计时"
        else:
            confirmation = "网页是否发码尚需核对"
        monitor.stage("awaiting_code", f"{confirmation}；网页请求：{receipt}。请在本页输入短信验证码")
        print(f"{platform}：{confirmation}；{receipt}。", flush=True)
    else:
        print(f"{platform}：尝试使用已有验证码，不重复发送短信。", flush=True)
        monitor.stage("awaiting_code", "尝试使用已有验证码，未重复发送短信")
    code = (await input_reader("code", 300, monitor.cancel_requested)
            if input_reader else await _secret(
                f"{platform} 短信验证码（若已过期，直接回车终止）：", 300,
                monitor.cancel_requested, show_input))
    if not re.fullmatch(r"\d{4,8}", code):
        raise SourceError("未提供有效短信验证码；可能出现平台验证挑战，登录未完成")
    await code_input.fill(code, timeout=timeout_ms)
    code = ""
    monitor.stage("submitting_code", "正在提交验证码并确认登录")
    await submit.click(timeout=timeout_ms)
    if not await _wait_authenticated(page, platform, cancel=monitor.cancel_requested):
        message = await page.locator("body").inner_text(timeout=timeout_ms)
        if "验证码已过期" in message:
            raise SourceError(f"{platform} 验证码已过期，需要重新发送")
        if "无效的短信验证码" in message or "验证码错误" in message:
            raise SourceError(f"{platform} 验证码未通过平台校验")
        raise SourceError(f"{platform} 登录后未确认会话；请检查验证码或平台验证挑战")


async def login(platform: str, settings: Settings, request_sms: bool = True,
                input_reader: LoginInputReader | None = None) -> None:
    """Create or refresh a persisted session without any visible browser window."""
    if platform not in {"sonkwo", "steampy"}:
        raise ValueError("未知平台")
    profile = settings.profile_path(platform)
    profile.parent.mkdir(parents=True, exist_ok=True)
    url = f"{SONKWO_ORIGIN}/store/search" if platform == "sonkwo" else STEAMPY_HOME
    print(f"{platform}：检查现有会话…", flush=True)
    started = time.monotonic()
    monitor = AuthMonitor(settings.data_dir, platform, show_input=settings.show_login_input)
    async with playwright_session(settings.data_dir) as playwright:
        launch_options = browser_launch_options(playwright, settings, True)
        context = await playwright.chromium.launch_persistent_context(str(profile), **launch_options)
        success = False
        final_message = "登录未完成"
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await monitor.start(page)
            if platform == "steampy":
                await ensure_steampy_session(context, page, profile,
                                             int(settings.operation_timeout * 1000))
            else:
                if await ensure_sonkwo_session(context, profile, int(settings.operation_timeout * 1000),
                                               lambda message: monitor.stage("checking_session", message)):
                    success, final_message = True, "现有会话有效，已核对账号接口"
                    print("sonkwo：现有会话有效，无需短信验证。", flush=True)
                    return
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=int(settings.operation_timeout * 1000))
            if await _wait_authenticated(page, platform, 10, monitor.cancel_requested):
                success, final_message = True, "现有会话有效"
                print(f"{platform}：现有会话有效，无需短信验证。", flush=True)
                return
            print(f"{platform}：会话已失效，开始无头短信登录。", flush=True)
            try:
                await _sms_login(page, platform, int(settings.operation_timeout * 1000), monitor,
                                 request_sms=request_sms, show_input=settings.show_login_input,
                                 input_reader=input_reader)
            except Exception as exc:
                final_message = str(exc)[:200] if isinstance(exc, SourceError) else "平台页面操作失败"
                if not monitor.cancel_requested.is_set():
                    diagnostic = await monitor.capture_diagnostic()
                    if diagnostic is not None:
                        print(f"{platform}：诊断截图保存在 {diagnostic}，请勿外传。", flush=True)
                raise
            if platform == "sonkwo":
                await save_sonkwo_session(context, profile)
            else:
                await context.storage_state(path=str(profile / "storage_state.json"))
            success, final_message = True, "登录已确认，会话已保存"
            print(f"{platform}：登录已确认，会话已保存；耗时 {int(time.monotonic() - started)} 秒。", flush=True)
        finally:
            await monitor.stop(success, final_message)
            await context.close()
