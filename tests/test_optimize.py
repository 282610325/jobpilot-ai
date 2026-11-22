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


def test_export_without_chrome_reports_reason(monkeypatch, tmp_path):
    """没有 Chrome 时：HTML 照出，PDF 为空，且能给出明确原因。"""
    import config
    monkeypatch.setattr(config, "CHROME", str(tmp_path / "no-chrome"))
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path)
    html, pdf = R.export({"name": "张三"})
    assert html.exists() and pdf is None
    assert "Chrome" in R.pdf_error()


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


# ---------------- 对话补充经历 ----------------

def test_interview_reply_includes_resume_context(monkeypatch):
    seen = {}

    def fake_chat(msgs, json_mode=False, timeout=150):
        seen["msgs"] = msgs
        return "这个项目你负责哪部分？"

    monkeypatch.setattr(ai, "chat", fake_chat)
    out = ai.interview_reply(
        [{"role": "user", "content": "我做过 WMS 上线"}],
        {"name": "张三", "intent": "IT经理", "experience": []})
    assert "哪部分" in out
    assert any("访谈助手" in m["content"] for m in seen["msgs"])
    assert any("张三" in m["content"] and "简历摘要" in m["content"]
               for m in seen["msgs"])


def test_interview_build_allows_conversation_facts(monkeypatch):
    """对话里说过的事实允许进简历；两边都没有的数字才告警。"""
    conv = "我 2021 年主导 WMS 上线，覆盖 3 个仓库，库存准确率从 85% 提到 98%"

    def fake_chat(msgs, json_mode=False, timeout=150):
        return json.dumps({
            "resume": {"name": "张三", "experience": [
                {"company": "A科技有限公司", "role": "IT经理",
                 "bullets": ["主导 WMS 上线，库存准确率提升到 98%",
                             "带来 9999 万收益"]}]},
            "notes": [{"from": "新增", "to": "主导 WMS 上线", "why": "对话提到"}],
            "focus": "补充 WMS 项目"}, ensure_ascii=False)

    monkeypatch.setattr(ai, "chat", fake_chat)
    out = ai.interview_build([{"role": "user", "content": conv}],
                             {"name": "张三", "experience": []})
    assert out["resume"]["name"] == "张三" and out["notes"]
    warn = " ".join(out["warnings"])
    assert "98" not in warn          # 对话里出现过 → 不算编造
    assert "9999" in warn            # 简历和对话都没有 → 告警


def test_interview_routes_validate_input(client):
    r = client.post("/api/ai/interview", json={"messages": []})
    assert r.status_code == 400
    r = client.post("/api/ai/interview/build", json={"messages": []})
    assert r.get_json()["ok"] is False


# ---------------- 在线简历核查与合并 ----------------

def test_merge_online_resume(monkeypatch):
    local = {"name": "张三", "experience": [
        {"company": "A科技有限公司", "role": "IT经理", "period": "2020-2024",
         "bullets": ["营收4800万"]}]}
    online = ("工作经历 A科技有限公司 IT经理 2020-2024 负责ERP与跨境电商信息化，"
              "营收4800万；B集团有限公司 2018-2020 信息化主管，团队 12 人，"
              "负责OA、财务系统与机房运维，主导两家工厂的系统上线与培训。")

    def fake_chat(msgs, json_mode=False, timeout=150):
        return json.dumps({
            "summary": "补充了 B 公司经历",
            "same": ["A科技有限公司"], "only_online": ["B集团有限公司 2018-2020"],
            "only_local": [], "conflicts": [],
            "merged_resume": {"name": "张三", "experience": [
                {"company": "A科技有限公司", "role": "IT经理",
                 "period": "2020-2024", "bullets": ["营收4800万"]},
                {"company": "B集团有限公司", "role": "信息化主管",
                 "period": "2018-2020", "bullets": ["团队 12 人"]}]},
            "notes": [{"from": "新增", "to": "B集团有限公司", "why": "平台在线简历"}]},
            ensure_ascii=False)

    monkeypatch.setattr(ai, "chat", fake_chat)
    out = ai.merge_online_resume(online, local)
    assert out["resume"]["experience"][1]["company"] == "B集团有限公司"
    assert out["only_online"]
    warn = " ".join(out["warnings"])
    assert "12" not in warn          # 在线简历里出现过 → 不算编造
