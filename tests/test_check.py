"""有效性检测：失效页面识别（跳转/文案）与检测任务的状态落库。"""
import pytest

import app as A


@pytest.fixture
def client():
    A.app.config["TESTING"] = True
    return A.app.test_client()


class _D:
    def __init__(self, url):
        self.current_url = url


class RT:
    """最小页面替身：只返回指定当前 URL 与空页面内容。"""

    def __init__(self, cur, body=""):
        self.d = _D(cur)
        self._body = body

    def goto(self, url):
        pass

    def body(self):
        return self._body

    def js(self, script, *args):
        return ""


def test_dead_text_matches_closed_copy():
    from platforms import dead_text
    assert dead_text("该职位已下线，看看其他机会")
    assert dead_text("该职位已暂停招聘 投递了该职位的人") == "该职位已暂停招聘"
    assert dead_text("职位描述：负责IT系统") == ""


def test_zhaopin_read_jd_detects_redirect():
    from platforms import zhaopin
    text, err = zhaopin.read_jd(
        RT("https://www.zhaopin.com/jobs/?pageMode=recommend"),
        "https://www.zhaopin.com/jobdetail/CC136740500J41030508204.htm")
    assert text is None and "已关闭" in err


def test_liepin_read_jd_detects_redirect():
    from platforms import liepin
    text, err = liepin.read_jd(
        RT("https://c.liepin.com/"),
        "https://www.liepin.com/job/1980870001.shtml")
    assert text is None and "已关闭" in err


def test_boss_read_jd_detects_expired_link(monkeypatch):
    from platforms import boss_api
    monkeypatch.setattr(boss_api.risk, "guard_security", lambda rt, t: None)
    text, err = boss_api.read_jd(
        RT("https://www.zhipin.com/web/geek/jobs"),
        "https://www.zhipin.com/job_detail/abc123.html?securityId=x")
    assert text is None and "过期" in err


def test_dead_reason_hints():
    assert A._dead_reason("已关闭（跳转到其他页面）")
    assert A._dead_reason("链接已过期（跳回搜索页）")
    assert not A._dead_reason("JD 读取失败（页面结构可能变化）")


def test_task_stop_endpoint(client):
    A.STATE.update({"running": True, "task": "有效性检测", "cancel": False})
    try:
        d = client.post("/api/task/stop").get_json()
        assert d["ok"] is True
        assert A._cancelled() is True
    finally:
        A._cancel_evt.clear()
        A.STATE.update({"running": False, "task": None, "cancel": False})


def test_task_check_cancel_stops_early(monkeypatch):
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "t", "company": "c",
        "url": "https://www.zhaopin.com/jobdetail/CC5J6.htm"})
    db.set_job_status(jid, "待投")
    calls = []
    monkeypatch.setattr(A, "_live", lambda: True)
    monkeypatch.setattr(A, "_preflight", lambda ps: ["zhaopin"])
    monkeypatch.setattr(A.bot, "call", lambda fn, *a, **k: calls.append(1) or ("", ""))
    monkeypatch.setattr(A.bot.pacer, "wait", lambda p: None)
    A._cancel_evt.set()
    try:
        A._task_check(["zhaopin"])
    finally:
        A._cancel_evt.clear()
    assert calls == []


def test_task_check_marks_closed(monkeypatch):
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "t", "company": "c",
        "url": "https://www.zhaopin.com/jobdetail/CC1J2.htm"})
    db.set_job_status(jid, "待投")
    monkeypatch.setattr(A, "_live", lambda: True)
    monkeypatch.setattr(A, "_preflight", lambda ps: ["zhaopin"])
    monkeypatch.setattr(A.bot, "call",
                        lambda fn, *a, **k: (None, "已关闭（跳转到其他页面）"))
    monkeypatch.setattr(A.bot.pacer, "wait", lambda p: None)
    A._task_check(["zhaopin"])
    job = db.get_job(jid)
    assert job["status"] == "已关闭" and "已关闭" in (job["reason"] or "")


def test_task_check_blocked_page_continues(monkeypatch):
    """单条页面打不开只记「无法判断」，不能中断整轮检测。"""
    import db
    from browser import Blocked
    j1, _ = db.upsert_job("zhaopin", {
        "title": "a", "company": "c",
        "url": "https://www.zhaopin.com/jobdetail/CC1J1.htm"})
    j2, _ = db.upsert_job("zhaopin", {
        "title": "b", "company": "c",
        "url": "https://www.zhaopin.com/jobdetail/CC2J2.htm"})
    for jid in (j1, j2):
        db.set_job_status(jid, "待投")
    seq = iter([Blocked("页面打开失败：net::ERR_ABORTED"), ("岗位职责" * 30, "")])

    def fake_call(fn, *a, **k):
        v = next(seq)
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(A, "_live", lambda: True)
    monkeypatch.setattr(A, "_preflight", lambda ps: ["zhaopin"])
    monkeypatch.setattr(A.bot, "call", fake_call)
    monkeypatch.setattr(A.bot.pacer, "wait", lambda p: None)
    A._task_check(["zhaopin"])
    rows = [db.get_job(j1), db.get_job(j2)]
    assert any((r["jd"] or "").startswith("岗位职责") for r in rows)
    assert any(r["status"] == "待投" for r in rows)


def test_task_check_retries_unknown_once(monkeypatch):
    """首次读不到（非失效）会重试一次，第二次成功即保存 JD。"""
    import db
    jid, _ = db.upsert_job("zhaopin", {
        "title": "t", "company": "c",
        "url": "https://www.zhaopin.com/jobdetail/CC3J4.htm"})
    db.set_job_status(jid, "待投")
    seq = iter([(None, "JD 读取失败"), ("岗位职责" * 30, "")])
    monkeypatch.setattr(A, "_live", lambda: True)
    monkeypatch.setattr(A, "_preflight", lambda ps: ["zhaopin"])
    monkeypatch.setattr(A.bot, "call", lambda fn, *a, **k: next(seq))
    monkeypatch.setattr(A.bot.pacer, "wait", lambda p: None)
    A._task_check(["zhaopin"])
    job = db.get_job(jid)
    assert job["status"] == "待投"
    assert (job["jd"] or "").startswith("岗位职责")
