"""把 L1 的依賴失敗處理那段 shell 從 YAML 抽出來,**真的跑一次**。

為什麼不只讀 YAML
-----------------
這個 repo 的紀錄裡有兩次「讀起來是對的、跑起來不是」:

  * `A && B || C` 讀起來像 if/else,實際上 B 失敗時 C 也會跑
  * `git show X > f` 讀起來像「取回檔案」,實際上 f 在 git show 執行之前
    就被截斷成 0 bytes

workflow 的 shell 沒有編譯器、沒有型別、也沒有 CI 會去跑它。而這一段的工作
是在「安裝剛剛失敗」的那一刻把事情講出來 —— 它自己壞掉的症狀,就是整個
通知通道安靜。所以這裡的判準是**跑過**,不是**看過**。

做法:抽出那一步的 `run:`,把 `gh` 換成一支會記錄 argv 的假程式,
在 tmp 目錄裡真的執行,然後問三件事:
manifest 有沒有寫出來、Issue 有沒有被建立、用的是不是帶標記的內文檔。

⚠ 這仍然**不是**一次真實的 GitHub Actions 實跑。`${{ }}` 運算式由這裡代入,
step 的 `if:` 條件也沒有被 runner 求值 —— 那兩件事由
`tests/test_dependency_policy.py` 以靜態方式釘住。兩邊合起來才是完整的。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "daily-health.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="需要 bash 才能真的執行那段 workflow shell(CI 的 ubuntu runner 一定有)",
)


def _absolutise(shell: str) -> str:
    """把 repo 相對路徑換成絕對路徑,讓這段 shell 可以在 tmp 目錄裡跑。"""
    return (shell
            .replace("python ops/src/dep_manifest.py",
                     f"python {(REPO_ROOT / 'ops/src/dep_manifest.py').as_posix()}")
            .replace("--requested ops/requirements.txt",
                     f"--requested {(REPO_ROOT / 'ops/requirements.txt').as_posix()}"))


def _failure_step_shell() -> str:
    """workflow 裡「依賴安裝失敗 → manifest 與 Issue」那一步的 run 內容。"""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    hits = [s for s in data["jobs"]["health"]["steps"]
            if "--phase failure" in (s.get("run") or "")]
    assert len(hits) == 1, f"找不到(或有多個)失敗處理步驟:{len(hits)}"
    return hits[0]["run"]


@pytest.fixture()
def harness(tmp_path: Path):
    """假的 gh + 假的工作區。回傳 (執行函式, 記錄檔路徑)。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "gh_calls.log"

    # 假的 gh:把每次呼叫的參數記下來,並對 `issue list` 回空(=沒有既有 Issue)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{calls.as_posix()}"\n'
        'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then echo ""; fi\n'
        'if [ "$1" = "issue" ] && [ "$2" = "view" ]; then echo "(no body)"; fi\n'
        "exit 0\n",
        encoding="utf-8", newline="\n",
    )
    gh.chmod(0o755)

    log = tmp_path / "dep_install.log"
    log.write_text(
        "ERROR: Could not find a version that satisfies the requirement pandas>=2.2,<3\n"
        "ERROR: ResolutionImpossible\n",
        encoding="utf-8", newline="\n",
    )

    def run(shell: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        env["GITHUB_WORKSPACE"] = tmp_path.as_posix()
        env["GH_TOKEN"] = "fake-token-value-not-real"
        env["GITHUB_SHA"] = "d" * 40
        env["GITHUB_RUN_ID"] = "4242"
        env["PYTHONIOENCODING"] = "utf-8"
        # ⚠ cwd 用 tmp 目錄,不是 repo root。
        # 第一版用 repo root,於是那段 shell 的 `gh issue view ... > old_body.md`
        # 真的在**工作目錄裡**留下一個檔案 —— 一條會污染版控目錄的測試,
        # 下一個人 `git add -A` 的時候就會把它一起提交進去。
        # 代價是要把兩個 repo 相對路徑換成絕對路徑(在真的 runner 上,
        # cwd 就是 workspace root,所以那兩個相對路徑是對的)。
        return subprocess.run(
            # 與 GitHub runner 的預設一致:`bash --noprofile --norc -eo pipefail`。
            # 少了 -e,一個失敗的指令不會中斷,於是後面的斷言會對著一個
            # 「其實沒做成」的狀態說成功 —— 第一版就是這樣綠的。
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", shell],
            cwd=tmp_path, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    return run, calls, tmp_path


def test_the_failure_step_actually_writes_a_manifest_and_opens_an_issue(
    harness, tmp_path: Path
) -> None:
    run, calls, workspace = harness
    shell = _absolutise(_failure_step_shell()).replace(
        "ops/data/deps/install.json", (tmp_path / "install.json").as_posix())
    proc = run(shell)
    assert proc.returncode == 0, f"那段 shell 自己失敗了:\n{proc.stdout}\n{proc.stderr}"

    out = tmp_path / "install.json"
    assert out.exists(), "manifest 沒有被寫出來 —— 失敗那天會什麼都不剩"
    m = json.loads(out.read_text(encoding="utf-8"))
    assert m["resolution_complete"] is False
    assert m["error"]["class"] == "resolution"
    assert m["run"]["run_id"] == "4242"

    recorded = calls.read_text(encoding="utf-8")
    assert "issue create" in recorded, f"沒有建立 Issue。gh 的呼叫紀錄:\n{recorded}"
    assert "--label health-fail" in recorded, "Issue 沒有帶 health-fail 標籤"
    assert "dep_issue_body.md" in recorded, "Issue 用的不是帶 failset 標記的內文檔"

    body = (workspace / "dep_issue_body.md").read_text(encoding="utf-8")
    assert "<!-- failset: 依賴安裝 -->" in body
    assert "健檢完全沒有執行" in body or "健檢未執行" in body, (
        "內文沒有講明健檢根本沒跑 —— 讀的人會以為這是一次資料源故障"
    )


def test_the_issue_body_never_leaks_the_token(harness, tmp_path: Path) -> None:
    """GH_TOKEN 的值不得出現在要貼進【公開】repo 的內文裡。

    假 token 刻意設成一個不符合任何已知前綴樣式的字串,所以擋住它的只能是
    「比對實際環境變數的值」那一層。
    """
    run, _, workspace = harness
    shell = _absolutise(_failure_step_shell()).replace(
        "ops/data/deps/install.json", (tmp_path / "install.json").as_posix())
    # 讓安裝 log 裡真的含有那個 token(最壞情況:pip 把帶憑證的 URL 印出來)
    (workspace / "dep_install.log").write_text(
        "ERROR: failed https://x:fake-token-value-not-real@pypi.example/simple\n"
        "ResolutionImpossible\n",
        encoding="utf-8", newline="\n")
    proc = run(shell)
    assert proc.returncode == 0, proc.stderr

    body = (workspace / "dep_issue_body.md").read_text(encoding="utf-8")
    manifest = (tmp_path / "install.json").read_text(encoding="utf-8")
    for text, where in ((body, "Issue 內文"), (manifest, "manifest")):
        assert "fake-token-value-not-real" not in text, f"{where} 洩漏了 token"
    assert "***" in body, "遮蔽完全沒有發生 —— 檢查 redact() 有沒有被呼叫到"


def test_a_second_consecutive_failure_updates_instead_of_creating(
    harness, tmp_path: Path
) -> None:
    """已經有一張開放的 Issue 時,必須是**更新**而不是再開一張。

    「永遠只有一張 health-fail Issue」是既有的不變量 —— 一年 365 張的話,
    使用者會直接關掉通知,整層監控就等於不存在。
    """
    run, calls, workspace = harness
    bin_dir = tmp_path / "bin"
    # 這一次假的 gh 回報「已經有 #7 開著,而且上次就是依賴安裝失敗」
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{(tmp_path / "gh_calls.log").as_posix()}"\n'
        'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then echo "7"; fi\n'
        'if [ "$1" = "issue" ] && [ "$2" = "view" ]; then\n'
        '  echo "前情提要"; echo "<!-- failset: 依賴安裝 -->"; fi\n'
        "exit 0\n",
        encoding="utf-8", newline="\n")
    (bin_dir / "gh").chmod(0o755)

    shell = _absolutise(_failure_step_shell()).replace(
        "ops/data/deps/install.json", (tmp_path / "install.json").as_posix())
    proc = run(shell)
    assert proc.returncode == 0, proc.stderr

    recorded = calls.read_text(encoding="utf-8")
    assert "issue create" not in recorded, "已經有開放的 Issue 了,卻又開了一張新的"
    assert "issue edit 7" in recorded, f"沒有更新 #7:\n{recorded}"
    assert "issue comment" not in recorded, (
        "上次就是同一個原因,還是留了言 —— 每天一則會把 Issue 洗成沒人看的"
        "討論串,而這個 repo 實測過那個後果(52 則留言)"
    )
