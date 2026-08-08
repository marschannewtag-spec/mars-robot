"""PWA 端「監控心跳指示器」的性質測試。

背景:L1 每日健檢是排程 workflow,GitHub 會自動停用長期靜置 repo 的排程。
這套系統的前提是「沒消息就是好消息」—— 監控自己死掉時,症狀和一切正常完全一樣。
`index.html` 的 `checkOpsHeartbeat()` 是補上的第二條偵測線。

**這裡測的是原始碼的性質,不是行為。** 行為(3天/9天/離線/404/壞資料五個分支)
是在瀏覽器裡人工驗過的 —— 那需要 DOM + fetch,不適合放進這個離線測試層。
這幾條的用途是釘住兩個【很容易被好意破壞】的設計決定,以及一個跨檔案耦合。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def html(index_html: Path) -> str:
    return index_html.read_text(encoding="utf-8")


def test_opsnote_element_exists(html: str) -> None:
    """沒有這個節點,警告就無處可去 —— 而且會靜靜地無處可去。"""
    assert 'id="opsnote"' in html, "PWA 少了 #opsnote,心跳警告無法顯示"


def test_failure_is_silent(html: str) -> None:
    """失敗即靜默 —— 這條被破壞的話,離線就會誤報「監控死了」。

    誤報比漏報更糟:一個會亂叫的指示燈幾天後就會被無視,那等於沒有。
    如果哪天真的想在失敗時顯示東西,先想清楚使用者離線時該看到什麼。
    """
    body = re.search(r"function checkOpsHeartbeat\(\)\s*\{.*?\n\}", html, re.DOTALL)
    assert body, "找不到 checkOpsHeartbeat()"
    assert re.search(r"\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)", body.group(0)), (
        "checkOpsHeartbeat() 的 catch 不再是空的 —— "
        "失敗即靜默被破壞了,離線會變成誤報"
    )


def test_threshold_not_too_aggressive(html: str) -> None:
    """門檻不得低於 3 天。

    心跳是每日的,但單次 outage、GitHub 事故、workflow 排隊都會製造 1–2 天空窗。
    距離 60 天的停用大限還很遠,沒有理由激進。
    """
    m = re.search(r"const OPS_STALE_DAYS\s*=\s*(\d+)", html)
    assert m, "找不到 OPS_STALE_DAYS"
    assert int(m.group(1)) >= 3, f"門檻 {m.group(1)} 天太短,週末或單次 outage 就會誤報"


def test_url_matches_where_the_heartbeat_actually_writes(html: str) -> None:
    """PWA 讀的路徑必須等於 fetch_data.py 實際寫的路徑。

    這兩個檔案沒有任何程式上的關聯,靠的是一個手打的字串 —— 搬動 manifest
    的位置就會讓指示器永遠 404。而 404 是靜默的(上面那條測試保證的),
    所以這個錯誤【不會有任何症狀】,只會讓第二條防線無聲失效。
    """
    import fetch_data

    repo_root = Path(fetch_data.__file__).resolve().parents[2]
    rel = fetch_data.MANIFEST_PATH.resolve().relative_to(repo_root).as_posix()

    m = re.search(r'const OPS_MANIFEST\s*=\s*"([^"]+)"', html)
    assert m, "找不到 OPS_MANIFEST"
    url = m.group(1)

    assert url.endswith("/" + rel), (
        f"PWA 讀 {url}\n"
        f"但心跳實際寫在 {rel} —— 路徑不一致,指示器會永遠 404 且不會有任何症狀"
    )
    assert "/ops-data/" in url, "心跳資料在 ops-data 分支,不在 main"
