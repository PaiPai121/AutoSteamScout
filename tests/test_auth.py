import pytest
import asyncio
import json
import sqlite3

from autoscout import auth
from autoscout.auth_monitor import AuthMonitor, request_login_cancel
from autoscout.ports import SourceError


@pytest.mark.asyncio
async def test_headless_login_requires_a_private_terminal(monkeypatch):
    monkeypatch.setattr(auth.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SourceError, match="交互式终端"):
        await auth._secret("phone", 1)


@pytest.mark.asyncio
async def test_windows_terminal_wait_can_be_cancelled(monkeypatch):
    import msvcrt

    monkeypatch.setattr(msvcrt, "kbhit", lambda: False)
    cancel = asyncio.Event()
    task = asyncio.create_task(auth._secret_windows("private: ", 5, cancel))
    await asyncio.sleep(0.15)
    cancel.set()
    with pytest.raises(SourceError, match="登录已取消"):
        await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_windows_debug_input_echoes_characters_and_backspace(monkeypatch, capsys):
    import msvcrt

    keys = iter(["1", "2", "\b", "3", "\r"])
    monkeypatch.setattr(msvcrt, "kbhit", lambda: True)
    monkeypatch.setattr(msvcrt, "getwch", lambda: next(keys))
    value = await auth._secret_windows("number: ", 5, None, show_input=True)
    assert value == "13"
    assert "number: 12\b \b3" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_monitor_confirms_phone_fill_and_processes_cancel(tmp_path):
    class FakePage:
        viewport_size = {"width": 1280, "height": 720}

        def locator(self, selector):
            return selector

        async def screenshot(self, path, **kwargs):
            from pathlib import Path
            Path(path).write_bytes(b"\x89PNG-test")

    monitor = AuthMonitor(tmp_path, "sonkwo")
    await monitor.start(FakePage())
    monitor.phone_filled()
    with sqlite3.connect(monitor.control_path) as connection:
        connection.execute("INSERT INTO events (session, action, x, y) VALUES (?, 'continue', 0, 0)",
                           (monitor._session,))
    await asyncio.wait_for(monitor.continue_requested.wait(), 2)
    assert json.loads(monitor.status_path.read_text(encoding="utf-8"))["stage"] == "challenge_confirmed"
    assert request_login_cancel(tmp_path, "sonkwo") is True
    await asyncio.wait_for(monitor.cancel_requested.wait(), 2)
    state = json.loads(monitor.status_path.read_text(encoding="utf-8"))
    assert state["phone_entered"] is True
    assert state["stage"] == "cancelling"
    await monitor.stop(False, "登录已取消")


@pytest.mark.asyncio
async def test_debug_frame_shows_input_only_while_login_is_active(tmp_path):
    class FakePage:
        viewport_size = {"width": 1280, "height": 720}

        def __init__(self):
            self.shots = []

        def locator(self, selector):
            return selector

        async def screenshot(self, path, **kwargs):
            from pathlib import Path
            self.shots.append(kwargs)
            Path(path).write_bytes(b"\x89PNG-test")

    page = FakePage()
    monitor = AuthMonitor(tmp_path, "sonkwo", show_input=True)
    await monitor.start(page)
    monitor.stage("awaiting_phone")
    for _ in range(20):
        if monitor.frame_path.exists():
            break
        await asyncio.sleep(0.1)
    assert monitor.frame_path.exists()
    assert any("mask" not in shot for shot in page.shots)
    await monitor.stop(False, "测试结束")
    assert "mask" in page.shots[-1]
    assert monitor.frame_path.exists()


@pytest.mark.asyncio
async def test_hidden_captcha_iframe_does_not_keep_login_waiting():
    class Element:
        async def evaluate(self, _script):
            return False

    class Locator:
        first = None

        def __init__(self):
            self.first = self

        async def is_visible(self):
            return True

        async def evaluate(self, _script):
            return True

    class Frame:
        def __init__(self, parent_frame=None):
            self.parent_frame = parent_frame

        def get_by_text(self, _text):
            return Locator()

        async def frame_element(self):
            return Element()

    main = Frame()
    child = Frame(main)
    page = type("Page", (), {"frames": [child], "main_frame": main})()
    assert await auth._challenge_visible(page) is False


@pytest.mark.asyncio
async def test_covered_captcha_is_not_treated_as_visible(tmp_path):
    from autoscout.playwright_runtime import playwright_session

    async with playwright_session(tmp_path) as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"Chrome not available: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content("""
                <style>body{margin:0}#challenge{position:absolute;top:100px;left:100px}
                #cover{display:none;position:absolute;inset:0;background:white;z-index:2}</style>
                <div id="challenge"><iframe width="300" height="200"
                  srcdoc="<p>拖动下方滑块完成拼图</p>"></iframe></div>
                <div id="cover"></div>
            """)
            await page.frame_locator("iframe").get_by_text("拖动下方滑块完成拼图").wait_for()
            assert await auth._challenge_visible(page) is True
            await page.locator("#cover").evaluate("element => element.style.display = 'block'")
            assert await auth._challenge_visible(page) is False
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("sms_confirmed", [False, True])
@pytest.mark.parametrize("web_input", [False, True])
async def test_challenge_completion_reaches_code_prompt(monkeypatch, manual, sms_confirmed,
                                                        web_input):
    class Locator:
        value = ""

        def nth(self, _index):
            return self

        async def click(self, **_kwargs):
            pass

        async def fill(self, value, **_kwargs):
            self.value = value

        async def input_value(self, **_kwargs):
            return self.value

    class Page:
        def __init__(self):
            self.inputs = {}

        async def goto(self, *_args, **_kwargs):
            pass

        def locator(self, selector):
            return self.inputs.setdefault(selector, Locator())

        def get_by_role(self, *_args, **_kwargs):
            return Locator()

        def on(self, *_args):
            pass

        def remove_listener(self, *_args):
            pass

    class Monitor:
        cancel_requested = asyncio.Event()
        continue_requested = asyncio.Event()

        def __init__(self):
            self.stages = []
            self.phone_confirmed = False

        def stage(self, stage, _message=""):
            self.stages.append(stage)

        def phone_filled(self):
            self.phone_confirmed = True

    monitor = Monitor()
    if manual:
        monitor.continue_requested.set()
    prompts = []

    async def secret(prompt, _timeout, _cancel, _show_input):
        prompts.append(prompt)
        return "12345678901" if len(prompts) == 1 else "123456"

    async def web_reader(kind, _timeout, _cancel):
        prompts.append(kind)
        return "12345678901" if kind == "phone" else "123456"

    checks = iter([True, True, False, False, False])

    async def visible(_page):
        return next(checks) if not manual else True

    async def authenticated(*_args, **_kwargs):
        return True

    async def countdown(_page):
        return sms_confirmed

    async def countdown_expired(_page):
        return False

    original_sleep = asyncio.sleep

    async def fast_sleep(_duration):
        await original_sleep(0)

    monkeypatch.setattr(auth, "_secret", secret)
    monkeypatch.setattr(auth, "_challenge_visible", visible)
    monkeypatch.setattr(auth, "_sonkwo_sms_timer_visible", countdown)
    monkeypatch.setattr(auth, "_sonkwo_sms_confirmed", countdown_expired)
    monkeypatch.setattr(auth, "_wait_authenticated", authenticated)
    monkeypatch.setattr(auth.asyncio, "sleep", fast_sleep)
    kwargs = {"input_reader": web_reader} if web_input else {}
    if sms_confirmed:
        await auth._sms_login(Page(), "sonkwo", 1000, monitor, **kwargs)
    else:
        with pytest.raises(SourceError, match="没有显示短信重发倒计时"):
            await auth._sms_login(Page(), "sonkwo", 1000, monitor, **kwargs)
    assert monitor.phone_confirmed is True
    if not manual:
        assert "challenge_confirmed" in monitor.stages
    assert ("awaiting_code" in monitor.stages) is sms_confirmed
    assert len(prompts) == (2 if sms_confirmed else 1)
    if sms_confirmed:
        assert prompts[1] == "code" if web_input else "短信验证码" in prompts[1]
