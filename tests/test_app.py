import json
import time

import pytest

import app as A


@pytest.fixture
def client():
    A.app.config["TESTING"] = True
    return A.app.test_client()


def test_state_shape(client):
    r = client.get("/api/state")
    d = r.get_json()
    assert r.status_code == 200
    for k in ("state", "logs", "stats", "quota", "risk", "live", "initialized"):
        assert k in d
    assert d["live"] is False          # 测试环境未开 ALLOW_LIVE


def test_offline_collect_reports_live_disabled(client):
    r = client.post("/api/collect", json={"platforms": ["zhaopin"]})
    assert r.get_json()["started"] is True
    for _ in range(40):
        time.sleep(0.1)
        s = client.get("/api/state").get_json()["state"]
        if not s["running"]:
            break
    msg = s["message"]
    assert "未执行" in msg or "出错" in msg or "离线" in msg or "ALLOW_LIVE" in msg


def test_boss_gate_closed_by_default(client):
    d = client.get("/api/state").get_json()
    assert d["risk"]["boss"]["enabled"] in (False, True)
    r = client.post("/api/job/jd", json={"id": 999999})
    assert r.get_json()["ok"] is False


def test_settings_roundtrip(client):
    r = client.post("/api/settings", json={"salary_floor": 28000,
                                           "cooldown_minutes": 45})
    assert r.get_json()["ok"]
    import db
    assert db.setting("salary_floor") == 28000
    assert db.setting("cooldown_minutes") == 45


def test_allow_live_toggle(client, monkeypatch):
    """设置页的在线/离线开关：写入后立即生效，并反映到 /api/state。"""
    import browser
    monkeypatch.delenv("JOBPILOT_ALLOW_LIVE", raising=False)
    assert browser.allow_live() is False
    r = client.post("/api/settings", json={"allow_live": 1})
    body = r.get_json()
    assert body["ok"] and body["saved"]["allow_live"] == 1
    assert browser.allow_live() is True
    assert client.get("/api/state").get_json()["live"] is True
    client.post("/api/settings", json={"allow_live": 0})
    assert browser.allow_live() is False


def test_allow_live_env_override(client, monkeypatch):
    """启动时设置环境变量仍可强制在线（自动化用）。"""
    import browser
    monkeypatch.setenv("JOBPILOT_ALLOW_LIVE", "1")
    assert browser.allow_live() is True
    assert client.get("/api/settings").get_json()["env_live"] is True


def test_resume_platform_routes_validate(client, monkeypatch):
    """简历同步接口：参数校验 + 离线时明确拒绝。"""
    monkeypatch.setenv("JOBPILOT_ALLOW_LIVE", "")
    r = client.post("/api/resume/upload", json={"platforms": []})
    assert r.status_code == 400
    r = client.post("/api/resume/upload", json={"platforms": ["zhaopin"]})
    assert r.get_json()["started"] is False
    assert "离线" in r.get_json()["error"]
    r = client.post("/api/resume/merge", json={"platform": "xxx"})
    assert r.status_code == 400
    r = client.post("/api/resume/merge", json={"platform": "zhaopin"})
    assert r.get_json()["ok"] is False
    assert "离线" in r.get_json()["error"]


def test_merge_status_shape(client):
    d = client.get("/api/resume/merge/status").get_json()
    assert set(d) == {"platform", "stage", "seconds"}
    assert d["stage"] == "idle"


def test_ai_plan_appends_only_new_rows(client, monkeypatch):
    """AI 重新生成：已有行保留，只追加不重复的新行。"""
    import ai
    import db
    db.plan_save([{"platform": "zhaopin", "keyword": "IT经理",
                   "city": "珠海", "enabled": 1}])
    monkeypatch.setattr(ai, "plan_search", lambda data=None: {
        "plan": [{"platform": "zhaopin", "keyword": "IT经理", "city": "珠海"},
                 {"platform": "boss", "keyword": "信息化经理", "city": "中山"}],
        "notes": ""})
    d = client.post("/api/ai/plan", json={}).get_json()
    assert d["ok"] and d["added"] == 1 and d["skipped"] == 1
    rows = db.plan_list()
    assert len(rows) == 2
    assert any(r["platform"] == "boss" for r in rows)
    assert any(r["platform"] == "zhaopin" and r["enabled"] for r in rows)


def test_plan_batch_add_and_delete(client):
    """批量新增去重 + 批量删除。"""
    import db
    d = client.post("/api/plan/batch_add", json={
        "platform": "zhaopin", "city": "珠海",
        "keywords": "IT经理\nIT经理，信息化经理"}).get_json()
    assert d["ok"] and d["added"] == 2 and d["skipped"] == 1
    rows = db.plan_list()
    assert len(rows) == 2 and all(r["enabled"] for r in rows)
    r = client.post("/api/plan/batch_delete",
                    json={"ids": [x["id"] for x in rows]}).get_json()
    assert r["ok"] and r["removed"] == 2
    assert db.plan_list() == []


def test_plan_cities_come_from_resume_intent(client):
    """计划的城市下拉来自简历「求职意向」；没有时返回提示。"""
    import resume as R
    R.save({"name": "张三", "intent": "IT经理｜珠海、深圳｜27K"})
    d = client.get("/api/plan").get_json()
    assert d["cities"] == ["珠海", "深圳"] and d["city_hint"] == ""
    R.save({"name": "张三", "intent": "IT经理｜27K，不限城市"})
    d = client.get("/api/plan").get_json()
    assert d["cities"] == []
    assert "求职意向" in d["city_hint"] and "intent" in d["city_hint"]


def test_support_cities_config_roundtrip(client):
    d = client.get("/api/cities").get_json()
    assert d["names"] == ["珠海", "中山", "广州", "深圳", "东莞"]
    assert all(x["zl"] and x["boss"] for x in d["status"])
    r = client.post("/api/cities", json={"names": ["佛山", "珠海"]})
    body = r.get_json()
    assert body["ok"] and body["names"] == ["佛山", "珠海"]
    st = {x["name"]: x for x in body["status"]}
    assert st["珠海"]["zl"] and st["珠海"]["boss"]
    assert st["佛山"]["zl"] is False          # 测试环境离线，不会去抓智联城市表
    assert "离线模式" in body["zl_msg"]        # 界面会提示怎么补码
    r = client.post("/api/cities", json={"names": []})
    assert r.status_code == 400


def test_cities_live_auto_matches_zl_codes(client, monkeypatch):
    """在线模式下保存城市：自动调用智联搜索接口抓城市码并缓存。"""
    import db
    db.set_setting("allow_live", 1)
    calls = []

    def fake_call(fn, *a, **k):
        calls.append(fn)
        return {"佛山": 768, "珠海": 766}

    monkeypatch.setattr(A.bot, "call", fake_call)
    body = client.post("/api/cities", json={"names": ["佛山", "珠海"]}).get_json()
    assert body["ok"] and body["zl_msg"] == ""
    st = {x["name"]: x for x in body["status"]}
    assert st["佛山"]["zl"] and st["珠海"]["zl"]
    assert len(calls) == 1
    # 第二次保存：城市码已缓存，不再打开浏览器
    client.post("/api/cities", json={"names": ["佛山", "珠海"]})
    assert len(calls) == 1


def test_cities_live_fetch_failure_is_reported(client, monkeypatch):
    import db
    from browser import Blocked
    db.set_setting("allow_live", 1)

    def fake_call(fn, *a, **k):
        raise Blocked("没拿到智联城市表")

    monkeypatch.setattr(A.bot, "call", fake_call)
    body = client.post("/api/cities", json={"names": ["佛山"]}).get_json()
    assert body["ok"]
    assert "智联城市码未匹配" in body["zl_msg"]
    assert {x["name"]: x for x in body["status"]}["佛山"]["zl"] is False


def test_pipeline_syncs_applied_jobs(client):
    """岗位库投递成功后自动进跟进看板；历史已投岗位启动时补入。"""
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "IT经理", "company": "某公司",
        "url": "https://www.zhaopin.com/job/123"})
    db.set_job_status(jid, "已投", "test")
    assert db.pipeline_backfill_applied() >= 1
    d = client.get("/api/pipeline").get_json()
    row = next(x for x in d["jobs"] if x["id"] == jid)
    assert row["p_stage"] == "applied"
    assert d["counts"]["applied"] >= 1
    assert db.pipeline_backfill_applied() == 0        # 幂等
    db.pipeline_set(jid, stage="interview")
    db.pipeline_mark_applied(jid)                     # 不回退已有阶段
    assert db.fu_get(jid)["stage"] == "interview"


def test_manual_stage_applied_updates_status(client):
    """在列表里把跟进阶段设为已投递：岗位状态同步为已投，不会继续出现在可投里。"""
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "IT经理", "company": "某公司",
        "url": "https://www.zhaopin.com/job/789"})
    db.set_job_status(jid, "待投", "")
    d = client.post("/api/pipeline", json={"id": jid, "stage": "applied"}).get_json()
    assert d["ok"]
    assert db.get_job(jid)["status"] == "已投"


def test_jobs_list_includes_pipeline_stage(client):
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "IT经理", "company": "某公司",
        "url": "https://www.zhaopin.com/job/321"})
    db.pipeline_set(jid, stage="interview")
    rows = client.get("/api/jobs").get_json()["jobs"]
    assert next(r for r in rows if r["id"] == jid)["p_stage"] == "interview"


def test_job_open_uses_automation_browser(client, monkeypatch):
    """打开岗位走采集/投递同一个专用浏览器，不用系统默认浏览器。"""
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "IT经理", "company": "某公司",
        "url": "https://www.zhaopin.com/job/456"})
    seen = []

    def fake_call(fn, *a, **k):
        seen.append(fn)

    monkeypatch.setattr(A.bot, "call", fake_call)
    d = client.post("/api/job/open", json={"id": jid}).get_json()
    assert d["ok"] and seen
    assert client.post("/api/job/open", json={"id": 0}).status_code == 404


def test_purge_rejected_jobs(client):
    """一键清除「不投」记录：只删不投岗位，并清掉它的跟进记录。"""
    import db
    a, _ = db.upsert_job("zhaopin", {
        "title": "A", "company": "X",
        "url": "https://www.zhaopin.com/job/9001"})
    b, _ = db.upsert_job("zhaopin", {
        "title": "B", "company": "X",
        "url": "https://www.zhaopin.com/job/9002"})
    db.set_job_status(a, "不投-薪资低", "")
    db.set_job_status(b, "待投", "")
    db.pipeline_set(a, stage="applied")
    d = client.post("/api/jobs/purge_rejected", json={}).get_json()
    assert d["ok"] and d["removed"] == 1
    assert db.get_job(a) is None and db.fu_get(a) is None
    assert db.get_job(b)


def test_chat_requires_feature_flag(client):
    """机制没开启时立即报错，不进入浏览器。"""
    import db
    db.set_setting("blacklist_learn_enabled", 0)
    d = client.post("/api/chat/scan", json={}).get_json()
    assert d["ok"] is False and "未开启" in d["error"]


def test_chat_scan_runs_as_background_task(client, monkeypatch):
    """机制 B 改到后台任务跑：请求立刻返回，日志页能看到开始/完成。"""
    import chat as chatmod
    import db
    db.set_setting("blacklist_learn_enabled", 1)
    db.set_setting("enabled_boss", 1)
    seen = []
    monkeypatch.setattr(chatmod, "scan_hr",
                        lambda bot, log: (seen.append(1) or
                                          {"scanned": 2, "blacklisted": 0}))
    d = client.post("/api/chat/scan", json={}).get_json()
    assert d["ok"] and d["started"]
    for _ in range(60):
        time.sleep(0.05)
        if not client.get("/api/state").get_json()["state"]["running"]:
            break
    assert seen == [1]
    logs = client.get("/api/state").get_json()["logs"]
    assert any("扫HR回复" in ln for ln in logs)


def test_chat_scan_dispatches_by_platform(client, monkeypatch):
    """机制 B 支持三平台：payload.platform 决定调用哪一个扫描器。"""
    import chat as chatmod
    import db
    db.set_setting("blacklist_learn_enabled", 1)
    db.set_setting("enabled_zhaopin", 1)
    seen = []
    monkeypatch.setattr(chatmod, "scan_hr",
                        lambda bot, log: seen.append("boss") or {"scanned": 0})
    monkeypatch.setattr(chatmod, "scan_hr_zhaopin",
                        lambda bot, log: seen.append("zl") or {"scanned": 0})
    d = client.post("/api/chat/scan", json={"platform": "zhaopin"}).get_json()
    assert d["ok"] and d["started"]
    for _ in range(60):
        time.sleep(0.05)
        if not client.get("/api/state").get_json()["state"]["running"]:
            break
    assert seen == ["zl"]
    assert client.post("/api/chat/scan", json={"platform": "x"}).status_code == 400


def test_chat_followup_dispatches_to_zhaopin(client, monkeypatch):
    """机制 D 支持智联：payload.platform=zhaopin 走智联复聊实现。"""
    import chat as chatmod
    import db
    db.set_setting("followup_enabled", 1)
    db.set_setting("enabled_zhaopin", 1)
    seen = []
    monkeypatch.setattr(chatmod, "followup_sweep_zhaopin",
                        lambda bot, log: seen.append("zl") or {"checked": 0, "sent": 0})
    d = client.post("/api/chat/followup", json={"platform": "zhaopin"}).get_json()
    assert d["ok"] and d["started"]
    for _ in range(60):
        time.sleep(0.05)
        if not client.get("/api/state").get_json()["state"]["running"]:
            break
    assert seen == ["zl"]


def test_chat_scan_all_runs_each_platform(client, monkeypatch):
    """platform=all：按 BOSS→智联→猎聘 顺序逐个扫。"""
    import chat as chatmod
    import db
    db.set_setting("blacklist_learn_enabled", 1)
    for p in ("boss", "zhaopin", "liepin"):
        db.set_setting(f"enabled_{p}", 1)
    seen = []
    for fn_name, tag in (("scan_hr", "boss"), ("scan_hr_zhaopin", "zl"),
                         ("scan_hr_liepin", "lp")):
        monkeypatch.setattr(chatmod, fn_name,
                            lambda bot, log, t=tag: seen.append(t) or {"scanned": 0})
    d = client.post("/api/chat/scan", json={"platform": "all"}).get_json()
    assert d["ok"] and d["started"]
    assert d["platforms"] == ["boss", "zhaopin", "liepin"]
    for _ in range(80):
        time.sleep(0.05)
        if not client.get("/api/state").get_json()["state"]["running"]:
            break
    assert seen == ["boss", "zl", "lp"]


def test_blacklist_sync_endpoint(client):
    """设置页「同步到岗位库」：已有岗位按黑名单连带更新。"""
    import db
    db.blacklist_add("软通动力信息技术(集团)", source="hr", reason="不合适")
    jid, _ = db.upsert_job("zhaopin", {
        "title": "项目经理", "company": "软通动力信息技术(集团)股份有限公司",
        "url": "https://www.zhaopin.com/job/900"})
    db.set_job_status(jid, "待投", "")
    d = client.post("/api/blacklist/sync", json={}).get_json()
    assert d["ok"] and d["updated"] >= 1
    assert db.get_job(jid)["status"] == "已投"
    assert db.fu_get(jid)["stage"] == "rejected"


def test_blacklist_list_and_remove(client):
    """设置页黑名单：能列出、能移除误伤公司（软删除）。"""
    import db
    db.blacklist_add("误伤公司", source="hr", reason="不合适")
    d = client.get("/api/blacklist").get_json()
    assert any(x["company"] == "误伤公司" for x in d["items"])
    r = client.post("/api/blacklist/remove", json={"company": "误伤公司"}).get_json()
    assert r["ok"] and all(x["company"] != "误伤公司" for x in r["items"])
    assert not db.blacklist_has("误伤公司")
    assert client.post("/api/blacklist/remove", json={}).status_code == 400


def test_city_config_affects_plan_and_resume(client):
    """改支持城市后：简历城市提取、计划批量新增都跟着变。"""
    import resume as R
    client.post("/api/cities", json={"names": ["佛山"]})
    R.save({"name": "张三", "intent": "IT经理｜佛山、珠海"})
    d = client.get("/api/plan").get_json()
    assert d["cities"] == ["佛山"]
    r = client.post("/api/plan/batch_add", json={
        "platform": "zhaopin", "city": "佛山", "keywords": "IT经理"}).get_json()
    assert r["ok"] and r["added"] == 1


def test_boss_toggle_persists(client):
    client.post("/api/boss/toggle", json={"on": True})
    import risk
    assert risk.boss_enabled()
    client.post("/api/boss/toggle", json={"on": False})
    assert not risk.boss_enabled()


def test_job_flow_rules_apply(client):
    import db
    db.upsert_job("boss", {"title": "IT经理", "company": "好公司",
                           "salary": "30-40K", "city": "珠海",
                           "url": "https://www.zhipin.com/job_detail/j1.html?securityId=S",
                           "security_id": "S"})
    db.upsert_job("boss", {"title": "运维工程师", "company": "差公司",
                           "salary": "10-15K", "city": "珠海",
                           "url": "https://www.zhipin.com/job_detail/j2.html?securityId=T",
                           "security_id": "T"})
    A._apply_rules_to_new()
    rows = {j["title"]: j["status"] for j in db.list_jobs("boss")}
    assert rows["IT经理"] == "待投"
    assert rows["运维工程师"].startswith("不投")
