"""三个 GitHub 学习来的增强机制（全部默认关闭、同一天互斥只允许开一种）：

B 黑名单学习：读 BOSS 聊天列表里 HR 的最新回复，命中"不合适"类措辞 → 公司进黑名单。
C 推荐流清洗：BOSS 推荐列表里不符合规则的岗位点「不合适」，让推荐越洗越准。
D 已读不回复聊：对已读不回的 HR 用 AI 生成单句跟进，日上限+最小间隔。

注意：三者都要进 /web/geek/chat 或推荐页，是 BOSS 风控敏感区——
入口统一走 risk.check('boss')，任何风控信号立即 trip 冷却并终止本轮。
"""
import datetime
import random
import re
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


def _gate(kind, flag, name, platform="boss"):
    if not db.setting(flag, 0):
        raise RuntimeError(f"{name} 未开启（设置页勾选）")
    other = _kind_locked(kind)
    if other:
        raise RuntimeError(f"今天已运行过机制「{other}」，聊天类操作同天只能开一种（防风控）")
    risk.check(platform)
    _kind_take(kind)


CHAT_ROWS_JS = """
const rows=[...document.querySelectorAll('.chat-item, .user-list-item, [class*=friend]')];
return rows.slice(0, arguments[0]).map(r=>{
  const raw=(r.innerText||'');
  const msgEl=r.querySelector('.last-msg-text');
  const t=((msgEl&&msgEl.innerText)||raw).replace(/\\s+/g,' ').slice(0,200);
  const timeEl=r.querySelector('.time');
  const tm=(timeEl&&timeEl.innerText||raw).match(/(?:\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}|\\d{1,2}[-/]\\d{1,2}|昨天|前天|\\d{1,2}:\\d{2})/);
  // BOSS 会话列表结构：.name-box 内依次是 HR 姓名、公司名、职位
  const box=r.querySelector('.name-box');
  let company='', src='';
  if(box){
    const sp=[...box.querySelectorAll('span')];
    company=((sp[1]&&sp[1].innerText)||'').trim();
    if(company) src='dom';
  }
  if(!company){
    const s=raw.replace(/^\\d{1,2}:\\d{2}\\s*/,'')
             .replace(/^[\\u4e00-\\u9fa5A-Za-z]{1,4}(?:女士|先生|小姐|同学)/,'');
    const m=s.match(/(?:^| )([\\u4e00-\\u9fa5A-Za-z0-9（）()]{2,24}(?:有限公司|集团|控股|科技|股份|银行|研究院|事务所))/);
    company=m?m[1]:''; src=company?'text':'';
  }
  const nm=box&&box.querySelector('.name-text');
  return {txt:t, name:(nm&&nm.innerText||'').trim(), company, company_src:src,
          time:tm?tm[0]:''};
}).filter(x=>x.txt);
"""

SCROLL_ITEM_JS = """
(function(sel){
  const it=document.querySelector(sel);
  if(!it) return 0;
  let el=it.parentElement;
  while(el){
    const s=getComputedStyle(el);
    if((s.overflowY==='auto'||s.overflowY==='scroll')&&el.scrollHeight>el.clientHeight+40){
      const b=el.scrollTop;
      el.scrollTop=Math.min(el.scrollHeight,b+el.clientHeight*0.9);
      return el.scrollTop>b?1:0;
    }
    el=el.parentElement;
  }
  return 0;
})(arguments[0]);
"""

ZL_IM = "https://i.zhaopin.com/im"
LP_HOME = "https://c.liepin.com/"

ZL_ROWS_JS = """
const items=[...document.querySelectorAll('.im-session-item')];
return items.slice(0, arguments[0]).map(it=>{
  const q=s=>{const e=it.querySelector(s);return e?(e.innerText||'').trim():'';};
  const t=q('.im-session-item__preview-text').replace(/\\s+/g,' ').slice(0,200);
  return {txt:t, name:q('.im-session-item__name'),
          company:q('.im-session-item__company-name'), company_src:'dom',
          time:q('.im-session-item__time')};
}).filter(x=>x.txt);
"""

LP_ROWS_JS = """
const items=[...document.querySelectorAll('.im-ui-contact-list-item')];
return items.slice(0, arguments[0]).map(it=>{
  const q=s=>{const e=it.querySelector(s);return e?(e.innerText||'').trim():'';};
  const sub=q('.im-ui-contact-title-sub');
  const comp=sub.split('·').pop().trim();
  const t=q('.im-ui-last-message').replace(/\\s+/g,' ').slice(0,200);
  return {txt:t, name:q('.im-ui-contact-title'),
          company:comp, company_src:'dom',
          time:q('.im-ui-system-tip.contact-time')||q('.contact-time')};
}).filter(x=>x.txt);
"""


def clean_company(s):
    """从「吴女士传音控股」这类串里去掉称呼，得到公司名。"""
    s = str(s or "").strip()
    m = re.search(r"(?:女士|先生|小姐|同学|招聘者|HR)", s)
    return s[m.end():].strip() if m and m.end() < len(s) else s


COMPANY_HINTS = ("公司", "集团", "控股", "科技", "股份", "银行", "研究院",
                 "事务所", "企业", "中心", "工厂", "厂")


def valid_company(s):
    """过滤掉日期/时间/称呼等非公司名，避免把「11月21日」当公司写进黑名单。"""
    s = str(s or "").strip()
    if len(s) < 3:
        return False
    if re.search(r"\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}|昨天|前天|星期|周[一二三四五六日]", s):
        return False
    if re.fullmatch(r"[\d\s:/\-月日年月]+", s):
        return False
    if re.search(r"(招聘|人事|HR|经理|主管|总监|专员|顾问|猎头)$", s):
        return False
    return any(h in s for h in COMPANY_HINTS)


def row_recent(ts, days=3, now=None):
    """聊天列表的时间文本是否在最近 days 天内。无法判断时按「保留」处理。"""
    s = str(ts or "").strip()
    now = now or datetime.datetime.now()
    if not s:
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}", s):        # 今天
        return days >= 0
    if s == "昨天":
        return days >= 1
    if s == "前天":
        return days >= 2
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            d = datetime.date(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return True
        if d > now.date():                       # 跨年
            d = d.replace(year=now.year - 1)
        return (now.date() - d).days <= days
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return True
        return (now.date() - d).days <= days
    if "更早" in s:
        return False
    return True


def _collect(rt, rows_js, item_sel, days, limit=200, max_scrolls=8):
    """边滚动会话列表边收集，直到出现超出时间窗的记录或滚不动。"""
    rows, seen, scrolls = [], set(), 0
    for _ in range(max_scrolls):
        batch = rt.js(rows_js, limit) or []
        for r in batch:
            key = (r.get("company") or "", r.get("txt", "")[:80], r.get("time", ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(r)
        if batch and any(not row_recent(r.get("time"), days) for r in batch):
            break
        scrolls += 1
        if not rt.js(SCROLL_ITEM_JS, item_sel):
            break
        time.sleep(random.uniform(1.0, 1.8))
    return rows, [r for r in rows if row_recent(r.get("time"), days)], scrolls


def _apply_hits(rows, log, platform):
    """命中拒答词的会话 → 公司入黑名单 + 同步在库岗位。"""
    hits = updated = 0
    hit_companies = set()
    for r in rows:
        kw = rules_hit(r["txt"])
        company = clean_company(r.get("company") or r.get("name") or "")
        from_dom = r.get("company_src") == "dom"
        if kw and company and not from_dom and not valid_company(company):
            log(f"  跳过（没识别出公司名）：{r['txt'][:40]}")
            company = ""
        if kw and company and company not in hit_companies:
            hit_companies.add(company)
            db.blacklist_add(company, source="hr", reason=kw)
            db.hr_msg_log(platform, company, None, r["txt"], kw)
            updated += db.mark_company_rejected(company)
            hits += 1
            log(f"  命中「{company}」：{kw}")
        time.sleep(random.uniform(0.3, 0.7))
    return hits, updated


def _scan_reports(bot, log, platform, name, goto, rows_js, item_sel,
                  days, timeout=300, after_open=None):
    """三平台共用的扫会话流程。after_open(rt) 用于平台特有准备/校验。"""
    def _run(rt):
        rt.goto(goto)
        if platform == "boss":
            risk.guard_security(rt, 10)
            ok, _ = rt.wait_login_state("boss", 14)
            if not ok:
                raise Blocked("BOSS 未登录（请到「设置 → 账号连接」用专用窗口登录）")
        time.sleep(random.uniform(2, 4))
        if after_open:
            after_open(rt)
        rows, recent, scrolls = _collect(rt, rows_js, item_sel, days)
        log(f"{name}扫HR回复：收集 {len(rows)} 条会话（滚动 {scrolls} 次），"
            f"近 {days} 天 {len(recent)} 条")
        hits, updated = _apply_hits(recent, log, platform)
        return len(recent), hits, updated
    n, hits, updated = bot.call(_run, timeout=timeout)
    db.event("chat", platform, "scan_hr",
             f"扫近 {days} 天 {n} 条会话，命中 {hits} 家进黑名单，"
             f"标记 {updated} 条在库岗位")
    return {"scanned": n, "blacklisted": hits, "updated": updated}


def scan_hr(bot, log, days=3):
    """机制B（BOSS）：按时间扫最近 days 天的会话。"""
    _gate("黑名单学习", "blacklist_learn_enabled", "黑名单学习", "boss")
    log(f"扫HR回复（BOSS）：开关与风控检查通过（近 {days} 天）")
    return _scan_reports(bot, log, "boss", "BOSS", CHAT,
                         CHAT_ROWS_JS, ".chat-item", days)


def scan_hr_zhaopin(bot, log, days=3):
    """机制B（智联）：扫 i.zhaopin.com/im 的会话列表。"""
    _gate("黑名单学习", "blacklist_learn_enabled", "黑名单学习", "zhaopin")
    log(f"扫HR回复（智联）：开关与风控检查通过（近 {days} 天）")

    def after_open(rt):
        if not rt.find(".im-session-item"):
            raise Blocked("智联消息页没有会话列表（可能未登录，请到「设置 → 账号连接」"
                          "用专用窗口登录）")
        log("扫HR回复（智联）：消息页已加载")

    return _scan_reports(bot, log, "zhaopin", "智联", ZL_IM,
                         ZL_ROWS_JS, ".im-session-item", days,
                         after_open=after_open)


def scan_hr_liepin(bot, log, days=3):
    """机制B（猎聘）：打开首页右下「我的沟通」弹层，读会话列表。"""
    _gate("黑名单学习", "blacklist_learn_enabled", "黑名单学习", "liepin")
    log(f"扫HR回复（猎聘）：开关与风控检查通过（近 {days} 天）")

    def after_open(rt):
        entry = rt.find(".im-ui-basic-entry")
        if not entry:
            raise Blocked("猎聘没找到「我的沟通」入口（可能未登录）")
        rt.click_el(entry[0])
        time.sleep(random.uniform(2, 4))
        if not rt.find(".im-ui-contact-list-item"):
            raise Blocked("猎聘沟通列表没有加载出来（可能未登录或网络慢）")
        log("扫HR回复（猎聘）：沟通弹层已加载")

    return _scan_reports(bot, log, "liepin", "猎聘", LP_HOME,
                         LP_ROWS_JS, ".im-ui-contact-list-item", days,
                         after_open=after_open)


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
    log("推荐清洗：开关与风控检查通过，准备打开推荐页")

    def _run(rt):
        import rules as _r
        rt.goto("https://www.zhipin.com/web/geek/job-recommend")
        risk.guard_security(rt, 10)
        time.sleep(random.uniform(2, 4))
        rows = rt.js(CLEANSE_CAND_JS, limit) or []
        log(f"推荐清洗：读取到 {len(rows)} 条推荐，逐条判断")
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

    n, marked = bot.call(_run, timeout=600)
    db.event("chat", "boss", "cleanse", f"过 {n} 条推荐，标不合适 {marked} 条")
    return {"checked": n, "marked": marked}


FOLLOW_ZL_JS = """
const all=[...document.querySelectorAll('.im-message')]
  .filter(m=>m.querySelector(':scope > .im-message__bubble'));
if(!all.length) return {state:'empty'};
const last=all[all.length-1];
const rc=last.querySelector('.im-message__receipt');
return {mine:last.classList.contains('im-message--custom'),
        receipt:rc?(rc.innerText||'').trim():'',
        last:(last.innerText||'').replace(/\\s+/g,' ').slice(0,200)};
"""


def followup_sweep_zhaopin(bot, log, limit=8):
    """机制D（智联）：读完会话，只对「我方最后一条已被 HR 已读、且没有回复」的
    会话发一句 AI 跟进。日上限 + 长间隔，任何风控信号立即停。"""
    _gate("已读不回复聊", "followup_enabled", "已读不回复聊", "zhaopin")
    cap = int(db.setting("followup_daily_cap", 3))
    left = db.quota_left("zhaopin", "followup", cap)
    if left <= 0:
        raise RuntimeError(f"复聊今日上限 {cap} 条已用完")
    log(f"已读不回复聊（智联）：今日还可发 {left} 条，开始检查会话")

    def _run(rt):
        rt.goto(ZL_IM)
        time.sleep(random.uniform(3, 4))
        items = rt.find(".im-session-item")
        if not items:
            raise Blocked("智联消息页没有会话（可能未登录，请到「设置 → 账号连接」登录）")
        checked = sent = 0
        for it in items[:min(len(items), limit)]:
            if sent >= left:
                break
            meta = rt.js(r"""
            const q=s=>{const e=arguments[0].querySelector(s);return e?(e.innerText||'').trim():'';};
            return {name:q('.im-session-item__name'), company:q('.im-session-item__company-name')};
            """, it) or {}
            try:
                rt.click_el(it)
                time.sleep(random.uniform(1.5, 2.5))
                info = rt.js(FOLLOW_ZL_JS) or {}
                checked += 1
                if not info.get("mine"):
                    continue                       # 最后一条是 HR 的 → 已回复
                rc = info.get("receipt", "")
                if rc != "已读":
                    why = "HR未读" if rc == "送达" else "无已读回执"
                    log(f"  跳过（{why}）：{meta.get('company', '')} "
                        f"{info.get('last', '')[:24]}")
                    continue
                line = ai.followup_line(
                    {"company": meta.get("company", ""), "last": info.get("last", "")},
                    info.get("last", ""))
                if not line:
                    continue
                box = rt.find(".im-sender__input")
                if not box:
                    log("  跳过：没找到输入框")
                    continue
                box[0]._h.fill(line)
                box[0]._h.press("Enter")
                sent += 1
                db.quota_add("zhaopin", "followup")
                db.hr_msg_log("zhaopin", meta.get("company", ""), None, line, "复聊")
                log(f"  ✓ 已复聊：{meta.get('company', '')}｜{line[:24]}")
                time.sleep(random.uniform(8, 14))
            except (Blocked, risk.Cooldown):
                raise
            except Exception as e:
                log(f"  ! 复聊失败（{meta.get('company', '')}）："
                    f"{str(e).splitlines()[0][:80]}")
                break
        return checked, sent
    checked, sent = bot.call(_run, timeout=600)
    db.event("chat", "zhaopin", "followup",
             f"检查 {checked} 条会话，复聊 {sent} 条")
    return {"checked": checked, "sent": sent}


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
    log("已读不回复聊：开关与风控检查通过，准备打开聊天页")

    def _run(rt):
        rt.goto(CHAT)
        risk.guard_security(rt, 10)
        time.sleep(random.uniform(2, 4))
        rows = rt.js(FOLLOW_JS, limit) or []
        log(f"已读不回复聊：找到 {len(rows)} 条候选会话")
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
                    h = el[0]
                    rt.js("arguments[0].focus();", h)
                    try:
                        h._h.fill(line)          # input/textarea
                    except Exception:
                        h._h.type(line, delay=25)  # contenteditable
                    h._h.press("Enter")
                    sent += 1
                    db.quota_add("boss", "followup")
            except Exception as e:
                log(f"已读不回复聊：发送失败（{r['name']}）："
                    f"{str(e).splitlines()[0][:60]}")
                break
            time.sleep(random.uniform(5, 10))
        return len(rows), sent

    n, sent = bot.call(_run, timeout=600)
    db.event("chat", "boss", "followup", f"扫 {n} 条已读不回，复聊 {sent} 条")
    return {"read_no_reply": n, "sent": sent}
