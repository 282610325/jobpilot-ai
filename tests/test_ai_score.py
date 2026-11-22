import json

import pytest

import ai
import db


def test_score_jobs_batch(monkeypatch):
    calls = []

    def fake_chat(msgs, json_mode=False, timeout=150):
        calls.append(msgs)
        payload = json.loads(msgs[1]["content"])
        items = [{"id": j["id"], "tier": "强匹配" if "IT" in j["title"] else "不匹配",
                  "why": "ok"} for j in payload["jobs"]]
        return json.dumps({"items": items}, ensure_ascii=False)
    monkeypatch.setattr(ai, "chat", fake_chat)
    jobs = [{"id": 1, "title": "IT经理", "company": "A", "salary_text": "30-40K",
             "city": "珠海", "match_note": ""},
            {"id": 2, "title": "销售", "company": "B", "salary_text": "8-9K",
             "city": "珠海", "match_note": ""}]
    res = ai.score_jobs(jobs, resume_text="测试用户 IT经理")
    assert res[1][0] == "强匹配" and res[2][0] == "不匹配"
    assert len(calls) == 1                      # 一批搞定


def test_review_plan(monkeypatch):
    def fake_chat(msgs, json_mode=False, timeout=150):
        payload = json.loads(msgs[1]["content"])
        items = [{"index": it["index"],
                  "verdict": "砍掉" if it["keyword"] == "运维" else "保留",
                  "why": "x"} for it in payload["plan"]]
        return json.dumps({"items": items}, ensure_ascii=False)
    monkeypatch.setattr(ai, "chat", fake_chat)
    out = ai.review_plan([{"platform": "zhaopin", "keyword": "运维", "city": "珠海"},
                          {"platform": "zhaopin", "keyword": "IT经理", "city": "珠海"}],
                         resume_text="r")
    assert out[0]["verdict"] == "砍掉" and out[1]["verdict"] == "保留"


def test_zhaopin_card_field_mapping(monkeypatch):
    """回归：卡片解析必须用真实 Vue 字段名（name/salary60/companyName/companyNumber）。"""
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    from tests.fakes import FakeRuntime
    from platforms import zhaopin
    # FakeRuntime 返回的是 CARD_JS 执行后的形状（不是原始 Vue 对象）
    fake_jobs = [{"jobId": "40753134411", "name": "IT经理", "salary": "25-40K",
                  "edu": "本科", "exp": "5-10年", "company": "某某科技",
                  "number": "CZ123456", "time": "2026-11-20",
                  "area": "香洲区", "tags": "ERP/团队管理"}]
    rt = FakeRuntime(
        js_table={"__header__": "首页 消息 简历 测试用户",
                  "document.querySelectorAll('.job-card')": fake_jobs},
        body_table={"zhaopin.com/jobs": "正常搜索结果页" * 40},
        url="https://www.zhaopin.com/jobs/")
    out = zhaopin.collect(rt, lambda m: None, [("IT经理", 766, "珠海")], 1)
    assert out["added"] == 1
    j = db.list_jobs("zhaopin")[0]
    assert j["title"] == "IT经理" and j["company"] == "某某科技"
    assert j["salary_text"] == "25-40K"
    assert "/jobdetail/CC123456J40753134411.htm" in j["url"]   # CZ 前缀正确剥离
    assert j["status"] == "new"                          # 判定留给任务层
