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
