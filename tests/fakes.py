"""FakeRuntime：不启动浏览器的可编程替身，供采集/投递/风控离线测试。"""
import time


class FakeDriver:
    def __init__(self, rt):
        self.rt = rt

    @property
    def current_url(self):
        return self.rt.url_stack[-1] if self.rt.url_stack else ""

    @property
    def window_handles(self):
        return self.rt.handles

    def switch_to_property(self):
        pass


class SwitchTo:
    def __init__(self, rt):
        self.rt = rt

    def window(self, h):
        if h not in self.rt.handles:
            raise RuntimeError(f"no such window {h}")
        self.rt.active = h


class FakeRuntime:
    """js/js_xhr/goto/body 可编程：
    js_table: {脚本关键子串: 返回值 或 callable(args)->值}
    xhr_table: {API路径前缀: dict返回 或 callable(params)->dict/None}
    body_table: {URL子串: 正文文本}
    raise_on: {URL子串: 异常实例}（模拟 Blocked/验证链）
    """

    def __init__(self, js_table=None, xhr_table=None, body_table=None,
                 url="about:blank", clock=None):
        self.js_table = js_table or {}
        self.xhr_table = xhr_table or {}
        self.body_table = body_table or {}
        self.url_stack = [url]
        self.handles = ["w0"]
        self.active = "w0"
        self.time_fn = clock or time.time
        self.goto_log = []
        self.js_log = []
        self.xhr_log = []
        self._body = ""
        self.d = self  # 兼容 rt.d.current_url 写法

    # --- d 代理 ---
    class _T:
        pass

    @property
    def switch_to(self):
        rt = self
        class S:
            def window(h):
                if h not in rt.handles:
                    raise RuntimeError("no such window")
                rt.active = h
        return S

    @property
    def current_window_handle(self):
        return self.active

    # --- 接口 ---
    def ensure(self):
        pass

    def alive(self):
        return True

    def goto(self, url, **kw):
        self.goto_log.append(url)
        self.url_stack.append(url)
        for needle, exc in getattr(self, "raises", {}).items():
            if needle in url:
                raise exc
        return url

    def set_url(self, url):
        self.url_stack[-1] = url

    def body(self):
        for needle, text in self.body_table.items():
            if needle in (self.url_stack[-1] or ""):
                return text
        return self._body

    def js(self, script, *args):
        self.js_log.append((script, args))
        for needle, val in self.js_table.items():
            if needle in script:
                return val(*args) if callable(val) else val
        return None

    def js_xhr(self, api_path, params="", timeout_ms=15000):
        self.xhr_log.append(api_path + params)
        for prefix, val in self.xhr_table.items():
            if api_path.startswith(prefix) or prefix in api_path:
                return val(params) if callable(val) else val
        return None

    def header_text(self):
        return self.js_table.get("__header__", "")

    def wait_login_state(self, platform, timeout=16, names=None):
        from browser import login_ok
        head = self.header_text()
        return login_ok(platform, head, names), head

    def wait_css(self, css, timeout=8):
        return True

    def click_text(self, css, text, timeout=8):
        return True

    def click_el(self, el):
        pass

    def find(self, css):
        return []

    def tabs(self):
        return list(self.handles)

    def current_tab(self):
        return self.active

    def switch_tab(self, h):
        self.active = h

    def switch_to_url(self, needle):
        return needle in (self.url_stack[-1] or "")

    def close_extra_tabs(self, keep):
        self.handles = [keep]
        self.active = keep

    def close(self):
        pass
