"""L3 年度健檢和 `ops-data` 之間那兩步 —— 從 YAML 抽出來,對真的 git 跑一次。

## 為什麼這個檔案要存在

`annual-review.yml` 是 `workflow_dispatch` 的,一年跑一次 ——
**它是整個 repo 裡最久沒有被走過的路徑**,而且比關閉 Issue 那條更久:
那條至少在 2026-10-19 走過一次,這條一次都沒有。

它裡面有兩個 bug,而兩個的形狀都**已經寫在 `test_l1_failure_harness.py`
的 docstring 裡**(「A && B || C 讀起來像 if/else」、
「git show X > f 在 git show 執行之前就把 f 截斷成 0 bytes」)。
也就是說:這個 repo 認得這兩個陷阱、把它們寫下來了、在 `daily-health.yml`
的心跳步驟修掉了 —— 然後隔壁那個檔案原封不動。
**修掉一個實例不等於修掉那個形狀。**

## 實測到的連鎖(2026-10-19,本機真的 git)

`actions/checkout` 只會設定目前分支的 fetch refspec:

```
remote.origin.fetch = +refs/heads/main:refs/remotes/origin/main
git fetch origin ops-data        → exit 0,只寫 FETCH_HEAD
refs/remotes/origin/ops-data     → 不存在
git show origin/ops-data:…  > f  → 失敗,而 f 已經是 0 bytes
明確 refspec 的 fetch            → refs/remotes/origin/ops-data 存在
```

`git fetch` 回 0,所以 `if` 走的是成功那一支;`git show` 失敗,
訊息卻印「分支上尚無軌跡檔」—— 那個分支上有 59 天的軌跡。接著:

1. `ops/data/health_log.csv` 是 0 bytes,`exists()` 為真,
   `pd.read_csv` 拋 `EmptyDataError` → **整份年度報告產不出來**。
2. commit 那一步的 `A && B || C`:fetch 成功、checkout 失敗 → 走 C,
   也就是「從 main 開一條新的 ops-data」→ `git push` 非快轉被拒。

兩件事都不是「有機率發生」,是**每次都會發生**。

## 這裡怎麼驗

建一個真的 bare repo 當 origin,照 `actions/checkout` 的方式單分支複製,
把那一步的 `run:` 從 YAML 抽出來,在 `bash --noprofile --norc -eo pipefail`
底下真的執行(GitHub 預設的 shell 旗標),然後問檔案與訊息。

⚠ 這不是一次真實的 Actions 實跑:`${{ }}` 由這裡代入,step 的 `if:`
也沒有被 runner 求值。但它問的正是「讀起來對、跑起來不對」的那一層。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "annual-review.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="需要 bash 與 git 才能真的執行那段 workflow shell",
)


def _step_shell(needle: str) -> str:
    """抽出 `run:` 裡含有 `needle` 的那一步。"""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    hits = [s for s in data["jobs"]["review"]["steps"]
            if needle in (s.get("run") or "")]
    assert len(hits) == 1, f"找不到(或有多個)含 {needle!r} 的步驟:{len(hits)}"
    return hits[0]["run"]


def _run(shell: str, cwd: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    # GitHub 預設的 shell 是 `bash --noprofile --norc -eo pipefail`。
    # 少了 -e,失敗的指令不會中斷後面 —— 那會讓斷言跑在一個從未產生過的狀態上。
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", shell],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **env_extra}, timeout=180,
    )


def _origin(tmp_path: Path, *, with_log: bool) -> Path:
    """做一個真的 bare origin:main + ops-data 兩條分支。"""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", str(seed)], check=True)
    g = ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t"]
    (seed / "a.txt").write_text("main\n", encoding="utf-8")
    subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "main"], check=True)
    subprocess.run([*g, "branch", "-M", "main"], check=True)
    subprocess.run([*g, "checkout", "-q", "-b", "ops-data"], check=True)
    if with_log:
        d = seed / "ops" / "data"
        d.mkdir(parents=True)
        (d / "health_log.csv").write_text(
            "date,ok,n_checks,failed\n2026-08-21,1,19,\n2026-08-22,0,19,vix\n",
            encoding="utf-8")
        subprocess.run([*g, "add", "-A"], check=True)
    else:
        (seed / "b.txt").write_text("no log here\n", encoding="utf-8")
        subprocess.run([*g, "add", "-A"], check=True)
    subprocess.run([*g, "commit", "-qm", "data"], check=True)
    subprocess.run([*g, "remote", "add", "origin", str(bare)], check=True)
    subprocess.run([*g, "push", "-q", "--all", "origin"], check=True)
    return bare


def _runner_checkout(tmp_path: Path, bare: Path) -> Path:
    """照 `actions/checkout` 的方式複製:單分支,只設定 main 的 fetch refspec。"""
    work = tmp_path / "runner"
    subprocess.run(["git", "clone", "-q", "--branch", "main", "--single-branch",
                    str(bare), str(work)], check=True)
    spec = subprocess.run(["git", "-C", str(work), "config", "remote.origin.fetch"],
                          capture_output=True, text=True, check=True).stdout.strip()
    assert spec == "+refs/heads/main:refs/remotes/origin/main", (
        f"前提變了:這個複製的 fetch refspec 是 {spec!r}。整個 bug 的成因就是"
        "這條 refspec 不涵蓋 ops-data —— 前提不成立的話下面的測試沒有意義。"
    )
    return work


# --------------------------------------------------------------------------
# 取回健檢軌跡
# --------------------------------------------------------------------------

def test_the_trail_is_actually_fetched_when_ops_data_has_one(tmp_path: Path) -> None:
    """ops-data 上有軌跡 → 真的要拿到,而且內容不是空的。

    這是**會紅在舊版上**的那一條:舊版用 `git fetch origin ops-data`,
    exit 0 但不建立 `origin/ops-data`,於是 `git show` 失敗、檔案 0 bytes。
    """
    work = _runner_checkout(tmp_path, _origin(tmp_path, with_log=True))
    (tmp_path / "rt").mkdir(exist_ok=True)
    r = _run(_step_shell("health_log.csv"), work,
             {"RUNNER_TEMP": str(tmp_path / "rt")})

    assert r.returncode == 0, f"步驟失敗:\n{r.stdout}\n{r.stderr}"
    log = work / "ops" / "data" / "health_log.csv"
    assert log.exists(), f"軌跡沒有被取回:\n{r.stdout}"
    assert log.stat().st_size > 0, (
        "軌跡檔是 0 bytes —— 重導向在 git show 之前就把它建出來了。"
        "這種檔案 exists() 為真,而 pandas 讀它會讓整份年度報告產不出來。"
    )
    assert "2026-08-22" in log.read_text(encoding="utf-8")
    assert "已取回" in r.stdout, f"訊息沒說取回成功:{r.stdout!r}"


def test_a_missing_trail_file_leaves_no_empty_shell(tmp_path: Path) -> None:
    """ops-data 存在但上面沒有 health_log.csv → **不可以**留下一個 0 bytes 的檔案。

    「沒有這個檔」和「這個檔是空的」在下游是兩種完全不同的行為:
    前者走 `exists()` 為假的那條,後者會拋 EmptyDataError。
    """
    work = _runner_checkout(tmp_path, _origin(tmp_path, with_log=False))
    (tmp_path / "rt").mkdir(exist_ok=True)
    r = _run(_step_shell("health_log.csv"), work, {"RUNNER_TEMP": str(tmp_path / "rt")})

    assert r.returncode == 0, f"步驟失敗:\n{r.stdout}\n{r.stderr}"
    log = work / "ops" / "data" / "health_log.csv"
    assert not log.exists(), (
        f"取回失敗卻留下了一個 {log.stat().st_size} bytes 的檔案 —— "
        "下游會把它當成「有紀錄」去讀"
    )
    assert "尚無紀錄" in r.stdout or "還沒有" in r.stdout, (
        f"訊息要說清楚是「分支上沒有這個檔」:{r.stdout!r}"
    )


def test_the_message_does_not_blame_the_branch_when_the_branch_is_fine(
        tmp_path: Path) -> None:
    """ops-data 有軌跡時,不可以印出「分支尚不存在」之類的話。

    舊版就是這樣:fetch 回 0、show 失敗 → 印「分支上尚無軌跡檔」,
    而那個分支上有 59 天的資料。**錯的診斷比沒有診斷更貴** ——
    讀的人會去查一個沒有問題的地方。
    """
    work = _runner_checkout(tmp_path, _origin(tmp_path, with_log=True))
    (tmp_path / "rt").mkdir(exist_ok=True)
    r = _run(_step_shell("health_log.csv"), work, {"RUNNER_TEMP": str(tmp_path / "rt")})

    assert "尚不存在" not in r.stdout and "尚無軌跡" not in r.stdout, (
        f"軌跡明明取得到,訊息卻在怪分支:{r.stdout!r}"
    )


# --------------------------------------------------------------------------
# Commit 報告到 ops-data
# --------------------------------------------------------------------------

def test_the_report_commit_lands_on_top_of_ops_data(tmp_path: Path) -> None:
    """報告要接在 ops-data 既有歷史上,不是從 main 另開一條。

    `A && B || C`:fetch 成功、checkout 失敗 → C 從 main 開新分支 →
    push 非快轉被拒。這一條直接問結果:push 完之後,origin/ops-data
    的歷史裡**還找得到原本那一筆**。
    """
    bare = _origin(tmp_path, with_log=True)
    work = _runner_checkout(tmp_path, bare)
    before = subprocess.run(["git", "-C", str(bare), "rev-parse", "ops-data"],
                            capture_output=True, text=True, check=True).stdout.strip()

    reports = work / "ops" / "reports"
    reports.mkdir(parents=True)
    (reports / "2026-health.md").write_text("# 報告\n", encoding="utf-8")

    r = _run(_step_shell("ops-data 尚不存在,由本次建立"), work, {})
    assert r.returncode == 0, f"commit 步驟失敗:\n{r.stdout}\n{r.stderr}"

    after = subprocess.run(["git", "-C", str(bare), "rev-parse", "ops-data"],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert after != before, f"什麼都沒推上去:\n{r.stdout}"

    anc = subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor",
                          before, after])
    assert anc.returncode == 0, (
        "新的 ops-data 不是接在舊的後面 —— 那是從 main 另開的一條,"
        "真實環境裡這個 push 會被非快轉拒絕"
    )
    files = subprocess.run(["git", "-C", str(bare), "show", "--name-only",
                            "--format=", f"{after}"],
                           capture_output=True, text=True, check=True).stdout
    assert "ops/reports/2026-health.md" in files, f"報告沒有進 commit:{files!r}"
    assert "a.txt" not in files, (
        f"commit 裡出現了 main 的檔案 —— 分支基底錯了:{files!r}"
    )
