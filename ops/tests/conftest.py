"""pytest 共用設定。

所有測試一律吃 tests/fixtures/ 的凍結快照,**不碰網路**。
這是刻意的分層:L2(邏輯)必須離線可跑,否則資料源某天抽風會讓 CI 變紅燈,
你就分不清是「邏輯壞了」還是「Yahoo 今天不舒服」—— L1 的問題污染 L2 的訊號,
兩層就都失去意義了。資料源的死活由 L1 的 health_check.py 負責。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

OPS_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO_ROOT = OPS_ROOT.parent

sys.path.insert(0, str(OPS_ROOT / "src"))


@pytest.fixture(scope="session")
def vix_daily() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "vix_daily.csv")


@pytest.fixture(scope="session")
def sp_daily() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "sp500_daily.csv")


@pytest.fixture(scope="session")
def shiller() -> pd.DataFrame:
    return pd.read_csv(FIXTURES / "shiller_sp500.csv")


@pytest.fixture(scope="session")
def signals(sp_daily: pd.DataFrame, vix_daily: pd.DataFrame) -> pd.DataFrame:
    import engine

    return engine.signal_frame(
        engine.sp_monthly(sp_daily), engine.build_vix_monthly(vix_daily)
    )


@pytest.fixture(scope="session")
def index_html() -> Path:
    """線上真的在跑的那個 PWA 檔案。"""
    return REPO_ROOT / "index.html"
