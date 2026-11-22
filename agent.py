"""agent.py：AI 主导的编排层。程序只提供工具与安全闸门，决策交给模型。

与旧架构的区别：
  旧 —— 用户点按钮 → 固定流程（_task_collect 里写死采集→规则→打分）
  新 —— 用户说目标 → 模型自己决定调哪个工具、什么顺序、要不要再来一轮

安全设计（账号安全高于一切，不可协商）：
  local 工具  模型可自由调用，零触达
  read  工具  过闸门：在线模式 + 平台开关 + 冷却 + 配额；被拒时把原因回传给模型，
              让它自己改策略（而不是崩掉）
  write 工具  **模型无权执行**，一律挂起等人确认；确认后由 resume() 继续

另有步数上限、无进展检测、消息裁剪，防止模型绕圈子烧 token。
"""
import json
import threading
import time
from datetime import datetime

import ai
import config
import db
import risk
import tools

SYSTEM_PROMPT = """你是 jobpilot 的求职 Agent，替用户完成求职流程。你可以调用工具，自己决定顺序和策略。

## 用户背景（不得违背）
- 目标方向：IT 管理 / IT 总监 / CIO / 信息化 / 数字化，珠海·中山·广州·深圳·东莞
- 薪资底线：见 get_context 返回的 salary_floor（默认 27000/月）
- 学历：非统招本科。凡是 JD 明确要求「统招本科/全日制本科/985/211」的岗位，直接判为不投，不要推荐
- 简历内容**绝对不可编造**：定制简历只能重排和改写已有事实

## 工具使用纪律（违反会导致用户账号被封，务必遵守）
1. 任何任务开始先调 get_context，了解已有数据和平台风控状态
2. read 类工具（search_jobs / read_jd）会真实访问招聘平台：
   - search_jobs 一次只传一个平台，pages ≤ 2
   - read_jd 一次只读一条，不要连续批量读
   - 返回里出现「冷却」「风控」「异常」「未开启」等字样时，**立即停止该平台，不要重试**，
     转而处理本地数据或换平台，并在最终总结里告知用户
3. write 类工具（apply_job）会被系统挂起等待用户人工确认：
   - 调用一次就够，不要重复调用同一个岗位
   - 只对 ai_tier 为「强匹配」或「匹配」的岗位调用
4. 优先用 local 工具把价值做出来：score_jobs 判定 → 筛选 → tailor_resume 定制 →
   export_apply_list 出清单。这些零风险，可以放手做
5. 工具返回 error 时，读懂原因再决定下一步，不要盲目重试同一个调用

## 输出要求
- 每次调用工具前，用一句话说明你要做什么、为什么
- 最终给中文总结：做了什么、结果如何、用户接下来该做什么、有哪些被闸门拦下了
- 不要输出 JSON，不要编造工具没返回的数据"""

MAX_STEPS = 25              # 步数硬上限
MAX_TOOL_PER_STEP = 4       # 单步最多工具数
NO_PROGRESS_LIMIT = 4       # 连续无进展步数上限
MAX_MESSAGES = 40           # 消息裁剪阈值
RESULT_CHARS = 2200         # 单个工具结果回传上限


class Paused(Exception):
    """write 工具被闸门挂起。"""


ST = {"running": False, "goal": "", "step": 0, "result": "", "error": "",
      "pending": None, "messages": [], "events": [], "started_at": "",
      "tool_calls": 0, "blocked": 0}
_lock = threading.Lock()
_ev_seq = [0]
_log_fn = None
# 这些事件同时写进主运行日志，否则用户在总览看不到 agent 为什么停了
_MIRROR = ("error", "blocked", "confirm", "done", "approved", "denied", "stop")


def set_logger(fn):
    global _log_fn
    _log_fn = fn


def _ev(type_, **kw):
    _ev_seq[0] += 1
    e = {"seq": _ev_seq[0], "ts": datetime.now().strftime("%H:%M:%S"),
         "type": type_, "step": ST["step"], **kw}
    ST["events"].append(e)
    if len(ST["events"]) > 400:
        del ST["events"][:200]
    if _log_fn and type_ in _MIRROR:
        txt = kw.get("text") or kw.get("reason") or ""
        name = kw.get("name") or ""
        _log_fn(f"[Agent] {type_}" + (f" {name}" if name else "")
                + (f"：{str(txt)[:150]}" if txt else ""))
    return e


def events(since=0):
    return [e for e in ST["events"] if e["seq"] > since]


def state():
    return {k: ST[k] for k in ("running", "goal", "step", "result", "error",
                               "pending", "started_at", "tool_calls", "blocked")}


# ---------------- 闸门 ----------------

def gate(name, args, ctx):
    """返回 (放行?, 原因)。True 放行 / False 拒绝 / None 挂起等确认。"""
    r = tools.risk_of(name)
    if r == tools.RISK_LOCAL:
        return True, ""
    if r == tools.RISK_WRITE:
        return None, "需要人工确认"
    plat = (args or {}).get("platform")
    if not plat:                       # read_jd 只给 job_id，从库里反查平台
        jid = (args or {}).get("job_id")
        j = db.get_job(int(jid)) if jid else None
        plat = (j or {}).get("platform")
    if not ctx.live():
        return False, "离线模式：真实访问未开启（需 JOBPILOT_ALLOW_LIVE=1）"
    if not plat:
        return True, ""
    if not risk.platform_on(plat):
        reason = db.setting(f"off_reason_{plat}", "") or ""
        return False, (f"{config.PLATFORM_NAMES.get(plat, plat)} 曾被风控自动关停"
                       if reason == "risk" else
                       f"{config.PLATFORM_NAMES.get(plat, plat)} 已被用户暂停")
    try:
        risk.check(plat)
    except risk.Cooldown as e:
        return False, str(e)[:120]
    if db.quota_left(plat, "collect", config.RATE[plat]["daily_collect"]) <= 0:
        return False, f"{config.PLATFORM_NAMES.get(plat, plat)} 今日采集轮次已用完"
    return True, ""


# ---------------- 循环 ----------------

def _trim(messages):
    """裁剪历史：保留 system + 最近若干条，旧的工具结果截断。"""
    if len(messages) <= MAX_MESSAGES:
        return messages
    tail = messages[-(MAX_MESSAGES - 1):]
    for m in tail:
        if m.get("role") == "tool" and len(m.get("content") or "") > 600:
            m["content"] = m["content"][:600] + "…(已截断)"
    return [messages[0]] + tail


def _progress(results):
    """这一步有没有实质进展（防绕圈）。"""
    return any(isinstance(r, dict) and (r.get("ok") or not r.get("error"))
               for r in results)


def _run_steps(msgs, ctx, max_steps):
    """主循环。首轮与确认后续跑共用。遇 write 工具抛 Paused（msgs 已存入 ST）。"""
    no_progress = 0
    while ST["step"] < max_steps:
        ST["step"] += 1
        try:
            msg = ai.chat_msg(_trim(msgs), tools=tools.schemas(), timeout=180)
        except Exception as e:
            ST["error"] = f"模型调用失败：{str(e).splitlines()[0][:160]}"
            _ev("error", text=ST["error"])
            return
        msgs.append(msg)
        think = (msg.get("content") or "").strip()
        if think:
            _ev("think", text=think[:800])
        calls = (msg.get("tool_calls") or [])[:MAX_TOOL_PER_STEP]
        if not calls:
            ST["result"] = think or "（模型没有给出结论）"
            _ev("done", text=ST["result"][:1500])
            return

        results = []
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            ST["tool_calls"] += 1
            ok, reason = gate(name, args, ctx)
            _ev("tool", name=name, args=args, risk=tools.risk_of(name),
                label=tools.describe(name))

            if ok is None:                          # write：挂起等人工确认
                ST["pending"] = {"call": c, "name": name, "args": args,
                                 "reason": args.get("reason") or "",
                                 "asked_at": datetime.now().strftime("%H:%M:%S")}
                ST["messages"] = msgs
                _ev("confirm", name=name, args=args,
                    text=f"「{name}」是写操作，已挂起等待你确认")
                raise Paused()

            if ok is False:                         # 闸门拒绝：原因回传给模型自行调整
                ST["blocked"] += 1
                res = {"error": f"闸门拒绝：{reason}", "tool": name}
                _ev("blocked", name=name, reason=reason)
            else:
                t0 = time.time()
                res = tools.execute(name, args, ctx)
                _ev("result", name=name, secs=round(time.time() - t0, 1),
                    ok=not (isinstance(res, dict) and res.get("error")),
                    summary=json.dumps(res, ensure_ascii=False)[:400])
            results.append(res)
            msgs.append({"role": "tool", "tool_call_id": c.get("id") or "",
                         "content": json.dumps(res, ensure_ascii=False)[:RESULT_CHARS]})

        if _progress(results):
            no_progress = 0
        else:
            no_progress += 1
            if no_progress >= NO_PROGRESS_LIMIT:
                ST["result"] = (f"连续 {no_progress} 步没有实质进展（多是被闸门拦下或数据"
                                "已处理完），已主动收敛。请看执行记录。")
                _ev("done", text=ST["result"])
                return
    ST["result"] = f"已达步数上限 {max_steps}，任务未自然结束。可缩小目标后重试。"
    _ev("done", text=ST["result"])


def _guard(body, ctx):
    """统一的线程包装：捕获 Paused / 异常，收尾置 running=False。"""
    try:
        body()
    except Paused:
        pass
    except Exception as e:
        ST["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        _ev("error", text=ST["error"])
    finally:
        ST["running"] = False


def start(goal, ctx, max_steps=MAX_STEPS):
    """后台启动 agent。返回是否成功启动。"""
    with _lock:
        if ST["running"]:
            return False
        if ST["pending"]:
            return False
        ST.update({"running": True, "goal": goal, "step": 0, "result": "",
                   "error": "", "pending": None, "messages": [],
                   "started_at": datetime.now().strftime("%H:%M:%S"),
                   "tool_calls": 0, "blocked": 0})
        _ev_seq[0] = 0
        ST["events"] = []

    def body():
        if not ai.configured():
            ST["error"] = "AI 未配置：请先到设置页填 Base URL / 模型 / API Key"
            _ev("error", text=ST["error"])
            return
        msgs = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": goal}]
        ST["messages"] = msgs
        _ev("start", goal=goal)
        _run_steps(msgs, ctx, max_steps)

    threading.Thread(target=_guard, args=(body, ctx), daemon=True,
                     name="agent").start()
    return True


def resume(approved, ctx, note=""):
    """用户对待确认的 write 操作表态后继续。拒绝时把原因回传给模型。"""
    with _lock:
        pend = ST["pending"]
        if not pend:
            return False, "没有待确认的操作"
        if ST["running"]:
            return False, "Agent 正在运行中"
        ST["pending"] = None
        ST["running"] = True

    msgs = ST["messages"]
    call, name, args = pend["call"], pend["name"], pend["args"]
    if approved:
        _ev("approved", name=name, args=args, text=f"用户已批准 {name}")
        res = tools.execute(name, args, ctx)
        db.event("agent", "", "agent_approved",
                 f"{name} {json.dumps(args, ensure_ascii=False)[:100]} {note}"[:200])
    else:
        _ev("denied", name=name, args=args, text=f"用户拒绝了 {name}")
        res = {"error": f"用户拒绝执行此操作（{note or '未说明原因'}）。"
                        "不要再尝试该岗位，继续处理其他事项或直接总结。"}
        db.event("agent", "", "agent_denied", f"{name} {note}"[:160])
    msgs.append({"role": "tool", "tool_call_id": call.get("id") or "",
                 "content": json.dumps(res, ensure_ascii=False)[:RESULT_CHARS]})
    _ev("result", name=name,
        ok=bool(approved and isinstance(res, dict) and res.get("ok")),
        summary=json.dumps(res, ensure_ascii=False)[:400])

    def body():
        _run_steps(msgs, ctx, MAX_STEPS)

    threading.Thread(target=_guard, args=(body, ctx), daemon=True,
                     name="agent-resume").start()
    return True, "已继续"


def stop():
    """中止（下一步生效；已发出的工具调用会跑完）。"""
    ST["step"] = MAX_STEPS + 1
    _ev("stop", text="用户请求中止")
    return True


def clear_pending():
    """丢弃挂起的确认（不执行，也不回传给模型）。"""
    with _lock:
        if not ST["pending"]:
            return False
        _ev("denied", name=ST["pending"]["name"], text="挂起的确认已被丢弃")
        ST["pending"] = None
        ST["messages"] = []
        ST["running"] = False
    return True
