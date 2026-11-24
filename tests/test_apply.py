"""投递：单条页面打不开不应中断整个平台。"""
import app as A
from browser import Blocked


def test_task_apply_blocked_page_continues(monkeypatch):
    import db
    j1, _ = db.upsert_job("zhaopin", {
        "title": "A", "company": "公司A", "salary": "20-30K",
        "url": "https://www.zhaopin.com/jobdetail/CC1J1.htm"})
    j2, _ = db.upsert_job("zhaopin", {
        "title": "B", "company": "公司B", "salary": "20-30K",
        "url": "https://www.zhaopin.com/jobdetail/CC2J2.htm"})
    for jid in (j1, j2):
        db.set_job_status(jid, "待投")
    seq = iter([Blocked("页面打开失败：net::ERR_ABORTED"), (True, "已投递")])

    def fake_call(fn, *a, **k):
        v = next(seq)
        if isinstance(v, BaseException):
            raise v
        return v

    monkeypatch.setattr(A, "_live", lambda: True)
    monkeypatch.setattr(A, "_preflight", lambda ps: ["zhaopin"])
    monkeypatch.setattr(A, "_apply_channel", lambda p: "browser")
    monkeypatch.setattr(A.bot, "call", fake_call)
    monkeypatch.setattr(A.bot.pacer, "wait", lambda p: None)
    monkeypatch.setattr(A.bot.pacer, "can", lambda *a, **k: True)
    monkeypatch.setattr(A.bot.pacer, "use", lambda *a, **k: None)
    A._task_apply(["zhaopin"], 10, {"zhaopin": [j1, j2]})
    rows = [db.get_job(j1), db.get_job(j2)]
    assert any(r["status"] == "已投" for r in rows)
    assert any(r["status"] == "待投" for r in rows)
