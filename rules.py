"""规则引擎：薪资解析、硬门槛、黑白名单、匹配度、投递判定、HR 拒答识别。"""
import re

import config


def salary_cap(text):
    """薪资区间上限（元/月）。'1.5-3万·14薪'→30000；'20-30K'→30000；'8000-12000元'→12000。"""
    s = str(text or "").replace(" ", "")
    m = re.findall(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)(万|k|K|千|元)", s)
    if m:
        hi, unit = float(m[0][1]), m[0][2]
        return int(hi * (10000 if unit == "万" else 1000 if unit in "kK" else 1))
    m = re.findall(r"(\d+(?:\.\d+)?)(万|k|K)", s)
    if m:
        return int(float(m[0][0]) * (10000 if m[0][1] == "万" else 1000))
    m = re.findall(r"(\d+)-(\d+)元", s)
    if m:
        return int(m[0][1])
    return None


def hard_gate(text, patterns=None):
    t = str(text or "")
    return [p for p in (patterns or config.HARD_GATE_PATTERNS) if p in t]


def blacklist_reason(title, words=None):
    t = str(title or "")
    for k in (words or config.BLACKLIST_KW):
        if k in t:
            return k
    return None


def in_whitelist(title, words=None):
    t = str(title or "")
    return any(k in t for k in (words or config.WHITELIST_KW))


def hr_reject_hit(text):
    """HR 回复是否是"不合适"类信号（黑名单学习用）。只匹配明确措辞，避免误伤。"""
    t = str(text or "")
    for kw in config.HR_REJECT_WORDS:
        if kw in t:
            return kw
    return None


def score(title, city, salary, jd="", floor=None):
    floor = floor or config.SALARY_FLOOR
    s = 0
    t = str(title or "")
    if any(k in t for k in ("IT总监", "信息化总监", "CIO", "信息中心", "数智部", "IT负责人")):
        s += 6
    elif any(k in t for k in ("IT经理", "信息部经理", "信息化经理", "信息技术经理",
                              "数字化经理", "技术经理")):
        s += 5
    elif any(k in t for k in ("项目经理", "项目总监", "交付经理")):
        s += 4
    elif "架构师" in t:
        s += 3
    elif "AI" in t.upper():
        s += 3
    if city == "珠海":
        s += 6
    elif city == "中山":
        s += 4
    elif city in ("广州", "深圳"):
        s += 3
    elif city == "东莞":
        s += 2
    cap = salary_cap(salary)
    if cap:
        if cap >= 40000:
            s += 4
        elif cap >= int(floor):
            s += 3
        elif cap >= 20000:
            s += 1
    blob = f"{t}{jd}"
    if any(k in blob for k in ("跨境", "电商", "出海")):
        s += 2
    if any(k in blob for k in ("ERP", "MES", "SAP", "WMS", "业财")):
        s += 1
    if any(k in blob for k in ("AI", "智能体", "大模型", "RAG")):
        s += 2
    return ("★★★" if s >= 15 else "★★" if s >= 11 else "★"), s


def verdict(job, jd="", floor=None, hard=None, black=None):
    """一条岗位入库前判定：(status, reason)。
    状态：待投 / 不投-薪资低 / 不投-硬门槛 / 不投-岗位偏离 / 待定-薪资未标 /
          保留-低于底线(总监级)"""
    floor = config.SALARY_FLOOR if floor is None else int(floor)
    title = job.get("title", "")
    city = job.get("city", "")
    blob = f"{title} {job.get('edu', '')} {job.get('exp', '')} {jd}"
    hits = hard_gate(blob, hard)
    if hits:
        return "不投-硬门槛", "硬门槛：" + "、".join(hits)
    bad = blacklist_reason(title, black)
    if bad:
        return "不投-岗位偏离", f"非目标岗位（{bad}）"
    cap = salary_cap(job.get("salary", ""))
    if cap is None:
        return "待定-薪资未标", "薪资未标注，需先确认"
    if cap <= config.SALARY_HARD_CAP:
        return "不投-薪资低", f"薪资上限{cap // 1000}K≤{config.SALARY_HARD_CAP // 1000}K（硬过滤，不受底线影响）"
    if cap < floor:
        if city in config.ALLOW_DIRECTOR_LOW and "总监" in title:
            return "保留-低于底线(总监级)", f"{city}总监级，上限{cap // 1000}K<{floor // 1000}K，按规则保留"
        return "不投-薪资低", f"薪资上限{cap // 1000}K<{floor // 1000}K 且非珠中总监级"
    return "待投", ""
