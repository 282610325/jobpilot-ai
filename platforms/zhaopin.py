"""智联招聘：搜索页卡片采集（__vue__.$props.job）+ 详情页投递。"""
import re
import time
from urllib.parse import quote

import config
import db
import risk
from browser import Blocked


RECOMMEND_URL = "https://www.zhaopin.com/jobs/?pageMode=recommend"

REC_CARD_JS = """
const cs=[...document.querySelectorAll('div.job-card')];
return cs.map(c=>{
  const t=c.querySelector('.job-card__title-clamp,.job-card__title');
  const s=c.querySelector('.job-card__salary');
  const comp=c.querySelector('.job-card__company');
  const tags=[...c.querySelectorAll('.job-card__skill-tag')]
    .map(x=>(x.innerText||'').trim()).filter(Boolean);
  return {title:t?(t.innerText||'').trim():'',
          salary:s?(s.innerText||'').trim():'',
          company:comp?(comp.innerText||'').replace(/\\s+/g,' ').trim().slice(0,80):'',
          tags:tags};
});
"""

REC_LINK_JS = """
const ls=[...document.querySelectorAll('a[href*="jobdetail"]')];
return ls.length ? ls[ls.length-1].href : '';
"""


def _parse_detail_url(url):
    """详情 URL → (公司编号, 岗位 ID)。"""
    m = re.search(r"/jobdetail/(C[A-Z]*\d+)J(\d+)\.htm", str(url or ""))
    return (m.group(1), m.group(2)) if m else (None, None)


def collect_recommend(rt, log, cities, pages=1):
    """按账号求职意向取智联「职位 → 推荐」。

    该页只给 3 条、卡片本身没有岗位链接：逐张点开，从右侧详情面板取链接后入库。
    """
    rt.goto(RECOMMEND_URL)
    bad = risk.scan(rt.body())
    if bad:
        raise Blocked(f"智联风控信号：{bad}")
    ok, _ = rt.wait_login_state("zhaopin", 15)
    if not ok:
        raise Blocked("智联未登录")
    rows = rt.js(REC_CARD_JS) or []
    if not rows:
        log("智联推荐页没有卡片（可能未登录或页面结构变化）")
        return {"added": 0, "dup": 0}
    log(f"智联推荐页 {len(rows)} 条（按账号求职意向）")
    added = dup = 0
    prev = ""
    for i, it in enumerate(rows):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        try:
            rt.js("const i=arguments[0];"
                  "const cs=[...document.querySelectorAll('div.job-card')];"
                  "if(cs[i]){cs[i].click();return true;}return false;", i)
        except Exception:
            continue
        url = ""
        for _ in range(10):                    # 等右侧面板换成这条岗位的链接
            time.sleep(0.6)
            try:
                url = rt.js(REC_LINK_JS) or ""
            except Exception:
                url = ""
            if url and url != prev:
                break
        _, jid = _parse_detail_url(url)
        if not jid or url == prev:
            log(f"  跳过「{title}」：没拿到岗位链接")
            continue
        prev = url
        tags = [t for t in (it.get("tags") or []) if t]
        edu = next((t for t in tags
                    if any(k in t for k in ("本科", "大专", "硕士", "博士", "学历"))), "")
        exp = next((t for t in tags if "年" in t and "薪" not in t), "")
        text = " ".join([title, it.get("company") or ""] + tags)
        city = next((c for c in (cities or []) if c in text), "")
        job = {"ext_id": jid, "url": url.split("&data_identity")[0],
               "title": title, "company": it.get("company") or "",
               "salary": it.get("salary") or "", "city": city, "area": "",
               "edu": edu, "exp": exp,
               "match": "、".join([t for t in tags if t not in (edu, exp)][:4]),
               "publish": ""}
        _, how = db.upsert_job("zhaopin", job)
        added += how == "added"
        dup += how == "exists"
    return {"added": added, "dup": dup}


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
