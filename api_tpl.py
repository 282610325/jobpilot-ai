"""HTTP 请求模板：BOSS 内置直连；智联/猎聘"抓包一次 → 纯 HTTP 回放"。

为什么必须抓包：智联/猎聘的搜索网关要求浏览器侧签名头（x-zp-* / x-fscp-*），
纯离线复现已证实不可行（GitHub 高星项目因此退回浏览器方案）。可靠做法只有一种：
在登录态真实浏览器里做一次搜索，被动录制页面自己发出的 JSON 请求
（URL/头/请求体），存成模板；之后采集全部用 netio 纯 HTTP 回放，浏览器不再参与。
敏感头（Cookie/UA）不入库，回放时由 netio 按当前 Cookie 重新拼。
"""
import json
import re
import time
from urllib.parse import quote

import config
import db
import risk
from browser import Blocked

DROP_H = {"cookie", "host", "connection", "content-length", "user-agent",
          "accept-encoding", "accept-language", "upgrade-insecure-requests",
          "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
          "sec-fetch-mode", "sec-fetch-site", "sec-fetch-dest"}

MARKERS = {"boss": ("jobList", "securityId"),
           "zhaopin": ("jobList", "positionList", "results", "unifiedJobSearch",
                       "searchPositions", "jobName"),
           "liepin": ("jobCardList", "compName", "searchfront")}

SEARCH_PAGE = {
    "zhaopin": "https://www.zhaopin.com/jobs/?jl={code}",
    "liepin": "https://www.liepin.com/zhaopin/?key=",
}

BLOCKED_URL_HINTS = ("safe.liepin.com", "verify", "passport", "login",
                     "security-check", "captcha")


def builtin(platform, kind="search"):
    """无需抓包的平台模板（BOSS 接口参数形态已被多个开源项目验证）。"""
    if platform == "boss" and kind == "search":
        return {"platform": "boss", "kind": "search", "method": "GET",
                "url": "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
                "headers": {"X-Requested-With": "XMLHttpRequest"},
                "body": "", "anchor": {}, "note": "内置模板"}
    return None


def get(platform, kind="search"):
    return db.tpl_get(platform, kind) or builtin(platform, kind)


def _kwd_variants(old):
    if not old:
        return []
    try:
        esc = json.dumps(old)[1:-1]           # \uXXXX 形态
    except Exception:
        esc = old
    return [(old, None), (quote(old), None), (quote(old, safe=""), None),
            (esc, None)]


def render(tpl, kwd=None, page=None, city=None, city_code=None):
    """模板 + 新参数 → (url, body)。锚点值（抓包时的关键词/页码/城市）做替换。"""
    url, body = tpl["url"], tpl.get("body") or ""
    anch = tpl.get("anchor") or {}
    if city_code and anch.get("city_code"):
        old = str(anch["city_code"])
        if old != str(city_code):
            url = url.replace(old, str(city_code))
            body = body.replace(old, str(city_code))
    # 锚点替换：新旧词的所有编码形态交叉比对
    has_kwd_replacement = bool(kwd and anch.get("kwd"))
    if has_kwd_replacement and kwd != anch["kwd"]:
        for new in (kwd, quote(kwd), quote(kwd, safe=""),
                    json.dumps(kwd)[1:-1]):
            for old_kw in {anch["kwd"], quote(anch["kwd"]),
                           quote(anch["kwd"], safe=""),
                           json.dumps(anch["kwd"])[1:-1]}:
                if old_kw and new != old_kw:
                    url = url.replace(old_kw, new)
                    body = body.replace(old_kw, new)
    if city and anch.get("city") and city != anch["city"]:
        url = url.replace(anch["city"], city)
        body = body.replace(anch["city"], city)
    if page:
        url = re.sub(r"([?&])(pageNum|currentPage|beginPos|page)=\d+",
                     rf"\1\2={page}", url)
        body = re.sub(r'"(pageNum|currentPage|beginPos|page)"(\s*:\s*)\d+',
                      rf'"\1"\2{page}', body)
    return url, body or None


# ---------------- 抓包（浏览器只在这里出现一次） ----------------

def record(rt, log, platform, probe_kw="IT经理", timeout=60):
    """录制该平台搜索接口的真实请求。返回模板 dict 或抛 Blocked。

    调用方负责在可见窗口里；本函数自动导航+自动搜索，用户无需任何操作。
    """
    if platform == "boss":
        raise Blocked("BOSS 用内置模板即可，无需抓包")
    risk.check(platform)
    log(f"自动录制 {config.PLATFORM_NAMES[platform]} 搜索接口（浏览器仅本次出现）")
    if platform == "zhaopin":
        url = SEARCH_PAGE["zhaopin"].format(code=config.ZL_CITY.get("珠海", 766))
    else:
        url = SEARCH_PAGE["liepin"] + quote(probe_kw)
    rt.goto(url)
    _assert_not_blocked(rt, platform)
    _auto_search(rt, platform, probe_kw)

    seen, bodies = {}, {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        for m, p in rt.perf_drain():
            if m == "Network.requestWillBeSent":
                r = p.get("request") or {}
                u = r.get("url") or ""
                if _mine(platform, u) and _looks_search(u, r.get("method")):
                    seen[p["requestId"]] = {"url": u, "method": r.get("method"),
                                            "headers": r.get("headers") or {},
                                            "post": r.get("postData") or ""}
            elif m == "Network.responseReceived" and p.get("requestId") in seen:
                resp = p.get("response") or {}
                if "json" in (resp.get("mimeType") or ""):
                    seen[p["requestId"]]["json"] = True
        _assert_not_blocked(rt, platform)
        for rid, info in list(seen.items()):
            if not info.get("json") or rid in bodies:
                continue
            b = _resp_body(rt, rid)
            if b:
                bodies[rid] = b
        good = [(rid, i) for rid, i in seen.items()
                if bodies.get(rid) and any(k in bodies[rid]
                                           for k in MARKERS[platform])]
        if good:
            rid, info = max(good, key=lambda x: len(bodies[x[0]]))
            info["sample"] = bodies[rid][:400]
            break
        time.sleep(0.8)
    else:
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} 没录到搜索接口："
                      "确认窗口里已登录且页面能正常显示岗位，然后重试")

    h = {k.lower(): v for k, v in (info["headers"] or {}).items()}
    keep = {k: v for k, v in h.items()
            if k not in DROP_H and (k.startswith("x-") or k in
                                    ("content-type", "referer"))}
    anchor = _anchor(info, probe_kw)
    tpl = {"platform": platform, "kind": "search", "url": info["url"],
           "method": info["method"] or "GET", "headers": keep,
           "body": info["post"], "anchor": anchor,
           "note": f"自动录制 {info['url'].split('?')[0]}"}
    db.tpl_save(tpl)
    log(f"✓ 接口模板已保存：{config.PLATFORM_NAMES[platform]} "
        f"{tpl['method']} {tpl['url'][:80]}（签名头 {len(keep)} 个）")
    return tpl


def _looks_search(u, method):
    keys = ("search", "job", "position", "recommend", "list", "query",
            "unified", "sou", "geek")
    return method in ("POST", "GET") and any(k in u.lower() for k in keys)


def _assert_not_blocked(rt, platform):
    cur = (rt.d.current_url or "") if rt.d else ""
    if any(k in cur for k in BLOCKED_URL_HINTS):
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} 登录态已失效"
                      f"（被带到 {cur.split('?')[0][:60]}）：请先在窗口里完成登录/验证")


def _auto_search(rt, platform, kw):
    """自动在搜索框输入关键词并回车（失败不致命，页面本身也会发搜索请求）。"""
    time.sleep(2.5)
    css_list = {
        "zhaopin": ["input[name='kw']", "input[placeholder*='搜索']",
                    "input[class*='search']", ".search-input input"],
        "liepin": ["input[placeholder*='搜索']", "input[name='key']",
                   ".search-box input"],
    }.get(platform, [])
    from selenium.webdriver.common.keys import Keys
    for css in css_list + ["input"]:
        try:
            for el in rt.find(css):
                try:
                    if not el.is_displayed():
                        continue
                    el.click()
                    el.clear()
                    el.send_keys(kw)
                    el.send_keys(Keys.ENTER)
                    time.sleep(3)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def _mine(platform, u):
    dom = {"zhaopin": "zhaopin.com", "liepin": "liepin.com"}[platform]
    return dom in u


def _anchor(info, probe_kw):
    a = {"kwd": probe_kw}
    hay = info["url"] + "|" + (info["post"] or "")
    if probe_kw not in hay:                 # 页面没用探针词
        for cand in re.findall(r'"(?:keyword|key|query|q)"\s*:\s*"([^"]{1,20})"',
                               hay):
            a["kwd"] = cand
            break
    m = re.search(r'"city[A-Za-z]*"\s*:\s*"?(\d{2,6})"?', hay)
    if m:
        a["city_code"] = m.group(1)
    return a


def _resp_body(rt, rid):
    try:
        r = rt.d.execute_cdp_cmd("Network.getResponseBody",
                                 {"requestId": rid})
        if r.get("base64Encoded"):
            import base64
            return base64.b64decode(r["body"]).decode("utf-8", "ignore")
        return r.get("body") or ""
    except Exception:
        return ""
