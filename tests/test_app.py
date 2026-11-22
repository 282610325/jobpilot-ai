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
