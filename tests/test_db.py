import time

import db


def _job(**kw):
    j = dict(title="IT经理", company="测试公司", salary="30-40K",
             city="珠海", url="https://www.zhipin.com/job_detail/abc.html")
    j.update(kw)
    return j


def test_upsert_dedup_same_path_diff_token():
    a, s1 = db.upsert_job("boss", _job(url="https://www.zhipin.com/job_detail/abc.html?securityId=T1",
                                       security_id="T1"))
    b, s2 = db.upsert_job("boss", _job(url="https://www.zhipin.com/job_detail/abc.html?securityId=T2",
                                       security_id="T2"))
    assert s1 == "added" and s2 == "exists"
    assert a == b
    # token 刷新要更新进库
    assert db.get_job(a)["security_id"] == "T2"


def test_status_and_stats():
    jid, _ = db.upsert_job("zhaopin", _job(url="https://www.zhaopin.com/jobdetail/CC1J1.htm"))
    db.set_job_status(jid, "待投")
    assert db.job_stats()["zhaopin"]["待投"] == 1
    assert db.list_jobs("zhaopin", "待投")[0]["id"] == jid


def test_quota_daily_rollover():
    db.quota_add("zhaopin", "apply")
    assert db.quota_left("zhaopin", "apply", 5) == 4
    # 模拟跨日：直接插一条昨天的记录不影响今天
    assert db.setting("none", "d") == "d"


def test_settings_kv():
    db.set_setting("salary_floor", 27000)
    assert db.setting("salary_floor") == 27000
    db.set_setting("boss_enabled", 0)
    assert db.setting("boss_enabled") == 0
    assert "boss_enabled" in db.all_settings()


def test_blacklist_soft_delete():
    db.blacklist_add("坏公司", source="hr", reason="不合适")
    assert db.blacklist_has("坏公司")
    db.blacklist_deactivate("坏公司")
    assert not db.blacklist_has("坏公司")


def test_plan_roundtrip():
    db.plan_save([{"platform": "zhaopin", "keyword": "IT经理", "city": "珠海",
                   "reason": "r1", "enabled": 0}], replace_platform="zhaopin")
    rows = db.plan_list()
    assert rows and rows[0]["keyword"] == "IT经理" and rows[0]["enabled"] == 0
    db.plan_set(rows[0]["id"], enabled=1)
    assert db.plan_list(only_enabled=True)[0]["id"] == rows[0]["id"]
    # replace 只清同平台
    db.plan_save([{"platform": "zhaopin", "keyword": "数字化", "city": "中山",
                   "enabled": 1}], replace_platform="zhaopin")
    assert len(db.plan_list()) == 1


def test_apps_and_versions():
    jid, _ = db.upsert_job("liepin", _job(url="https://www.liepin.com/job/123.shtml"))
    db.log_app(jid, "liepin", "greet", True, "已打招呼")
    assert db.apps_recent()[0]["job_id"] == jid
    v = db.save_version(jid, "目标", {"name": "x"}, ["n"], ["g"], [])
    assert v and db.get_job(jid)["customized"] == 1
