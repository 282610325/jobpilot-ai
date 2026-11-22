"""session_api：会话内接口采集/投递（用户零操作，窗口移到屏幕外）。

为什么不是纯 HTTP：三平台的 __zp_stoken__ / x-zp-* / x-fscp-* 令牌由页面 JS
现场生成并随请求轮换（实测 BOSS 第 2 次请求即 code=37）。因此必须在真实页面
上下文里发请求——但**不点击任何按钮、不滚动、不翻页**，只让页面自己完成令牌续期。

- BOSS：页面内 XMLHttpRequest 循环调 /wapi/zpgeek/search/joblist.json
- 智联/猎聘：自动在搜索框输入关键词回车，拦截页面自己发出的 JSON 响应
"""
import json
import time
from urllib.parse import quote

import config
import db
import risk
from browser import Blocked

XHR_JS = """
var u = arguments[0];
try {
  var x = new XMLHttpRequest();
  x.open('GET', u, false);
  x.withCredentials = true;
  x.setRequestHeader('Accept', 'application/json');
  x.send();
  return x.status + '||' + (x.responseText || '');
} catch (e) { return 'ERR||' + e.message; }
"""

XHR_POST_JS = """
var u = arguments[0], body = arguments[1];
try {
  var x = new XMLHttpRequest();
  x.open('POST', u, false);
  x.withCredentials = true;
  x.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  x.setRequestHeader('Accept', 'application/json');
  x.send(body);
  return x.status + '||' + (x.responseText || '');
} catch (e) { return 'ERR||' + e.message; }
"""

SEARCH_PAGE = {"zhaopin": "https://www.zhaopin.com/jobs/?jl={code}",
               "liepin": "https://www.liepin.com/zhaopin/"}
MARKERS = {"zhaopin": ("jobList", "positionList", "jobName", "salary60"),
           "liepin": ("jobCardList", "compName")}


def _settle(rt, platform, log, wait=6):
    """等页面从安全校验跳转回正常页；仍在校验页则提示。"""
    end = time.time() + wait
    while time.time() < end:
        cur = rt.d.current_url or ""
        if "_security_check" not in cur:
            break
        time.sleep(1.2)
    cur = rt.d.current_url or ""
    if "_security_check" in cur:
        raise Blocked(f"{config.PLATFORM_NAMES[platform]} 被带到环境校验页："
                      "该账号刚被频繁访问，请等 30 分钟后再试（不要连续重试）")
    return cur


def _xhr(rt, url):
    raw = rt.js(XHR_JS, url) or ""
    st, _, txt = raw.partition("||")
    try:
        data = json.loads(txt) if txt.lstrip().startswith(("{", "[")) else None
    except ValueError:
        data = None
    return st, data, txt


def _xhr_post(rt, url, body):
    raw = rt.js(XHR_POST_JS, url, body) or ""
    st, _, txt = raw.partition("||")
    try:
        data = json.loads(txt) if txt.lstrip().startswith(("{", "[")) else None
    except ValueError:
        data = None
    return st, data, txt


# ---------------- BOSS ----------------

def collect_boss(rt, log, combos, pages):
    """页面内 XHR 循环。页面自己轮换 __zp_stoken__，因此可连续翻页。"""
    from platforms import boss_api
    rt.goto(config.BOSS_JOBS)
    _settle(rt, "boss", log)
    ok, head = rt.wait_login_state("boss", 15)
    if not ok:
        raise Blocked("BOSS 未登录（页面头部没有账号名）")
    added = dup = n = 0
    cap = int(db.setting("rate_boss_daily_api", config.RATE["boss"]["daily_api"]))
    for kw, code, city in combos:
        if n >= cap:
            log(f"BOSS 今日接口配额用尽（{cap}）")
            break
        for pg in range(1, max(1, int(pages)) + 1):
            if n >= cap:
                break
            q = (f"?query={quote(kw)}&page={pg}&pageSize=30"
                 + (f"&city={code}" if code else ""))
            st, data, txt = _xhr(rt, config.BOSS_API + q)
            n += 1
            db.quota_add("boss", "api")
            if not isinstance(data, dict):
                bad = risk.scan(txt[:2000])
                if bad:
                    risk.trip("boss", f"页面内接口异常+{bad}")
                    raise Blocked(f"BOSS 风控信号：{bad}")
                raise Blocked(f"BOSS 接口无 JSON（status={st}）："
                              "请确认页面已正常加载岗位列表")
            if data.get("code") == 37:
                # 页面还没完成令牌初始化：等一下再试同页，不重复计数
                time.sleep(4)
                st, data, txt = _xhr(rt, config.BOSS_API + q)
            code_v = data.get("code")
            if code_v in (9, 31, 32, 36):
                risk.trip("boss", f"页面内接口 code={code_v}")
                raise Blocked(f"BOSS 风控 code={code_v}，已冷却，任务停止")
            jobs = boss_api.parse_joblist(data)
            if not jobs:
                raw_n = len(((data.get("zpData") or {}).get("jobList") or []))
                log(f"BOSS「{kw}」第{pg}页 0 条"
                    f"（code={code_v}，原始 {raw_n} 条）")
                break
            for j in jobs:
                j["city"] = j["city"] or city
                _, how = db.upsert_job("boss", j)
                added += (how == "added")
                dup += (how != "added")
            log(f"BOSS 会话内 {city or ''}「{kw}」第{pg}页：{len(jobs)} 条"
                f"（新{added}/重{dup}）")
            time.sleep(3.5)
    return {"added": added, "dup": dup, "api": n}


# ---------------- 智联 / 猎聘（页面自己发请求，我拦响应） ----------------

def _auto_search(rt, platform, kw, timeout=20):
    """在搜索框输入关键词并回车。

    招聘站都是 SPA：goto 用 eager 策略返回时 DOM 还没渲染出搜索框。
    必须轮询等待，否则会误判「页面没有搜索框」。
    """
    from selenium.webdriver.common.keys import Keys
    css_list = {
        "zhaopin": ["input[placeholder*='搜索']", "input[name='kw']", "input"],
        "liepin": ["input[placeholder*='搜索']", "input[name='key']", "input"],
    }[platform]
    end = time.time() + timeout
    while time.time() < end:
        for css in css_list:
            try:
                for el in rt.find(css):
                    try:
                        if not el.is_displayed():
                            continue
                        el.click()
                        el.clear()
                        el.send_keys(kw)
                        el.send_keys(Keys.ENTER)
                        time.sleep(1.5)      # 等页面响应搜索
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        time.sleep(1)
    return False


def collect_via_page(rt, log, platform, combos, pages):
    """打开搜索页 → 输入关键词 → 拦截页面自己发出的 JSON → 解析入库。

    pages 只用于限制翻页次数；本轮只做首页（后续页用「下一页」按钮时会另算）。
    """
    from platforms.http_collect import _upsert_all
    added = dup = 0
    last_city = None
    done_kw = set()                      # 同城市内关键词去重，避免重复搜
    for kw, code, city in combos:
        if platform == "zhaopin" and code and code != last_city:
            # 城市变化 → 重新打开该城市搜索页（URL 参数 ?jl=<code> 控制城市）
            rt.goto(SEARCH_PAGE["zhaopin"].format(code=code))
            _settle(rt, platform, log)
            rt.perf_drain()
            last_city = code
            done_kw.clear()
        key = (code or "", kw)
        if key in done_kw:
            continue
        if not _auto_search(rt, platform, kw):
            log(f"{config.PLATFORM_NAMES[platform]}「{kw}」没找到搜索框，跳过")
            continue
        done_kw.add(key)
        rows = _capture_rows(rt, platform, kw, timeout=25)
        if not rows:
            log(f"{config.PLATFORM_NAMES[platform]}「{kw}」没拦到岗位数据"
                "（可能未登录或页面结构变化）")
            continue
        a, d = _upsert_all(rows, platform, log, city_hint=city)
        added += a
        dup += d
        log(f"{config.PLATFORM_NAMES[platform]} 会话内 {city or ''}「{kw}」："
            f"{len(rows)} 条（新{a}/重{d}）")
        time.sleep(4)
    return {"added": added, "dup": dup}


def _capture_rows(rt, platform, kw, timeout=25):
    """轮询性能日志，取页面发出的、含岗位字段的 JSON 响应。"""
    from platforms.http_collect import _find_rows
    seen, bodies = {}, {}
    end = time.time() + timeout
    while time.time() < end:
        for m, p in rt.perf_drain():
            if m == "Network.requestWillBeSent":
                u = (p.get("request") or {}).get("url") or ""
                if any(k in u.lower() for k in
                       ("search", "job", "position", "recommend", "sou")):
                    seen[p["requestId"]] = u
            elif m == "Network.loadingFinished" and p.get("requestId") in seen:
                try:
                    r = rt.d.execute_cdp_cmd(
                        "Network.getResponseBody", {"requestId": p["requestId"]})
                    b = r.get("body") or ""
                    if r.get("base64Encoded"):
                        import base64
                        b = base64.b64decode(b).decode("utf-8", "ignore")
                    if b:
                        bodies[p["requestId"]] = b
                except Exception:
                    pass
        for rid, b in bodies.items():
            if any(k in b for k in MARKERS[platform]):
                try:
                    rows = _find_rows(json.loads(b))
                except ValueError:
                    continue
                if rows:
                    return rows
        time.sleep(0.7)
    return []


# ---------------- 投递 ----------------

def boss_greet(rt, job, log):
    """页面内 POST friend/add.json（不点按钮）。返回 (ok, msg)。"""
    sid = job.get("security_id") or ""
    jid = job.get("ext_id") or ""
    if not sid or not jid:
        return False, "缺新鲜 securityId（链接过期），重新采集后再投"
    rt.goto(config.BOSS_JOBS)
    _settle(rt, "boss", log)
    body = f"securityId={quote(sid)}&jobId={quote(jid)}"
    if job.get("lid"):
        body += f"&lid={quote(str(job['lid']))}"
    st, data, txt = _xhr_post(
        rt, "https://www.zhipin.com/wapi/zpgeek/friend/add.json", body)
    if not isinstance(data, dict):
        bad = risk.scan(txt[:2000])
        if bad:
            risk.trip("boss", f"页面内投递+{bad}")
            raise Blocked(f"BOSS 风控信号：{bad}")
        return False, f"接口无 JSON（status={st}）"
    code = data.get("code")
    msg = str(data.get("message") or "")
    if code in (17, 37):
        time.sleep(4)
        st, data, txt = _xhr_post(
            rt, "https://www.zhipin.com/wapi/zpgeek/friend/add.json", body)
        if isinstance(data, dict):
            code, msg = data.get("code"), str(data.get("message") or "")
    if code in (9, 31, 32, 36):
        risk.trip("boss", f"页面内投递 code={code}")
        raise Blocked(f"BOSS 风控 code={code}，已冷却，任务停止")
    if code == 0 or msg in ("Success", "投递成功"):
        return True, "已发起沟通（会话内接口）"
    if "上限" in msg or "频繁" in msg:
        return False, f"沟通达上限：{msg[:40]}"
    if "已" in msg and ("沟通" in msg or "聊天" in msg):
        return False, "已沟通"
    return False, f"投递失败 code={code} {msg[:50]}"
