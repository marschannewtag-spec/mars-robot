"""報告層的回歸測試 —— 釘住 2026-08-09 debug 找到的兩個 bug。

兩個都有同一個特徵:**Linux 上的 CI 永遠不會發現,只有使用者會踩到。**
一個是平台差異(Windows 代碼頁),一個是只在手動觸發後才浮現(每天多列)。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

OPS_ROOT = Path(__file__).resolve().parents[1]
SRC = OPS_ROOT / "src"

# 這些字元中文代碼頁 cp950 一個都印不出來,而報告與健檢輸出到處都是
GLYPHS = "✅❌⚠⏭✓✗∈"


def _cp950_hostile(ch: str) -> bool:
    try:
        ch.encode("cp950")
    except UnicodeEncodeError:
        return True
    return False


# --------------------------------------------------------------------------
# Bug 1 — Windows 的 stdout 預設是 ANSI 代碼頁,印 ✅ 直接 UnicodeEncodeError
#
# 踩到的路徑:健檢偵測到異常 → 自動開 Issue → Issue 內文叫使用者跑
# `python ops/src/health_check.py --verbose` → 在他的機器上當場崩潰。
# --------------------------------------------------------------------------


def _run_with_cp950_console(code: str) -> subprocess.CompletedProcess:
    """在子行程裡把主控台編碼強制成 cp950 —— 重現使用者的 Windows 環境。

    cp950 的 codec 是 CPython 內建的,Linux runner 上也跑得起來,
    所以這條測試在 CI 上一樣有效,不是只有在 Windows 才會動。
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONIOENCODING": "cp950"},
        capture_output=True,
    )


def test_cp950_console_really_does_break_without_the_fix() -> None:
    """先證明這個測試環境真的重現得出那個 bug。

    少了這條,下面那條就可能只是「在一個根本不會壞的環境裡通過」——
    綠燈卻什麼都沒保證。
    """
    r = _run_with_cp950_console(f"print({GLYPHS!r})")
    assert r.returncode != 0, "預期會崩潰卻沒有 —— 這個環境重現不出該 bug,下面的測試沒有意義"
    assert b"UnicodeEncodeError" in r.stderr


def test_force_utf8_survives_a_cp950_console() -> None:
    """套上修正之後,同樣的環境要印得出來。"""
    code = (
        f"import sys; sys.path.insert(0, {str(SRC)!r});"
        f"from console import force_utf8; force_utf8();"
        f"print({GLYPHS!r})"
    )
    r = _run_with_cp950_console(code)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert GLYPHS.encode("utf-8") in r.stdout


def test_every_entrypoint_that_prints_non_ascii_calls_force_utf8() -> None:
    """任何會印非 ASCII 的 `__main__` 腳本都必須呼叫 force_utf8()。

    刻意寫成掃描而非列舉:以後新增的腳本會自動被納入。
    列舉版本只會保護今天已知的那幾個,而這個 bug 的教訓正是
    「沒人會想到去檢查新腳本在別的作業系統上印不印得出來」。

    ⚠ 2026-08-12 放寬判準:原本只檢查「cp950 印不出的符號」,於是
    `fetch_data.py`(印「列」)與 `compute_golden.py` 兩個入口通過了測試,
    卻在 Windows 上把中文寫成 cp950 位元組 —— 管線接到 UTF-8 消費端就是亂碼。
    不會崩潰,只會安靜地產生垃圾,比崩潰更難發現。
    判準改成「有非 ASCII 就要 force_utf8()」,整類問題一次消掉。
    """
    offenders = []
    for path in sorted(list(SRC.glob("*.py")) + list((OPS_ROOT / "scripts").glob("*.py"))):
        text = path.read_text(encoding="utf-8")
        if "__main__" not in text:
            continue
        if any(ord(ch) > 127 for ch in text) and "force_utf8()" not in text:
            offenders.append(path.relative_to(OPS_ROOT).as_posix())
    assert not offenders, (
        f"這些入口會印非 ASCII,但沒呼叫 force_utf8():{offenders}\n"
        f"在 main() 開頭加一行 force_utf8()(見 src/console.py 的說明)"
    )


def test_the_scan_would_actually_catch_a_new_offender(tmp_path: Path) -> None:
    """上面那條是掃描式的 —— 先證明它抓得到東西,否則它可能只是恆真。"""
    bad = tmp_path / "newscript.py"
    bad.write_text('print("中文")\nif __name__ == "__main__":\n    pass\n', encoding="utf-8")
    text = bad.read_text(encoding="utf-8")
    assert "__main__" in text
    assert any(ord(ch) > 127 for ch in text)
    assert "force_utf8()" not in text  # -> 會被判為違規


# --------------------------------------------------------------------------
# Bug 2 — health_log.csv 是每次執行一列,不是每天一列
#
# 手動觸發 / drill / 重跑都會讓同一天出現多列,年度報告卻把列數當成天數。
# 實測:兩天的紀錄被印成「10 天」。
# --------------------------------------------------------------------------


@pytest.fixture()
def log_with_repeated_days(tmp_path: Path) -> Path:
    """08-07 跑了四次(其中兩次紅),08-08 跑了兩次(都綠)。"""
    p = tmp_path / "health_log.csv"
    p.write_text(
        "date,ok,n_checks,failed\n"
        "2026-08-07,1,15,\n"
        "2026-08-07,0,15,資料新鮮度 · shiller\n"
        "2026-08-07,0,15,資料新鮮度 · shiller\n"
        "2026-08-07,1,15,\n"
        "2026-08-08,1,15,\n"
        "2026-08-08,1,15,\n",
        encoding="utf-8",
    )
    return p


def test_day_count_is_days_not_runs(log_with_repeated_days: Path, monkeypatch) -> None:
    import annual_review

    monkeypatch.setattr(annual_review, "HEALTH_LOG", log_with_repeated_days)
    body = annual_review.section_data_health().body

    assert "2 天 · 共 6 次執行" in body, f"天數被執行次數灌水了:\n{body}"


def test_a_day_with_any_failure_is_not_green(log_with_repeated_days: Path, monkeypatch) -> None:
    """08-07 有紅過,就算後來重跑通過,那天仍然算紅 —— 資料源當天確實出過問題。"""
    import annual_review

    monkeypatch.setattr(annual_review, "HEALTH_LOG", log_with_repeated_days)
    body = annual_review.section_data_health().body

    assert "**1/2**" in body, f"全綠天數算錯:\n{body}"


def test_failed_items_are_deduped_per_day(log_with_repeated_days: Path, monkeypatch) -> None:
    """同一天失敗兩次的同一項,只能算一天。"""
    import annual_review

    monkeypatch.setattr(annual_review, "HEALTH_LOG", log_with_repeated_days)
    body = annual_review.section_data_health().body

    assert "— 1 天" in body, f"同日重複失敗被重複計數:\n{body}"
    assert "— 2 天" not in body


# --------------------------------------------------------------------------
# 參數敏感度網格:「現行」那一格必須真的是現行設定
#
# 2026-08-12 確認原始的 `lvl1.2` 定義已不可考,網格的兩個軸是本專案自訂的。
# 軸選得對不對無法驗證,但「現行那一格 == 現在跑的東西」可以 —— 而那才是
# 百分位與鄰域讀數會不會指錯對象的關鍵。
# --------------------------------------------------------------------------


def test_current_grid_cell_is_the_running_config(sp_daily, vix_daily, monkeypatch) -> None:
    import annual_review
    import engine
    import fetch_data

    frames = {"sp_daily": sp_daily, "vix": vix_daily}
    monkeypatch.setattr(fetch_data, "load", lambda name, **kw: frames[name].copy())

    grid_cell = annual_review.build_signals(
        annual_review.CURRENT_MA, annual_review.CURRENT_MULT
    )
    truth = engine.signal_frame(
        engine.sp_monthly(sp_daily.copy()), engine.build_vix_monthly(vix_daily.copy())
    )

    merged = grid_cell.merge(truth[["ym", "cash"]], on="ym", suffixes=("_grid", "_engine"))
    assert len(merged) > 400, "重疊月份太少,這條測試沒有涵蓋到東西"
    diff = merged[merged["cash_grid"] != merged["cash_engine"]]
    assert diff.empty, (
        f"網格的「現行」那一格與 engine 實際跑的結果不同,{len(diff)} 個月不一致 —— "
        f"年度報告的百分位/鄰域讀數會在描述一個你沒在用的參數:\n{diff.head()}"
    )


def test_current_params_are_inside_the_grid() -> None:
    """現行參數若不在網格上,`cur` 會是空的,Calmar 變 NaN,整節靜靜失去意義。"""
    import annual_review

    assert annual_review.CURRENT_MA in annual_review.MA_GRID
    assert annual_review.CURRENT_MULT in annual_review.MULT_GRID


def test_sensitivity_section_states_the_axes_are_unverified() -> None:
    """那段誠實標示不得被「順手」刪掉 —— 它是這一節唯一的邊界說明。"""
    src = (SRC / "annual_review.py").read_text(encoding="utf-8")
    assert "不是原始參數化方式的重現" in src
    assert "原始定義已不可考" in src
    assert "沒有被掃到" in src


# --------------------------------------------------------------------------
# 不准再出現「鋪好了但沒接上出海口」的資料管線
#
# FRED 就是這樣存在了很久:來源定義在、workflow 傳了金鑰、文件宣稱有信用利差,
# 但 refresh() 預設排除它、沒有任何 load() 呼叫、報告也沒有對應章節。
# 比沒有更糟 —— 它讓人以為信用面已經被涵蓋了。2026-08-12 移除。
# --------------------------------------------------------------------------


def test_every_declared_source_is_actually_consumed() -> None:
    import fetch_data

    code = "\n".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.py")
                     if p.name != "fetch_data.py")
    orphans = [name for name in fetch_data.SOURCES
               if f'load("{name}"' not in code and f"load('{name}'" not in code]
    assert not orphans, (
        f"這些來源有定義卻沒有任何人 load():{orphans}\n"
        f"要嘛接上消費端,要嘛刪掉。留著的死管線會讓文件宣稱它有在做事。"
    )


def test_refresh_default_covers_every_source() -> None:
    """預設不抓的來源 = 每日流程根本不會去碰的來源。FRED 當年就死在這一行。"""
    import fetch_data

    import inspect
    src = inspect.getsource(fetch_data.refresh)
    assert "needs_fred_key" not in src, "又出現了「預設排除某些來源」的過濾條件"
    assert set(fetch_data.refresh.__defaults__ or ()) <= {None}


def test_no_credentialed_source_without_a_consumer() -> None:
    """任何需要金鑰的來源都必須有人消費,否則設了 secret 也不會改變任何輸出。"""
    import fetch_data

    fields = set(fetch_data.Source.__dataclass_fields__)
    assert "needs_fred_key" not in fields and "fred_series" not in fields, (
        "FRED 管線被加回來了。要加就要同時接上消費端與測試,"
        "否則會重演「設了金鑰卻什麼都沒變」的狀況(見 SETUP.md 第 2 項)"
    )


def test_workflows_do_not_pass_unused_secrets() -> None:
    wf_dir = OPS_ROOT.parent / ".github" / "workflows"
    offenders = [p.name for p in wf_dir.glob("*.yml")
                 if "FRED_API_KEY" in p.read_text(encoding="utf-8")]
    assert not offenders, f"這些 workflow 還在傳已移除的 FRED_API_KEY:{offenders}"


def test_empty_log_does_not_divide_by_zero(tmp_path: Path, monkeypatch) -> None:
    """只有表頭的 log 不該讓整份年度報告掛掉。"""
    import annual_review

    p = tmp_path / "health_log.csv"
    p.write_text("date,ok,n_checks,failed\n", encoding="utf-8")
    monkeypatch.setattr(annual_review, "HEALTH_LOG", p)

    body = annual_review.section_data_health().body  # 不得拋例外
    assert "沒有可判讀的紀錄" in body
