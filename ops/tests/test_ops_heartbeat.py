"""監控心跳 —— workflow 端寫得出來、PWA 端說得對。

背景:L1 每日健檢是排程 workflow,GitHub 會自動停用長期靜置 repo 的排程。
這套系統的前提是「沒消息就是好消息」—— 監控自己死掉時,症狀和一切正常完全一樣。

2026-10-20 的根因(T2)
----------------------
心跳原本是「有資料變動才 commit」的副產物。於是:

    健檢失敗 -> 沒有資料檔變動 -> `git diff --cached --quiet` 為真
             -> commit 被跳過 -> repo 沒有活動 -> 60 天倒數繼續走

**最需要心跳的那一天,正好是心跳消失的那一天。** 修法是一個獨立的
`ops/data/heartbeat.txt`,每次執行都寫、走 `always()`、commit/push 不得被
「沒有變化」擋掉。

為什麼這一輪把性質測試換成行為測試
----------------------------------
舊的 `test_failure_is_silent` 用正則確認 `.catch(()=>{})` 是空的。
那是在對準**字串形狀**,不是對準那個宣稱(「離線時不得誤報監控死了」)。
失敗路徑現在要退回舊判準,catch 就不再是空的 —— **宣稱沒變,形狀變了**,
而只認形狀的測試會擋下一個正確的修改,並逼人把它刪掉。

所以改成在 jsdom 裡跑真的那一支,問六種情境下畫面顯示什麼。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from conftest import OPS_ROOT, REPO_ROOT, require_js_bridge, require_shell

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "daily-health.yml"
BRIDGE = OPS_ROOT / "tests" / "heartbeat_bridge.mjs"
HEARTBEAT_REL = "ops/data/heartbeat.txt"
# 「寫」的那一步用重導向當指紋 —— 只用檔名會同時命中推送步驟的 git add。
HEARTBEAT_WRITE = f"> {HEARTBEAT_REL}"


@pytest.fixture(scope="module")
def html(index_html: Path) -> str:
    return index_html.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def health_steps() -> list[dict]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"]["health"]["steps"]


def _step_with(steps: list[dict], needle: str) -> dict:
    hits = [s for s in steps if needle in (s.get("run") or "")]
    assert len(hits) == 1, f"找不到(或有多個)含 {needle!r} 的步驟:{len(hits)}"
    return hits[0]


# ==========================================================================
# workflow 端:心跳一定會被寫、一定會被推
# ==========================================================================

def test_the_timestamp_step_runs_even_when_everything_else_failed(
        health_steps: list[dict]) -> None:
    """寫時間戳那一步必須是 `always()`。

    少了它,安裝失敗或健檢崩潰時這一步會被跳過 —— 而那正是唯一需要它的場合。
    """
    step = _step_with(health_steps, HEARTBEAT_WRITE)
    assert step.get("if") == "always()", (
        f"寫心跳的步驟條件是 {step.get('if')!r},不是 always() ——\n"
        "健檢失敗那天它就不會跑,而那天正是 60 天倒數最需要被重置的一天。")


def test_the_timestamp_does_not_depend_on_the_health_result(
        health_steps: list[dict]) -> None:
    """時間戳的內容不得讀健檢結果。

    `always()` 只保證「步驟會跑」。如果內容還是 `if code == 0` 才寫,
    等於把同一個 bug 換一個地方再寫一次 —— 而症狀完全相同:安靜的無活動。
    """
    run = _step_with(health_steps, HEARTBEAT_WRITE)["run"]
    for forbidden in ("steps.code", "steps.check", "steps.install", "outputs.code"):
        assert forbidden not in run, (
            f"寫心跳那一步引用了 {forbidden} —— 心跳不得取決於健檢成敗")


def test_the_timestamp_command_really_produces_iso8601_utc() -> None:
    """`實測`:把 YAML 裡那一行 date 指令真的跑一次,確認格式。

    不是比對字串,是執行它。格式寫錯(少了 Z、用了本地時區)的症狀是
    PWA 的 `Date.parse` 回 NaN,而那條路徑是靜默的 —— 指示器會永遠不說話。
    """
    require_shell("心跳時間戳的格式(workflow 裡那行 date 指令的實際輸出)", "bash")

    run = _step_with(
        yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["health"]["steps"],
        HEARTBEAT_WRITE)["run"]
    m = re.search(r"^\s*(date -u [^>]+)>", run, re.M)
    assert m, f"找不到 date 指令:\n{run}"
    cmd = m.group(1).strip()

    # ⚠ 與時鐘無關的那一半。下面的「時間差 < 300 秒」在 UTC runner 上
    # 【抓不到】拿掉 -u 的突變,因為那裡本地時間就等於 UTC ——
    # 實測:M128 在本機(UTC+8)被時間差抓到,在 CI 上會是綠的。
    # 這是 M67 的同一個形狀:時區相關的判準要用與時鐘無關的方式釘。
    assert " -u " in f" {cmd} ", (
        f"{cmd!r} 沒有 -u,寫出去的會是 runner 的本地時間而尾巴仍掛著 Z")

    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                         timeout=30, encoding="utf-8").stdout.strip()

    assert "\n" not in out, f"心跳必須是單行,實際拿到 {out!r}"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", out), (
        f"{cmd!r} 產生 {out!r},不是單行 UTC ISO 8601(例:2026-10-20T03:50:00Z)")

    parsed = datetime.strptime(out, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    drift = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert drift < 300, f"時間戳與現在差 {drift:.0f} 秒 —— 可能寫的是本地時間不是 UTC"


def test_the_heartbeat_file_is_staged_for_the_push(health_steps: list[dict]) -> None:
    """推送那一步必須真的 add 這個檔案 —— 寫了卻沒推等於沒寫。"""
    push = _step_with(health_steps, "git push origin ops-data")["run"]
    assert f"git add -f {HEARTBEAT_REL}" in push, (
        f"推送步驟沒有 `git add -f {HEARTBEAT_REL}` —— 時間戳寫了但不會進 commit")
    assert f'cp "$tmp"/heartbeat.txt' in push, (
        "heartbeat.txt 沒有被搬到 ops-data 的工作區")


def test_the_push_cannot_be_skipped_by_an_empty_diff(health_steps: list[dict]) -> None:
    """`git push` 不得被關在「有變化」的 else 分支裡。

    ⚠ 這一條就是 T2 的核心。舊版長這樣:

        if git diff --cached --quiet; then
          echo "資料無變化,跳過 commit"
        else
          git commit ...
          git push ...
        fi

    於是「今天沒有新資料」和「今天監控沒跑」在 ops-data 上長得一模一樣,
    而前者每發生一次,60 天倒數就白走一天。
    """
    push = _step_with(health_steps, "git push origin ops-data")["run"]

    guard = push.find("if git diff --cached --quiet")
    assert guard != -1, "找不到那個 diff 判斷 —— 這條測試的前提變了,請重寫它"
    closing = push.find("\nfi", guard)
    assert closing != -1, f"找不到 diff 區塊的結尾 fi:\n{push[guard:]}"

    block = push[guard:closing]
    assert "git push" not in block, (
        "`git push` 還在 diff 判斷的區塊裡 —— 沒有變化的那天就不會推,"
        "而那正是 60 天懸崖的失效路徑")
    assert "--allow-empty" in block, (
        "沒有變化時必須仍然送出(空)commit。heartbeat.txt 每次都會變,"
        "所以 diff 為空本身就是異常 —— 更要留下紀錄,不是安靜跳過")
    assert push.find("git push origin ops-data", closing) != -1, (
        "`git push` 不在 diff 區塊之後 —— 它必須無條件執行")


# ==========================================================================
# PWA 端:路徑對得上
# ==========================================================================

def test_opsnote_element_exists(html: str) -> None:
    """沒有這個節點,警告就無處可去 —— 而且會靜靜地無處可去。"""
    assert 'id="opsnote"' in html, "PWA 少了 #opsnote,心跳警告無法顯示"


def test_threshold_not_too_aggressive(html: str) -> None:
    """門檻不得低於 3 天。

    心跳是每日的,但單次 outage、GitHub 事故、workflow 排隊都會製造 1–2 天空窗。
    距離 60 天的停用大限還很遠,沒有理由激進。
    """
    m = re.search(r"const OPS_STALE_DAYS\s*=\s*(\d+)", html)
    assert m, "找不到 OPS_STALE_DAYS"
    assert int(m.group(1)) >= 3, f"門檻 {m.group(1)} 天太短,週末或單次 outage 就會誤報"


def test_pwa_reads_the_path_the_workflow_writes(html: str) -> None:
    """PWA 讀的心跳路徑必須等於 workflow 實際寫的路徑。

    兩邊沒有任何程式上的關聯,靠的是一個手打的字串。搬動位置就會讓指示器
    永遠 404 —— 而 404 是靜默的,所以這個錯誤【不會有任何症狀】。
    """
    m = re.search(r'const OPS_HEARTBEAT\s*=\s*"([^"]+)"', html)
    assert m, "找不到 OPS_HEARTBEAT"
    url = m.group(1)
    assert url.endswith("/" + HEARTBEAT_REL), (
        f"PWA 讀 {url}\n但 workflow 寫在 {HEARTBEAT_REL} —— 路徑不一致且無症狀")
    assert "/ops-data/" in url, "心跳在 ops-data 分支,不在 main"


def test_url_matches_where_the_manifest_actually_writes(html: str) -> None:
    """資料年齡那一路仍然讀 manifest,路徑同樣要對得上。"""
    import fetch_data

    repo_root = Path(fetch_data.__file__).resolve().parents[2]
    rel = fetch_data.MANIFEST_PATH.resolve().relative_to(repo_root).as_posix()

    m = re.search(r'const OPS_MANIFEST\s*=\s*"([^"]+)"', html)
    assert m, "找不到 OPS_MANIFEST"
    assert m.group(1).endswith("/" + rel), (
        f"PWA 讀 {m.group(1)}\n但 manifest 實際寫在 {rel}")
    assert "/ops-data/" in m.group(1), "manifest 在 ops-data 分支,不在 main"


# ==========================================================================
# PWA 端:行為 —— 六個情境,在 jsdom 裡跑真的那一支
# ==========================================================================

def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _manifest(days: float) -> dict:
    return {"sp500": {"fetched_at": _ago(days)}, "vix": {"fetched_at": _ago(days)}}


def _run(tmp_path: Path, scenario: dict) -> dict:
    require_js_bridge(
        "心跳指示器的行為 —— 離線/404/壞資料時是否誤報「監控死了」,"
        "以及「監控死了」與「資料停了」有沒有說成同一句話")
    f = tmp_path / "scenario.json"
    f.write_text(json.dumps(scenario), encoding="utf-8")
    r = subprocess.run(
        ["node", str(BRIDGE), str(REPO_ROOT / "index.html"), str(f)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=OPS_ROOT, timeout=120)
    assert r.returncode == 0, f"橋接器失敗:\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


@pytest.mark.parametrize("name,scenario", [
    ("兩邊都離線", {"heartbeat": None, "manifest": None}),
    ("兩邊都 404", {"heartbeat": {"status": 404}, "manifest": {"status": 404}}),
    ("心跳新、資料新", {"heartbeat": {"status": 200, "text": _ago(0.5)},
                        "manifest": {"status": 200, "json": _manifest(0.5)}}),
    ("心跳新、資料也只有 2 天", {"heartbeat": {"status": 200, "text": _ago(0.2)},
                                 "manifest": {"status": 200, "json": _manifest(2)}}),
])
def test_these_situations_must_stay_silent(tmp_path: Path, name: str,
                                           scenario: dict) -> None:
    """誤報比漏報更糟 —— 一個會亂叫的指示燈幾天後就會被無視,那等於沒有。

    前兩個情境是使用者在飛機上、或在公司防火牆後面。那不是「監控死了」的證據。
    """
    out = _run(tmp_path, scenario)
    assert not out["on"], f"「{name}」不該顯示任何警告,卻顯示了:{out['text'][:120]}"
    assert out["text"].strip() == "", f"「{name}」留下了文字:{out['text'][:120]}"


def test_a_stale_heartbeat_says_the_monitor_is_down(tmp_path: Path) -> None:
    """心跳超過門檻 -> 指控排程本身。這是 60 天懸崖真的發生時的畫面。"""
    out = _run(tmp_path, {"heartbeat": {"status": 200, "text": _ago(9)},
                          "manifest": {"status": 200, "json": _manifest(9)}})
    assert out["on"], "心跳已 9 天,指示器卻沒亮"
    assert "沒有心跳" in out["text"], f"沒說出是心跳停了:{out['text'][:160]}"
    assert "9 天" in out["text"], f"沒說出幾天:{out['text'][:160]}"


def test_a_live_heartbeat_with_stale_data_does_not_blame_the_scheduler(
        tmp_path: Path) -> None:
    """心跳正常但資料停滯 -> 這【不是】排程被停用,不准那樣說。

    這就是健檢連續失敗的樣子。舊版只看 manifest,會說「監控已 N 天沒有心跳」——
    那句話是錯的,而它指向的修法(去查排程有沒有被 GitHub 停用)也是錯的。
    **說錯話的警示燈和不會響的警示燈一樣沒用**,而且更浪費人的時間。
    """
    out = _run(tmp_path, {"heartbeat": {"status": 200, "text": _ago(0.2)},
                          "manifest": {"status": 200, "json": _manifest(9)}})
    assert out["on"], "資料已 9 天沒前進,指示器卻沒亮"
    assert "沒有心跳" not in out["text"], (
        f"心跳是正常的,卻說成「沒有心跳」:{out['text'][:160]}")
    assert "仍在運作" in out["text"] and "9 天" in out["text"], (
        f"沒說出「排程活著、資料停了」:{out['text'][:160]}")


def test_an_unparseable_heartbeat_falls_back_instead_of_crashing(
        tmp_path: Path) -> None:
    """心跳檔內容壞掉 -> 退回舊判準(看資料年齡),不得當成「心跳正常」。

    把壞資料當成新鮮心跳,會讓這整條偵測線安靜地失效 ——
    而那是本 repo 一再踩到的同一個形狀:失敗時回安全值,在量測路徑上是陷阱。
    """
    out = _run(tmp_path, {"heartbeat": {"status": 200, "text": "(not a date)"},
                          "manifest": {"status": 200, "json": _manifest(9)}})
    assert out["on"], "心跳壞掉又退回去看資料(已 9 天),仍然該亮"
    assert "沒有心跳" in out["text"], (
        f"問不到心跳時只能沿用舊判準的說法:{out['text'][:160]}")


def test_a_missing_heartbeat_file_still_uses_the_old_criterion(
        tmp_path: Path) -> None:
    """heartbeat.txt 還沒出現(第一次部署、ops-data 上還沒有)時不得失去偵測。

    向後相容:404 退回看 manifest,行為與 2026-10-20 之前一致。
    """
    out = _run(tmp_path, {"heartbeat": {"status": 404},
                          "manifest": {"status": 200, "json": _manifest(9)}})
    assert out["on"], "heartbeat.txt 不存在時就完全不偵測了 —— 那是退步"
    assert "9 天" in out["text"]
