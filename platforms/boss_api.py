"""BOSS 直聘：API 采集（joblist.json，页面内同步 XHR）+ 立即沟通投递。

要点（GitHub 方案共识）：
- 采集不滚动列表页，直接在已登录的 geek/jobs 页面上下文调 /wapi/zpgeek/search/joblist.json，
  返回明文 salaryDesc，每条岗位带新鲜 securityId/lid —— 完整链接当场入库。
- 投递/读 JD 必须用库里的 security_id 现拼 URL；跳回搜索页=链接过期，标失效，绝不回退搜索点击。
- 任何验证链/空白页信号 → risk.trip 冷却，整个任务立即终止。
"""
import json
import random
import re
import time
from urllib.parse import quote

import config
import db
import risk
from browser import Blocked
from . import dead_text

CARD_SELECTORS = "li.job-card-box, .job-card-wrap, .job-list-box li"

LIST_JS = f"""
const cs=[...document.querySelectorAll('{CARD_SELECTORS}')];
return cs.map(c=>{{const a=c.querySelector('a[href*="/job_detail/"]');
  if(!a) return null;
  return {{href:a.href, txt:(c.innerText||'').slice(0,240)}};}}).filter(Boolean);
"""


def decode_pua(s):
    """字体反爬兜底：PUA 私有区 0xE031..0xE03A → '0'..'9'。"""
    return "".join(chr(ord(c) - config.PUA_BASE + ord("0"))
                   if config.PUA_BASE <= ord(c) <= config.PUA_BASE + 9 else c
                   for c in str(s or ""))


RECOMMEND_API = "/wapi/zpgeek/pc/recommend/job/list.json"


def _api_params(query, city_code, page):
    p = f"?query={quote(query)}&page={page}&pageSize=30"
    if city_code:
        p += f"&city={city_code}"
    return p


def _recommend_params(city_code, page, expect_id=""):
    """求职意向推荐接口参数：不传关键词，由账号里填的意向决定推荐内容。"""
    p = f"?page={page}&pageSize=15"
    if city_code:
        p += f"&city={city_code}"
    p += f"&encryptExpectId={quote(expect_id or '', safe='~_')}"
    p += ("&mixExpectType=&expectInfo=&jobType=&salary="
          "&experience=&degree=&industry=&scale=")
    p += f"&_={int(time.time() * 1000)}"
    return p


EXPECT_ITEM_JS = """
return [...document.querySelectorAll('.expect-item')]
  .map(e=>(e.innerText||'').replace(/\\s+/g,' ').trim()).filter(Boolean);
"""

CLICK_EXPECT_JS = """
const name=arguments[0];
const it=[...document.querySelectorAll('.expect-item')]
  .find(e=>(e.innerText||'').replace(/\\s+/g,' ').trim()===name);
if(!it) return false;
it.click(); return true;
"""


def _drain_urls(rt):
    out = []
    for m, p in rt.perf_drain():
        if m != "Network.requestWillBeSent":
            continue
        u = ((p.get("request") or {}).get("url") or "")
        if u:
            out.append(u)
    return out


def fetch_expects(rt, log):
    """读取账号求职意向，并逐个点击取得 encryptExpectId。

    页面 DOM 不暴露意向 ID，只有点击后站内推荐请求的参数里才带；
    逐个点一遍即可把「意向名 → 加密 ID」对应起来。
    """
    names = rt.js(EXPECT_ITEM_JS) or []
    if not names:
        log("BOSS 页面没有读到求职意向（账号未设置或页面结构变化）")
        return []
    out = []
    for name in names:
        try:
            rt.perf_drain()
            if not rt.js(CLICK_EXPECT_JS, name):
                continue
            time.sleep(random.uniform(1.2, 2.2))
            eid = ""
            for _ in range(8):
                for u in _drain_urls(rt):
                    m = re.search(r"encryptExpectId=([^&]+)", u)
                    if m and m.group(1):
                        eid = m.group(1)
                        break
                if eid:
                    break
                time.sleep(0.7)
        except Blocked:
            raise
        except Exception:
            eid = ""
        if eid:
            out.append({"name": name, "encrypt_id": eid})
        else:
            log(f"BOSS 意向「{name}」没取到加密 ID，跳过该意向")
        time.sleep(random.uniform(1.2, 2.0))
    return out


def parse_joblist(data):
    """joblist.json 响应 → 岗位 dict 列表（含 securityId/lid/完整链接）。"""
    out = []
    zps = []
    try:
        d = data or {}
        zps = (d.get("zpData") or {}).get("jobList") \
              or d.get("jobList") \
              or (d.get("zpgeekJobListResult") or {}).get("jobList") or []
    except AttributeError:
        zps = []
    for it in zps:
        # 2026-11 起 BOSS 搜索结果不再返回明文的 jobId，改用 encryptJobId；
        # 详情页/沟通接口都用这个加密 id，二者等价可直接当 ext_id。
        jid = it.get("jobId") or it.get("encryptJobId") or ""
        sid = it.get("securityId") or ""
        lid = it.get("lid") or ""
        if not jid or not sid:
            continue
        url = (f"https://www.zhipin.com/job_detail/{jid}.html"
               f"?securityId={quote(sid)}")
        if lid:
            url += f"&lid={quote(str(lid))}"
        salary = it.get("salaryDesc") or ""
        out.append({
            "ext_id": str(jid), "url": url, "security_id": sid, "lid": str(lid),
            "title": it.get("jobName") or "", "company": it.get("brandName") or "",
            "salary": salary, "city": it.get("cityName") or "",
            "area": it.get("areaDistrict") or "",
            "edu": it.get("jobDegree") or "",
            "exp": it.get("jobExperience") or "",
            "match": "、".join(map(str, ((it.get("jobLabels") or [])
                                        + (it.get("skills") or []))[:4])),
            "publish": "",
        })
    return out


def _ensure_jobs_page(rt, log):
    rt.goto(config.BOSS_JOBS)
    try:
        risk.guard_security(rt, 10)
    except risk.Cooldown:
        raise
    rt.switch_to_url("zhipin.com")
    ok, head = rt.wait_login_state("boss", 15)
    if not ok:
        raise Blocked("BOSS 未登录（头部未见账号名）")
    return ok


def collect(rt, log, combos, pages):
    """combos: [(keyword, city_code, city_name)]；pages: 每组合页数。
    返回新增条数统计 dict。风控信号直接抛 Blocked/Cooldown 由任务层终止。"""
    _ensure_jobs_page(rt, log)
    added = dup = 0
    cap = int(db.setting("rate_boss_daily_api", config.RATE["boss"]["daily_api"]))
    n_api = 0
    for kw, code, city in combos:
        if n_api >= cap:
            log(f"BOSS 今日 API 配额用尽（{cap}），停止采集")
            break
        for pg in range(1, pages + 1):
            if n_api >= cap:
                break
            params = _api_params(kw, code, pg)
            data = rt.js_xhr(config.BOSS_API, params)
            n_api += 1
            db.quota_add("boss", "api")
            if data is None:
                bad = risk.scan(rt.body())
                if bad:
                    risk.trip("boss", f"API 失败+{bad}")
                    raise Blocked(f"BOSS 风控信号：{bad}")
                break
            jobs = parse_joblist(data)
            if not jobs:
                break
            for j in jobs:
                j["city"] = j["city"] or city
                _, how = db.upsert_job("boss", j)
                if how == "added":
                    added += 1
                else:
                    dup += 1
            log(f"BOSS API {city}/{kw} 第{pg}页：{len(jobs)} 条")
            gap = db.setting("rate_boss_api_gap", None)
            lo, hi = (tuple(gap) if gap else config.RATE["boss"]["api_gap"])
            time.sleep(random.uniform(lo, hi))
        time.sleep(random.uniform(4, 8))
    return {"added": added, "dup": dup, "api": n_api}


def collect_recommend(rt, log, cities, pages=1, should_stop=None):
    """按账号求职意向 × 城市取平台推荐（不传关键词），直接入岗位库。

    cities: [(城市名, BOSS 城市码)]；城市码为空则按账号当前城市。
    should_stop: 可选回调，返回 True 时在本次请求之间停下（协作式中断）。
    """
    _ensure_jobs_page(rt, log)
    time.sleep(random.uniform(1.5, 2.5))      # 页面令牌刚刷新时接口易返回空
    expects = fetch_expects(rt, log)
    if expects:
        log("BOSS 求职意向：" + "、".join(e["name"] for e in expects))
    else:
        expects = [{"name": "账号默认意向", "encrypt_id": ""}]
    added = dup = n_api = 0
    cap = int(db.setting("rate_boss_daily_api", config.RATE["boss"]["daily_api"]))
    for exp in expects:
        for name, code in (cities or [("", "")]):
            for pg in range(1, max(1, int(pages or 1)) + 1):
                if should_stop and should_stop():
                    log("BOSS 推荐采集已中断")
                    return {"added": added, "dup": dup, "api": n_api}
                if n_api >= cap:
                    log(f"BOSS 今日 API 配额用尽（{cap}），停止推荐采集")
                    return {"added": added, "dup": dup, "api": n_api}
                data = rt.js_xhr(RECOMMEND_API,
                                 _recommend_params(code, pg, exp["encrypt_id"]))
                n_api += 1
                db.quota_add("boss", "api")
                if not parse_joblist(data) and n_api < cap:
                    log(f"BOSS 推荐 [{exp['name']}] {name or '默认城市'} 第{pg}页"
                        f"无数据（code={(data or {}).get('code')}），等待后重试一次")
                    time.sleep(random.uniform(2.5, 4))
                    data = rt.js_xhr(RECOMMEND_API,
                                     _recommend_params(code, pg, exp["encrypt_id"]))
                    n_api += 1
                    db.quota_add("boss", "api")
                jobs = parse_joblist(data)
                if not jobs:
                    log(f"BOSS 推荐 [{exp['name']}] {name or '默认城市'} "
                        f"第{pg}页：无数据")
                    break
                for j in jobs:
                    j["city"] = j["city"] or name
                    _, how = db.upsert_job("boss", j)
                    added += how == "added"
                    dup += how == "exists"
                log(f"BOSS 推荐 [{exp['name']}] {name or '默认城市'} "
                    f"第{pg}页：{len(jobs)} 条")
                if ((data or {}).get("zpData") or {}).get("hasMore") is False:
                    break
                gap = db.setting("rate_boss_api_gap", None)
                lo, hi = (tuple(gap) if gap else config.RATE["boss"]["api_gap"])
                time.sleep(random.uniform(lo, hi))
            time.sleep(random.uniform(3, 6))
    return {"added": added, "dup": dup, "api": n_api}


def detail_url(job):
    """从库里存的记录拼详情页 URL（必须带新鲜 securityId）。"""
    if not job or not job.get("ext_id"):
        return ""
    url = job.get("url") or ""
    if "securityId=" in url:
        return url
    sid = job.get("security_id") or ""
    if not sid:
        return ""
    u = f"https://www.zhipin.com/job_detail/{job['ext_id']}.html?securityId={quote(sid)}"
    if job.get("lid"):
        u += f"&lid={quote(str(job['lid']))}"
    return u


_JOB_PATH_RE = re.compile(r"/job_detail/([^/.?]+)\.html")


def read_jd(rt, url):
    """带 token 直达详情页读 JD；被弹回（链接过期）返回 (None, 原因)。"""
    rt.goto(url)
    try:
        risk.guard_security(rt, 10)
    except risk.Cooldown:
        raise
    m = _JOB_PATH_RE.search(str(url or ""))
    jid = m.group(1) if m else ""
    cur = rt.d.current_url or ""
    wrong = (f"/job_detail/{jid}.html" not in cur) if jid \
        else ("/job_detail/" not in cur)
    if wrong:                                   # 失效链接会跳回列表或别的岗位
        back = "搜索页" if "geek/jobs" in cur else "首页"
        return None, f"链接已过期（跳回{back}）"
    main = ""
    end = time.time() + 12
    while time.time() < end:
        try:
            main = (rt.js("const e=document.querySelector('.job-sec-text');"
                          "return e?(e.innerText||'').trim():'';") or "").strip()
        except Exception:
            main = ""
        if len(main) >= 60:
            break
        dead = dead_text(rt.body())
        if dead:
            return None, f"已关闭（{dead}）"
        time.sleep(0.8)
    body = rt.body()
    bad = risk.scan(body)
    if bad:
        raise Blocked(f"BOSS 风控信号：{bad}")
    if not main or len(main) < 60:
        return None, "详情页正文没渲染出来"
    return main, ""


def apply_greet(rt, url, log):
    """投递=立即沟通。成功后确认按钮变「继续沟通」。"""
    rt.goto(url)
    try:
        risk.guard_security(rt, 10)
    except risk.Cooldown:
        raise
    if "/job_detail/" not in (rt.d.current_url or ""):
        return False, "链接过期，跳回了列表"
    time.sleep(random.uniform(1.5, 2.8))
    body = rt.body()
    bad = risk.scan(body)
    if bad:
        raise Blocked(f"BOSS 风控信号：{bad}")
    if "继续沟通" in body and "立即沟通" not in body:
        return False, "已沟通"
    if not rt.click_text("a.btn-startchat", "立即沟通", 6):
        if not rt.click_text("button", "立即沟通", 3):
            return False, "找不到立即沟通按钮"
    time.sleep(random.uniform(2.0, 3.5))
    rt.goto(url)
    if "继续沟通" in (rt.body() or ""):
        return True, "已发起沟通"
    return False, "未确认（请人工核对）"


def mark_unfit(rt, url, log):
    """推荐流清洗：详情页点「不合适」（机制C，默认关闭时才可能被调到）。"""
    rt.goto(url)
    if "/job_detail/" not in (rt.d.current_url or ""):
        return False
    return rt.click_text("button, .btn-reject, .op-btn", "不合适", 4)
