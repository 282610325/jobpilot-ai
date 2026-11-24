"""猎聘：c.liepin.com 推荐流采集（懒加载需边滚边等）+ 投递。

GitHub 共识（get_jobs）：猎聘【打招呼】无上限、主动发消息才有上限——
所以猎聘动作只点"打招呼/投简历"入口，绝不输入追发消息（硬编码，不可配）。
"""
import random
import re
import time

import config
import db
import risk
from browser import Blocked
from . import dead_text

CARD_JS = """
const cards = [...document.querySelectorAll('.jobCardPcContainer')];
return cards.map(c => {
  const a = c.querySelector('a[href*=".shtml"]');
  if (!a) return null;
  return { href: a.href, lines: (c.innerText || '').split('\\n').map(x=>x.trim()).filter(Boolean) };
});
"""


def _parse(lines):
    title = lines[0] if lines else ""
    city = salary = exp = edu = ""
    for ln in lines[1:6]:
        if not city and any(c in ln for c in ("市", "珠海", "中山", "广州", "深圳", "东莞")) and len(ln) <= 8:
            city = ln
        elif not salary and ("K" in ln.upper() or "万" in ln or "-" in ln):
            salary = ln
        elif not exp and ("年" in ln or "经验" in ln):
            exp = ln
        elif not edu and any(k in ln for k in ("本科", "大专", "硕士", "学历")):
            edu = ln
    return title, city, salary, exp, edu


def collect(rt, log, cities, pages):
    out_new = out_dup = 0
    seen = set()
    for pg in range(1, pages + 1):
        log(f"猎聘推荐流 第{pg}屏")
        rt.goto("https://c.liepin.com/")
        bad = risk.scan(rt.body())
        if bad:
            raise Blocked(f"猎聘风控信号：{bad}")
        ok, _ = rt.wait_login_state("liepin", 12)
        if not ok:
            raise Blocked("猎聘未登录")
        items = []
        end = time.time() + 18
        while time.time() < end:
            rt.js("window.scrollBy(0, 1200);")
            time.sleep(random.uniform(1.8, 2.6))
            items = rt.js(CARD_JS) or []
            if items:
                break
        if not items:
            if pg == 1:
                raise Blocked("猎聘推荐流为空（结构可能变化或加载太慢）")
            break
        for it in items:
            href = it.get("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)
            title, city, salary, exp, edu = _parse(it.get("lines") or [])
            if not title:
                continue
            if cities and city and not any(c in city for c in cities):
                continue
            _, how = db.upsert_job("liepin", {
                "url": href, "title": title, "company": "", "salary": salary,
                "city": city, "edu": edu, "exp": exp, "match": "", "publish": ""})
            out_new += how == "added"
            out_dup += how == "exists"
        g = db.setting("rate_liepin_page_pause", None)
        lo, hi = (tuple(g) if g else config.RATE["liepin"]["page_pause"])
        time.sleep(random.uniform(lo, hi))
    return {"added": out_new, "dup": out_dup}


def collect_recommend(rt, log, cities, pages=1):
    """按账号求职意向取首页推荐流（猎聘推荐流本身就是意向驱动）。"""
    return collect(rt, log, cities or [], max(1, min(2, int(pages or 1))))


_JOB_ID_RE = re.compile(r"/(?:a|job)/(\d+)\.shtml")


def read_jd(rt, url):
    rt.goto(url)
    m = _JOB_ID_RE.search(str(url or ""))
    job_id = m.group(1) if m else ""
    main = ""
    end = time.time() + 12
    while time.time() < end:
        cur = rt.d.current_url or ""
        if job_id and f"/{job_id}.shtml" not in cur:
            return None, "已关闭（跳转到其他页面）"
        main = rt.js("const e=document.querySelector('.job-intro-container,.job-description');"
                     "return e?(e.innerText||'').trim():'';") or ""
        if len(main) >= 60:
            break
        dead = dead_text(rt.body())
        if dead:
            return None, f"已关闭（{dead}）"
        time.sleep(0.8)
    bad = risk.scan(rt.body())
    if bad:
        raise Blocked(f"猎聘风控信号：{bad}")
    if len(main) < 60:
        return None, "JD 读取失败"
    return main, ""


def _visible_text(rt, txt):
    """页面上是否存在**可见**的指定文本（按钮/链接级别）。"""
    try:
        return bool(rt.js(
            "const t=arguments[0];"
            "return [...document.querySelectorAll('button,a,div,span')].some(e=>{"
            "  const s=(e.innerText||'').trim(), r=e.getBoundingClientRect();"
            "  return s===t && r.width>20 && r.height>10;});", txt))
    except Exception:
        return False


def _visible_contains(rt, txt):
    """可见区域中是否包含某段文字（用于识别「投递成功」提示）。"""
    try:
        return bool(rt.js(
            "const t=arguments[0];"
            "return [...document.querySelectorAll('button,a,div,span')].some(e=>{"
            "  const s=(e.innerText||''), r=e.getBoundingClientRect();"
            "  return s.includes(t) && r.width>20 && r.height>10;});", txt))
    except Exception:
        return False


def apply(rt, url, log):
    """猎聘投递：优先投简历；没有投递入口的直聊岗点「聊一聊」发默认招呼。

    猎聘【打招呼】无上限、主动追发消息才有上限——所以只点官方入口，
    绝不自己输入消息（硬编码，不可配）。
    返回 (ok, msg)。
    """
    rt.goto(url)
    time.sleep(1.5)
    bad = risk.scan(rt.body())
    if bad:
        raise Blocked(f"猎聘风控信号：{bad}")
    if _visible_text(rt, "继续聊"):
        return False, "已沟通"
    if rt.click_text("a.btn-minor", "投简历", 5) or \
            rt.click_text("button", "投简历", 2):
        end = time.time() + 8
        while time.time() < end:               # 等「选择附件简历」弹窗渲染
            if _visible_text(rt, "立即投递"):
                break
            time.sleep(0.6)
        if not _visible_text(rt, "立即投递"):
            return False, "弹窗里找不到『立即投递』"
        if not rt.click_text("button", "立即投递", 5):
            return False, "点击『立即投递』失败"
        time.sleep(2.5)
        if "apply/success" in (rt.d.current_url or "") or \
                _visible_contains(rt, "投递成功") or _visible_contains(rt, "已投递"):
            return True, "已投递"
        return False, "投递未确认（请人工核对）"
    if _visible_text(rt, "聊一聊"):
        target = rt.find(".apply-box .btn-main") or rt.find(".btn-main")
        if not target:
            return False, "找不到可点击的『聊一聊』"
        rt.click_el(target[0])
        end = time.time() + 8
        while time.time() < end:
            if _visible_text(rt, "继续聊"):
                return True, "已发起沟通（聊一聊默认招呼）"
            time.sleep(0.8)
        return False, "点了聊一聊，未确认沟通"
    return False, "无投递/打招呼入口（猎聘可能仅支持聊天）"
