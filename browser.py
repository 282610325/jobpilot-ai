"""浏览器会话层：patchright（反检测版 Playwright）驱动本机 Chrome 专用配置目录。

为什么不再用 Selenium（2026-11-22 实测）：
- 猎聘/BOSS 都会读 window 上的 cdc_adoQpoasnfa76pfcZLmcfl_* 变量（ChromeDriver 注入
  的指纹），且该变量注入早于任何页面脚本 —— 页面脚本里根本删不掉。结果猎聘把页面
  清成 about:blank，BOSS 连续跳安全验证。
- patchright 不发这些变量（实测 cdc=0 / navigator.webdriver=False）：猎聘 9 秒稳定
  出结果，BOSS 正常停在 _security_check 初始化。

对外方法签名与旧 Selenium 版保持一致（goto/js/find/body/header_text/
wait_login_state/hide/show/perf_drain/tabs/...），业务层无需改动。
`rt.d` 保留为兼容壳（current_url / execute_cdp_cmd / get_cookies / add_cookie /
window_handles / switch_to），供 cookies.py、connect.py、session_api.py 直接调用。

其他约定不变：
- ALLOW_LIVE 硬闸：未设置 JOBPILOT_ALLOW_LIVE=1 时拒绝启动浏览器。
- Pacer 参数从 settings 表读（页面即时可调），配额从 db.quota 读（跨日自动重置）。
- 所有浏览器动作在单线程执行器里跑（Playwright 同步 API 不跨线程）。
"""
import os
import random
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import TimeoutError as PlaywrightTimeoutError

import config
import db


class Blocked(Exception):
    """空白页/页面未加载等平台异常，立即停手（不重试）。"""


class LaunchError(Exception):
    """Chrome 启动失败（多为残留进程占用配置目录）。"""


class LiveDisabled(Exception):
    """真实访问未开启（设置页「运行模式」为离线）。"""


def allow_live():
    """真实访问开关：设置页的「运行模式」持久化在 settings.allow_live；
    启动时设置 JOBPILOT_ALLOW_LIVE=1 也可强制开启（自动化/CI 用）。"""
    if os.environ.get("JOBPILOT_ALLOW_LIVE") == "1":
        return True
    try:
        return str(db.setting("allow_live", 0) or 0).strip() == "1"
    except Exception:
        return False


def login_ok(platform, head, names=None):
    """头部文本是否代表已登录。

    优先匹配 local_config 里配置的账号名；未配置时用通用特征：
    出现「登录/注册/扫码」→ 未登录；出现「你好/消息/我的简历/个人中心/退出」→ 已登录。
    """
    key = (names or config.LOGIN_TEXT).get(platform, "")
    head = str(head or "")
    if key:
        return key in head
    if any(k in head for k in ("登录", "注册", "扫码")):
        return False
    return any(k in head for k in ("你好", "消息", "我的简历", "个人中心", "退出"))


# ---------------- 进程与锁清理（只针对本程序配置目录） ----------------

def _no_window():
    """Windows 下调用外部命令时不弹黑框。"""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _profile_pids():
    """找出占用本程序专用配置目录的 Chrome 进程（跨平台）。"""
    prof = str(config.CHROME_PROFILE)
    pids = []
    if config.IS_WINDOWS:
        esc = prof.replace("'", "''")
        script = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*{esc}*' }} | "
                  "Select-Object -ExpandProperty ProcessId")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=20,
                creationflags=_no_window()).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
        except Exception:
            pass
        return pids
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    for line in out.splitlines():
        if prof not in line or "chromedriver" in line:
            continue
        try:
            pids.append(int(line.strip().split()[0]))
        except Exception:
            pass
    return pids


def _clean_locks():
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = config.CHROME_PROFILE / name
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
        except Exception:
            pass


def _kill_stale():
    if config.IS_WINDOWS:
        for pid in _profile_pids():
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15,
                               creationflags=_no_window())
            except Exception:
                pass
        time.sleep(1.0)
        _clean_locks()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = _profile_pids()
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
            except Exception:
                pass
        time.sleep(1.5)
    _clean_locks()


# ---------------- 限速与额度 ----------------

class Pacer:
    """动作间隔 + 每日额度。参数优先 settings 表 rate_<平台>_<键>，缺省用 config.RATE。"""

    def __init__(self):
        self._last = {}

    def cfg(self, platform):
        base = dict(config.RATE[platform])
        for k in list(base.keys()):
            v = db.setting(f"rate_{platform}_{k}", None)
            if v is None:
                continue
            if isinstance(base[k], (list, tuple)) and isinstance(v, list):
                base[k] = tuple(v)
            elif isinstance(base[k], (int, float)) and not isinstance(base[k], bool):
                base[k] = v
        return base

    def wait(self, platform):
        interval = float(self.cfg(platform)["interval"])
        gap = interval - (time.time() - self._last.get(platform, 0))
        if gap > 0:
            time.sleep(gap + random.uniform(0, 1.2))
        self._last[platform] = time.time()

    def can(self, platform, kind, cap):
        return db.quota_left(platform, kind, cap) > 0

    def use(self, platform, kind, n=1):
        db.quota_add(platform, kind, n)


# ---------------- 元素替身 ----------------

class _El:
    """Selenium 元素的最小替身：只保留业务层实际用到的动作。"""

    def __init__(self, page, handle):
        self._page = page
        self._h = handle

    @property
    def text(self):
        # 注意：ElementHandle.inner_text() 不接受 timeout 参数（Locator 才接受），
        # 传了会抛 TypeError 被吞掉，导致 click_text 永远匹配不到元素。
        try:
            return self._h.inner_text() or ""
        except Exception:
            try:
                return self._h.text_content() or ""
            except Exception:
                return ""

    def is_displayed(self):
        try:
            return bool(self._h.is_visible())
        except Exception:
            return False

    def click(self):
        self._h.click(timeout=6000)

    def clear(self):
        try:
            self._h.fill("")
        except Exception:
            try:
                self._h.click(timeout=3000)
                self._page.keyboard.press("Meta+A")
                self._page.keyboard.press("Backspace")
            except Exception:
                pass

    def send_keys(self, text):
        """兼容 Selenium Keys 常量（ENTER=\\ue007、TAB=\\ue004）。"""
        s = str(text or "")
        try:
            self._h.click(timeout=3000)
        except Exception:
            pass
        buf = ""

        def _flush():
            nonlocal buf
            if buf:
                self._page.keyboard.type(buf, delay=15)
                buf = ""

        for ch in s:
            if ch == "\ue007":
                _flush()
                self._page.keyboard.press("Enter")
            elif ch == "\ue004":
                _flush()
                self._page.keyboard.press("Tab")
            else:
                buf += ch
        _flush()


class _DriverShim:
    """把旧代码里的 `rt.d.xxx`（Selenium 写法）适配到 Playwright。

    业务层只用到这几项：current_url / execute_cdp_cmd / get_cookies /
    add_cookie / window_handles / current_window_handle / switch_to。
    """

    find_by = None                     # chat.py 会写它，置空即可

    def __init__(self, rt):
        self._rt = rt

    @property
    def current_url(self):
        return self._rt.url

    @property
    def window_handles(self):
        return self._rt.tabs()

    @property
    def current_window_handle(self):
        return self._rt.page

    @property
    def switch_to(self):
        rt = self._rt

        class _Switch:
            @staticmethod
            def window(handle):
                rt.switch_tab(handle)

        return _Switch()

    def get_cookies(self):
        out = []
        try:
            for c in self._rt.ctx.cookies() or []:
                d = dict(c)
                exp = d.pop("expires", None)
                d["expiry"] = int(exp) if exp and exp > 0 else None
                out.append(d)
        except Exception:
            pass
        return out

    def add_cookie(self, payload):
        p = {k: v for k, v in dict(payload).items() if v is not None}
        if "expiry" in p:
            p["expires"] = float(p.pop("expiry"))
        self._rt.ctx.add_cookies([p])

    def execute_cdp_cmd(self, cmd, params=None):
        return self._rt.cdp_send(cmd, params or {})


# ---------------- Runtime ----------------

class Runtime:
    """patchright 浏览器会话。外部方法签名与旧 Selenium 版完全一致。"""

    def __init__(self):
        self.d = None
        self.ctx = None
        self._pw = None
        self._active = None
        self._cdp = None
        self._events = []

    # ---- 启动 ----
    def ensure(self):
        if self.ctx is not None and self._active is not None:
            return
        if not allow_live():
            raise LiveDisabled(
                "真实访问未开启：请到「设置 → 运行模式」打开在线模式"
                "（或在启动服务时设置 JOBPILOT_ALLOW_LIVE=1）")
        config.CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        if _profile_pids():
            _kill_stale()
        else:
            _clean_locks()
        try:
            self._launch()
        except LaunchError:
            raise
        except Exception as e:
            msg = str(e)
            if "chrome" in msg.lower() and ("not found" in msg.lower()
                                            or "executable" in msg.lower()):
                raise LaunchError(
                    "未找到可用的 Google Chrome：请先安装 Chrome，或在 local_config.py / "
                    f"环境变量 JOBPILOT_CHROME 指定 chrome 路径。（{msg.splitlines()[0][:120]}）")
            if any(k in msg for k in ("already in use", "Singleton",
                                      "ProcessSingleton", "user data directory")):
                _kill_stale()
                time.sleep(2)
                try:
                    self._launch()
                    return
                except Exception as e2:
                    msg = str(e2)
            raise LaunchError(
                "Chrome 启动失败：配置目录被占用或无权限。请关闭本程序打开过的 "
                f"Chrome 窗口后重试。原始错误：{msg.splitlines()[0][:140]}")

    def _launch(self):
        from patchright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kw = dict(
            user_data_dir=str(config.CHROME_PROFILE),
            headless=False,
            no_viewport=True,
            locale="zh-CN",
            args=["--no-first-run",
                  "--no-default-browser-check",
                  "--disable-blink-features=AutomationControlled",
                  "--start-maximized",
                  "--lang=zh-CN",
                  # 采集时窗口移到屏幕外，必须关掉后台节流，否则页面不刷新令牌
                  "--disable-backgrounding-occluded-windows",
                  "--disable-renderer-backgrounding",
                  "--disable-background-timer-throttling",
                  "--disable-features=CalculateNativeWinOcclusion"])
        if config.chrome_exists():
            # 找到具体可执行文件就用它，避免 channel 在非标准安装路径下找不到
            kw["executable_path"] = config.CHROME
        else:
            kw["channel"] = "chrome"       # 交给 Playwright 按注册表/标准路径查找
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(**kw)
        except Exception as e:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
            raise LaunchError(
                "启动 Chrome 失败：请确认已安装 Google Chrome，且没有被其他窗口占用配置目录。"
                f"（{str(e).splitlines()[0][:140]}）")
        self.ctx.set_default_timeout(8000)
        self.ctx.set_default_navigation_timeout(60000)
        self._active = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._bind(self._active)
        self.d = _DriverShim(self)

    # ---- 页面句柄 ----
    @property
    def page(self):
        self.ensure()
        try:
            if self._active is None or self._active.is_closed():
                alive = [p for p in self.ctx.pages if not p.is_closed()]
                self._bind(alive[-1] if alive else self.ctx.new_page())
        except Exception:
            pass
        return self._active

    def _bind(self, page):
        """切活动页并给它挂 CDP 会话（Network 事件 → perf_drain 的缓冲）。"""
        self._active = page
        self._events = []
        self._cdp = None
        try:
            self._cdp = self.ctx.new_cdp_session(page)
            self._cdp.on("Network.requestWillBeSent", self._on_req)
            self._cdp.on("Network.loadingFinished", self._on_done)
            self._cdp.send("Network.enable")
        except Exception:
            self._cdp = None

    def _on_req(self, params):
        self._events.append(("Network.requestWillBeSent", params))
        if len(self._events) > 3000:        # 防无限增长
            del self._events[:1500]

    def _on_done(self, params):
        self._events.append(("Network.loadingFinished", params))

    def alive(self):
        try:
            return bool(self.ctx is not None and self._active is not None
                        and not self._active.is_closed())
        except Exception:
            return False

    # ---- 页面操作 ----
    @property
    def url(self):
        try:
            return self.page.url or ""
        except Exception:
            return ""

    def goto(self, url):
        page = self.page
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            pass                          # DOM 已可用，不重试
        except PlaywrightError as e:
            msg = str(e)
            if any(k in msg for k in ("closed", "crash", "Target page")):
                self.close()
                self.ensure()
                page = self.page
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
            else:
                raise Blocked(f"页面打开失败：{msg.splitlines()[0][:100]}")
        page.wait_for_timeout(int(random.uniform(800, 1400)))
        cur = ""
        for _ in range(20):               # 等页面离开 chrome:// / about:blank 过渡态
            cur = page.url or ""
            if cur and cur not in ("about:blank", "data:,") \
                    and not cur.startswith("chrome://"):
                break
            page.wait_for_timeout(500)
        if not cur or cur in ("about:blank", "data:,") or cur.startswith("chrome://"):
            raise Blocked(f"页面未加载：{url}")
        return cur

    def body(self):
        try:
            return self.page.inner_text("body", timeout=3000) or ""
        except Exception:
            return ""

    def js(self, script, *args):
        """在页面里执行脚本。沿用 Selenium 的 `arguments[0]` 写法：
        统一包进一个函数，元素参数自动换成 Playwright 的 ElementHandle。"""
        page = self.page
        vals = [a._h if isinstance(a, _El) else a for a in args]
        wrapped = ("(args) => { const __f = function(){ " + script +
                   "\n}; return __f.apply(null, args); }")
        try:
            return page.evaluate(wrapped, vals)
        except PlaywrightError as e:
            msg = str(e)
            if "Execution context was destroyed" in msg or "navigat" in msg:
                page.wait_for_timeout(1000)
                try:
                    return page.evaluate(wrapped, vals)
                except Exception:
                    return None
            raise

    def perf_drain(self):
        """取出并清空缓冲的 CDP 网络事件，返回 [(method, params)]。

        Playwright 同步 API 只在调用时派发事件，所以先做一次极短等待把已到达的
        事件泵出来，再返回。
        """
        try:
            self.page.wait_for_timeout(60)
        except Exception:
            pass
        out, self._events = self._events, []
        return [(m, dict(p or {})) for m, p in out
                if str(m).startswith("Network.")]

    def cdp_send(self, cmd, params=None):
        if self._cdp is None:
            self._bind(self.page)
        if self._cdp is None:
            raise RuntimeError("CDP 会话不可用")
        return self._cdp.send(cmd, params or {})

    def js_xhr(self, api_path, params="", timeout_ms=15000):
        """在已登录页面上下文里同步 GET 站内 API（BOSS 采集核心）。
        api_path 例：/wapi/zpgeek/search/joblist.json?query=..&city=..&page=..
        返回解析后的 dict；失败返回 None（调用方决定降级/停手）。"""
        script = """
        const u = arguments[0], t = arguments[1];
        try {
            const x = new XMLHttpRequest();
            x.open('GET', u, false);
            x.timeout = t;
            x.withCredentials = true;
            x.setRequestHeader('Accept', 'application/json');
            x.send();
            if (x.status === 200) return x.responseText;
            return null;
        } catch (e) { return null; }
        """
        raw = self.js(script, api_path + (params or ""), timeout_ms)
        if not raw:
            return None
        import json as _json
        try:
            return _json.loads(raw)
        except Exception:
            return None

    # ---- 窗口显隐（采集时移出屏幕，用户无需盯着） ----
    def _set_bounds(self, **bounds):
        try:
            cdp = self.ctx.new_cdp_session(self.page)
            try:
                win = cdp.send("Browser.getWindowForTarget")
                cdp.send("Browser.setWindowBounds",
                         {"windowId": win.get("windowId"), "bounds": bounds})
                return True
            finally:
                cdp.detach()
        except Exception:
            return False

    def hide(self):
        return self._set_bounds(windowState="normal", left=-3000, top=-3000)

    def show(self):
        ok = self._set_bounds(windowState="normal", left=60, top=40,
                              width=1280, height=900)
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        return ok

    # ---- 多标签 ----
    def tabs(self):
        try:
            return [p for p in self.ctx.pages if not p.is_closed()]
        except Exception:
            return []

    def current_tab(self):
        return self.page

    def switch_tab(self, handle):
        try:
            if handle is not None and not handle.is_closed():
                self._bind(handle)
        except Exception:
            pass

    def switch_to_url(self, needle):
        """找 URL 含 needle 的标签切过去；找到 True。"""
        try:
            if needle in (self.page.url or ""):
                return True
            for p in self.tabs():
                if p is self._active:
                    continue
                if needle in (p.url or ""):
                    self._bind(p)
                    return True
        except Exception:
            pass
        return False

    def close_extra_tabs(self, keep):
        for p in self.tabs():
            if p is not keep:
                try:
                    p.close()
                except Exception:
                    pass
        if keep is not None:
            try:
                if not keep.is_closed():
                    self._bind(keep)
            except Exception:
                pass

    # ---- 登录态 ----
    def header_text(self):
        """抓页面顶部文本（用于登录判定）。

        只取 `<header .nav>` 会漏掉用户区（智联的账号名在
        `.c-login__top__name` 里，不在导航栏），故额外拼上常见用户区元素。
        """
        try:
            return self.js(
                "const h=document.querySelector('header,.header,.top-bar,#header');"
                "let t=((h||document.body).innerText||'').slice(0,1200);"
                "const u=document.querySelector('.c-login__top__name,"
                "[class*=\"user-name\"],[class*=\"userName\"],[class*=\"user_name\"]');"
                "if(u){const s=(u.innerText||'').trim();"
                "if(s&&!t.includes(s)) t+=' '+s;}"
                "return t.slice(0,1600);") or ""
        except Exception:
            return ""

    def wait_login_state(self, platform, timeout=16, names=None):
        """等待头部出现账号名 → 已登录。

        注意：招聘站是 SPA，加载中途会先渲染含「登录」的骨架屏，随后才替换成
        用户信息。因此**不能**一看到「登录」就判失败（这是之前智联误判的根因），
        必须持续等到账号名出现；只有在「看到登录字样 + 已等足够久」时才提前判未登录。
        返回 (bool, 头部文本)。
        """
        start = time.time()
        end = start + timeout
        head = ""
        while time.time() < end:
            head = self.header_text()
            if login_ok(platform, head, names):
                return True, head
            if any(k in head for k in ("登录", "注册", "扫码")):
                # 骨架屏常见，给 6 秒让 SPA 换成用户信息后再下结论
                if time.time() - start >= 6:
                    return False, head
            time.sleep(0.8)
        return False, head

    # ---- 元素与点击 ----
    def find(self, css):
        try:
            return [_El(self.page, h) for h in self.page.query_selector_all(css)]
        except Exception:
            return []

    def wait_css(self, css, timeout=8):
        """等元素进入 DOM（对齐 Selenium 的 presence_of_element_located，
        不要求可见 —— 有些平台把输入框先挂成隐藏状态再显示）。"""
        try:
            self.page.wait_for_selector(css, state="attached",
                                        timeout=int(timeout * 1000))
            return True
        except Exception:
            return False

    def click_el(self, el):
        try:
            el.click()
        except Exception:
            try:
                el._h.scroll_into_view_if_needed(timeout=3000)
                el._h.click(timeout=4000, force=True)
            except Exception:
                pass

    def click_text(self, css, text, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            for el in self.find(css):
                try:
                    if text in (el.text or "") and el.is_displayed():
                        self.click_el(el)
                        return True
                except Exception:
                    continue
            time.sleep(0.4)
        return False

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self.ctx = None
            self._pw = None
            self._active = None
            self._cdp = None
            self._events = []
            self.d = None


class Bot:
    def __init__(self):
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot")
        self._rt = None
        self.pacer = Pacer()
        self._lock = threading.Lock()

    def call(self, fn, *args, **kwargs):
        return self._exec.submit(self._run, fn, args, kwargs).result()

    def _run(self, fn, args, kwargs):
        with self._lock:
            if self._rt is None:
                self._rt = Runtime()
            if self._rt.d is not None and not self._rt.alive():
                try:
                    self._rt.close()
                except Exception:
                    pass
            return fn(self._rt, *args, **kwargs)

    def shutdown(self):
        try:
            self.call(lambda rt: rt.close())
        except Exception:
            pass
