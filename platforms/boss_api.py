"""BOSS 直聘：API 采集（joblist.json，页面内同步 XHR）+ 立即沟通投递。

要点（GitHub 方案共识）：
- 采集不滚动列表页，直接在已登录的 geek/jobs 页面上下文调 /wapi/zpgeek/search/joblist.json，
  返回明文 salaryDesc，每条岗位带新鲜 securityId/lid —— 完整链接当场入库。
- 投递/读 JD 必须用库里的 security_id 现拼 URL；跳回搜索页=链接过期，标失效，绝不回退搜索点击。
- 任何验证链/空白页信号 → risk.trip 冷却，整个任务立即终止。
"""
import json
import random
import time
from urllib.parse import quote

import config
import db
import risk
from browser import Blocked

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


def _api_params(query, city_code, page):
    p = f"?query={quote(query)}&page={page}&pageSize=30"
    if city_code:
        p += f"&city={city_code}"
    return p


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


def read_jd(rt, url):
    """带 token 直达详情页读 JD；被弹回（链接过期）返回 (None, 原因)。"""
    rt.goto(url)
    try:
        risk.guard_security(rt, 10)
    except risk.Cooldown:
        raise
    if "/job_detail/" not in (rt.d.current_url or ""):
        return None, "链接已过期（跳回" + ("搜索页" if "geek/jobs" in rt.d.current_url else "首页") + "）"
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
