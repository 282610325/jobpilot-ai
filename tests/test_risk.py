import time

import pytest

import db
import risk


def test_trip_and_cooldown(monkeypatch):
    base = 1000.0
    monkeypatch.setattr(time, "time", lambda: base)
    risk.trip("boss", "安全验证", minutes=60, clock=lambda: base)
    assert risk.remaining("boss") == pytest.approx(3600, abs=1)
    with pytest.raises(risk.Cooldown):
        risk.check("boss")
    # 时间推进到冷却结束
    later = base + 3601
    monkeypatch.setattr(time, "time", lambda: later)
    assert risk.remaining("boss") == 0


def test_two_trips_disable_boss_switch():
    risk.trip("boss", "验证1", minutes=1)
    risk.trip("boss", "验证2", minutes=1)
    assert db.setting("boss_enabled", 0) == 0
    assert db.setting("enabled_boss", 1) == 0


def test_boss_disabled_blocks_check():
    db.set_setting("boss_enabled", 0)
    with pytest.raises(risk.Cooldown):
        risk.check("boss")
    risk.toggle_boss(True)
    db.set_setting("enabled_boss", 1)
    risk.check("boss")  # 不再抛异常


def test_scan_signals():
    assert risk.scan("x" * 10) == "空白页"
    assert "安全验证" in (risk.scan("请完成" + "安全验证" + "后继续" + "y" * 60) or "")
    assert risk.scan("正常页面内容" * 30) is None


def test_clear_requires_probe():
    risk.trip("zhaopin", "空白页", minutes=60)
    risk.clear("zhaopin")
    assert risk.remaining("zhaopin") == 0
    assert risk.need_probe("zhaopin")
    risk.probe_done("zhaopin")
    assert not risk.need_probe("zhaopin")


def test_status_shape():
    st = risk.status()
    for p in ("zhaopin", "liepin", "boss"):
        assert "cooldown_left" in st[p] and "enabled" in st[p]
