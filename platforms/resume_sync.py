"""简历同步：把本系统导出的 PDF 上传为平台附件简历；只读抓取在线简历文本。

- 上传属于对平台账号的真实修改：只在「在线模式」且用户确认后执行。
- 读取在线简历只做文本抓取，不改任何数据。
"""
import time

import config
import risk
from browser import Blocked

URL = {"zhaopin": "https://i.zhaopin.com/resume",
       "liepin": "https://c.liepin.com/",
       "boss": "https://www.zhipin.com/web/geek/resume"}


def _check(rt, platform):
    body = rt.body()
    bad = risk.scan(body)
    if bad:
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} 风控信号：{bad}")
    return body


def _pick_input(rt, allow_empty=False):
    """挑一个接受 PDF 的文件输入；BOSS 页面上还有头像/视频输入，不能乱选。"""
    first_empty = None
    for h in rt.page.query_selector_all("input[type=file]"):
        try:
            acc = (h.get_attribute("accept") or "").lower()
        except Exception:
            continue
        if "pdf" in acc:
            return h
        if allow_empty and not acc and first_empty is None:
            first_empty = h
    return first_empty


def _wait_success(rt, name, timeout=12):
    """等「上传成功」提示或文件名出现在页面上。"""
    end = time.time() + timeout
    stem = name[:8]
    while time.time() < end:
        body = rt.body() or ""
        if "上传成功" in body or (stem and stem in body):
            return True
        rt.page.wait_for_timeout(600)
    return False


def upload_attachment(rt, platform, pdf_path, log):
    """把 PDF 上传为平台附件简历。返回 (ok, msg)。"""
    name = str(pdf_path).split("/")[-1]
    if platform == "zhaopin":
        rt.goto(URL["zhaopin"])
        rt.page.wait_for_timeout(4000)
        _check(rt, platform)
        if not rt.click_text("button", "上传新附件", 6):
            return False, "找不到「上传新附件」入口"
        rt.page.wait_for_timeout(1200)
        h = _pick_input(rt, allow_empty=True)
        if not h:
            return False, "上传弹层里没有文件输入"
        h.set_input_files(str(pdf_path))
        rt.page.wait_for_timeout(2500)
        ok = _wait_success(rt, name)
        return (True, "已上传，请到附件列表核对") if ok else \
            (False, "已选择文件但未看到成功提示，请人工核对")
    if platform == "boss":
        rt.goto(URL["boss"])
        rt.page.wait_for_timeout(4500)
        _check(rt, platform)
        h = _pick_input(rt)
        if not h:
            return False, "找不到附件上传输入"
        h.set_input_files(str(pdf_path))
        rt.page.wait_for_timeout(3000)
        ok = _wait_success(rt, name)
        return (True, "已上传，请到附件列表核对") if ok else \
            (False, "已选择文件但未看到成功提示，请人工核对")
    if platform == "liepin":
        rt.goto(URL["liepin"])
        rt.page.wait_for_timeout(4000)
        _check(rt, platform)
        if not rt.click_text("a", "附件简历", 6):
            return False, "找不到「附件简历」入口"
        rt.page.wait_for_timeout(1800)
        h = _pick_input(rt)
        if not h:
            return False, "上传弹层里没有文件输入"
        log("猎聘提示：上传附件会被平台解析并可能完善在线简历")
        h.set_input_files(str(pdf_path))
        rt.page.wait_for_timeout(3000)
        for txt in ("确认上传", "确定", "保存"):
            if rt.click_text("button", txt, 1):
                break
        ok = _wait_success(rt, name)
        return (True, "已上传，请到附件列表核对") if ok else \
            (False, "已选择文件但未看到成功提示，请人工核对")
    return False, "不支持的平台"


def read_online(rt, platform):
    """只读：打开平台在线简历页，返回正文文本（截断 12000 字）。"""
    if platform not in URL:
        raise Blocked("不支持的平台")
    rt.goto(URL[platform])
    rt.page.wait_for_timeout(4500)
    body = _check(rt, platform)
    return (body or "")[:12000]
