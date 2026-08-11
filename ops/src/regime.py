"""市場體制層 —— 主體市場判定 + 外部早期警訊。純函式,零 IO,零網路。

與 `engine.py` 同一條紀律:這裡只做計算,資料怎麼來是 `fetch_signals.py` 的事。
可以離線測、可以突變測、CI 不會因為 CNN 今天抽風而變紅燈。

═══════════════════════════════════════════════════════════════════════
 這一層【不會】改變現金水位。這是硬邊界,不是風格選擇。
═══════════════════════════════════════════════════════════════════════

`cash = max(mom_cash, vix_cash)` 有 440 個月的凍結黃金基準釘著。把這裡的訊號
併進去會有兩個後果,兩個都是「越改越爛」:

1. 黃金基準全部失效 —— 而基準正是「有沒有改壞」的唯一判準,毀掉它等於毀掉
   驗證能力本身。
2. CNN 那條線只有 250 天歷史(端點就給這麼多)。用 250 天去改一個用 440 個月
   驗證過的規則,是拿雜訊換掉證據。

所以這層是**平行的第二個讀數**:報告事實與門檻,由使用者決定要不要動手。
對應 CLAUDE.md 的角色定義 —— 偵測到異常就報事實,不做結論。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# ── 主體判定 ─────────────────────────────────────────────────────────────

# 少於這麼多天的重疊樣本就不給結論。60 個交易日約三個月,
# 低於此的相關係數本身的標準誤就大到沒有意義。
MIN_OVERLAP = 60


def log_returns(closes: dict[int, float]) -> dict[int, float]:
    """{日序: 收盤} -> {日序: 對數報酬}。日序是 epoch 天數,不是連續整數。

    用對數報酬是因為要做跨市場相關 —— 不同幣別、不同點數量級,
    只有比率尺度可比。
    """
    days = sorted(closes)
    out: dict[int, float] = {}
    for i in range(1, len(days)):
        prev, cur = closes[days[i - 1]], closes[days[i]]
        if prev > 0 and cur > 0:
            out[days[i]] = math.log(cur / prev)
    return out


def _pearson(pairs: list[tuple[float, float]]) -> float:
    n = len(pairs)
    if n < MIN_OVERLAP:
        return float("nan")
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    sxx = sum((a - mx) ** 2 for a, _ in pairs)
    syy = sum((b - my) ** 2 for _, b in pairs)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def lead_lag_r2(follower: dict[int, float], leader: dict[int, float]) -> float:
    """leader 的【前一個交易日】報酬,能解釋 follower 今天報酬的多少比例。

    為什麼是前一日而不是同日:同日相關分不出因果方向 —— 台股和美股同一天
    一起漲,誰帶誰完全看不出來。錯開一天才有方向性。

    「前一個交易日」是指 leader 序列中【早於 follower 當日】的最近一筆,
    不是「日期減一」。兩地假期不同,硬減一天會大量落空。
    """
    if not follower or not leader:
        return float("nan")
    leader_days = sorted(leader)
    pairs: list[tuple[float, float]] = []
    for day, ret in follower.items():
        prior = None
        # 由後往前找第一個早於 day 的交易日
        for k in reversed(leader_days):
            if k < day:
                prior = k
                break
        if prior is not None:
            pairs.append((ret, leader[prior]))
    c = _pearson(pairs)
    return float("nan") if c != c else c * c


@dataclass(frozen=True)
class Dominance:
    """主體判定結果。`leader` 可能是 None —— 資料不足時不硬給答案。"""

    leader: str | None
    scores: dict[str, float]          # 市場 -> 領先總分(對其他市場 R² 的總和)
    matrix: dict[str, dict[str, float]]  # 跟隨者 -> {領先者: R²}
    margin: float                     # 第一名 / 第二名,越大越沒有懸念
    n_markets: int


def dominance(returns_by_market: dict[str, dict[int, float]]) -> Dominance:
    """誰在帶動誰。

    對每一對 (跟隨者 f, 領先者 l) 算 R²,再把每個市場當領先者時的 R² 加總。
    總分最高者就是「金融主體」。

    這個定義的好處是**它會失效**:如果哪天美國不再帶動全球,分數會自己掉下來。
    一個永遠回答「美國」的硬編碼就沒有這個性質 —— 那是信仰不是量測。
    """
    names = [n for n, r in returns_by_market.items() if len(r) >= MIN_OVERLAP]
    matrix: dict[str, dict[str, float]] = {}
    scores: dict[str, float] = {n: 0.0 for n in names}

    for f in names:
        row: dict[str, float] = {}
        for l in names:
            if f == l:
                continue
            r2 = lead_lag_r2(returns_by_market[f], returns_by_market[l])
            if r2 == r2:            # 非 NaN
                row[l] = r2
                scores[l] += r2
        matrix[f] = row

    if len(names) < 2:
        return Dominance(None, scores, matrix, float("nan"), len(names))

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    margin = top[1] / second[1] if second[1] > 0 else float("inf")
    # 總分為 0 代表所有配對都算不出來,這時沒有主體可言
    leader = top[0] if top[1] > 0 else None
    return Dominance(leader, scores, matrix, margin, len(names))


# ── 早期警訊 ─────────────────────────────────────────────────────────────
#
# 設計意圖:現有的兩腿(趨勢、VIX)都是【事件發生時】才會動 —— VIX 要先跳、
# 均線要先跌破。它們處理「崩盤中」很好,處理「崩盤前」按定義做不到。
#
# 這六條補的是另一件事:**價格還在高檔、但內部已經在惡化**。
# 每一條都對得上一個可查證的數字與一個寫死的門檻,沒有擬合、沒有權重。
#
# ⚠ 這些門檻【沒有經過回測】。CNN 那個端點只給 250 天歷史,做不出統計顯著性。
#    它們是「常識性的極端值」,不是最佳化出來的參數。UI 上必須照實說。

# CNN Fear & Greed 的分數方向:0 = 極度恐慌,100 = 極度貪婪。
GREED_EXTREME = 75     # 綜合分數 >= 此值視為極端貪婪
GREED_ELEVATED = 60    # 背離條件的前提:大盤情緒還在偏多
WEAK_INTERNAL = 35     # 廣度/強度子分數 <= 此值視為內部走弱
SAFE_HAVEN_BID = 30    # 避險需求子分數 <= 此值代表資金在往債跑
CREDIT_DROP = 25       # 垃圾債需求子分數 20 日內下滑幅度(分)
NDC_RED_SCORE = 38     # 台灣景氣對策信號:38-45 分為紅燈(熱絡/過熱)


@dataclass(frozen=True)
class Flag:
    key: str
    label: str
    triggered: bool
    detail: str            # 目前實際數值,無論有沒有觸發都要顯示
    rule: str              # 門檻的白話說明
    source: str
    ok: bool = True        # False = 這條算不出來(缺資料),不是「沒觸發」
    why: str = ""          # 為什麼要看這條


@dataclass(frozen=True)
class Warning:
    flags: list[Flag] = field(default_factory=list)

    @property
    def usable(self) -> list[Flag]:
        return [f for f in self.flags if f.ok]

    @property
    def n_triggered(self) -> int:
        return sum(1 for f in self.usable if f.triggered)

    @property
    def n_usable(self) -> int:
        return len(self.usable)

    @property
    def band(self) -> str:
        """0 = 無、1-2 = 留意、3+ = 明顯。刻意粗糙 —— 精細的分級會假裝有精度。"""
        n = self.n_triggered
        if n == 0:
            return "無"
        return "留意" if n <= 2 else "明顯"


def _sub(cnn: dict, key: str) -> float | None:
    """取 CNN 子指標的當前分數。取不到就回 None,不要用 0 冒充。"""
    v = (cnn or {}).get(key)
    if isinstance(v, dict):
        v = v.get("score")
    return float(v) if isinstance(v, (int, float)) else None


def _delta20(cnn: dict, key: str) -> float | None:
    """子指標「現在 - 約 20 個交易日前」。歷史不足就回 None。"""
    node = (cnn or {}).get(key)
    if not isinstance(node, dict):
        return None
    data = node.get("data")
    if not isinstance(data, list) or len(data) < 21:
        return None
    try:
        now = float(data[-1]["y"])
        then = float(data[-21]["y"])
    except (KeyError, TypeError, ValueError):
        return None
    return now - then


def warning_flags(cnn: dict | None, ndc: dict | None) -> Warning:
    """組出六條旗標。缺資料的那條標成 ok=False,而不是靜靜當作「沒觸發」。

    這個區別很重要:「沒觸發」和「不知道」在畫面上必須長得不一樣,
    否則資料源死掉會偽裝成一切平安 —— 那正是這整個專案在防的事。
    """
    cnn = cnn or {}
    ndc = ndc or {}
    flags: list[Flag] = []

    comp = _sub(cnn, "fear_and_greed")
    breadth = _sub(cnn, "stock_price_breadth")
    strength = _sub(cnn, "stock_price_strength")
    haven = _sub(cnn, "safe_haven_demand")
    junk_d = _delta20(cnn, "junk_bond_demand")
    junk_now = _sub(cnn, "junk_bond_demand")

    flags.append(Flag(
        key="greed", label="情緒極端貪婪",
        triggered=comp is not None and comp >= GREED_EXTREME,
        detail=f"綜合 {comp:.0f}" if comp is not None else "無資料",
        rule=f"綜合分數 ≥ {GREED_EXTREME}",
        source="CNN Fear & Greed", ok=comp is not None,
        why="極端貪婪本身不是賣出訊號,但歷史上的頭部幾乎都發生在這個區間,而不是恐慌區。",
    ))

    flags.append(Flag(
        key="breadth", label="廣度背離",
        triggered=(comp is not None and breadth is not None
                   and comp >= GREED_ELEVATED and breadth <= WEAK_INTERNAL),
        detail=(f"綜合 {comp:.0f} · 廣度 {breadth:.0f}"
                if comp is not None and breadth is not None else "無資料"),
        rule=f"綜合 ≥ {GREED_ELEVATED} 且 廣度 ≤ {WEAK_INTERNAL}",
        source="CNN · stock_price_breadth", ok=comp is not None and breadth is not None,
        why="指數還在漲、但上漲家數在縮 —— 代表指數靠少數權值股撐著,底下已經在退。",
    ))

    flags.append(Flag(
        key="strength", label="強度背離",
        triggered=(comp is not None and strength is not None
                   and comp >= GREED_ELEVATED and strength <= WEAK_INTERNAL),
        detail=(f"綜合 {comp:.0f} · 強度 {strength:.0f}"
                if comp is not None and strength is not None else "無資料"),
        rule=f"綜合 ≥ {GREED_ELEVATED} 且 強度 ≤ {WEAK_INTERNAL}",
        source="CNN · stock_price_strength", ok=comp is not None and strength is not None,
        why="創 52 週新高的家數相對新低在萎縮,和廣度是不同角度的同一件事。",
    ))

    flags.append(Flag(
        key="credit", label="信用需求轉弱",
        triggered=junk_d is not None and junk_d <= -CREDIT_DROP,
        # 用一位小數:整數格式會把 -0.4 印成「-0」,看起來像壞掉的數字
        detail=(f"垃圾債需求 {junk_now:.0f} · 20 日 {junk_d:+.1f}"
                if junk_d is not None and junk_now is not None else "無資料"),
        rule=f"20 個交易日內下滑 ≥ {CREDIT_DROP} 分",
        source="CNN · junk_bond_demand", ok=junk_d is not None,
        why="信用市場通常比股市早轉向。這是本工具最缺的一塊(見下方限制),用它當代理。",
    ))

    flags.append(Flag(
        key="haven", label="避險需求上升",
        triggered=haven is not None and haven <= SAFE_HAVEN_BID,
        detail=f"避險需求 {haven:.0f}" if haven is not None else "無資料",
        rule=f"避險需求子分數 ≤ {SAFE_HAVEN_BID}",
        source="CNN · safe_haven_demand", ok=haven is not None,
        why="資金從股票轉向公債。單獨看雜訊大,和上面幾條同時出現才有意義。",
    ))

    score = ndc.get("score")
    light = ndc.get("light") or ""
    has_ndc = isinstance(score, (int, float))
    flags.append(Flag(
        key="tw_cycle", label="台灣景氣過熱",
        triggered=has_ndc and float(score) >= NDC_RED_SCORE,
        detail=(f"對策信號 {int(score)} 分 · {light}({ndc.get('month', '')})"
                if has_ndc else "無資料"),
        rule=f"景氣對策信號綜合分數 ≥ {NDC_RED_SCORE}(紅燈)",
        source="國發會 · 景氣指標及燈號", ok=has_ndc,
        why="本地循環的位置。紅燈代表景氣熱絡到過熱區,是台股的循環頂部訊號,月頻、落後約兩個月。",
    ))

    return Warning(flags)
