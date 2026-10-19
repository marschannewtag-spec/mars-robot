"""VIX 來源:從 GitHub 鏡像換成 CBOE 上游(2026-10-19)。

為什麼要換
----------
`datasets/finance-vix` 自 2026-09-22 起停止更新。它仍然 HTTP 200、仍然回
9278 列,所以「來源可達性」永遠是綠的 —— 只有新鮮度看得出來。
**一個還在正常回應、但內容凍結的鏡像,是最難發現的那種死法。**

換來源最危險的地方是**數值悄悄變了**:黃金基準用的是 440 個月的凍結結果,
若新來源的 CLOSE 差一分錢,現金水位可能在某些月份翻轉,而沒有任何畫面會說。
所以這裡要釘的不是「抓得到」,是:

  1. 日期格式轉換不能猜 —— CBOE 用 `MM/DD/YYYY`,猜成日/月的話前 12 天
     會被分到錯的月份,而那是靜默的
  2. 重疊期間的數值必須與既有黃金 fixture 逐筆相同
  3. 欄位變了要【拋】,不能靜靜產出垃圾

網路實測(2026-10-19,不在這份測試裡 —— L2 一律離線):
    CBOE   9296 列 1990-01-02..2026-10-16   0.65s
    鏡像   9278 列 1990-01-02..2026-09-22
    重疊 9278 個交易日,CLOSE 逐筆比對 **0 筆不同**;
    鏡像的日期集合是 CBOE 的真子集,CBOE 多出的正好是停更那 18 個交易日。
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

import fetch_data

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CBOE_FIXTURE = FIXTURES / "cboe_vix_history.csv"


@pytest.fixture(scope="module")
def cboe_raw() -> str:
    """真的從 CBOE 下載下來的片段(120 個交易日,原始 MM/DD/YYYY 格式)。"""
    return CBOE_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def normalised(cboe_raw: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(fetch_data._normalise_cboe_vix(cboe_raw))))


def test_source_is_cboe_not_a_mirror():
    """VIX 必須直接指向 CBOE。

    釘住這一條的理由:改回任何鏡像都會重新引入「內容凍結但 HTTP 200」
    的死法,而那次連紅了 26 天。
    """
    src = fetch_data.SOURCES["vix"]
    assert src.kind == "cboe_vix"
    assert "cboe.com" in src.url
    assert "finance-vix" not in src.url


def test_columns_and_order_match_the_downstream_contract(normalised: list[dict]):
    """欄位名稱與順序必須與既有的 raw 檔一致。

    `engine.build_vix_monthly`、`health_check`、PWA 的 `buildVixMonthly`、
    以及黃金 fixture 全都讀這幾個名字 —— 換來源不該讓它們任何一個改。
    """
    assert list(normalised[0].keys()) == ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]


def test_dates_become_iso_and_the_month_day_order_is_not_guessed(
    normalised: list[dict]
):
    """`01/02/1990` 必須是 1990-01-02(1 月 2 日),不是 1990-02-01。

    這是整個轉換唯一會靜默出錯的地方:`pd.to_datetime` 不給 format 時
    會自己猜,而 1-12 號的日期兩種猜法都「成功」。猜錯的話月分組會錯,
    數字照樣算得出來 —— 只是錯的。
    """
    dates = [r["DATE"] for r in normalised]
    assert dates[0] == "1990-01-02"
    assert all(len(d) == 10 and d[4] == "-" and d[7] == "-" for d in dates)
    assert dates == sorted(dates), "轉換後必須仍是遞增"


def test_values_match_the_frozen_golden_fixture(normalised: list[dict]):
    """重疊日期的 CLOSE / HIGH 必須與黃金 fixture 逐筆相同。

    這是「換上游不是換資料」的離線證據。差一分錢就可能讓某個月份的
    現金水位翻轉,而那不會有任何畫面提示。
    """
    golden = {r["DATE"]: r for r in csv.DictReader(
        io.StringIO((FIXTURES / "vix_daily.csv").read_text(encoding="utf-8")))}

    checked = 0
    for row in normalised:
        want = golden.get(row["DATE"])
        if want is None:
            continue
        for col in ("OPEN", "HIGH", "LOW", "CLOSE"):
            assert float(row[col]) == pytest.approx(float(want[col]), abs=5e-3), (
                "%s 的 %s 不一致:CBOE %s vs 黃金 %s"
                % (row["DATE"], col, row[col], want[col])
            )
        checked += 1
    assert checked >= 100, "只對到 %d 筆,fixture 選得太窄,這條測試會變成空轉" % checked


def test_trailing_zeros_are_trimmed(normalised: list[dict]):
    """`17.240000` -> `17.24`。

    不是美觀問題:這份資料每天會被重新 commit 一次並發給 PWA,
    六位小數的尾零讓檔案大一倍而不帶任何資訊。
    """
    assert normalised[0]["CLOSE"] == "17.24"


def test_a_renamed_column_raises_instead_of_producing_garbage():
    """CBOE 哪天改欄位名,必須【拋】。

    靜靜回一份少了 HIGH 的 CSV 的話,「月內飆升」項會退回用收盤 ——
    歷史上有 9 個月份因此翻轉現金水位,而畫面上看不出來。
    """
    bad = "DATE,OPEN,HIGH,LOW,SETTLE\n01/02/1990,17.24,17.24,17.24,17.24\n"
    with pytest.raises(fetch_data.FetchError, match="欄位變了"):
        fetch_data._normalise_cboe_vix(bad)


def test_rows_with_no_close_are_dropped():
    """缺 CLOSE 的列丟掉 —— 留著會讓 `buildVixMonthly` 的月均被 NaN 污染。"""
    text = ("DATE,OPEN,HIGH,LOW,CLOSE\n"
            "01/02/1990,17.24,17.24,17.24,17.24\n"
            "01/03/1990,,,,\n"
            "01/04/1990,18.00,18.50,17.90,18.20\n")
    rows = list(csv.DictReader(io.StringIO(fetch_data._normalise_cboe_vix(text))))
    assert [r["DATE"] for r in rows] == ["1990-01-02", "1990-01-04"]


def test_freshness_contract_still_treats_vix_as_daily():
    """換來源不該順手改判準。

    CBOE 是日頻發布,所以 `cadence: daily` 仍然正確。若哪天有人把它改成
    monthly 來讓紅燈變綠,這條會擋下來。
    """
    import yaml

    cfg = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.yml").read_text(encoding="utf-8")
    )
    assert cfg["freshness"]["vix"]["cadence"] == "daily"
    assert cfg["freshness"]["vix"]["max_age_days"] <= 10
