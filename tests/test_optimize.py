import json

import pytest

import ai
import db
import resume as R


@pytest.fixture
def client():
    import app as A
    A.app.config["TESTING"] = True
    return A.app.test_client()

OLD = {"name": "张三", "title": "IT经理",
       "experience": [{"company": "A科技有限公司", "role": "IT经理",
                       "period": "2020.6-2024.2", "bullets": ["营收4800万"]}]}


def test_optimize_keeps_facts_and_flags_invention(monkeypatch):
    def fake_chat(msgs, json_mode=False, timeout=150):
        return json.dumps({
            "resume": {"name": "张三", "title": "IT负责人",
                       "experience": [{"company": "A科技有限公司", "role": "IT经理",
                                       "period": "2020.6-2024.2",
                                       "bullets": ["营收4800万", "自建10个AI智能体"]}]},
            "notes": [{"from": "营收4800万", "to": "主导营收4800万业务", "why": "突出主导"}],
            "focus": "前移IT管理成果"}, ensure_ascii=False)
    monkeypatch.setattr(ai, "chat", fake_chat)
    out = ai.optimize_resume(OLD)
    assert out["resume"]["name"] == "张三"
    assert out["notes"]
    # "10个" 是原简历没有的数字 → 必须告警
    assert any("数字" in w for w in out["warnings"])


def test_optimize_requires_resume(monkeypatch):
    monkeypatch.setattr(ai, "chat", lambda *a, **k: "{}")
    with pytest.raises(RuntimeError):
        ai.optimize_resume({"name": ""})


def test_optimize_uses_resume_intent(monkeypatch):
    """优化方向必须来自简历的求职意向，不能写死。"""
    seen = {}

    def fake_chat(msgs, json_mode=False, timeout=150):
        seen["sys"] = msgs[0]["content"]
        return json.dumps({"resume": {"name": "张三"},
                           "notes": [], "focus": "按意向重排"}, ensure_ascii=False)

    monkeypatch.setattr(ai, "chat", fake_chat)
    ai.optimize_resume({"name": "张三",
                        "intent": "数据总监 / BI 负责人｜珠海｜40K 以上"})
    assert "数据总监 / BI 负责人" in seen["sys"]
    assert "IT总监 / IT经理" not in seen["sys"]


def test_optimize_intent_fallback(monkeypatch):
    """简历没填意向时，退回标题方向，且不残留占位符。"""
    seen = {}

    def fake_chat(msgs, json_mode=False, timeout=150):
        seen["sys"] = msgs[0]["content"]
        return json.dumps({"resume": {"name": "张三"}}, ensure_ascii=False)

    monkeypatch.setattr(ai, "chat", fake_chat)
    ai.optimize_resume({"name": "张三", "title": "IT经理"})
    assert "IT经理 相关方向" in seen["sys"]
    assert "{INTENT}" not in seen["sys"]


def test_adopt_archives_old_version(client):
    R.save(OLD)
    new = dict(OLD, title="信息化总监")
    r = client.post("/api/ai/adopt", json={"resume": new})
    assert r.get_json()["ok"]
    assert R.load()["title"] == "信息化总监"
    vers = db.versions()
    assert any(v["target"] == "优化前的原版" for v in vers)


def test_adopt_rejects_empty(client):
    r = client.post("/api/ai/adopt", json={"resume": {}})
    assert r.get_json()["ok"] is False


def test_wizard_upload_accepts_multipart(client, tmp_path):
    """回归：multipart 上传不能再被 request.json 触发 415（只验解析，不验 AI）。"""
    import io
    p = tmp_path / "r.txt"
    p.write_text("张三 IT经理\n" + "负责ERP与跨境电商IT，营收4800万。" * 10,
                 encoding="utf-8")
    data = {"file": (io.BytesIO(p.read_bytes()), "r.txt")}
    r = client.post("/api/wizard/resume?mode=upload", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200          # 不是 415
    body = r.get_json()
    assert body["ok"] is False           # 没配 AI 会报"未配置"，但入口必须通
    assert "AI" in body["error"] or "配置" in body["error"]
