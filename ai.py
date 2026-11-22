"""AI 层（OpenAI 兼容接口）：简历结构化 / 简历优化分析 / 采集计划 / 按 JD 定制。

铁律：AI 只能基于用户真实简历做重排、同义改写、取舍；不得新增公司/项目/技能/
数字/时间/证书。tailor 结果用 _guard 与原文比对数字和公司名，越界即告警。
"""
import json
import re

import requests

import db
import resume as R

DEFAULTS = {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "api_key": "", "temperature": 0.4}


def settings():
    out = dict(DEFAULTS)
    for k in out:
        v = db.setting(f"ai_{k}")
        if v not in (None, ""):
            out[k] = v
    return out


def save_settings(d):
    for k in DEFAULTS:
        if k in d:
            db.set_setting(f"ai_{k}", str(d[k]).strip().rstrip("/") if k == "base_url" else d[k])
    return settings()


MIN_KEY_LEN = 16            # 正常服务商的 key 都远长于此；短于此必是残缺值


def configured():
    s = settings()
    return bool(s["api_key"] and s["base_url"] and s["model"]
                and len(str(s["api_key"]).strip()) >= MIN_KEY_LEN)


def key_problem():
    """返回 key 的问题描述；没问题返回 None。用于界面明确提示，而不是等到 401。"""
    k = str(settings()["api_key"] or "").strip()
    if not k:
        return "还没填 API Key"
    if k.endswith("…") or "…" in k:
        return "存的是脱敏占位值（带 …），不是真实 Key，请重新完整粘贴"
    if len(k) < MIN_KEY_LEN:
        return f"API Key 只有 {len(k)} 个字符，看起来不完整（通常 20 位以上）"
    return None


def chat_msg(messages, tools=None, json_mode=False, timeout=150):
    """返回完整的 assistant message（含 tool_calls），供 agent 循环使用。"""
    s = settings()
    if not configured():
        raise RuntimeError("AI 未配置：请在设置页填 Base URL / 模型 / API Key")
    body = {"model": s["model"], "messages": messages,
            "temperature": float(s.get("temperature") or 0.4)}
    if json_mode and not tools:
        body["response_format"] = {"type": "json_object"}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    r = requests.post(s["base_url"].rstrip("/") + "/chat/completions",
                      headers={"Authorization": f"Bearer {s['api_key']}"},
                      json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"AI 接口 {r.status_code}: {r.text[:180]}")
    return r.json()["choices"][0]["message"]


def chat(messages, json_mode=False, timeout=150):
    """向后兼容接口：只返回文本内容（现有 8 个 AI 能力都走这里）。"""
    return chat_msg(messages, None, json_mode, timeout).get("content") or ""


def supports_tools():
    """探测当前模型是否支持 function calling（不支持则 agent 退化为纯建议模式）。"""
    try:
        msg = chat_msg([{"role": "user", "content": "用 get_time 工具查现在几点"}],
                       tools=[{"type": "function", "function": {
                           "name": "get_time", "description": "获取当前时间",
                           "parameters": {"type": "object", "properties": {}}}}],
                       timeout=40)
        return bool(msg.get("tool_calls"))
    except Exception:
        return False


def test_connection():
    try:
        out = chat([{"role": "user", "content": "回复两个字：可用"}], timeout=30)
        return True, (out or "")[:20]
    except Exception as e:
        return False, str(e)[:120]


def _unwrap(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if 0 <= i < j else t


# ---------------- 简历结构化 ----------------
STRUCT_PROMPT = """你是简历整理助手。把用户简历原文整理成严格 JSON，规则：
- 只搬运原文有的内容，禁止新增/推测任何事实、数字、公司、时间。
- 数字保持原样（如 "4800万"、"90%+"、"2020.6-2024.2"）。
JSON 字段：
{"name":"","title":"","intent":"","lead":"两三句自我概述","contact":{"city":"","phone":"","email":"","status":""},
 "metrics":[{"value":"","label":""}],
 "experience":[{"company":"","role":"","period":"","bullets":[""]}],
 "ai_projects":[{"title":"","role":"","period":"","points":[""]}],
 "other_projects":[{"name":"","role":"","period":"","text":""}],
 "early":[{"company":"","role":"","period":"","text":""}],
 "education":[{"school":"","major":"","degree":"","period":""}],
 "certs":[""], "focus":[""]}
lead 控制在120字内；experience 每段 bullets ≤6 条、每条 ≤60字。只输出 JSON。"""


def structure_resume(text):
    if len(text or "") < 100:
        raise RuntimeError("简历文本太短，无法结构化")
    out = chat([{"role": "system", "content": STRUCT_PROMPT},
                {"role": "user", "content": text[:12000]}], json_mode=True)
    d = json.loads(_unwrap(out))
    if not d.get("name"):
        raise RuntimeError("AI 未能识别姓名，请检查简历原文")
    return d


# ---------------- 简历优化分析（只建议不改稿） ----------------
ANALYZE_PROMPT = """你是资深 IT 管理岗求职顾问。基于简历 JSON 给出优化建议，规则：
- 不得新增事实；建议只能重排、删冗、量化已有内容、突出与"IT总监/信息化经理/数字化经理/项目经理(跨境电商IT)"岗位的匹配。
- 目标画像：18年企业信息化+近10年跨境电商IT管理，应聘珠海/中山/广州/深圳/东莞 27K+ 岗位。
输出 JSON：{"summary":"整体判断两句话",
 "sections":[{"where":"experience[0].bullets","issue":"问题","suggest":"建议改法(给替换文本示例)","why":"理由"}],
 "risks":["可能被HR质疑的点"]}，sections ≤8 条，按重要性排序。只输出 JSON。"""


def analyze_resume(data=None):
    d = data or R.load()
    out = chat([{"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": json.dumps(d, ensure_ascii=False)[:12000]}],
               json_mode=True)
    return json.loads(_unwrap(out))


# ---------------- 采集计划 ----------------
PLAN_PROMPT = """你是求职采集策略助手。基于简历 JSON 制定三平台采集计划，规则：
- 平台键：zhaopin(智联)/boss(BOSS)/liepin(猎聘，猎聘走推荐流，keyword 可空)。
- 每平台 ≤6 个关键词；城市只从 珠海/中山/广州/深圳/东莞 选（≤5）；
- 关键词必须贴合真实目标岗：IT总监/IT经理/信息化经理/信息部经理/数字化经理/项目经理(信息化)/技术经理；避开纯开发/运维/算法岗词。
输出 JSON：{"plan":[{"platform":"","keyword":"","city":"","filters":{"salary_min":27000},"reason":"","priority":0}],
 "notes":"策略说明两句话"}。智联词要短（≤8字）。只输出 JSON。"""


def plan_search(data=None):
    d = data or R.load()
    out = chat([{"role": "system", "content": PLAN_PROMPT},
                {"role": "user", "content": json.dumps(d, ensure_ascii=False)[:10000]}],
               json_mode=True)
    return json.loads(_unwrap(out))


# ---------------- 简历优化：直接生成新的一版（防虚构） ----------------
OPTIMIZE_PROMPT = """你是资深简历顾问。基于我的简历 JSON 生成"更匹配求职意向"的新版简历 JSON。
目标方向（严格取自简历里的「求职意向」，不得自行替换成别的岗位方向）：{INTENT}
要求：
- 围绕上述目标方向重排、改写、取舍；意向里明确写的方向优先呈现，没写的内容不要臆造；
- 意向里的城市/薪资只作为参考，不作为改写依据。
硬约束（违反即废稿）：
1. 禁止新增任何事实：公司、职位、项目、技能、证书、时间段、数字必须逐一来自原文；
2. 允许：调整顺序与侧重点、合并/拆分表述、同义改写、删弱相关内容、把与目标方向强相关的经历前移；
3. 数值成果原样保留，不得推算、四舍五入或夸大。
输出 JSON：{"resume":<新版完整简历JSON，结构与输入相同>,
 "notes":[{"from":"原句","to":"新句","why":"为什么更匹配"}],
 "focus":"整体改动思路，两句话"}
notes ≤10 条。只输出 JSON。"""


def _optimize_intent(d):
    """优化方向：优先用简历的「求职意向」；缺失时退回标题或通用描述。"""
    intent = str(d.get("intent") or "").strip()
    if intent:
        return intent
    title = str(d.get("title") or "").strip()
    if title:
        return f"{title} 相关方向（简历未填写求职意向，按现有经历呈现）"
    return "按简历现有经历做最佳呈现（简历未填写求职意向）"


def optimize_resume(data=None):
    d = data or R.load()
    if not d.get("name"):
        raise RuntimeError("当前没有简历，请先在「简历」页导入")
    prompt = OPTIMIZE_PROMPT.replace("{INTENT}", _optimize_intent(d))
    out = chat([{"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(d, ensure_ascii=False)[:14000]}],
               json_mode=True)
    parsed = json.loads(_unwrap(out))
    new = parsed.get("resume") or {}
    if not new.get("name"):
        raise RuntimeError("AI 返回的简历缺少姓名，请重试")
    warnings = _guard(R.to_text(d), R.to_text(new))
    return {"resume": new, "notes": parsed.get("notes") or [],
            "focus": parsed.get("focus") or "", "warnings": warnings}


# ---------------- 岗位 AI 判定（批量匹配度） ----------------
SCORE_PROMPT = """你是求职匹配评估器。给出我的简历摘要和一批岗位，逐条判断匹配度。
规则：
- 只依据岗位标题/公司/薪资/城市/标签这些给定信息判断，不得想象岗位内容；
- tier 取值：强匹配 / 匹配 / 弱匹配 / 不匹配；
- why 一句话（≤40字），说不清就写依据不足；
- 输出必须覆盖输入的每个 id，不得增删。
输出 JSON：{"items":[{"id":<原id>,"tier":"...","why":"..."}]}。只输出 JSON。"""


def score_jobs(jobs, resume_text=None):
    """jobs: [{id,title,company,salary_text,city,match_note}]，≤25 条一批。
    返回 {id: (tier, why)}。"""
    if not jobs:
        return {}
    rt = (resume_text or R.to_text())[:4000]
    slim = [{"id": j["id"], "title": j.get("title"), "company": j.get("company"),
             "salary": j.get("salary_text"), "city": j.get("city"),
             "tags": (j.get("match_note") or "")[:60]} for j in jobs]
    out = chat([{"role": "system", "content": SCORE_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"resume": rt, "jobs": slim}, ensure_ascii=False)}], json_mode=True)
    parsed = json.loads(_unwrap(out))
    res = {}
    for it in parsed.get("items") or []:
        try:
            res[int(it.get("id"))] = (str(it.get("tier") or "")[:8], str(it.get("why") or ""))
        except (TypeError, ValueError):
            continue
    return res


# ---------------- 采集计划 AI 评估 ----------------
REVIEW_PROMPT = """评估这份三平台采集计划是否值得执行（基于我的简历背景）。
对每一行给 verdict：保留 / 砍掉 / 改写（keyword 太宽或太窄），一句话理由。
输出 JSON：{"items":[{"index":<行号,从0开始>,"verdict":"保留|砍掉|改写","suggest_keyword":"仅改写时给","why":"..."}]}。只输出 JSON。"""


def review_plan(plan_rows, resume_text=None):
    rt = (resume_text or R.to_text())[:3000]
    slim = [{"index": i, "platform": r.get("platform"), "keyword": r.get("keyword"),
             "city": r.get("city")} for i, r in enumerate(plan_rows)]
    out = chat([{"role": "system", "content": REVIEW_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"resume": rt, "plan": slim}, ensure_ascii=False)}], json_mode=True)
    parsed = json.loads(_unwrap(out))
    return parsed.get("items") or []


# ---------------- 按 JD 定制（防编造） ----------------
TAILOR_PROMPT = """按目标 JD 重写我的简历（JSON 进、JSON 出），硬约束：
1. 只允许：重排、合并/拆分句子、同义改写、取舍、调整侧重点、替换措辞。
2. 禁止新增：公司、职位、项目、技能、证书、时间段、任何数字（数字必须逐字来自原简历）。
3. JD 要求但我没有的点 → 写进 gaps，不得编造。
输出 JSON：{"resume":<改写后的同结构简历JSON>,"notes":[{"from":"原句","to":"新句","why":"对应JD哪点"}],"gaps":["缺证据项"]}。
notes ≤8 条。只输出 JSON。"""


def tailor(jd_text, target="", data=None):
    d = data or R.load()
    if len(jd_text or "") < 30:
        raise RuntimeError("JD 太短")
    out = chat([{"role": "system", "content": TAILOR_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"resume": d, "target": target, "jd": jd_text[:8000]},
                    ensure_ascii=False)}], json_mode=True)
    parsed = json.loads(_unwrap(out))
    new = parsed.get("resume") or {}
    warnings = _guard(R.to_text(d), R.to_text(new))
    return {"resume": new, "notes": parsed.get("notes") or [],
            "gaps": parsed.get("gaps") or [], "warnings": warnings}


def followup_line(job, history):
    """已读不回的复聊跟进（单句，≤40字，不编造）。"""
    out = chat([{"role": "system", "content":
                 "写一句跟进消息：礼貌、≤40字、不得新增任何事实或承诺，只表达"
                 "对岗位的兴趣与期待回复。只输出这句话。"},
                {"role": "user", "content": json.dumps(
                    {"job": job, "chat": history[:600]}, ensure_ascii=False)}],
               timeout=45)
    line = (out or "").strip().strip('"').strip("'").strip()
    return line[:80]


# ---------------- HR 回复助手（零触达：只生成草稿，由用户自己去发） ----------------
REPLY_PROMPT = """你是求职沟通顾问。我会给你：目标岗位、我的简历摘要、HR/招聘方发来的消息原文。
请判断对方意图并帮我起草回复。硬约束：
1. 回复草稿只能使用简历摘要里已有的事实，禁止编造经历、技能、数字、时间、薪资期望。
2. 若对方问到简历里没有的信息（如具体薪资期望、到岗时间），草稿里用【待你确认：xxx】占位，不要替我编。
3. 草稿要口语化、简洁，每条 ≤120 字，给 2-3 个不同语气版本。
输出 JSON：{"intent":"对方这句话的真实意图(≤30字)","signal":"positive|neutral|negative|reject",
"advice":"我该怎么应对(≤60字)","drafts":["版本1","版本2"],"need_confirm":["需要我自己确认的信息"],
"next_stage":"interview|replied|rejected|closed"}。只输出 JSON。"""


def reply_assist(hr_text, job=None, data=None):
    """分析 HR 回复并生成回复草稿。不发送任何消息。"""
    if len(str(hr_text or "").strip()) < 2:
        raise RuntimeError("HR 消息内容太短")
    d = data or R.load()
    payload = {"job": {k: (job or {}).get(k) for k in
                       ("title", "company", "salary_text", "city", "jd")},
               "my_resume_summary": R.to_text(d)[:3000],
               "hr_message": str(hr_text)[:2000]}
    out = chat([{"role": "system", "content": REPLY_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
               json_mode=True, timeout=120)
    parsed = json.loads(_unwrap(out))
    sig = parsed.get("signal")
    if sig not in ("positive", "neutral", "negative", "reject"):
        sig = "neutral"
    stage = parsed.get("next_stage")
    if stage not in ("interview", "replied", "rejected", "closed"):
        stage = "replied"
    return {"intent": str(parsed.get("intent") or "")[:80],
            "signal": sig,
            "advice": str(parsed.get("advice") or "")[:160],
            "drafts": [str(x)[:400] for x in (parsed.get("drafts") or [])][:3],
            "need_confirm": [str(x)[:120] for x in
                             (parsed.get("need_confirm") or [])][:5],
            "next_stage": stage}


# ---------------- 防编造校验 ----------------
_NUM_RE = re.compile(r"\d+(?:\.\d+)?(?:万|k|K|%|％|年|个月|人|元|倍)?")


def _nums(text):
    return set(_NUM_RE.findall(str(text or "")))


def _guard(old_text, new_text):
    """新简历出现原简历没有的数字或公司名 → 告警（不拦截，人工判断）。"""
    warnings = []
    added = _nums(new_text) - _nums(old_text)
    added = {n for n in added if n not in {"1", "2", "3"}}
    if added:
        warnings.append("出现原简历没有的数字：" + "、".join(sorted(added)[:10]))
    olds = re.findall(r"[一-龥A-Za-z（）()]{2,12}(?:有限公司|集团|股份|科技(?!部)|技术公司)",
                      old_text or "")
    news = re.findall(r"[一-龥A-Za-z（）()]{2,12}(?:有限公司|集团|股份|科技(?!部)|技术公司)",
                      new_text or "")
    new_com = set(news) - set(olds)
    if new_com:
        warnings.append("出现原简历没有的公司：" + "、".join(sorted(new_com)[:6]))
    return warnings
