"""tools.py：Agent 的工具箱。程序退化为「能力提供方」，由 AI 决定调用顺序。

三级风险（决定闸门强度，这是账号安全的唯一防线）：
  local —— 零触达：查库、AI 加工、导出。Agent 可自由调用，不需要任何确认。
  read  —— 只读触达平台：搜索采集、读 JD。过冷却 + 配额 + 平台开关 + 在线闸。
  write —— 写触达平台：投递/打招呼。**Agent 无权自主执行**，一律挂起等人工确认。

工具返回值一律裁剪（不回传 JD 全文等大字段），避免 token 爆炸。
本模块不 import app，所需运行时能力由 ToolContext 注入，避免循环依赖。
"""
import json

import ai
import citycfg
import config
import db
import rules

REGISTRY = {}

RISK_LOCAL, RISK_READ, RISK_WRITE = "local", "read", "write"
RISK_NAMES = {RISK_LOCAL: "零触达", RISK_READ: "只读访问平台",
              RISK_WRITE: "写操作（需人工确认）"}


class ToolContext:
    """运行时依赖注入：由 app.py 构造，避免 tools 反向 import app。"""

    def __init__(self, log=print, live=lambda: False, collect=None,
                 read_jd=None, apply_one=None, bot=None):
        self.log = log
        self.live = live
        self.collect = collect          # fn(platforms, kw, cities, pages) -> dict
        self.read_jd = read_jd          # fn(job) -> (text, err)
        self.apply_one = apply_one      # fn(job) -> (ok, msg)
        self.bot = bot


def tool(name, risk, desc, params=None):
    def deco(fn):
        REGISTRY[name] = {"name": name, "risk": risk, "desc": desc,
                          "params": params or {"type": "object", "properties": {}},
                          "fn": fn}
        return fn
    return deco


def schemas():
    """OpenAI function-calling 格式的工具清单。"""
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["desc"], "parameters": t["params"]}}
        for t in REGISTRY.values()]


def risk_of(name):
    return (REGISTRY.get(name) or {}).get("risk", RISK_WRITE)


def describe(name):
    t = REGISTRY.get(name)
    return f"{name}（{RISK_NAMES.get(t['risk'], '未知')}）" if t else name


def execute(name, args, ctx):
    """执行工具。返回 dict（会被 JSON 序列化回传给模型）。"""
    t = REGISTRY.get(name)
    if not t:
        return {"error": f"未知工具：{name}"}
    try:
        return t["fn"](ctx, **(args or {}))
    except TypeError as e:
        return {"error": f"参数不匹配：{str(e)[:120]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"}


def _slim(j, with_jd=False):
    """裁剪岗位字段，控制回传体积。"""
    out = {"id": j["id"], "platform": j["platform"], "title": j.get("title"),
           "company": j.get("company"), "salary": j.get("salary_text"),
           "city": j.get("city"), "status": j.get("status"),
           "ai_tier": j.get("ai_tier"), "ai_why": (j.get("ai_why") or "")[:80],
           "score": j.get("score"), "customized": bool(j.get("customized")),
           "has_jd": bool((j.get("jd") or "").strip()),
           "stage": j.get("p_stage") or ""}
    if with_jd:
        out["jd"] = (j.get("jd") or "")[:1500]
    return out


# ================= local：零触达 =================

@tool("get_context", RISK_LOCAL,
      "获取当前全部上下文：我的简历摘要、薪资底线、各平台岗位统计、风控与开关状态、"
      "跟进看板计数、在线模式。开始任何任务前应先调用一次。")
def t_context(ctx):
    import risk
    import resume as R
    floor = int(db.setting("salary_floor", config.SALARY_FLOOR))
    return {
        "me": {
            "resume_summary": R.to_text(db.resume_load())[:900],
            "salary_floor": floor,
            "hard_constraints": "非统招本科，需回避明确要求统招/全日制本科、985/211、"
                                "35岁以下的岗位；目标为 IT 管理/总监/CIO/数字化方向",
        },
        "live_mode": bool(ctx.live()),
        "job_stats": db.job_stats(),
        "pipeline": db.pipeline_counts(),
        "risk": risk.status(),
        "platform_names": config.PLATFORM_NAMES,
        "apply_list_count": len(db.apply_list(limit=500)),
        "ai_configured": ai.configured(),
    }


@tool("list_jobs", RISK_LOCAL, "按条件查询已采集的岗位。返回裁剪后的列表（不含 JD 全文）。",
      {"type": "object", "properties": {
          "platform": {"type": "string", "enum": list(config.PLATFORM_NAMES)},
          "status": {"type": "string", "description": "如 待投/已投/new/不投-薪资低"},
          "ai_tier": {"type": "string", "enum": ["强匹配", "匹配", "弱匹配", "不匹配"]},
          "keyword": {"type": "string", "description": "职位或公司名模糊匹配"},
          "city": {"type": "string"},
          "limit": {"type": "integer", "description": "默认20，最大80"}},
       })
def t_list_jobs(ctx, platform=None, status=None, ai_tier=None, keyword=None,
                city=None, limit=20):
    rows = db.list_jobs(platform, status)
    if ai_tier:
        rows = [r for r in rows if r.get("ai_tier") == ai_tier]
    if keyword:
        k = str(keyword).lower()
        rows = [r for r in rows
                if k in (r.get("title") or "").lower() or k in (r.get("company") or "").lower()]
    if city:
        rows = [r for r in rows if (r.get("city") or "") == city]
    n = max(1, min(int(limit or 20), 80))
    return {"total_matched": len(rows), "returned": min(n, len(rows)),
            "jobs": [_slim(r) for r in rows[:n]]}


@tool("get_apply_list", RISK_LOCAL,
      "获取「今日投递清单」：可投状态且 AI 判定为强匹配/匹配的岗位，按匹配度排序。"
      "这是推荐给用户去投的清单。",
      {"type": "object", "properties": {
          "platform": {"type": "string", "enum": list(config.PLATFORM_NAMES)},
          "limit": {"type": "integer"}}})
def t_apply_list(ctx, platform=None, limit=20):
    rows = db.apply_list(platform, max(1, min(int(limit or 20), 100)))
    return {"count": len(rows),
            "need_jd": sum(1 for r in rows if not (r.get("jd") or "").strip()),
            "jobs": [_slim(r) for r in rows]}


@tool("score_jobs", RISK_LOCAL,
      "对已采集但还没做 AI 匹配度判定的岗位跑批量判定（每批25条）。"
      "采集之后、筛选之前调用。",
      {"type": "object", "properties": {
          "platform": {"type": "string", "enum": list(config.PLATFORM_NAMES)}}})
def t_score_jobs(ctx, platform=None):
    if not ai.configured():
        return {"error": "AI 未配置"}
    rows = db.jobs_for_ai(platform)
    if not rows:
        return {"done": 0, "note": "没有待判定的岗位"}
    done, tiers = 0, {}
    for i in range(0, len(rows), 25):
        try:
            res = ai.score_jobs(rows[i:i + 25])
        except Exception as e:
            ctx.log(f"AI 判定中断：{str(e).splitlines()[0][:80]}")
            break
        for jid, (tier, why) in res.items():
            db.set_ai(jid, tier, why)
            tiers[tier] = tiers.get(tier, 0) + 1
            done += 1
    ctx.log(f"AI 判定完成 {done} 条")
    return {"done": done, "remaining": len(rows) - done, "tiers": tiers}


@tool("tailor_resume", RISK_LOCAL,
      "基于我的真实简历，为指定岗位定制一版简历（防编造校验）。"
      "前提：该岗位库里已有 JD，否则会返回 error（不会为此访问平台）。",
      {"type": "object", "properties": {
          "job_id": {"type": "integer", "description": "岗位 id"}},
       "required": ["job_id"]})
def t_tailor(ctx, job_id):
    j = db.get_job(int(job_id))
    if not j:
        return {"error": f"岗位 {job_id} 不存在"}
    jd = (j.get("jd") or "").strip()
    if len(jd) < 30:
        return {"error": "该岗位库里没有 JD。请先用 read_jd 获取（会访问平台），"
                         "或跳过这条", "job_id": j["id"]}
    target = f"{j.get('company','')} · {j.get('title','')}"
    out = ai.tailor(jd, target)
    db.save_version(j["id"], target, out["resume"], out["notes"],
                    out["gaps"], out["warnings"])
    with db.connect() as con:
        con.execute("UPDATE jobs SET customized=1 WHERE id=?", (j["id"],))
    return {"ok": True, "job_id": j["id"], "target": target,
            "notes": out["notes"][:5], "gaps": out["gaps"][:5],
            "warnings": out["warnings"],
            "hint": "已存入简历版本记录，用户可在「简历」页查看并导出 PDF"}


@tool("set_job_status", RISK_LOCAL, "修改岗位状态（如标记 不投-人工 / 待投）。",
      {"type": "object", "properties": {
          "job_id": {"type": "integer"},
          "status": {"type": "string", "description": "待投/不投-人工/待定-需确认 等"},
          "reason": {"type": "string"}},
       "required": ["job_id", "status"]})
def t_set_status(ctx, job_id, status, reason=""):
    db.set_job_status(int(job_id), str(status)[:40], str(reason or "")[:120])
    return {"ok": True, "job_id": int(job_id), "status": status}


@tool("add_blacklist", RISK_LOCAL, "把公司加入黑名单，之后采集到该公司岗位会自动判为不投。",
      {"type": "object", "properties": {
          "company": {"type": "string"}, "reason": {"type": "string"}},
       "required": ["company"]})
def t_blacklist(ctx, company, reason=""):
    db.blacklist_add(str(company).strip(), "ai", str(reason or "")[:120])
    return {"ok": True, "company": company}


@tool("update_pipeline", RISK_LOCAL,
      "更新岗位的跟进阶段（看板）。阶段：applied/read/replied/interview/offer/rejected/closed。",
      {"type": "object", "properties": {
          "job_id": {"type": "integer"},
          "stage": {"type": "string", "enum": db.STAGES},
          "note": {"type": "string"}},
       "required": ["job_id", "stage"]})
def t_pipeline(ctx, job_id, stage, note=None):
    db.pipeline_set(int(job_id), stage=stage, note=note)
    return {"ok": True, "counts": db.pipeline_counts()}


@tool("reply_assist", RISK_LOCAL,
      "分析 HR/招聘方发来的消息，判断意图并生成回复草稿。**只产出草稿，绝不发送**。"
      "草稿严格基于我的简历，缺信息会用占位符标注。",
      {"type": "object", "properties": {
          "hr_message": {"type": "string", "description": "对方发来的原文"},
          "job_id": {"type": "integer", "description": "关联岗位（可选）"}},
       "required": ["hr_message"]})
def t_reply(ctx, hr_message, job_id=None):
    job = db.get_job(int(job_id)) if job_id else None
    return ai.reply_assist(hr_message, job)


@tool("export_apply_list", RISK_LOCAL, "把今日投递清单导出为 Markdown 文件，方便手机查看或打印。")
def t_export(ctx):
    rows = db.apply_list(limit=500)
    if not rows:
        return {"error": "清单为空"}
    from datetime import datetime
    lines = [f"# 今日投递清单 · {datetime.now():%Y-%m-%d %H:%M}", "",
             f"共 {len(rows)} 条，按 AI 匹配度排序。投递请在平台手动完成。", ""]
    for i, j in enumerate(rows, 1):
        lines += [f"## {i}. {j.get('title','')} · {j.get('company','')}",
                  f"- {config.PLATFORM_NAMES.get(j['platform'], j['platform'])}"
                  f"｜{j.get('city','')}｜{j.get('salary_text','')}",
                  f"- AI：**{j.get('ai_tier') or '待判'}** {j.get('ai_why','') or ''}",
                  f"- {j.get('url','')}",
                  f"- 定制简历：{'已生成' if j.get('customized') else '未生成'}", ""]
    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = config.EXPORT_DIR / f"投递清单-{datetime.now():%Y%m%d-%H%M}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    ctx.log(f"投递清单已导出：{p.name}")
    return {"ok": True, "file": str(p), "count": len(rows)}


@tool("review_plan", RISK_LOCAL, "让 AI 评估当前采集计划是否值得执行，并给出改写建议。")
def t_review_plan(ctx):
    rows = db.plan_list()
    if not rows:
        return {"error": "采集计划为空，请先用 save_plan 生成"}
    return ai.review_plan(rows)


@tool("save_plan", RISK_LOCAL,
      "写入采集计划（整表替换）。每行含 platform/keyword/city/reason。"
      "采集时会按启用的行执行。",
      {"type": "object", "properties": {
          "items": {"type": "array", "items": {"type": "object", "properties": {
              "platform": {"type": "string", "enum": list(config.PLATFORM_NAMES)},
              "keyword": {"type": "string"}, "city": {"type": "string"},
              "reason": {"type": "string"},
              "enabled": {"type": "integer", "description": "1启用 0停用"}}}},
          "replace_platform": {"type": "string"}},
       "required": ["items"]})
def t_save_plan(ctx, items, replace_platform=None):
    if not items:
        return {"error": "计划为空"}
    bad = [i for i in items if i.get("platform") not in config.PLATFORM_NAMES]
    if bad:
        return {"error": f"平台名不合法：{bad[0].get('platform')}"}
    db.plan_save(items, replace_platform)
    return {"ok": True, "saved": len(items), "total": len(db.plan_list())}


@tool("run_rules", RISK_LOCAL,
      "对新入库岗位跑规则引擎（薪资底线/学历硬门槛/关键词黑白名单），产出 待投/不投 等状态。")
def t_run_rules(ctx):
    floor = int(db.setting("salary_floor", config.SALARY_FLOOR))
    n = 0
    for j in db.list_jobs(status="new"):
        if db.blacklist_has(j.get("company") or ""):
            db.set_job_status(j["id"], "不投-黑名单", "公司在黑名单")
            n += 1
            continue
        st, why = rules.verdict({"title": j["title"], "city": j["city"],
                                 "salary": j["salary_text"], "edu": j["edu"],
                                 "exp": j["exp"]}, floor=floor)
        tier, sc = rules.score(j["title"], j["city"], j["salary_text"], floor=floor)
        with db.connect() as con:
            con.execute("UPDATE jobs SET status=?, reason=?, tier=?, score=?, "
                        "salary_cap=? WHERE id=?",
                        (st, why, tier, sc, rules.salary_cap(j["salary_text"]), j["id"]))
        n += 1
    return {"judged": n, "floor": floor}


# ================= read：只读触达平台 =================

@tool("search_jobs", RISK_READ,
      "采集岗位（只读，不投递）。受冷却/配额/平台开关约束，失败会返回 error 而不是抛异常。"
      "**一次只采一个平台、页数≤2**，避免触发风控。猎聘不吃关键词（推荐流）。",
      {"type": "object", "properties": {
          "platform": {"type": "string", "enum": list(config.PLATFORM_NAMES)},
          "keywords": {"type": "array", "items": {"type": "string"},
                       "description": "最多3个关键词"},
          "cities": {"type": "array", "items": {"type": "string"},
                     "description": f"可选：{citycfg.names()}"},
          "pages": {"type": "integer", "description": "每关键词页数，最大2"}},
       "required": ["platform"]})
def t_search(ctx, platform, keywords=None, cities=None, pages=1):
    if not ctx.live():
        return {"error": "离线模式：真实访问未开启，无法采集"}
    kw = [str(k).strip() for k in (keywords or []) if str(k).strip()][:3]
    cs = [c for c in (cities or []) if c in citycfg.names()]
    if cities and not cs:
        return {"error": f"城市不合法，可选：{citycfg.names()}"}
    if not ctx.collect:
        return {"error": "采集能力未注入"}
    pg = max(1, min(int(pages or 1), 2))
    ctx.log(f"Agent 请求采集：{config.PLATFORM_NAMES[platform]} "
            f"关键词={kw or '按计划'} 城市={cs or '全部'} 页数={pg}")
    res = ctx.collect([platform], kw or None, cs or None, pg)
    return res if isinstance(res, dict) else {"added": 0, "note": "采集已执行"}


@tool("read_jd", RISK_READ,
      "读取单个岗位的完整 JD（会访问平台详情页）。用于定制简历前补齐信息。"
      "**一次只读一条**，读完请间隔再读下一条。",
      {"type": "object", "properties": {"job_id": {"type": "integer"}},
       "required": ["job_id"]})
def t_read_jd(ctx, job_id):
    if not ctx.live():
        return {"error": "离线模式：真实访问未开启"}
    j = db.get_job(int(job_id))
    if not j:
        return {"error": f"岗位 {job_id} 不存在"}
    if not ctx.read_jd:
        return {"error": "读 JD 能力未注入"}
    text, err = ctx.read_jd(j)
    if not text:
        return {"error": err or "没读到 JD", "job_id": j["id"]}
    db.save_jd(j["id"], text)
    return {"ok": True, "job_id": j["id"], "jd_chars": len(text),
            "jd_head": text[:600]}


# ================= write：必须人工确认 =================

@tool("apply_job", RISK_WRITE,
      "投递/打招呼单个岗位。**这是写操作，会被闸门挂起等待用户人工确认，你不要重复调用。**"
      "调用前必须确认该岗位 AI 判定为强匹配或匹配。",
      {"type": "object", "properties": {
          "job_id": {"type": "integer"},
          "reason": {"type": "string", "description": "为什么推荐投这条（会展示给用户）"}},
       "required": ["job_id"]})
def t_apply(ctx, job_id, reason=""):
    if not ctx.apply_one:
        return {"error": "投递能力未注入"}
    j = db.get_job(int(job_id))
    if not j:
        return {"error": f"岗位 {job_id} 不存在"}
    ok, msg = ctx.apply_one(j)
    db.log_app(j["id"], j["platform"], "apply", ok, msg)
    if ok:
        db.set_job_status(j["id"], "已投", msg)
        db.pipeline_set(j["id"], stage="applied", note=str(reason or "")[:200])
    return {"ok": ok, "job_id": j["id"], "msg": msg,
            "title": j.get("title"), "company": j.get("company")}
