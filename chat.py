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

# 同天互斥：只约束「写类」机制（会点按钮/发消息）。
# B 黑名单学习是只读扫描，不参与锁；否则每 15 分钟的自动扫描会把当天的 C/D 全部锁死。
_LOCK_KEY = "chat_kind_day"
_WRITE_KINDS = ("推荐清洗", "已读不回复聊")


def _kind_locked(kind):
    if kind not in _WRITE_KINDS:
        return None
    cur = db.setting(_LOCK_KEY)
    today = datetime.date.today().isoformat()
    if isinstance(cur, dict) and cur.get("day") == today \
            and cur.get("kind") in _WRITE_KINDS and cur.get("kind") != kind:
        return cur.get("kind")
    return None


def _kind_take(kind):
    if kind not in _WRITE_KINDS:
        return
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
  // BOSS 会话列表：.name-box 依次是 HR 姓名、公司名、HR 自己的职位（不是投递岗位）
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
          company:q('.im-session-item__company-name'),
          job:q('.im-session-item__job'), company_src:'dom',
          time:q('.im-session-item__time')};
}).filter(x=>x.txt);
"""

LP_ROWS_JS = """
const items=[...document.querySelectorAll('.im-ui-contact-list-item')];
return items.slice(0, arguments[0]).map(it=>{
  const q=s=>{const e=it.querySelector(s);return e?(e.innerText||'').trim():'';};
  const sub=q('.im-ui-contact-title-sub');
  const parts=sub.split('·').map(x=>x.trim()).filter(Boolean);
  const comp=parts.length?parts[parts.length-1]:'';   // 猎聘这里是「对方职位·公司」，不是投递岗位
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


_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_DATE_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?")


def row_recent(ts, days=3, now=None):
    """聊天列表的时间文本是否在最近 days 天内（含今天）。

    支持：HH:MM、昨天[ HH:MM]、前天[ HH:MM]、今天[ HH:MM]、刚刚、
    M-D / M月D日 / YYYY-MM-DD / YYYY年M月D日、星期X、周X。
    识别不了的一律按「不在时间窗」处理（严格），避免把很早的会话算进来。
    """
    s = str(ts or "").strip()
    now = now or datetime.datetime.now()
    today = now.date()
    if not s:
        return False
    if s.startswith("刚刚") or s.startswith("刚才"):
        return days >= 0
    if re.fullmatch(r"\d{1,2}:\d{2}", s):        # 今天
        return days >= 0
    if s.startswith("今天"):
        return days >= 0
    if s.startswith("昨天"):
        return days >= 1
    if s.startswith("前天"):
        return days >= 2
    m = _DATE_RE.search(s)
    if m:
        y = int(m.group(1)) if m.group(1) else today.year
        try:
            d = datetime.date(y, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return False
        if not m.group(1) and d > today:         # 跨年（如 12-31 在 1 月看到）
            d = d.replace(year=today.year - 1)
        return (today - d).days <= days
    m = re.search(r"(?:星期|周)([一二三四五六日天])", s)
    if m:
        target = _WEEKDAYS[m.group(1)]
        d = today - datetime.timedelta(days=(today.weekday() - target) % 7)
        return (today - d).days <= days
    if "更早" in s:
        return False
    return False


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
        if _is_self_msg(r.get("txt")):
            continue                     # 我方自动招呼/简历消息，不当成 HR 回复
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


CHAT_IMPORT_MAX = 60          # 单轮最多补录条数，避免一次写太多
# 我方发出的模板消息（自动招呼/发简历），不能当成 HR 回复参与拒答识别
SELF_MSG_HINTS = ("这个岗位我很感兴趣，请看下我的在线简历",
                  "这是我的简历，合适的话可以随时联系我",
                  "在线简历", ".pdf")


def _is_self_msg(txt):
    t = str(txt or "")
    return any(h in t for h in SELF_MSG_HINTS)


_NOT_JOB_RE = re.compile(
    r"^(hr|人事|招聘|猎头|顾问|consultant|客服|小助手|统招|本科|大专|硕士|博士|"
    r"学历不限|经验不限|应届|全职|兼职|面议|不限)$", re.I)


def _chat_company_ok(s):
    """补录用公司名校验：排除日期/称呼/纯数字等。"""
    s = str(s or "").strip()
    if not (2 <= len(s) <= 60):
        return False
    if re.search(r"\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}|昨天|前天|星期|周[一二三四五六日]", s):
        return False
    return not re.fullmatch(r"[\d\s:/\-月日年月]+", s)


def _chat_job_ok(s):
    """补录用岗位名校验：排除 HR/招聘等人名标签和异常串。"""
    s = str(s or "").strip()
    if not (2 <= len(s) <= 40):
        return False
    if _NOT_JOB_RE.fullmatch(s):
        return False
    return not re.search(r"\d{1,2}月\d{1,2}日|\d{1,2}:\d{2}", s)


def _import_external(rows, log, platform):
    """会话里出现、但岗位库没有的岗位 → 补录为「已投」并纳入跟进。"""
    added = linked = 0
    for r in rows:
        company = clean_company(r.get("company") or "")
        job = str(r.get("job") or "").strip()
        if not _chat_company_ok(company) or not _chat_job_ok(job):
            continue
        if added + linked >= CHAT_IMPORT_MAX:
            log(f"  聊天补录达到本轮上限 {CHAT_IMPORT_MAX} 条，其余留待下次")
            break
        note = "聊天记录识别（外部投递）"
        txt = str(r.get("txt") or "").strip()
        if txt:
            tag = "我方主动消息" if _is_self_msg(txt) else "HR 回复"
            note += f"｜{tag}：" + txt[:60]
        try:
            how = db.import_chat_job(platform, company, job, note=note)
        except Exception as e:
            log(f"  ! 补录失败 {company}/{job}：{str(e).splitlines()[0][:60]}")
            continue
        if how == "added":
            added += 1
            log(f"  + 补录外部投递：{company} / {job}")
        elif how == "linked":
            linked += 1
        time.sleep(random.uniform(0.2, 0.5))
    if added or linked:
        log(f"  聊天记录补录：新入库 {added} 条，关联已有 {linked} 条（均标记已投）")
    return added, linked


DEEP_OPEN_MAX = 10             # 单轮最多点开多少条会话去读投递岗位
_LP_EDU_RE = re.compile(r"(本科|大专|硕士|博士|中专|高中|学历不限|经验不限|应届)")


def _parse_lp_header(txt):
    """猎聘会话头部文本 → 投递岗位名。

    形如「梁女士 华为 · 财经数字化工程师 IT经理/主管 本科 3-5年 30-50k·16薪 华为 深圳-龙岗区」，
    学历之前的最后一段是投递岗位。
    """
    t = str(txt or "").strip()
    if "·" in t:
        t = t.split("·", 1)[1].strip()
    m = _LP_EDU_RE.search(t)
    seg = t[:m.start()].strip() if m else ""
    parts = [p for p in seg.split(" ") if p]
    return parts[-1] if parts else ""


def _lp_header(rt):
    return rt.js("""
      const c=document.querySelector('.im-ui-chat-container');
      const h=c?c.firstElementChild:null;
      return h?(h.innerText||'').replace(/\\s+/g,' ').trim():'';
    """) or ""


def _boss_open_job(rt, name, company):
    """点开 BOSS 会话，读右侧岗位卡（.position-name）。"""
    target = None
    for e in rt.find(".friend-content .text"):
        try:
            t = (e.text or "").replace("\n", " ")
        except Exception:
            continue
        if name and name in t and company in t:
            target = e
            break
    if not target:
        return ""
    before = rt.js("const e=document.querySelector('.position-name');"
                   "return e?(e.innerText||'').trim():''") or ""
    rt.click_el(target)
    end = time.time() + 7
    while time.time() < end:
        time.sleep(1.0)
        job = rt.js("const e=document.querySelector('.position-name');"
                    "return e?(e.innerText||'').trim():''") or ""
        if job and job != before:
            return job
    return ""


def _liepin_open_job(rt, name, company):
    """点开猎聘会话，从弹层头部解析投递岗位。"""
    target = None
    for e in rt.find(".im-ui-contact-list-item"):
        try:
            t = (e.text or "").replace("\n", " ")
        except Exception:
            continue
        if name and name in t:
            target = e
            break
    if not target:
        return ""
    before = _lp_header(rt)
    rt.click_el(target)
    end = time.time() + 7
    while time.time() < end:
        time.sleep(1.0)
        h = _lp_header(rt)
        if h and h != before:
            return _parse_lp_header(h)
    return ""


def _import_deep(rt, rows, log, platform):
    """对「公司不在库」的会话逐条点开，读投递岗位后补录（每轮有点开上限）。"""
    opened = added = linked = 0
    for r in rows:
        if opened >= DEEP_OPEN_MAX:
            log(f"  本轮最多点开 {DEEP_OPEN_MAX} 条会话，其余留待下次")
            break
        company = clean_company(r.get("company") or "")
        name = str(r.get("name") or "").strip()
        if not _chat_company_ok(company):
            continue
        if db.company_has_job(platform, company):
            continue                            # 这家公司已有岗位在库，不再点开
        try:
            job = (_boss_open_job(rt, name, company) if platform == "boss"
                   else _liepin_open_job(rt, name, company))
        except Blocked:
            raise
        except Exception as e:
            log(f"  ! 点开会话失败 {company}：{str(e).splitlines()[0][:60]}")
            continue
        opened += 1
        if not _chat_job_ok(job):
            log(f"  跳过 {company}：会话里没识别出投递岗位")
            time.sleep(random.uniform(0.8, 1.5))
            continue
        try:
            txt = str(r.get("txt") or "").strip()
            tag = ("我方主动消息" if _is_self_msg(txt) else "HR 回复") if txt else ""
            note = ("聊天记录识别（外部投递·点开会话）"
                    + (f"｜{tag}：{txt[:60]}" if tag else ""))
            how = db.import_chat_job(platform, company, job, note=note)
        except Exception as e:
            log(f"  ! 补录失败 {company}/{job}：{str(e).splitlines()[0][:60]}")
            continue
        if how == "added":
            added += 1
            log(f"  + 补录外部投递：{company} / {job}")
        elif how == "linked":
            linked += 1
        time.sleep(random.uniform(0.8, 1.5))
    if added or linked:
        log(f"  聊天补录（点开会话）：新入库 {added} 条，关联已有 {linked} 条")
    return added, linked


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
        selfn = sum(1 for r in recent if _is_self_msg(r.get("txt")))
        hrn = len(recent) - selfn
        log(f"{name}扫HR回复：收集 {len(rows)} 条会话（滚动 {scrolls} 次），"
            f"近 {days} 天 {len(recent)} 条（HR 回复 {hrn} 条 / 我方消息 {selfn} 条；"
            f"HR 回复用于拒答识别，两类都用于投递补录）")
        hits, updated = _apply_hits(recent, log, platform)
        # 智联列表自带投递岗位；BOSS/猎聘列表只有 HR 头衔，需要逐条点开会话读
        if platform == "zhaopin":
            imported, linked = _import_external(recent, log, platform)
        else:
            imported, linked = _import_deep(rt, recent, log, platform)
        return len(recent), hits, updated, imported, linked
    n, hits, updated, imported, linked = bot.call(_run, timeout=timeout)
    db.event("chat", platform, "scan_hr",
             f"扫近 {days} 天 {n} 条会话，命中 {hits} 家进黑名单，"
             f"标记 {updated} 条在库岗位，补录外部投递 {imported} 条")
    return {"scanned": n, "blacklisted": hits, "updated": updated,
            "imported": imported, "linked": linked}


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
