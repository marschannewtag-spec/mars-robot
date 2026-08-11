"""signals.json 的契約 —— 產生端與消費端之間的那條縫。

這層的設計是「失敗即靜默」,所以任何路徑或欄位對不上都**沒有症狀**:
畫面就是不顯示那塊,和「今天剛好沒訊號」長得一模一樣。心跳指示器就踩過
同一個坑(路徑寫成 `data/manifest.json`,實際是 `ops/data/manifest.json`)。
所以這裡把兩端釘在一起,靠測試而不是靠記性。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import fetch_signals
import regime

OPS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parent
INDEX = REPO_ROOT / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def payload() -> dict:
    """完全離線組出一份 payload —— 六條旗標全部缺資料的極端情況。

    刻意用最壞的輸入:如果連「什麼都抓不到」都能產出結構正確的 JSON,
    那真實資料只會更好。
    """
    dom = regime.dominance({})
    warn = regime.warning_flags(None, None)
    return {
        "schema": 1,
        "generated_at": "2026-08-12T00:00:00+00:00",
        "dominance": {"leader": dom.leader, "scores": dom.scores, "matrix": dom.matrix,
                      "margin": None, "n_markets": dom.n_markets, "window": "2y"},
        "warning": {"n_triggered": warn.n_triggered, "n_usable": warn.n_usable,
                    "band": warn.band,
                    "flags": [{"key": f.key, "label": f.label, "triggered": f.triggered,
                               "detail": f.detail, "rule": f.rule, "source": f.source,
                               "ok": f.ok, "why": f.why} for f in warn.flags]},
        "cnn": None, "ndc": None, "sources": {},
    }


# ── 路徑必須對得上 ───────────────────────────────────────────────────────


def test_pwa_url_matches_where_the_writer_actually_writes(html: str) -> None:
    m = re.search(r'OPS_SIGNALS\s*=\s*"([^"]+)"', html)
    assert m, "index.html 找不到 OPS_SIGNALS"
    url = m.group(1)

    rel = fetch_signals.SIGNALS_PATH.relative_to(REPO_ROOT).as_posix()
    assert url.endswith(rel), (
        f"PWA 抓 {url},但 fetch_signals 寫到 {rel} —— 路徑對不上會 404,"
        f"而 404 在這個設計下【完全沒有症狀】"
    )
    assert "/ops-data/" in url, "必須讀 ops-data 分支(main 上沒有這個檔)"
    assert url.startswith("https://raw.githubusercontent.com/"), (
        "只有 raw.githubusercontent.com 會送 Access-Control-Allow-Origin: *"
    )


def test_signals_is_pushed_by_the_daily_workflow() -> None:
    """產生了卻沒推上去,PWA 一樣讀不到。把 workflow 也綁進來。"""
    wf = (REPO_ROOT / ".github" / "workflows" / "daily-health.yml").read_text(encoding="utf-8")
    assert "fetch_signals.py" in wf, "daily-health.yml 沒有產生 signals.json"
    assert "ops/data/signals.json" in wf, "daily-health.yml 沒有把 signals.json 加進 git"


# ── 結構契約 ─────────────────────────────────────────────────────────────


FLAG_FIELDS = {"key", "label", "triggered", "detail", "rule", "source", "ok", "why"}


def test_payload_shape(payload: dict) -> None:
    assert payload["schema"] == 1
    for k in ("generated_at", "dominance", "warning"):
        assert k in payload
    for k in ("leader", "scores", "margin", "n_markets", "window"):
        assert k in payload["dominance"]
    for k in ("n_triggered", "n_usable", "band", "flags"):
        assert k in payload["warning"]


def test_every_flag_carries_every_field_the_pwa_renders(payload: dict) -> None:
    for f in payload["warning"]["flags"]:
        assert FLAG_FIELDS <= set(f), f"旗標缺欄位:{FLAG_FIELDS - set(f)}"


def test_pwa_reads_only_fields_the_writer_emits(html: str, payload: dict) -> None:
    """PWA 裡出現的 f.xxx 必須都是產生端真的有寫的欄位。

    少一個欄位在畫面上只會顯示 undefined —— 沒有錯誤、沒人會發現。
    """
    used = set(re.findall(r"\bf\.([a-z_]+)", html))
    assert used, "index.html 裡找不到旗標欄位的使用"
    assert used <= FLAG_FIELDS, f"PWA 讀了產生端沒寫的欄位:{used - FLAG_FIELDS}"


def test_payload_survives_json_roundtrip(payload: dict) -> None:
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_everything_missing_still_produces_a_valid_payload(payload: dict) -> None:
    """全部資料源掛掉時不能爆,而且必須誠實回報「不知道」。"""
    assert payload["dominance"]["leader"] is None
    assert payload["warning"]["n_usable"] == 0
    assert payload["warning"]["band"] == "無"
    assert len(payload["warning"]["flags"]) == 6


# ── 失敗即靜默 ───────────────────────────────────────────────────────────


def test_regime_fetch_failure_is_silent(html: str) -> None:
    """抓不到就整塊不顯示。會亂叫的指示燈幾天後就會被無視,那等於沒有。"""
    block = html[html.index("function checkRegime"):]
    block = block[:block.index("checkRegime();")]
    assert re.search(r"\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)", block), (
        "checkRegime 的 catch 被改成會顯示東西了 —— 這是刻意的靜默,見 CLAUDE.md"
    )


def test_panel_is_hidden_until_data_arrives(html: str) -> None:
    assert "#regimepanel{display:none}" in html
    assert "#regimepanel.on{display:block}" in html
    assert 'classList.add("on")' in html


def test_external_strings_are_escaped_before_innerhtml(html: str) -> None:
    """signals.json 帶有外部來源的字串(國發會燈號、錯誤訊息)。

    CLAUDE.md 第四道防線:外部內容必須淨化才能進入會被渲染的地方。
    """
    block = html[html.index("function renderRegime"):html.index("function checkRegime")]
    for field in ("f.label", "f.detail", "f.rule", "f.source", "f.why"):
        assert f"esc({field})" in block, f"{field} 未經 esc() 就進了 innerHTML"
    assert "esc(warn.band" in block
    assert "esc(n)" in block, "市場名稱未逸出"


# ── 硬邊界 ───────────────────────────────────────────────────────────────


def test_pwa_regime_layer_does_not_touch_the_cash_number(html: str) -> None:
    """renderRegime 不得寫入任何核心讀數的 DOM 節點。

    這是「不要把 PWA 改爛」的機械化保證:就算以後有人想「順手」讓警訊
    調整水位,這條會擋下來。
    """
    block = html[html.index("function renderRegime"):html.index("function checkRegime")]
    core_ids = ["bigpct", "verdict", "momcash", "vixcash", "tmark", "tval",
                "fill", "cap", "allin", "svix", "sma", "ddbig"]
    touched = [i for i in core_ids if f'$("{i}")' in block]
    assert not touched, f"體制層碰到了核心讀數節點:{touched}"

    for fn in ("momCash", "vixCash", "compute(", "paintLadder", "renderTemp"):
        assert fn not in block, f"體制層呼叫了核心計算 {fn}"


def test_core_ladder_constants_are_untouched(html: str) -> None:
    """順手把兩腿階梯抄一份在這裡當金絲雀。改到它們的人會先撞到這條。"""
    assert "function momCash(mom){let c=0; if(mom<0)c=.30; if(mom<-.03)c=.60; " \
           "if(mom<-.08)c=.90; if(mom<-.13)c=1.0; return c;}" in html
    assert "function vixCash(r){let c=0; if(r>.5)c=.24; if(r>1.0)c=.42; " \
           "if(r>1.8)c=.66; if(r>2.6)c=.90; if(r>3.4)c=1.0; return c;}" in html
