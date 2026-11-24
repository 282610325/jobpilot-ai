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


def test_company_has_job_fuzzy():
    import db
    db.upsert_job("boss", {
        "title": "T", "company": "软通动力信息技术(集团)股份有限公司",
        "url": "https://www.zhipin.com/job_detail/a.html"})
    assert db.company_has_job("boss", "软通动力信息技术(集团)")
    assert not db.company_has_job("boss", "完全不存在的公司")
    assert not db.company_has_job("zhaopin", "软通动力信息技术(集团)")


def test_import_chat_job_adds_applied_and_followup():
    """聊天里出现、库里没有的岗位 → 新建为已投并进跟进看板。"""
    import db
    how = db.import_chat_job("zhaopin", "天虹股份", "数字化发展部总监",
                             note="聊天记录识别（外部投递）")
    assert how == "added"
    job = db.find_job_by_chat("zhaopin", "天虹股份", "数字化发展部总监")
    assert job and job["status"] == "已投"
    fu = db.fu_get(job["id"])
    assert fu and fu["stage"] == "applied"


def test_import_chat_job_links_existing_and_is_idempotent():
    """已存在同公司相似岗位 → 关联并标已投；重复扫描不重复入库。"""
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "项目经理（海工，正式合同）", "company": "上海克龙化工技术有限公司",
        "url": "https://www.zhaopin.com/jobdetail/CC1J1.htm"})
    db.set_job_status(jid, "待投")
    assert db.import_chat_job("zhaopin", "上海克龙化工技术", "项目经理") == "linked"
    assert db.get_job(jid)["status"] == "已投"
    assert (db.fu_get(jid) or {}).get("stage") == "applied"
    assert db.import_chat_job("zhaopin", "上海克龙化工技术", "项目经理") == "linked"
    assert len(db.list_jobs("zhaopin")) == 1


def test_import_chat_job_skips_blacklist():
    import db
    db.blacklist_add("深圳网新新思", source="hr", reason="不合适")
    assert db.import_chat_job("boss", "深圳网新新思", "Tead Lead") == "skip"
    assert db.find_job_by_chat("boss", "深圳网新新思", "Tead Lead") is None


def test_mark_company_rejected_updates_jobs_and_pipeline():
    """HR 拒绝后：该公司在库岗位统一标「已投」+ 跟进「已婉拒」。"""
    a, _ = db.upsert_job("boss", _job(company="传音控股",
                                     url="https://www.zhipin.com/job_detail/t1.html"))
    b, _ = db.upsert_job("boss", _job(company="传音控股",
                                     url="https://www.zhipin.com/job_detail/t2.html"))
    db.set_job_status(a, "待投")
    db.set_job_status(b, "已投")
    db.pipeline_set(b, stage="read")
    n = db.mark_company_rejected("传音控股")
    assert n == 2
    assert db.get_job(a)["status"] == "已投"
    assert db.fu_get(a)["stage"] == "rejected"
    assert db.get_job(b)["status"] == "已投"
    assert db.fu_get(b)["stage"] == "rejected"


def test_blacklist_fuzzy_match_and_sync():
    """黑名单公司名与岗位公司名不一致（多了「股份有限公司」）也能连带更新。"""
    db.blacklist_add("软通动力信息技术(集团)", source="hr", reason="不合适")
    assert db.blacklist_has("软通动力信息技术(集团)股份有限公司")
    a, _ = db.upsert_job("boss", _job(
        company="软通动力信息技术(集团)股份有限公司",
        url="https://www.zhipin.com/job_detail/f1.html"))
    b, _ = db.upsert_job("boss", _job(
        company="道一(天津)企业管理咨询有限公司",
        url="https://www.zhipin.com/job_detail/f2.html"))
    c, _ = db.upsert_job("boss", _job(
        company="软通动力信息技术(集团)股份有限公司",
        url="https://www.zhipin.com/job_detail/f3.html"))
    db.set_job_status(a, "待投")
    db.set_job_status(b, "待投")
    db.set_job_status(c, "已投")
    db.pipeline_set(c, stage="read")
    n = db.sync_blacklist_to_jobs()
    assert n == 2
    assert db.get_job(a)["status"] == "已投"
    assert db.fu_get(a)["stage"] == "rejected"
    assert db.get_job(b)["status"] == "待投"          # 不同公司，不动
    assert db.fu_get(c)["stage"] == "rejected"        # 已投的转已婉拒


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


def test_plan_upsert_keeps_id_and_priority():
    """按 id 保存：原地更新并保留优先级/来源；未提交的行被清掉。"""
    db.plan_save([{"platform": "boss", "keyword": "IT经理", "city": "珠海",
                   "enabled": 1, "priority": 5}])
    row = db.plan_list()[0]
    n = db.plan_upsert([
        {"id": row["id"], "platform": "boss", "keyword": "IT总监",
         "city": "深圳", "reason": "改", "enabled": 0},
        {"platform": "zhaopin", "keyword": "数字化", "city": "中山", "enabled": 1},
    ])
    rows = {r["keyword"]: r for r in db.plan_list()}
    assert n == 2 and len(rows) == 2
    assert rows["IT总监"]["id"] == row["id"]          # id 不变
    assert rows["IT总监"]["priority"] == 5            # AI 排序保留
    assert rows["IT总监"]["city"] == "深圳" and rows["IT总监"]["enabled"] == 0
    # 只提交一行 → 另一行被删掉（整表替换语义）
    assert db.plan_upsert([{"id": row["id"], "platform": "boss",
                            "keyword": "IT总监", "city": "深圳",
                            "enabled": 1}]) == 1
    left = db.plan_list()
    assert len(left) == 1 and left[0]["id"] == row["id"]
    # 空计划 → 清空
    assert db.plan_upsert([]) == 0 and db.plan_list() == []


def test_url_key_normalizes_zhaopin_detail_and_jobs_host():
    """智联 detail 页与 jobs. 页两种链路指向同一岗位，去重键要一致。"""
    a = db.url_key("https://www.zhaopin.com/jobdetail/CC189285810J40908573205.htm")
    b = db.url_key("http://jobs.zhaopin.com/CC189285810J40908573205.htm")
    assert a == b == "CC189285810J40908573205"
    # 其它平台仍按路径去重（query 里的 token 不参与）
    assert db.url_key(
        "https://www.zhipin.com/job_detail/abc.html?securityId=1") == "/job_detail/abc.html"


def test_merge_duplicate_jobs_keeps_applied():
    """同一岗位的重复行合并：保留已投的行，删掉待投的重复行。"""
    jid, _ = db.upsert_job("zhaopin", _job(
        url="https://www.zhaopin.com/jobdetail/CC9J9.htm",
        title="IT项目经理", company="甲公司"))
    db.set_job_status(jid, "已投", "已投过")
    with db.connect() as con:      # 模拟历史库里的另一条链路重复行
        con.execute(
            "INSERT INTO jobs(platform,url,url_key,title,company,status,first_seen)"
            " VALUES('zhaopin',?,?,?,?,?,?)",
            ("http://jobs.zhaopin.com/CC9J9.htm", "CC9J9.htm",
             "IT项目经理", "甲公司", "待投", "2026-11-24 20:35:05"))
    assert db.merge_duplicate_jobs() == 1
    rows = [r for r in db.list_jobs("zhaopin") if r["title"] == "IT项目经理"]
    assert len(rows) == 1 and rows[0]["id"] == jid and rows[0]["status"] == "已投"


def test_merge_duplicate_jobs_promotes_applied():
    """保留行是待投、重复行是已投时，合并后应变成已投（不能把投递记录弄丢）。"""
    jid, _ = db.upsert_job("zhaopin", _job(
        url="https://www.zhaopin.com/jobdetail/CC8J8.htm",
        title="项目经理", company="乙公司"))
    db.set_job_status(jid, "待投")
    with db.connect() as con:
        con.execute(
            "INSERT INTO jobs(platform,url,url_key,title,company,status,first_seen)"
            " VALUES('zhaopin',?,?,?,?,?,?)",
            ("http://jobs.zhaopin.com/CC8J8.htm", "CC8J8.htm",
             "项目经理", "乙公司", "已投", "2026-11-24 20:35:05"))
    db.merge_duplicate_jobs()
    rows = [r for r in db.list_jobs("zhaopin") if r["title"] == "项目经理"]
    assert len(rows) == 1 and rows[0]["status"] == "已投"


def test_apps_and_versions():
    jid, _ = db.upsert_job("liepin", _job(url="https://www.liepin.com/job/123.shtml"))
    db.log_app(jid, "liepin", "greet", True, "已打招呼")
    assert db.apps_recent()[0]["job_id"] == jid
    v = db.save_version(jid, "目标", {"name": "x"}, ["n"], ["g"], [])
    assert v and db.get_job(jid)["customized"] == 1
