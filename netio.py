"""netio：纯 HTTP 请求层（不启动浏览器）。采集/投递/探测都从这里走。

铁律（对应 BOSS 封号教训）：
- 离线硬闸与浏览器一致：未设 JOBPILOT_ALLOW_LIVE=1 一律拒绝，测试期发不出真实请求。
- 每次请求前 risk.check + 平台最小间隔限速（rate_<平台>_interval，随机抖动）。
- 响应风控信号分级处理：BOSS 业务码 9/31/32/36/37/17、HTTP 403/429、正文命中
  BLOCK_WORDS → risk.trip 冷却 + 抛 Blocked，由任务层整单刹停；绝不自动重试。
- 每次请求记一行审计日志（平台/接口/状态/耗时/业务码）。
"""
import json
import random
import re
import time
from urllib.parse import urlsplit

import requests

import config
import db
import risk
from browser import Blocked, LiveDisabled, allow_live

DOMAIN = {"zhaopin": "zhaopin.com", "liepin": "liepin.com", "boss": "zhipin.com"}
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

_log_fn = print


def set_logger(fn):
    global _log_fn
    _log_fn = fn


class TokenExpired(Exception):
    """站内令牌失效/轮换（BOSS __zp_stoken__）。不是风控，可刷新后继续。"""


def log(msg):
    _log_fn(msg)


# ---------------- Cookie ----------------

def _cookies_from_blob(platform):
    """data/cookies.json（专用窗口备份）里取该平台的 name=value 串。"""
    p = config.COOKIES_PATH
    if not p.exists():
        return {}
    try:
        blob = json.loads(p.read_text())
        item = blob.get(platform) or {}
        out = {}
        for c in item.get("cookies") or []:
            dom = c.get("domain") or ""
            if dom.replace(".", "").endswith(DOMAIN[platform]):
                out[c["name"]] = c["value"]
        return out
    except Exception:
        return {}


def parse_cookie_string(raw):
    out = {}
    for seg in re.split(r"[;\n]+", str(raw or "")):
        if "=" in seg:
            k, v = seg.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


def cookie_header(platform):
    """优先页面粘贴的 cookie_raw_<平台>，并合并 cookies.json 备份。"""
    ck = _cookies_from_blob(platform)
    ck.update(parse_cookie_string(db.setting(f"cookie_raw_{platform}", "")))
    return ck


def site_headers(platform):
    """站内 API 必需的自定义头（实测得出，缺一个就返回空列表）。

    BOSS：__zp_stoken__ 必须作为请求头回传（只放 Cookie 里不够）；
          bst 需同时写成 zp_token（部分账号有 bst，没有则跳过）。
    """
    ck = cookie_header(platform)
    h = {"X-Requested-With": "XMLHttpRequest"}
    if platform == "boss":
        stoken = ck.get("__zp_stoken__") or ""
        if stoken:
            h["__zp_stoken__"] = stoken
        bst = ck.get("bst") or ""
        if bst:
            h["zp_token"] = bst
    return h


# ---------------- 限速 ----------------

_last = {}


def _pace(platform):
    interval = 6.0
    try:
        interval = float(browser_interval(platform))
    except Exception:
        pass
    gap = interval - (time.time() - _last.get(platform, 0))
    if gap > 0:
        time.sleep(gap + random.uniform(0, 2.0))
    _last[platform] = time.time()


def browser_interval(platform):
    return db.setting(f"rate_{platform}_interval",
                      config.RATE[platform]["interval"])


# ---------------- 风控信号 ----------------

BOSS_CODE_MSG = {9: "限流", 31: "IP 受限", 32: "账号受限", 36: "账号风险",
                 37: "安全令牌失效（需重新登录/导出 Cookie）", 17: "登录失效"}


def _judge(platform, resp, data):
    """返回 None=正常；否则抛 Blocked（并先 trip 冷却）。"""
    text = (resp.text or "")[:4000]
    bad = risk.scan(text)
    if bad:
        risk.trip(platform, f"HTTP 响应含风控文案：{bad}")
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} 风控信号：{bad}（已冷却，任务停止）")
    if resp.status_code in (403, 429):
        risk.trip(platform, f"HTTP {resp.status_code}")
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} HTTP {resp.status_code}"
                      f"（疑似频控，已冷却，任务停止）")
    if platform == "boss" and isinstance(data, dict):
        code = data.get("code")
        if code in BOSS_CODE_MSG:
            msg = BOSS_CODE_MSG[code]
            if code in (17, 37):
                # 凭证/token 轮换，不是平台风控：不冷却，交给上层刷新令牌
                raise TokenExpired(f"BOSS {msg}（code={code}）")
            risk.trip(platform, f"BOSS code={code} {msg}")
            raise Blocked(f"BOSS {msg}（code={code}），已冷却 60 分钟，任务停止")
        message = str(data.get("message") or "")
        if any(k in message for k in ("验证", "频繁", "异常")):
            risk.trip(platform, f"BOSS message={message[:40]}")
            raise Blocked(f"BOSS 异常提示：{message[:60]}（已冷却）")
    if resp.status_code >= 400:
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} HTTP "
                      f"{resp.status_code}：{text[:80]}")
    if resp.status_code in (301, 302, 303, 307):
        loc = resp.headers.get("location") or ""
        if any(k in loc for k in ("verify", "login", "passport", "safe")):
            raise Blocked(f"{config.PLATFORM_NAMES[platform]} 被重定向到"
                          f"登录/验证页（Cookie 失效？）：{loc[:70]}")
    return None


# ---------------- 请求入口 ----------------

def request(platform, method, url, *, params=None, json_body=None, form=None,
            referer=None, extra_headers=None, quiet=False):
    """一次受闸门保护的 HTTP 调用。返回 (status, data_or_text)。"""
    if not allow_live():
        raise LiveDisabled("真实访问未开启：请到「设置 → 运行模式」打开在线模式"
                           "（HTTP 直连同样算真实访问）")
    if not risk.platform_on(platform):
        raise Blocked(f"{config.PLATFORM_NAMES.get(platform, platform)} 已被暂停"
                      "（到总览勾选「允许自动访问」即可开启）")
    risk.check(platform)
    _pace(platform)
    ck = cookie_header(platform)
    if not ck:
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} 没有 Cookie："
                      "请先「一键连接」（自动读取浏览器登录态）")
    # 只保留能安全放进 HTTP 头的 ASCII 值（解密失败/脏数据直接丢）
    ck = {k: v for k, v in ck.items()
          if v and all(32 <= ord(c) < 127 for c in str(v))}
    host = urlsplit(url).netloc
    if not host.endswith(DOMAIN.get(platform, host)):
        raise Blocked(f"越域请求被拒绝：{host} 不属于 {platform}")
    headers = {"User-Agent": db.setting("net_ua", DEFAULT_UA),
               "Accept": "application/json, text/plain, */*",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items())}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = f"{urlsplit(referer).scheme}://{urlsplit(referer).netloc}"
    headers.update(extra_headers or {})
    t0 = time.time()
    resp = requests.request(method.upper(), url, params=params,
                            json=json_body if json_body is not None else None,
                            data=form if form is not None else None,
                            headers=headers, timeout=(10, 30),
                            allow_redirects=False)
    data = None
    ctype = resp.headers.get("content-type") or ""
    if "json" in ctype or (resp.text or "").lstrip()[:1] in "{[":
        try:
            data = resp.json()
        except ValueError:
            data = None
    verdict = _judge(platform, resp, data)
    if verdict is None and not quiet:
        code = data.get("code") if isinstance(data, dict) else "-"
        log(f"netio {config.PLATFORM_NAMES[platform]} {method.upper()} "
            f"{urlsplit(url).path} → {resp.status_code} code={code} "
            f"{round(time.time()-t0, 1)}s")
    if isinstance(data, dict):
        return resp.status_code, data
    return resp.status_code, resp.text
