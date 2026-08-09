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


def test_every_entrypoint_that_needs_utf8_calls_force_utf8() -> None:
    """任何會印特殊符號的 `__main__` 腳本都必須呼叫 force_utf8()。

    刻意寫成掃描而非列舉:以後新增的腳本會自動被納入。
    列舉版本只會保護今天已知的三個,而這個 bug 的教訓正是
    「沒人會想到去檢查新腳本在別的作業系統上印不印得出來」。
    """
    offenders = []
    for path in sorted(list(SRC.glob("*.py")) + list((OPS_ROOT / "scripts").glob("*.py"))):
        text = path.read_text(encoding="utf-8")
        if "__main__" not in text:
            continue
        if any(_cp950_hostile(ch) for ch in text) and "force_utf8()" not in text:
            offenders.append(path.relative_to(OPS_ROOT).as_posix())
    assert not offenders, (
        f"這些入口會印 cp950 印不出的符號,但沒呼叫 force_utf8():{offenders}\n"
        f"在 main() 開頭加一行 force_utf8()(見 src/console.py 的說明)"
    )


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


def test_empty_log_does_not_divide_by_zero(tmp_path: Path, monkeypatch) -> None:
    """只有表頭的 log 不該讓整份年度報告掛掉。"""
    import annual_review

    p = tmp_path / "health_log.csv"
    p.write_text("date,ok,n_checks,failed\n", encoding="utf-8")
    monkeypatch.setattr(annual_review, "HEALTH_LOG", p)

    body = annual_review.section_data_health().body  # 不得拋例外
    assert "沒有可判讀的紀錄" in body
