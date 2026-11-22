"""采集：netio 纯 HTTP（BOSS 内置模板 / 智联·猎聘抓包模板回放），不启动浏览器。

字段映射：BOSS 复用 boss_api.parse_joblist；智联/猎聘用通用嗅探
（响应里第一份"岗位形状"的 dict 列表），映射失败会打印样本键帮助调参。
"""
import json
import random
import time

import api_tpl
import config
import db
import netio
from browser import Blocked
from platforms import boss_api

_TITLES = ("jobName", "title", "positionName", "postName", "jobTitle", "name")
_COMPS = ("companyName", "corpName", "brandName", "companyTitle",
          "simpleCompanyName", "compName")
_SALS = ("salary60Str", "salaryStr", "salary", "positionSalary", "salaryDesc",
         "monthlyIncome", "salaryRangeAndLimited", "salary60")
_IDS = ("jobId", "positionId", "positionNumber", "encryptJobId", "id", "code")
_URLS = ("jobDetailUrl", "detailUrl", "url", "href", "positionUrl", "link")
_CITY = ("cityName", "workCityName", "city", "dq", "locationName")
_EDU = ("jobDegree", "degreeName", "requireEduLevel", "eduLevel", "education")
_EXP = ("jobExperience", "experienceName", "requireWorkYears", "expYear", "workExp")
_TAGS = ("labels", "jobSkillsTags", "skillTags", "tags", "jobTag")
_TIME = ("publishTime", "refreshTime", "jobPublishTime", "publishTimeDesc")

_TLDOMAIN = {"zhaopin": "https://jobs.zhaopin.com/", "liepin": "https://www.liepin.com/"}


def _descramble(s):
    s = str(s or "")
    if any(config.PUA_BASE <= ord(c) <= config.PUA_BASE + 9 for c in s):
        return boss_api.decode_pua(s)
    return s


def _find_rows(obj, depth=0):
    """BFS 找第一份像岗位列表的 dict 数组。"""
    if depth > 6:
        return []
    if isinstance(obj, list):
        ds = [x for x in obj if isinstance(x, dict)]
        if len(ds) >= 2 and any(
                k in x for x in ds[:3] for k in _TITLES + ("job",)):
            return ds
        for x in obj:
            r = _find_rows(x, depth + 1)
            if r:
                return r
        return []
    if isinstance(obj, dict):
        for v in obj.values():
            r = _find_rows(v, depth + 1)
            if r:
                return r
    return []


def _pick(d, names):
    for k in names:
        v = d.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ""


def _tags_of(d):
    for k in _TAGS:
        v = d.get(k)
        if isinstance(v, list):
            out = []
            for t in v[:5]:
                if isinstance(t, dict):
                    out.append(str(t.get("name") or t.get("tagName") or ""))
                else:
                    out.append(str(t))
            return "、".join(x for x in out if x)
    return ""


def _row_to_job(row, platform, city_hint):
    """一行原始 JSON → 入库 dict（与 boss_api.parse_joblist 同契约）。"""
    if platform == "liepin" and ("job" in row or "comp" in row):
        job, comp = row.get("job") or {}, row.get("comp") or {}
        merged = dict(job)
        merged.update({k: v for k, v in comp.items() if k.startswith("comp")})
        row = merged
    jid = _pick(row, _IDS)
    title = _pick(row, _TITLES)
    if not title:
        return None
    url = _pick(row, _URLS)
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = (_TLDOMAIN.get(platform, "").rstrip("/") or "https://www.liepin.com") + url
    if not url and jid:
        if platform == "liepin":
            url = f"https://www.liepin.com/job/{jid}.shtml"
        else:
            url = f"https://jobs.zhaopin.com/{jid}.htm"
    if not url:
        return None
    return {"ext_id": jid, "url": url, "security_id": "", "lid": "",
            "title": title, "company": _pick(row, _COMPS),
            "salary": _descramble(_pick(row, _SALS)),
            "city": _pick(row, _CITY) or city_hint or "",
            "area": "", "edu": _pick(row, _EDU), "exp": _pick(row, _EXP),
            "match": _tags_of(row), "publish": _pick(row, _TIME)}


def _upsert_all(rows, platform, log, city_hint):
    added = dup = lost = 0
    for r in rows:
        j = _row_to_job(r, platform, city_hint)
        if not j:
            lost += 1
            continue
        _, how = db.upsert_job(platform, j)
        added += (how == "added")
        dup += (how != "added")
    if lost:
        log(f"{config.PLATFORM_NAMES[platform]} {lost} 行没解析出标题/链接"
            "（模板字段映射需调整）")
    return added, dup


def _tpl_headers(tpl):
    h = dict(tpl.get("headers") or {})
    h.pop("referer", None)
    return h


def collect_http(platform, log, combos, pages):
    """combos: [(keyword, city_code_or_None, city_name)]。返回 {"added","dup"}。"""
    if platform == "boss":
        return _collect_boss(log, combos, pages)
    tpl = api_tpl.get(platform)
    if not tpl:
        raise NoTpl(f"{config.PLATFORM_NAMES[platform]} 还没有 HTTP 模板："
                    "请先到设置页「抓包」一次")
    added = dup = 0
    referer = {"zhaopin": "https://www.zhaopin.com/",
               "liepin": "https://www.liepin.com/"}[platform]
    for kw, code, city in combos:
        for pg in range(1, max(1, int(pages)) + 1):
            url, body = api_tpl.render(tpl, kwd=kw, page=pg, city_code=code)
            kw_body = None
            form_body = None
            if body:
                try:
                    kw_body = json.loads(body)
                except ValueError:
                    form_body = body
            st, data = netio.request(
                platform, tpl["method"], url,
                json_body=kw_body, form=None if kw_body is not None else form_body,
                referer=referer, extra_headers=_tpl_headers(tpl))
            if not isinstance(data, dict):
                raise Blocked(f"{config.PLATFORM_NAMES[platform]} 接口没返回 JSON"
                              f"（status={st}），模板可能失效，请重新抓包")
            rows = _find_rows(data)
            if not rows:
                sample = json.dumps(data, ensure_ascii=False)[:260]
                log(f"{config.PLATFORM_NAMES[platform]} 响应里没找到岗位列表，"
                    f"样本：{sample}")
                break
            a, d = _upsert_all(rows, platform, log, city_hint=city)
            added += a
            dup += d
            log(f"{config.PLATFORM_NAMES[platform]} HTTP 「{kw}」第{pg}页："
                f"{len(rows)} 条（新{a}/重{d}）")
            if len(rows) < 5:
                break
    return {"added": added, "dup": dup}


class NoTpl(Exception):
    """缺模板：可跳过该平台继续，不算风控。"""


def _collect_boss(log, combos, pages):
    tpl = api_tpl.get("boss")
    added = dup = 0
    n = 0
    cap = int(db.setting("rate_boss_daily_api", config.RATE["boss"]["daily_api"]))
    for kw, code, city in combos:
        if n >= cap:
            log(f"BOSS 今日 API 配额用尽（{cap}），停止")
            break
        for pg in range(1, max(1, int(pages)) + 1):
            if n >= cap:
                break
            params = {"query": kw, "page": pg, "pageSize": 30}
            if code:
                params["city"] = code
            st, data = netio.request(
                "boss", "GET", tpl["url"], params=params,
                referer=config.BOSS_JOBS,
                extra_headers=netio.site_headers("boss"))
            n += 1
            db.quota_add("boss", "api")
            jobs = boss_api.parse_joblist(data if isinstance(data, dict) else {})
            if not jobs:
                log(f"BOSS HTTP 「{kw}」第{pg}页 0 条（Cookie 可能只够看首页）")
                break
            for j in jobs:
                j["city"] = j["city"] or city
                _, how = db.upsert_job("boss", j)
                added += (how == "added")
                dup += (how != "added")
            log(f"BOSS HTTP {city}「{kw}」第{pg}页：{len(jobs)} 条（新{added}/重{dup}）")
            gap_lo, gap_hi = config.RATE["boss"]["api_gap"]
            g = db.setting("rate_boss_api_gap", None)
            if g:
                gap_lo, gap_hi = tuple(g)
            time.sleep(random.uniform(gap_lo, gap_hi))
    return {"added": added, "dup": dup, "api": n}
