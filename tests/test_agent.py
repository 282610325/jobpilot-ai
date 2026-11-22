"""Agent 层测试：闸门分级、write 挂起/恢复、工具容错、无进展收敛。

关键保证：模型无论如何都不能自主执行 write（投递）——这是账号安全的底线。
"""
import json

import pytest

import agent
import ai
import config
import db
import risk
import tools


@pytest.fixture(autouse=True)
def reset_agent():
    """每个测试前后清空 agent 全局状态，避免串味。"""
    agent.ST.update({"running": False, "goal": "", "step": 0, "result": "",
                     "error": "", "pending": None, "messages": [], "events": [],
                     "started_at": "", "tool_calls": 0, "blocked": 0})
    yield
    agent.ST.update({"running": False, "pending": None, "messages": [],
                     "events": []})


def ctx(live=False):
    return tools.ToolContext(log=lambda m: None, live=lambda: live)


def seed_job(platform="zhaopin", **kw):
    j = {"ext_id": "X1", "url": "https://jobs.zhaopin.com/X1.htm",
         "title": "IT总监", "company": "测试公司", "salary": "30-50K",
         "city": "珠海", "edu": "本科", "exp": "10年", "match": "",
         "publish": "", "security_id": "", "lid": ""}
    j.update(kw)
    jid, _ = db.upsert_job(platform, j)
    return jid


# ---------------- 闸门 ----------------

def test_gate_local_always_passes():
    for name in ("get_context", "list_jobs", "score_jobs", "tailor_resume",
                 "export_apply_list", "reply_assist", "run_rules"):
        ok, why = agent.gate(name, {}, ctx(live=False))
        assert ok is True, f"{name} 应无条件放行，实际 {why}"


def test_gate_write_always_pauses():
    """write 工具必须挂起，任何情况下都不放行给模型。"""
    ok, why = agent.gate("apply_job", {"job_id": 1}, ctx(live=True))
    assert ok is None
    assert "确认" in why


def test_gate_read_blocked_when_offline():
    ok, why = agent.gate("search_jobs", {"platform": "zhaopin"}, ctx(live=False))
    assert ok is False
    assert "离线" in why


def test_gate_read_blocked_when_boss_off():
    db.set_setting("boss_enabled", 0)
    ok, why = agent.gate("search_jobs", {"platform": "boss"}, ctx(live=True))
    assert ok is False
    assert "BOSS" in why


def test_gate_read_blocked_when_platform_disabled():
    db.set_setting("enabled_liepin", 0)
    ok, why = agent.gate("search_jobs", {"platform": "liepin"}, ctx(live=True))
    assert ok is False
    assert "暂停" in why


def test_gate_read_blocked_when_platform_risk_off():
    db.set_setting("enabled_liepin", 0)
    db.set_setting("off_reason_liepin", "risk")
    ok, why = agent.gate("search_jobs", {"platform": "liepin"}, ctx(live=True))
    assert ok is False
    assert "风控" in why


def test_gate_read_blocked_during_cooldown():
    risk.trip("zhaopin", "测试触发", minutes=30)
    ok, why = agent.gate("search_jobs", {"platform": "zhaopin"}, ctx(live=True))
    assert ok is False
    assert "冷却" in why


def test_gate_read_passes_when_healthy():
    db.set_setting("enabled_zhaopin", 1)
    ok, why = agent.gate("search_jobs", {"platform": "zhaopin"}, ctx(live=True))
    assert ok is True, why


def test_gate_read_jd_resolves_platform_from_db():
    """read_jd 只给 job_id，闸门要能反查平台再判定。"""
    jid = seed_job()
    risk.trip("zhaopin", "测试", minutes=30)
    ok, why = agent.gate("read_jd", {"job_id": jid}, ctx(live=True))
    assert ok is False and "冷却" in why


# ---------------- 工具注册表 ----------------

def test_registry_risk_coverage():
    assert tools.risk_of("apply_job") == tools.RISK_WRITE
    assert tools.risk_of("search_jobs") == tools.RISK_READ
    assert tools.risk_of("get_context") == tools.RISK_LOCAL
    # 未注册的工具按最严格处理（默认 write），防止拼错名字绕过闸门
    assert tools.risk_of("not_a_real_tool") == tools.RISK_WRITE


def test_schemas_shape():
    s = tools.schemas()
    assert len(s) == len(tools.REGISTRY)
    for t in s:
        assert t["type"] == "function"
        assert t["function"]["name"] and t["function"]["description"]
        assert t["function"]["parameters"]["type"] == "object"


def test_execute_unknown_tool_returns_error():
    res = tools.execute("nope", {}, ctx())
    assert "error" in res


def test_execute_bad_args_returns_error_not_raise():
    res = tools.execute("list_jobs", {"not_a_param": 1, "limit": "abc"}, ctx())
    assert isinstance(res, dict)
    assert "error" in res


def test_execute_get_context_shape():
    seed_job()
    res = tools.execute("get_context", {}, ctx())
    assert res["me"]["salary_floor"] == config.SALARY_FLOOR
    assert "非统招本科" in res["me"]["hard_constraints"]
    assert res["live_mode"] is False
    assert "job_stats" in res and "risk" in res


def test_execute_list_jobs_filters():
    seed_job(title="IT总监", company="甲公司")
    seed_job(ext_id="X2", url="https://jobs.zhaopin.com/X2.htm",
             title="运维工程师", company="乙公司")
    res = tools.execute("list_jobs", {"keyword": "总监"}, ctx())
    assert res["total_matched"] == 1
    assert res["jobs"][0]["company"] == "甲公司"
    assert "jd" not in res["jobs"][0]          # 不回传大字段


def test_execute_apply_list_sorted_by_tier():
    a = seed_job(title="IT总监", ext_id="A", url="https://x/A.htm")
    b = seed_job(title="IT经理", ext_id="B", url="https://x/B.htm")
    db.set_ai(a, "匹配", "还行")
    db.set_ai(b, "强匹配", "很配")
    with db.connect() as con:
        con.execute("UPDATE jobs SET status='待投'")
    res = tools.execute("get_apply_list", {}, ctx())
    assert [j["title"] for j in res["jobs"]] == ["IT经理", "IT总监"]


def test_execute_tailor_refuses_without_jd():
    """没有 JD 时必须拒绝，绝不为了拿 JD 去访问平台。"""
    jid = seed_job()
    res = tools.execute("tailor_resume", {"job_id": jid}, ctx())
    assert "error" in res and "JD" in res["error"]


def test_execute_search_blocked_offline():
    res = tools.execute("search_jobs", {"platform": "zhaopin"}, ctx(live=False))
    assert "error" in res and "离线" in res["error"]


def test_execute_rejects_bad_city():
    res = tools.execute("search_jobs", {"platform": "zhaopin", "cities": ["火星"]},
                        ctx(live=True))
    assert "error" in res and "城市" in res["error"]


def test_execute_pipeline_roundtrip():
    jid = seed_job()
    res = tools.execute("update_pipeline",
                        {"job_id": jid, "stage": "interview", "note": "周三面"}, ctx())
    assert res["ok"] and res["counts"]["interview"] == 1
    assert db.pipeline_list("interview")[0]["p_note"] == "周三面"


def test_execute_blacklist():
    tools.execute("add_blacklist", {"company": "黑名单公司", "reason": "骗面试"}, ctx())
    assert db.blacklist_has("黑名单公司")


def test_execute_reply_assist(monkeypatch):
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai, "chat", lambda *a, **k: json.dumps({
        "intent": "问薪资", "signal": "positive", "advice": "报区间",
        "drafts": ["版本一"], "need_confirm": ["期望薪资"],
        "next_stage": "replied"}, ensure_ascii=False))
    res = tools.execute("reply_assist", {"hr_message": "方便聊聊薪资吗"}, ctx())
    assert res["signal"] == "positive" and res["drafts"] == ["版本一"]
    assert res["need_confirm"] == ["期望薪资"]


def test_reply_assist_normalizes_bad_signal(monkeypatch):
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai, "chat", lambda *a, **k: json.dumps({
        "intent": "x", "signal": "乱来的值", "advice": "y",
        "drafts": [], "need_confirm": [], "next_stage": "也是乱来的"}))
    res = tools.execute("reply_assist", {"hr_message": "在吗"}, ctx())
    assert res["signal"] == "neutral"
    assert res["next_stage"] == "replied"


# ---------------- Agent 循环 ----------------

def _fake_model(script):
    """按脚本依次返回 assistant message。"""
    it = iter(script)

    def fake(messages, tools=None, json_mode=False, timeout=150):
        return next(it)
    return fake


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def test_run_steps_executes_local_tool_then_finishes(monkeypatch):
    seed_job()
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai, "chat_msg", _fake_model([
        {"content": "我先看看库里有什么", "tool_calls": [_call("list_jobs", {})]},
        {"content": "库里有 1 条岗位，建议先做 AI 判定。"},
    ]))
    msgs = [{"role": "user", "content": "盘点一下"}]
    agent._run_steps(msgs, ctx(), 10)
    assert agent.ST["result"].startswith("库里有 1 条")
    assert agent.ST["tool_calls"] == 1
    types = [e["type"] for e in agent.ST["events"]]
    assert "think" in types and "tool" in types and "result" in types and "done" in types


def test_run_steps_pauses_on_write(monkeypatch):
    """模型想投递 → 必须挂起，且工具没有被执行。"""
    jid = seed_job()
    called = []
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai, "chat_msg", _fake_model([
        {"content": "这条很匹配，我去投",
         "tool_calls": [_call("apply_job", {"job_id": jid, "reason": "强匹配"})]},
    ]))

    def boom(job):
        called.append(job["id"])
        return True, "不该被调用"

    c = tools.ToolContext(log=lambda m: None, live=lambda: True, apply_one=boom)
    msgs = [{"role": "user", "content": "投一条"}]
    with pytest.raises(agent.Paused):
        agent._run_steps(msgs, c, 10)
    assert called == []                        # 关键：没有真的投递
    assert agent.ST["pending"]["name"] == "apply_job"
    assert agent.ST["pending"]["args"]["job_id"] == jid
    assert msgs == agent.ST["messages"]        # 对话状态已保存，可恢复


def test_run_blocks_read_tool_and_tells_model(monkeypatch):
    """闸门拒绝时把原因回传给模型，让它自己改策略（不是崩掉）。"""
    monkeypatch.setattr(ai, "configured", lambda: True)
    seen = []

    def fake(messages, tools=None, json_mode=False, timeout=150):
        seen.append([m for m in messages if m.get("role") == "tool"])
        if len(seen) == 1:
            return {"content": "那我采集一下",
                    "tool_calls": [_call("search_jobs", {"platform": "boss"})]}
        return {"content": "BOSS 开关关着，我改用本地数据。"}

    monkeypatch.setattr(ai, "chat_msg", fake)
    db.set_setting("boss_enabled", 0)
    agent._run_steps([{"role": "user", "content": "去 BOSS 采集"}],
                     ctx(live=True), 10)
    assert agent.ST["blocked"] == 1
    fed = json.loads(seen[1][0]["content"])
    assert "闸门拒绝" in fed["error"] and "BOSS" in fed["error"]


def test_no_progress_converges(monkeypatch):
    """连续失败到阈值就主动收敛，不无限烧 token。"""
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai, "chat_msg", lambda *a, **k: {
        "content": "再试一次", "tool_calls": [_call("list_jobs", {"limit": "abc"})]})
    agent._run_steps([{"role": "user", "content": "x"}], ctx(), 50)
    assert "没有实质进展" in agent.ST["result"]
    assert agent.ST["step"] <= agent.NO_PROGRESS_LIMIT + 1


def test_step_limit_respected(monkeypatch):
    monkeypatch.setattr(ai, "configured", lambda: True)
    monkeypatch.setattr(ai, "chat_msg", lambda *a, **k: {
        "content": "继续", "tool_calls": [_call("get_context", {})]})
    agent._run_steps([{"role": "user", "content": "x"}], ctx(), 3)
    assert agent.ST["step"] == 3
    assert "步数上限" in agent.ST["result"]


def test_resume_deny_feeds_reason_back(monkeypatch):
    jid = seed_job()
    monkeypatch.setattr(ai, "configured", lambda: True)
    calls = []

    def fake(messages, tools=None, json_mode=False, timeout=150):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": "投这条",
                    "tool_calls": [_call("apply_job", {"job_id": jid})]}
        return {"content": "好，那我不投了。"}

    monkeypatch.setattr(ai, "chat_msg", fake)
    c = tools.ToolContext(log=lambda m: None, live=lambda: True,
                          apply_one=lambda job: (_ for _ in ()).throw(
                              AssertionError("拒绝后不该执行投递")))
    with pytest.raises(agent.Paused):
        agent._run_steps([{"role": "user", "content": "投"}], c, 10)

    # 直接跑同步版本，避免线程时序问题
    agent.ST["running"] = True
    msgs = agent.ST["messages"]
    pend = agent.ST["pending"]
    agent.ST["pending"] = None
    msgs.append({"role": "tool", "tool_call_id": pend["call"]["id"],
                 "content": json.dumps({"error": "用户拒绝"}, ensure_ascii=False)})
    agent._run_steps(msgs, c, 10)
    assert "不投了" in agent.ST["result"]
    tool_msgs = [m for m in calls[1] if m.get("role") == "tool"]
    assert "用户拒绝" in tool_msgs[0]["content"]


def test_resume_without_pending_fails():
    ok, msg = agent.resume(True, ctx())
    assert ok is False and "没有待确认" in msg


def test_clear_pending():
    agent.ST["pending"] = {"name": "apply_job", "args": {}, "call": {"id": "x"}}
    assert agent.clear_pending() is True
    assert agent.ST["pending"] is None
    assert agent.clear_pending() is False


def test_stop_raises_step_limit():
    agent.ST["step"] = 2
    agent.stop()
    assert agent.ST["step"] > agent.MAX_STEPS


def test_trim_keeps_system_and_caps_length():
    msgs = [{"role": "system", "content": "S"}] + [
        {"role": "tool", "content": "x" * 2000} for _ in range(60)]
    out = agent._trim(msgs)
    assert len(out) == agent.MAX_MESSAGES
    assert out[0]["role"] == "system"
    assert all(len(m["content"]) < 700 for m in out[1:] if m["role"] == "tool")


def test_events_since_cursor():
    agent._ev("think", text="a")
    agent._ev("tool", name="x")
    first = agent.events(0)
    assert len(first) == 2
    assert agent.events(first[0]["seq"]) == [first[1]]
    assert agent.events(first[-1]["seq"]) == []
