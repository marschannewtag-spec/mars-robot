"""終端輸出編碼修正。

## 為什麼需要這個檔

Windows 的 Python 預設用系統 ANSI 代碼頁寫 stdout(繁體中文是 cp950)。
中文字它印得出來,但 `✅ ❌ ⚠ ⏭ ✓ ✗ ∈` 這些符號印不出來 ——
不是印成亂碼,是直接丟 `UnicodeEncodeError` 讓整支腳本中斷。

**CI 永遠不會發現這件事**,因為 GitHub runner 是 Linux + UTF-8。
2026-08-09 實測發現三個入口全中,而且全都是使用者會在自己機器上手動跑的:

| 指令 | 誰叫他跑的 |
|---|---|
| `health_check.py --verbose` | 每日健檢自動開的 Issue,內文就寫這行 |
| `check_pine_parity.py` | Pine 執行層驗證的最後一步 |
| `annual_review.py --stdout` | 年度健檢 |

也就是說:健檢偵測到異常 → 開 Issue → Issue 告訴使用者跑一個
**在他的作業系統上必定崩潰**的指令。

## 為什麼不改用 ASCII 符號

那是在遷就工具而不是修問題,而且報告會變難讀。輸出是 UTF-8 才是對的,
錯的是沒有把串流設成 UTF-8。

## 為什麼不靠環境變數

`PYTHONIOENCODING=utf-8` 或 `python -X utf8` 都能解,但那要求使用者
記得設 —— 而他看到的是一份叫他直接複製貼上的指令。修在程式裡才有效。
"""

from __future__ import annotations

import sys


def force_utf8() -> None:
    """把 stdout/stderr 切成 UTF-8。每個有 `__main__` 的腳本都要在 main() 開頭呼叫。

    失敗時安靜略過:串流被重導到不支援 reconfigure 的物件(例如測試用的
    StringIO、某些 CI 包裝層)時不該讓整支腳本掛掉 —— 那會比原本的問題更糟。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
