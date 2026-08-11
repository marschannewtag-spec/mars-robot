"""體制層的測試 —— 主體判定與早期警訊。

全部離線:用合成資料,答案事先就知道。跟 L2 其他測試同一條規則,
不碰網路(見 conftest.py 的說明)。
"""

from __future__ import annotations

import math

import pytest

import regime


# ── 主體判定 ─────────────────────────────────────────────────────────────


def _leader_follower(n: int = 300, noise: float = 0.0):
    """造一組「follower 精確複製 leader 前一日」的序列。

    日序刻意用 3 的倍數,確保測試不是靠「日期減一」湊巧會過 ——
    真實世界兩地假期不同,相鄰交易日很少剛好差一天。
    """
    leader = {3 * i: math.sin(i) * 0.01 for i in range(n)}
    days = sorted(leader)
    follower = {}
    for i in range(1, n):
        follower[days[i]] = leader[days[i - 1]] + noise * math.cos(i * 7)
    return leader, follower


def test_perfect_follower_gives_r2_one() -> None:
    leader, follower = _leader_follower()
    assert regime.lead_lag_r2(follower, leader) == pytest.approx(1.0, abs=1e-9)


def test_direction_is_asymmetric() -> None:
    """跟隨方向 R² 高、反方向要低。抓不出方向的指標等於沒有指標。"""
    leader, follower = _leader_follower()
    forward = regime.lead_lag_r2(follower, leader)
    backward = regime.lead_lag_r2(leader, follower)
    assert forward > 0.9
    assert backward < forward


def test_prior_trading_day_not_calendar_minus_one() -> None:
    """leader 只在「第 0、10、20…天」有資料,follower 在「第 5、15…天」。

    若實作是「日期 -1」,配對數會是 0,R² 變 NaN。這條就是在釘死那個實作差異。
    """
    leader = {10 * i: (0.01 if i % 2 else -0.01) for i in range(120)}
    follower = {10 * i + 5: leader[10 * i] for i in range(120)}
    r2 = regime.lead_lag_r2(follower, leader)
    assert r2 == pytest.approx(1.0, abs=1e-9)


def test_too_few_overlapping_days_returns_nan() -> None:
    """樣本太少不給答案,而不是給一個看起來很確定的假數字。"""
    leader = {i: 0.01 for i in range(10)}
    follower = {i: 0.01 for i in range(1, 10)}
    assert math.isnan(regime.lead_lag_r2(follower, leader))


def test_constant_series_returns_nan_not_zero_division() -> None:
    leader = {i: 0.0 for i in range(200)}
    follower = {i: 0.0 for i in range(200)}
    assert math.isnan(regime.lead_lag_r2(follower, leader))


def test_dominance_finds_the_actual_leader() -> None:
    leader, f1 = _leader_follower(noise=0.002)
    _, f2 = _leader_follower(noise=0.004)
    dom = regime.dominance({"US": leader, "TW": f1, "JP": f2})
    assert dom.leader == "US"
    assert dom.scores["US"] > dom.scores["TW"]
    assert dom.margin > 1.0
    assert dom.n_markets == 3


def test_dominance_refuses_to_answer_on_thin_data() -> None:
    """只有一個市場算得出來時不能宣稱它是主體 —— 它只是唯一的倖存者。"""
    dom = regime.dominance({"US": {i: 0.01 for i in range(200)},
                            "TW": {i: 0.01 for i in range(5)}})
    assert dom.leader is None
    assert dom.n_markets == 1


def test_log_returns_skips_bad_prices() -> None:
    """壞價格會污染【兩天】:它自己那天,以及下一天(它是下一天的分母)。

    Yahoo 偶爾會回 0 或 null。只跳過壞的那一天、卻拿它當下一天的基準,
    會算出一個巨大的假報酬 —— 那種數字灌進相關係數會直接毀掉主體判定。
    """
    out = regime.log_returns({1: 100.0, 2: 0.0, 3: 110.0, 4: 121.0, 5: 133.1})
    assert 2 not in out, "壞價格那天要跳過"
    assert 3 not in out, "以壞價格為分母的那天也要跳過"
    assert out[4] == pytest.approx(math.log(121.0 / 110.0))
    assert out[5] == pytest.approx(math.log(133.1 / 121.0))


# ── 早期警訊 ─────────────────────────────────────────────────────────────


def _cnn(comp=50, breadth=50, strength=50, haven=50, junk=50, junk_then=50):
    return {
        "fear_and_greed": {"score": comp},
        "stock_price_breadth": {"score": breadth},
        "stock_price_strength": {"score": strength},
        "safe_haven_demand": {"score": haven},
        "junk_bond_demand": {"score": junk,
                             "data": [{"y": junk_then}] * 20 + [{"y": junk}]},
    }


def _flag(w, key):
    return next(f for f in w.flags if f.key == key)


def test_all_six_flags_are_always_present() -> None:
    """就算完全沒資料,六格都要在 —— 畫面上少一格比顯示「無資料」更難察覺。"""
    w = regime.warning_flags(None, None)
    assert len(w.flags) == 6
    assert all(not f.ok for f in w.flags)
    assert w.n_usable == 0 and w.n_triggered == 0


def test_missing_data_is_not_the_same_as_not_triggered() -> None:
    w = regime.warning_flags(None, None)
    assert w.band == "無"
    assert all(f.detail == "無資料" for f in w.flags)
    # 缺資料的旗標不能被算進可用數,否則資料源死掉會偽裝成一切平安
    assert w.n_usable == 0


@pytest.mark.parametrize("comp,expect", [(74, False), (75, True), (90, True)])
def test_greed_threshold_is_inclusive(comp, expect) -> None:
    w = regime.warning_flags(_cnn(comp=comp), None)
    assert _flag(w, "greed").triggered is expect


def test_breadth_divergence_needs_both_conditions() -> None:
    # 廣度弱但情緒也弱 = 單純的下跌,不是背離
    w = regime.warning_flags(_cnn(comp=30, breadth=20), None)
    assert _flag(w, "breadth").triggered is False
    # 情緒偏多 + 廣度弱 = 背離
    w = regime.warning_flags(_cnn(comp=65, breadth=20), None)
    assert _flag(w, "breadth").triggered is True


def test_strength_divergence_needs_both_conditions() -> None:
    assert _flag(regime.warning_flags(_cnn(comp=30, strength=20), None), "strength").triggered is False
    assert _flag(regime.warning_flags(_cnn(comp=65, strength=20), None), "strength").triggered is True


def test_credit_flag_measures_change_not_level() -> None:
    """垃圾債需求「一直很低」不算警訊,「從高檔掉下來」才算。

    這是刻意的:低檔盤整是常態,轉折才是資訊。
    """
    flat_low = regime.warning_flags(_cnn(junk=10, junk_then=10), None)
    assert _flag(flat_low, "credit").triggered is False

    falling = regime.warning_flags(_cnn(junk=60, junk_then=90), None)
    assert _flag(falling, "credit").triggered is True


def test_credit_flag_needs_enough_history() -> None:
    short = {"junk_bond_demand": {"score": 50, "data": [{"y": 90}] * 5}}
    w = regime.warning_flags(short, None)
    assert _flag(w, "credit").ok is False


@pytest.mark.parametrize("score,expect", [(37, False), (38, True), (45, True)])
def test_tw_cycle_red_light_threshold(score, expect) -> None:
    w = regime.warning_flags(None, {"score": score, "light": "紅", "month": "2026-06"})
    assert _flag(w, "tw_cycle").triggered is expect


def test_band_thresholds() -> None:
    assert regime.warning_flags(_cnn(), None).band == "無"
    # 1 條:情緒極端貪婪
    assert regime.warning_flags(_cnn(comp=80), None).band == "留意"
    # 3 條:貪婪 + 廣度背離 + 強度背離
    assert regime.warning_flags(_cnn(comp=80, breadth=20, strength=20), None).band == "明顯"


def test_flags_carry_their_own_explanation() -> None:
    """每一條都要能自己說明「為什麼看它」與「門檻是什麼」——
    畫面上不解釋的指標,三個月後連作者都不記得該怎麼讀。
    """
    w = regime.warning_flags(_cnn(), {"score": 20, "light": "綠", "month": "2026-06"})
    for f in w.flags:
        assert f.why and f.rule and f.source
        assert f.label


# ── 硬邊界:這一層不得碰核心訊號 ────────────────────────────────────────


def test_regime_does_not_touch_the_engine() -> None:
    """靜態保證:regime.py 沒有 import engine,也沒有呼叫任何現金水位函式。

    比「跑一次看數字有沒有變」強 —— 那只證明今天沒變。

    用 AST 而不是字串搜尋:模組 docstring 裡本來就要寫「為什麼不能碰
    mom_cash / vix_cash」,字串比對會把那段說明本身判成違規,逼人刪掉
    最該留的註解。這裡看的是真正的程式碼。
    """
    import ast

    tree = ast.parse(open(regime.__file__, encoding="utf-8").read())
    forbidden_calls = {"mom_cash", "vix_cash", "signal_frame", "cash_series"}

    imported: list[str] = []
    called: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in forbidden_calls:
                called.append(name)

    assert "engine" not in imported, f"regime.py 不得依賴 engine:{imported}"
    assert not called, f"regime.py 不得呼叫現金水位函式:{called}"


def test_core_cash_is_unchanged_by_this_layer(signals) -> None:
    """核心不變量的回歸錨:聯集邏輯與階梯值不因新層而改變。

    signals fixture 走的是 engine.py 的完整管線。如果哪天有人把早期警訊
    併進現金水位,這條會立刻紅燈。
    """
    import engine

    ladder = {0.0, 0.24, 0.30, 0.42, 0.60, 0.66, 0.90, 1.00}
    assert set(signals["cash"].unique()) <= ladder

    recomputed = [
        max(engine.mom_cash(m), engine.vix_cash(r))
        for m, r in zip(signals["mom"], signals["risk"])
    ]
    assert list(signals["cash"]) == pytest.approx(recomputed)
