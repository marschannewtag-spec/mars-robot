"""壓縮 Markdown 時,確認「有效載荷」一個都沒有掉。

散文可以縮,**payload 不行**。payload 是:
  · 圍欄程式碼區塊(逐字)
  · 行內 code span(`...`)
  · URL
  · 路徑(含斜線、或以已知副檔名結尾的字串)
  · 數字(含小數、百分比、版本號)
  · 標題文字

用法:
    python scripts/md_payload_check.py snapshot <file.md> <out.json>
    python scripts/md_payload_check.py verify   <file.md> <snapshot.json>

`verify` 只檢查「舊的有、新的沒有」—— 新增東西是允許的。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from console import force_utf8  # noqa: E402

# ⚠ 錨在行首。散文裡會出現行中的 ```bash（在講那條會執行文件指令的
#   測試時），不錨的話配對會歪掉，把整段散文當成程式碼區塊。
#   判準要對準結構，不是對準字串 —— 這個檢查器自己踩過一次。
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.S | re.M)
INLINE = re.compile(r"`([^`\n]+)`")
URL = re.compile(r"https?://[^\s)\]\"'<>]+")
PATH = re.compile(
    r"[A-Za-z0-9_.\-/]+\.(?:py|js|md|yml|yaml|json|csv|txt|html|lock|pine|mjs|sh)\b")
NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)*%?")
HEAD = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)


def payload(text: str) -> dict[str, list[str]]:
    fences = [b.strip() for b in FENCE.findall(text)]
    # 行內 code / 數字等,要把圍欄區塊的內容排除在外再抽一次,
    # 否則「區塊被保留」會讓裡面的 token 重複計算、掩蓋散文裡掉的東西。
    prose = FENCE.sub("\n", text)
    return {
        "fences": fences,
        "inline": sorted(set(INLINE.findall(prose))),
        "urls": sorted(set(URL.findall(text))),
        "paths": sorted(set(PATH.findall(text))),
        "numbers": sorted(set(NUM.findall(prose))),
        "headings": HEAD.findall(text),
    }


def main() -> int:
    force_utf8()
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    mode, md, snap = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    p = payload(md.read_text(encoding="utf-8"))

    if mode == "snapshot":
        snap.write_text(json.dumps(p, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")
        print(f"{md.name}: " + " · ".join(f"{k} {len(v)}" for k, v in p.items()))
        return 0

    if mode != "verify":
        print(__doc__)
        return 2

    old = json.loads(snap.read_text(encoding="utf-8"))
    lost: dict[str, list[str]] = {}
    for key, items in old.items():
        now = p[key]
        if key == "fences":
            # 區塊要逐字還在
            missing = [b for b in items if b not in now]
        else:
            missing = [x for x in items if x not in set(now)]
        if missing:
            lost[key] = missing

    if not lost:
        print(f"✓ {md.name}: payload 完整 —— " +
              " · ".join(f"{k} {len(v)}" for k, v in old.items()))
        return 0

    print(f"✗ {md.name}: 以下 payload 不見了")
    for key, items in lost.items():
        print(f"\n  [{key}] 少了 {len(items)} 項:")
        for x in items[:40]:
            one = x.replace("\n", " ⏎ ")
            print(f"    - {one[:110]}")
        if len(items) > 40:
            print(f"    … 還有 {len(items) - 40} 項")
    return 1


if __name__ == "__main__":
    sys.exit(main())
