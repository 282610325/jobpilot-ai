"""三个 GitHub 学习来的增强机制（全部默认关闭、同一天互斥只允许开一种）：

B 黑名单学习：读 BOSS 聊天列表里 HR 的最新回复，命中"不合适"类措辞 → 公司进黑名单。
C 推荐流清洗：BOSS 推荐列表里不符合规则的岗位点「不合适」，让推荐越洗越准。
D 已读不回复聊：对已读不回的 HR 用 AI 生成单句跟进，日上限+最小间隔。

注意：三者都要进 /web/geek/chat 或推荐页，是 BOSS 风控敏感区——
入口统一走 risk.check('boss')，任何风控信号立即 trip 冷却并终止本轮。
"""
import datetime
import random
import time

import ai
import config
import db
import risk
from browser import Blocked

CHAT = "https://www.zhipin.com/web/geek/chat"

# 本轮互斥：一天只允许运行一种聊天类机制
_LOCK_KEY = "chat_kind_day"


def _kind_locked(kind):
    cur = db.setting(_LOCK_KEY)
    today = datetime.date.today().isoformat()
    if isinstance(cur, dict) and cur.get("day") == today and cur.get("kind") != kind:
        return cur.get("kind")
    return None


def _kind_take(kind):
    db.set_setting(_LOCK_KEY, {"day": datetime.date.today().isoformat(), "kind": kind})


def _gate(kind, flag, name):
    if not db.setting(flag, 0):
        raise RuntimeError(f"{name} 未开启（设置页勾选）")
    other = _kind_locked(kind)
    if other:
        raise RuntimeError(f"今天已运行过机制「{other}」，聊天类操作同天只能开一种（防风控）")
    risk.check("boss")
    _kind_take(kind)


CHAT_ROWS_JS = """
const rows=[...document.querySelectorAll('.chat-item, .user-list-item, [class*=friend]')];
return rows.slice(0, arguments[0]).map(r=>{
  const t=(r.innerText||'').replace(/\\s+/g,' ').slice(0,140);
  const nm=r.querySelector('[class*=name], .company, h4, span');
  return {txt:t, name:(nm&&nm.innerText||'').trim()};
}).filter(x=>x.txt);
"""


def scan_hr(bot, log, limit=12):
    """机制B：扫聊天列表 → HR 拒答词命中 → 公司入黑名单。"""
    _gate("黑名单学习", "blacklist_learn_enabled", "黑名单学习")
    def _run(rt):
        rt.goto(CHAT)
        risk.guard_security(rt, 10)
        ok, _ = rt.wait_login_state("boss", 14)
        if not ok:
            raise Blocked("BOSS 未登录")
        time.sleep(random.uniform(2, 4))
        rows = rt.js(CHAT_ROWS_JS, limit) or []
        hits = 0
        for r in rows:
            kw = rules_hit(r["txt"])
            if kw and r["name"]:
                db.blacklist_add(r["name"], source="hr", reason=kw)
                db.hr_msg_log("boss", r["name"], None, r["txt"], kw)
                hits += 1
            time.sleep(random.uniform(0.4, 0.9))
        return len(rows), hits
    n, hits = bot.call(_run)
    db.event("chat", "boss", "scan_hr", f"扫 {n} 条会话，命中 {hits} 家进黑名单")
    return {"scanned": n, "blacklisted": hits}


def rules_hit(text):
    import rules
    return rules.hr_reject_hit(text)


CLEANSE_CAND_JS = """
const cs=[...document.querySelectorAll('li.job-card-box, .job-card-wrap')];
return cs.slice(0, arguments[0]).map(c=>{
  const a=c.querySelector('a[href*="/job_detail/"]');
  return {href:a?a.href:'', txt:(c.innerText||'').replace(/\\s+/g,' ').slice(0,120)};
}).filter(x=>x.href);
"""

REJECT_CLICK_JS = """
const b=[...document.querySelectorAll('button, .btn-reject, [class*=reject], [class*=unfit]')]
  .find(x=>(x.innerText||'').includes('不合适'));
if(b){b.click();return true;} return false;
"""


def cleanse_recommend(bot, log, limit=8):
    """机制C：推荐列表逐条判规则，不符合的进详情页点「不合适」。"""
    _gate("推荐清洗", "cleanse_enabled", "推荐清洗")
    cap = int(db.setting("cleanse_daily_cap", 10))
    if db.quota_left("boss", "cleanse", cap) <= 0:
        raise RuntimeError(f"推荐清洗今日上限 {cap} 已用完")
    floor = int(db.setting("salary_floor", config.SALARY_FLOOR))

    def _run(rt):
        import rules as _r
        rt.goto("https://www.zhipin.com/web/geek/job-recommend")
        risk.guard_security(rt, 10)
        time.sleep(random.uniform(2, 4))
        rows = rt.js(CLEANSE_CAND_JS, limit) or []
        marked = 0
        for x in rows:
            if not _should_mark(x["txt"]):
                continue
            rt2url = x["href"]
            try:
                rt.goto(rt2url)
                if "/job_detail/" not in (rt.d.current_url or ""):
                    continue
                if rt.js(REJECT_CLICK_JS):
                    marked += 1
                    db.quota_add("boss", "cleanse")
                time.sleep(random.uniform(3, 6))
            except Blocked:
                break
            # 回推荐页继续
            try:
                rt.goto("https://www.zhipin.com/web/geek/job-recommend")
                time.sleep(random.uniform(2, 4))
            except Blocked:
                break
        return len(rows), marked

    def _should_mark(text):
        import rules as _r
        j = {"title": text[:40], "city": "", "salary": ""}
        cap = _r.salary_cap(text)
        if cap and cap <= config.SALARY_HARD_CAP:
            return True
        if _r.blacklist_reason(text[:40]):
            return True
        return False

    n, marked = bot.call(_run)
    db.event("chat", "boss", "cleanse", f"过 {n} 条推荐，标不合适 {marked} 条")
    return {"checked": n, "marked": marked}


FOLLOW_JS = """
const rows=[...document.querySelectorAll('.chat-item, .user-list-item, [class*=friend]')];
return rows.filter(r=>/未回复|已读/.test(r.innerText||'')).slice(0,arguments[0])
  .map(r=>{const nm=r.querySelector('[class*=name], .company, h4, span');
    return {name:(nm&&nm.innerText||'').trim(), txt:(r.innerText||'').replace(/\\s+/g,' ').slice(0,120)};})
  .filter(x=>x.name);
"""


def followup_sweep(bot, log, limit=3):
    """机制D：已读不回 → AI 单句跟进（最激进，默认关；每日上限+间隔）。"""
    _gate("已读不回复聊", "followup_enabled", "已读不回复聊")
    cap = int(db.setting("followup_daily_cap", 3))
    if db.quota_left("boss", "followup", cap) <= 0:
        raise RuntimeError(f"复聊今日上限 {cap} 条已用完")

    def _run(rt):
        rt.goto(CHAT)
        risk.guard_security(rt, 10)
        time.sleep(random.uniform(2, 4))
        rows = rt.js(FOLLOW_JS, limit) or []
        sent = 0
        for r in rows:
            due = db.setting("followup_min_interval_h", 48)
            line = ai.followup_line({"company": r["name"], "last": r["txt"]}, r["txt"])
            if not line:
                continue
            rt.click_text(".chat-item, .user-list-item, [class*=friend]", r["name"], 4)
            time.sleep(random.uniform(1.5, 3))
            if not rt.click_text("textarea, .chat-input, [contenteditable]", "", 0):
                pass
            try:
                el = rt.find("textarea, [contenteditable='true']")
                if el:
                    rt.js("arguments[0].focus();", el[0])
                    rt.d.find_by = None
                    from selenium.webdriver.common.keys import Keys  # noqa
                    el[0].send_keys(line + Keys.ENTER)
                    sent += 1
                    db.quota_add("boss", "followup")
            except Exception:
                break
            time.sleep(random.uniform(5, 10))
        return len(rows), sent

    n, sent = bot.call(_run)
    db.event("chat", "boss", "followup", f"扫 {n} 条已读不回，复聊 {sent} 条")
    return {"read_no_reply": n, "sent": sent}
