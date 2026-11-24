"""平台推荐采集（按账号求职意向）：参数、解析与按平台分发。"""
import time

import pytest

import app as A


@pytest.fixture
def client():
    A.app.config["TESTING"] = True
    return A.app.test_client()


def test_liepin_card_selector_uses_shtml():
    """猎聘卡片链接域名已变更，选择器必须按 .shtml 取，否则采不到 href。"""
    from platforms import liepin
    assert ".shtml" in liepin.CARD_JS
    assert 'href*="/job/"' not in liepin.CARD_JS


def test_zhaopin_parse_detail_url():
    from platforms import zhaopin
    assert zhaopin._parse_detail_url(
        "https://www.zhaopin.com/jobdetail/CC136740500J41030508204.htm?srccode=1"
    ) == ("CC136740500", "41030508204")
    assert zhaopin._parse_detail_url(
        "https://www.zhaopin.com/jobdetail/CCL1392907610J40885976411.htm"
    ) == ("CCL1392907610", "40885976411")
    assert zhaopin._parse_detail_url("https://www.zhaopin.com/") == (None, None)


def test_boss_recommend_params_carry_city_and_page():
    """推荐接口不吃关键词，带城市/页码/意向加密 ID。"""
    from platforms import boss_api
    p = boss_api._recommend_params("101280700", 2, "abc~~")
    assert "page=2" in p and "pageSize=15" in p
    assert "city=101280700" in p
    assert "encryptExpectId=abc~~" in p      # ~ 保留，不做百分号编码
    assert "query=" not in p


def test_fetch_expects_maps_names_to_ids(monkeypatch):
    from platforms import boss_api
    monkeypatch.setattr(boss_api.time, "sleep", lambda *a: None)
    events = iter([
        [],
        [("Network.requestWillBeSent", {"request": {"url":
            "https://www.zhipin.com/wapi/zpgeek/pc/recommend/job/list.json"
            "?encryptExpectId=e1~~"}})],
        [],
        [("Network.requestWillBeSent", {"request": {"url":
            "https://www.zhipin.com/wapi/zpgeek/pc/recommend/job/list.json"
            "?encryptExpectId=e2~~"}})],
    ])

    class RT:
        def js(self, script, *args):
            if script == boss_api.EXPECT_ITEM_JS:
                return ["项目经理/主管(珠海)", "产品经理(珠海)"]
            return True

        def perf_drain(self):
            return next(events, [])

    out = boss_api.fetch_expects(RT(), lambda m: None)
    assert out == [{"name": "项目经理/主管(珠海)", "encrypt_id": "e1~~"},
                   {"name": "产品经理(珠海)", "encrypt_id": "e2~~"}]


def test_boss_collect_recommend_iterates_expects_and_cities(monkeypatch):
    """每个意向 × 每个城市各发一次推荐请求。"""
    from platforms import boss_api
    monkeypatch.setattr(boss_api, "_ensure_jobs_page", lambda rt, log: None)
    monkeypatch.setattr(boss_api, "fetch_expects",
                        lambda rt, log: [{"name": "项目经理", "encrypt_id": "e1"},
                                         {"name": "产品经理", "encrypt_id": "e2"}])
    monkeypatch.setattr(boss_api.time, "sleep", lambda *a: None)
    calls = []

    class RT:
        def js_xhr(self, path, params):
            calls.append(params)
            return {"zpData": {"hasMore": False, "jobList": [{
                "jobName": "岗位", "salaryDesc": "20-30K", "brandName": "某公司",
                "cityName": "珠海", "encryptJobId": params, "securityId": "s1"}]}}

    res = boss_api.collect_recommend(
        RT(), lambda m: None,
        [("珠海", "101280700"), ("广州", "101280100")], 1)
    assert len(calls) == 4                        # 2 意向 × 2 城市
    assert "encryptExpectId=e1" in calls[0]
    assert "encryptExpectId=e2" in calls[2]
    assert "city=101280700" in calls[0] and "city=101280100" in calls[1]
    assert res["api"] == 4


def test_boss_collect_recommend_honours_should_stop(monkeypatch):
    """中断标志一置位，推荐采集不再发下一次请求。"""
    from platforms import boss_api
    monkeypatch.setattr(boss_api, "_ensure_jobs_page", lambda rt, log: None)
    monkeypatch.setattr(boss_api, "fetch_expects",
                        lambda rt, log: [{"name": "A", "encrypt_id": "e1"}])
    monkeypatch.setattr(boss_api.time, "sleep", lambda *a: None)
    calls = []

    class RT:
        def js_xhr(self, path, params):
            calls.append(params)
            return {"zpData": {"hasMore": True, "jobList": []}}

    res = boss_api.collect_recommend(RT(), lambda m: None,
                                     [("珠海", "101280700")], 3, lambda: True)
    assert calls == []
    assert res["api"] == 0


def test_collect_recommend_dispatches_by_platform(client, monkeypatch):
    from platforms import boss_api, liepin, zhaopin

    monkeypatch.setattr(A, "_live", lambda: True)
    monkeypatch.setattr(A, "_preflight", lambda ps: list(ps))
    monkeypatch.setattr(A.db, "quota_left", lambda *a, **k: 1)
    monkeypatch.setattr(A.db, "quota_add", lambda *a, **k: None)
    monkeypatch.setattr(A, "_apply_rules_to_new", lambda: 0)
    monkeypatch.setattr(A, "_ai_score_new", lambda p=None: 0)
    monkeypatch.setattr(A.citycfg, "map_boss", lambda: {"珠海": "101280700"})

    calls = []
    monkeypatch.setattr(boss_api, "collect_recommend",
                        lambda rt, log, pairs, pages, stop=None:
                        calls.append(("boss", pairs, pages)) or {"added": 3})
    monkeypatch.setattr(zhaopin, "collect_recommend",
                        lambda rt, log, cities, pages:
                        calls.append(("zl", cities, pages)) or {"added": 3})
    monkeypatch.setattr(liepin, "collect_recommend",
                        lambda rt, log, cities, pages:
                        calls.append(("lp", cities, pages)) or {"added": 3})

    class RT:
        def hide(self):
            pass

        def show(self):
            pass

    monkeypatch.setattr(A.bot, "call", lambda fn, *a, **k: fn(RT()))
    monkeypatch.setattr(A.bot.pacer, "wait", lambda p: None)

    d = client.post("/api/collect_recommend",
                    json={"platforms": ["boss", "zhaopin", "liepin"],
                          "cities": ["珠海"], "pages": 2}).get_json()
    assert d["started"] is True
    for _ in range(80):
        time.sleep(0.05)
        if not client.get("/api/state").get_json()["state"]["running"]:
            break
    assert [c[0] for c in calls] == ["boss", "zl", "lp"]
    assert calls[0][1] == [("珠海", "101280700")]
    assert calls[0][2] == 2
    assert calls[1][2] == 1 and calls[2][2] == 1   # 智联/猎聘推荐位固定一屏
