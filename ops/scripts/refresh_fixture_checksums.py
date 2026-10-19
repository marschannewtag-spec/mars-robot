"""重新產生 ops/tests/fixtures/CHECKSUMS.txt。

⚠ 跑這支腳本等於說「我知道我在移動黃金基準,而且我是故意的」。

那不是被禁止的動作 —— Shiller 修訂歷史資料、CBOE 補回缺漏的交易日,
都是真的會發生而且應該被接受的事。被禁止的是**讓它看起來沒發生過**。

所以跑完之後,commit 訊息要寫清楚三件事:
  1. 為什麼要重產(哪個資料源、改了什麼)
  2. 重產之後哪些數字動了(跑 `python src/annual_review.py --stdout` 的第 1 節)
  3. 訊號層有沒有動 —— **訊號層任何差異都是 bug**,不是資料修訂

用法:
    python scripts/refresh_fixture_checksums.py            # 預覽,不寫檔
    python scripts/refresh_fixture_checksums.py --write    # 真的寫
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from console import force_utf8  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = OPS_ROOT / "tests" / "fixtures"
CHECKSUMS = FIXTURES / "CHECKSUMS.txt"

HEADER = """\
# 黃金基準與輸入快照的 sha256。
#
# ⚠ 這個檔案【不是】防止修改,是讓修改**無法悄悄發生**。
#
# 為什麼需要它:test_golden.py 比的是「現在算出來的」對「黃金基準」。
# 單獨改基準會紅(實測:改一個值 -> 3 failed)。但**同時**改程式與基準,
# 或改輸入快照再重產基準,整組是綠的 —— 那是定義上必然的,沒有任何
# 測試能從結果看出來。
#
# 一個 20000 列的 CSV 在 diff 裡看起來像雜訊;這裡一行 hash 變了不會。
# 這就是全部的價值:把一個看不見的動作變成一個看得見的動作。
#
# 真的要改(例如 Shiller 修訂了歷史資料)就更新這裡,並在 commit 訊息
# 寫清楚為什麼、以及重產之後哪些數字動了。
#
# 產生方式:python scripts/refresh_fixture_checksums.py --write#
# ⚠ hash 算的是**行尾正規化之後**的內容(CRLF -> LF),不是磁碟上的原始
#   位元組。2026-10-20 第一版沒有正規化,結果:本機(Windows,
#   golden_metrics.json 的工作副本是 CRLF)全綠,CI(Linux 檢出 LF)紅。
#   一個用來守護黃金基準的檢查,自己踩了這個 repo 最早那條教訓 ——
#   **本機與 CI 的環境差異**。行尾不是內容,不該讓它決定紅綠。
"""


def content_hash(path: Path) -> str:
    """行尾正規化之後的 sha256。

    ⚠ 不要改成直接 hash 原始位元組。第一版就是那樣,結果本機全綠、CI 紅:
    `golden_metrics.json` 的 Windows 工作副本是 CRLF,而 git 存的是 LF。
    行尾不是內容 —— 讓它決定紅綠,等於把一個環境差異偽裝成基準漂移。
    """
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def current() -> list[tuple[str, str]]:
    out = []
    for f in sorted(FIXTURES.iterdir()):
        if f.name == CHECKSUMS.name or not f.is_file():
            continue
        out.append((content_hash(f), f.name))
    return out


def render(rows: list[tuple[str, str]]) -> str:
    return HEADER + "\n" + "".join(f"{h}  {n}\n" for h, n in rows)


def main() -> int:
    # 這支腳本印中文。cp950 終端機上不先轉 UTF-8 會 UnicodeEncodeError ——
    # 由 test_reporting.py::test_every_entrypoint_that_prints_non_ascii_calls_force_utf8
    # 抓到(寫這支腳本時就被它抓了一次)。
    force_utf8()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="真的寫入檔案")
    args = ap.parse_args()

    body = render(current())
    old = CHECKSUMS.read_text(encoding="utf-8") if CHECKSUMS.exists() else ""

    if body == old:
        print("CHECKSUMS.txt 已經是最新的,沒有東西要改。")
        return 0

    print("以下 hash 會變動:")
    old_map = dict(
        (ln.split("  ")[1], ln.split("  ")[0])
        for ln in old.splitlines() if ln and not ln.startswith("#") and "  " in ln
    )
    for h, n in current():
        was = old_map.get(n)
        if was != h:
            print(f"  {n}: {(was or '(新檔案)')[:12]} -> {h[:12]}")
    for n in old_map:
        if n not in {x[1] for x in current()}:
            print(f"  {n}: 不見了")

    if not args.write:
        print()
        print("這只是預覽。確定要移動黃金基準的話加 --write,")
        print("並在 commit 訊息寫清楚為什麼、以及哪些數字動了。")
        return 1

    CHECKSUMS.write_text(body, encoding="utf-8", newline="\n")
    print()
    print(f"已寫入 {CHECKSUMS}")
    print("⚠ 別忘了 commit 訊息要說明:為什麼重產、哪些數字動了、訊號層有沒有動。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
