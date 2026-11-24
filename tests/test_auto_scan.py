"""自动扫 HR 回复：定时判断与按平台分发。"""
import app as A


def test_auto_scan_due_uses_interval(monkeypatch):
    monkeypatch.setattr(A, "_auto_scan_interval_min", lambda: 15)
    assert A._auto_scan_due(now=1000, last=0) is True
    assert A._auto_scan_due(now=800, last=0) is False
    assert A._auto_scan_due(now=1000, last=100) is True


def test_auto_scan_chat_dispatches_all_platforms(monkeypatch):
    calls = []
    monkeypatch.setattr(A.risk, "check", lambda p: None)
    for name in ("scan_hr", "scan_hr_zhaopin", "scan_hr_liepin"):
        monkeypatch.setattr(
            A.chat, name,
            lambda *a, _n=name, **k: calls.append(_n) or {"scanned": 0})
    A._auto_scan_chat()
    assert calls == ["scan_hr", "scan_hr_zhaopin", "scan_hr_liepin"]


def test_auto_scan_skips_cooldown_platform(monkeypatch):
    import risk
    calls = []

    def fake_check(p):
        if p == "boss":
            raise risk.Cooldown("冷却中")

    monkeypatch.setattr(A.risk, "check", fake_check)
    for name in ("scan_hr", "scan_hr_zhaopin", "scan_hr_liepin"):
        monkeypatch.setattr(
            A.chat, name,
            lambda *a, _n=name, **k: calls.append(_n) or {"scanned": 0})
    A._auto_scan_chat()
    assert calls == ["scan_hr_zhaopin", "scan_hr_liepin"]
