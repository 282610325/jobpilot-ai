"""智联招聘：搜索页卡片采集（__vue__.$props.job）+ 详情页投递。"""
import time
from urllib.parse import quote

import config
import db
import risk
from browser import Blocked


def _detail_url(number, job_id):
    n = str(number or "").strip()
    if n.startswith("CZ"):
        n = n[2:]
    return f"https://www.zhaopin.com/jobdetail/CC{n}J{job_id}.htm"


CARD_JS = """const cards = [...document.querySelectorAll('.job-card')];
return cards.map(c => {
  const j = c.__vue__ && c.__vue__.$props ? c.__vue__.$props.job : null;
  if (!j) return null;
  return {
    jobId: j.jobId, name: j.name, salary: j.salary60,
    edu: j.education, exp: j.workingExp,
    company: j.companyName, number: j.companyNumber,
    time: j.publishTime || j.firstPublishTime || '',
    area: (j.cardCustomJson && j.cardCustomJson.address) || '',
    tags: (j.jobSkillTags || []).join('/')
  };
}).filter(Boolean);"""


def collect(rt, log, combos, pages):
    added = dup = 0
    for kw, code, city in combos:
        for pg in range(1, pages + 1):
            url = f"https://www.zhaopin.com/jobs/?jl={code}&kw={quote(kw)}&p={pg}"
            log(f"智联采集 {city}/{kw} 第{pg}页")
            rt.goto(url)
            bad = risk.scan(rt.body())
            if bad:
                raise Blocked(f"智联风控信号：{bad}")
            ok, _ = rt.wait_login_state("zhaopin", 15)
            if not ok:
                raise Blocked("智联未登录")
            items = rt.js(CARD_JS) or []
            if not items:
                break
            for it in items:
                if not it.get("jobId"):
                    continue
                j = {"ext_id": str(it["jobId"]),
                     "url": _detail_url(it.get("number"), it["jobId"]),
                     "title": it.get("name"), "company": it.get("company"),
                     "salary": it.get("salary"), "city": city,
                     "area": (it.get("area") or "").strip(),
                     "edu": it.get("edu"), "exp": it.get("exp"),
                     "match": (it.get("tags") or "")[:80],
                     "publish": it.get("time") or ""}
                _, how = db.upsert_job("zhaopin", j)
                added += how == "added"
                dup += how == "exists"
            g = db.setting("rate_zhaopin_page_pause", None)
            lo, hi = (tuple(g) if g else config.RATE["zhaopin"]["page_pause"])
            time.sleep(__import__("random").uniform(lo, hi))
    return {"added": added, "dup": dup}


def read_jd(rt, url):
    rt.goto(url)
    time.sleep(1.2)
    body = rt.body()
    bad = risk.scan(body)
    if bad:
        raise Blocked(f"智联风控信号：{bad}")
    main = rt.js("const e=document.querySelector('.describtion-card');"
                 "return e?(e.innerText||'').trim():'';") or ""
    if len(main) < 60:
        return None, "JD 读取失败（页面结构可能变化）"
    return main, ""


def _visible_btn(rt, text):
    """页面上是否存在可见的指定按钮（隐藏的弹窗按钮不算）。"""
    for el in rt.find("button"):
        try:
            if el.is_displayed() and text in (el.text or ""):
                return True
        except Exception:
            continue
    return False


def apply(rt, url, log):
    """智联投递：立即投递 → 选择简历面板 → 投递简历。

    已投过判定看**可见的**「继续沟通」按钮：页面 body 里常驻着隐藏的
    沟通弹窗按钮，用文本判断会误判成已投。
    """
    rt.goto(url)
    time.sleep(1.2)
    body = rt.body()
    bad = risk.scan(body)
    if bad:
        raise Blocked(f"智联风控信号：{bad}")
    if _visible_btn(rt, "继续沟通"):
        return False, "已投过"
    if not rt.click_text("button", "立即投递", 6):
        return False, "找不到投递按钮（可能已投过或页面结构变化）"
    time.sleep(1.5)
    if not rt.wait_css(".a-attachment-select", 8):
        return False, "未弹出简历面板"
    els = rt.find("a.a-attachment-select__action-btn__delivery")
    if not els:
        return False, "找不到『投递简历』按钮"
    rt.click_el(els[0])
    time.sleep(2.5)
    body = rt.body()
    if _visible_btn(rt, "继续沟通") or "投递成功" in body:
        return True, "已投递"
    still_open = any(el.is_displayed() for el in rt.find(".a-attachment-select"))
    if not still_open:
        return True, "已投递"
    return False, "未确认（请人工核对）"
