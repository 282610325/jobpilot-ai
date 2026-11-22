import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    """每个测试独立数据库。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # 测试不依赖本机 local_config.py（账号名留空 → 走通用登录判定）
    monkeypatch.setattr(config, "LOGIN_TEXT",
                        {"zhaopin": "", "liepin": "", "boss": ""})
    import db
    db.init()
    yield tmp_path / "t.db"


@pytest.fixture
def no_live(monkeypatch):
    monkeypatch.setenv("JOBPILOT_ALLOW_LIVE", "")
