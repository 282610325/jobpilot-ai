import json
from pathlib import Path

import pytest

import config
import db
import risk
from platforms import boss_api

FIX = Path(__file__).parent / "fixtures"


def test_parse_joblist_plaintext_salary_and_tokens():
    data = json.loads((FIX / "boss_joblist.json").read_text())
    jobs = boss_api.parse_joblist(data)
    assert len(jobs) == 2                      # 第三条无 securityId 被跳过
    j0 = jobs[0]
    assert j0["salary"] == "25-40K"            # 明文，无需字体解码
    assert "securityId=SEC-TOKEN-001" in j0["url"] and "lid=lid-aaa" in j0["url"]
    assert j0["ext_id"] == "abc123def456"


def test_parse_joblist_alt_shape():
    alt = {"jobList": [{"jobId": "a", "jobName": "T", "brandName": "C",
                        "salaryDesc": "30-40K", "securityId": "S1"}]}
    jobs = boss_api.parse_joblist(alt)
    assert jobs and jobs[0]["security_id"] == "S1"


def test_collect_offline_flow(tmp_db, monkeypatch):
    from tests.fakes import FakeRuntime
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    db.set_setting("boss_enabled", 1)
    data = json.loads((FIX / "boss_joblist.json").read_text())
    rt = FakeRuntime(js_table={"__header__": "首页 职位 消息 简历 测试用户",
                               "job-card-box": []},
                     xhr_table={"/wapi/zpgeek/search/joblist.json": data},
                     url=config.BOSS_JOBS)
    out = boss_api.collect(rt, lambda m: None, [("IT经理", "101280700", "珠海")], 1)
    assert out["added"] == 2 and out["api"] == 1
    rows = db.list_jobs("boss")
    assert all("securityId=" in r["url"] for r in rows)
    # 投递 URL 拼回新鲜 token
    assert "SEC-TOKEN" in boss_api.detail_url(rows[0])


def test_collect_risk_signal_trips_cooldown(tmp_db, monkeypatch):
    from tests.fakes import FakeRuntime
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    db.set_setting("boss_enabled", 1)
    rt = FakeRuntime(js_table={"__header__": "首页 消息 简历 测试用户"},
                     xhr_table={"/wapi/zpgeek/search/joblist.json": None},
                     body_table={"zhipin": "安全验证" + "x" * 80},
                     url=config.BOSS_JOBS)
    with pytest.raises(Exception):
        boss_api.collect(rt, lambda m: None, [("IT经理", "101280700", "珠海")], 1)
    assert risk.remaining("boss") > 0          # 已自动进入冷却


import time as time_mod
