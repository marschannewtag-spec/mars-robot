"""相依治理的契約 —— 宣告檔、L1/L2 的安裝路徑、canary 的獨立性。

決策與證據寫在 `ops/DEPENDENCIES.md`。這個檔案的工作是讓那些決策**不能
被默默改掉**:它們幾乎全部活在 YAML 裡,而 YAML 沒有型別、沒有編譯器,
改錯了唯一的症狀是「某一天的行為跟文件說的不一樣」。

⚠ 掃描式測試最容易變成恆真,所以這裡每一條「某個東西必須存在」的斷言,
都配一條「掃描真的找到東西」的前置斷言(`_step` 找不到就直接失敗)。
而且所有掃描都**先把 YAML 註解行拿掉** —— 這個 repo 已經兩次比對到自己的
解釋性註解,而症狀都是「測試在看一個不是事實產生處的地方」。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

RUNTIME_FILE = OPS_ROOT / "requirements.txt"
TEST_FILE = OPS_ROOT / "requirements-test.txt"
LOCK_FILE = OPS_ROOT / "requirements-ci.lock"
ENTRYPOINT = OPS_ROOT / "scripts" / "ci_checks.sh"
POLICY_DOC = OPS_ROOT / "DEPENDENCIES.md"

TEST_ONLY = {"pytest", "pytest-cov", "packaging"}
RUNTIME_ONLY = {"pandas", "numpy", "requests", "pyyaml"}


def _requirements(path: Path) -> dict[str, str]:
    """{正規化套件名: 完整需求行}。"""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[]", line)[0].strip().lower()
        out[name] = line
    assert out, f"{path.name} 解析不出任何需求 —— 下面的斷言會變成恆真"
    return out


def _code(path: Path) -> str:
    """去掉整行註解之後的 YAML/shell 文字。

    不去掉的話,斷言可能會比對到解釋它自己的那段註解 —— 這個 repo 踩過兩次。
    """
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


def _job(workflow: str, job: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    assert job in data["jobs"], f"{workflow} 裡沒有 {job} job"
    return data["jobs"][job]


def _step(job: dict, needle: str) -> dict:
    """跑了 `needle` 的那一步。找不到或找到多個就**大聲失敗**。"""
    hits = [s for s in job["steps"] if needle in (s.get("run") or "")]
    assert len(hits) == 1, f"跑 `{needle}` 的步驟有 {len(hits)} 個,預期恰好 1 個"
    return hits[0]


@pytest.fixture(scope="module")
def health() -> dict:
    return _job("daily-health.yml", "health")


@pytest.fixture(scope="module")
def canary() -> dict:
    return _job("dependency-canary.yml", "canary")


# ══════════════════════════════════════════════════════════════════════════
# 1. 宣告檔的分工
# ══════════════════════════════════════════════════════════════════════════

def test_the_runtime_file_contains_no_test_tooling() -> None:
    """L1 的安裝清單裡不得有測試工具。

    這是整個拆分的理由:在拆開之前,**pytest 的一次解析失敗有能力讓每日
    資料供應停擺**(L1 裝的就是同一份檔案)。測試工具對那條路徑毫無貢獻。
    """
    runtime = set(_requirements(RUNTIME_FILE))
    leaked = runtime & TEST_ONLY
    assert not leaked, f"測試工具回到 runtime 宣告裡了:{sorted(leaked)}"
    assert runtime == RUNTIME_ONLY, (
        f"runtime 宣告與實際 import 掃描的結果不符:{sorted(runtime)} "
        f"vs {sorted(RUNTIME_ONLY)} —— 新增套件時請同時確認它真的被 src/ import"
    )


def test_the_test_file_contains_exactly_the_test_tooling() -> None:
    assert set(_requirements(TEST_FILE)) == TEST_ONLY


def test_no_package_is_declared_in_both_files() -> None:
    """同一個套件出現在兩份檔案裡,兩個範圍遲早會漂成不一致。"""
    both = set(_requirements(RUNTIME_FILE)) & set(_requirements(TEST_FILE))
    assert not both, f"重複宣告:{sorted(both)}"


def test_every_declared_range_has_an_upper_bound() -> None:
    """沒有上界 = 承諾會跑還不存在的程式碼。

    黃金基準是 440 個月 `1e-9` 精確比對,而「裝到新版」是最難歸因的紅燈 ——
    它會偽裝成「程式被改壞了」。
    """
    for path in (RUNTIME_FILE, TEST_FILE):
        for name, line in _requirements(path).items():
            assert "<" in line, f"{path.name} 的 {name} 沒有上界:{line}"


# ══════════════════════════════════════════════════════════════════════════
# 2. L1:只裝 runtime,而且安裝失敗必須是看得見的健康事件
# ══════════════════════════════════════════════════════════════════════════

def test_l1_installs_only_the_runtime_file(health: dict) -> None:
    step = _step(health, "pip install -r ops/requirements.txt")
    run = step["run"]
    assert "requirements-test.txt" not in run, "L1 裝了測試工具"
    assert "requirements-ci.lock" not in run, (
        "L1 裝了精確鎖定 —— 鎖定檔過期/平台沒 wheel 會變成每日資料供應故障,"
        "那正是 D1 要避免的"
    )
    assert step.get("id") == "install", "後面的失敗處理靠 steps.install.outcome"
    assert step.get("continue-on-error") is True, (
        "少了 continue-on-error,安裝失敗會直接中止 job,於是失敗處理步驟"
        "完全跑不到 —— 結果是 workflow 紅了但 Issue 安靜"
    )


def test_no_workflow_sneaks_test_tooling_into_the_l1_job(health: dict) -> None:
    """整個 health job 都不該出現測試工具。對照組:它確實出現在 autofix。"""
    joined = "\n".join(s.get("run") or "" for s in health["steps"])
    assert "requirements-test.txt" not in joined
    autofix = _job("daily-health.yml", "autofix")
    joined_fix = "\n".join(s.get("run") or "" for s in autofix["steps"])
    assert "requirements-test.txt" in joined_fix, (
        "autofix 要跑 pytest,它必須裝測試工具 —— 這條是上面那條的對照組,"
        "少了它,「沒出現」可能只是因為整個掃描是壞的"
    )


def test_the_health_code_is_written_by_a_step_that_always_runs(health: dict) -> None:
    """安裝失敗時健康碼不得是空字串。

    空字串在 `!= '0'` 下為真、在 `== '0'` 下為假 —— 下游每一個條件都會拿到
    一個沒有人決定過的答案。
    """
    step = _step(health, "code=90")
    assert step.get("id") == "code"
    assert step.get("if") == "always()", "健康碼的步驟必須一定會執行"
    data = yaml.safe_load((WORKFLOWS / "daily-health.yml").read_text(encoding="utf-8"))
    assert data["jobs"]["health"]["outputs"]["code"] == "${{ steps.code.outputs.code }}", (
        "job output 還是直接取健檢那一步的 code —— 安裝失敗時那是空字串"
    )


def test_install_failure_writes_a_manifest_and_opens_an_issue(health: dict) -> None:
    step = _step(health, "--phase failure")
    assert step.get("if") == "steps.install.outcome == 'failure'"
    run = step["run"]
    assert "dep_manifest.py" in run
    assert "--emit-issue" in run
    assert "gh issue create" in run and "gh issue edit" in run, (
        "只建立不更新(或反過來)會讓「永遠只有一張 Issue」那條不變量破掉"
    )
    assert "--label health-fail" in run, "不帶標籤的話,恢復正常那天不會被關掉"


def test_the_report_driven_issue_step_cannot_run_without_a_report(health: dict) -> None:
    """安裝失敗時那一步【不能】跑:報告不存在,而 --issue-plan 需要 pandas。"""
    step = _step(health, "--issue-plan")
    cond = step.get("if") or ""
    assert "steps.install.outcome == 'success'" in cond, (
        "安裝失敗時這一步會跑 → 讀不到 health_report.md,而且它需要的 pandas "
        "正是剛剛裝失敗的東西"
    )


def test_the_dependency_manifest_artifact_is_uploaded_on_both_paths(health: dict) -> None:
    hits = [s for s in health["steps"]
            if (s.get("uses") or "").startswith("actions/upload-artifact")
            and (s.get("with") or {}).get("name") == "dependency-manifest"]
    assert len(hits) == 1, "找不到(或有多個)dependency-manifest artifact"
    step = hits[0]
    assert step.get("if") == "always()", (
        "少了 always(),安裝失敗時這個 artifact 會被跳過 —— 而那天的 manifest "
        "正是唯一有診斷價值的輸出"
    )
    assert int((step["with"]).get("retention-days", 0)) > 0, "沒有明確的保留天數"


def test_the_heartbeat_persists_the_dependency_manifest(health: dict) -> None:
    """安裝失敗那天,manifest 是唯一會變動的檔案 ——

    也就是那天唯一能讓心跳真的 commit 的東西,而「有 commit」就是排程
    不被 GitHub 自動停用的條件。
    """
    step = _step(health, "git push origin ops-data")
    run = step["run"]
    assert "git add -f ops/data/deps" in run
    assert "ops/data/deps/" in run, "沒有把 manifest 複製進 ops-data 的工作目錄"


def test_autofix_is_not_triggered_by_a_dependency_failure() -> None:
    """叫 Claude 來修「pip install 失敗」沒有意義 ——

    它自己的 job 也要跑 pip install,會在同一個地方失敗;而且它的輸入是
    health_report.md,那份報告在安裝失敗時不存在。
    """
    data = yaml.safe_load((WORKFLOWS / "daily-health.yml").read_text(encoding="utf-8"))
    cond = data["jobs"]["autofix"]["if"]
    assert "!= '90'" in cond, f"autofix 的條件沒有排除依賴安裝失敗:{cond}"


# ══════════════════════════════════════════════════════════════════════════
# 3. L2:精確鎖定 + hash 驗證
# ══════════════════════════════════════════════════════════════════════════

def test_l2_installs_the_lock_with_hash_verification() -> None:
    job = _job("ci.yml", "test")
    step = _step(job, "pip install --require-hashes")
    run = step["run"]
    assert "requirements-ci.lock" in run
    assert "pip install -r ops/requirements.txt" not in run, (
        "L2 同時裝了範圍 —— 那會讓鎖定失去意義(最後裝到的是解析出來的最新版)"
    )


def test_the_lock_pins_every_declared_requirement_with_hashes() -> None:
    text = LOCK_FILE.read_text(encoding="utf-8")
    pinned = dict(re.findall(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", text, re.M))
    assert pinned, "鎖定檔解析不出任何 pin"
    normalised = {re.sub(r"[-_.]+", "-", k).lower(): v for k, v in pinned.items()}
    declared = set(_requirements(RUNTIME_FILE)) | set(_requirements(TEST_FILE))
    missing = declared - set(normalised)
    assert not missing, f"宣告了但鎖定檔裡沒有:{sorted(missing)}"
    # 每一個 pin 後面都必須有 hash。沒有的話 --require-hashes 會直接拒絕安裝。
    for name in pinned:
        block = text.split(f"{name}==", 1)[1][:4000]
        assert "--hash=sha256:" in block.split("\n# via")[0], f"{name} 沒有 hash"


def test_the_locked_versions_satisfy_the_declared_ranges() -> None:
    """鎖定的版本必須落在宣告範圍內。

    不然會出現最糟的狀態:**文件說支援 A,CI 實際跑 B**,而這個 repo
    已經在 pytest 上踩過一次(宣告 `<9`、開發機 9.1.1,整個專案期間沒人發現)。
    """
    from packaging.requirements import Requirement
    from packaging.version import Version

    text = LOCK_FILE.read_text(encoding="utf-8")
    pinned = {re.sub(r"[-_.]+", "-", k).lower(): v
              for k, v in re.findall(r"^([A-Za-z0-9._-]+)==([^\s\\]+)", text, re.M)}
    for path in (RUNTIME_FILE, TEST_FILE):
        for name, line in _requirements(path).items():
            req = Requirement(line)
            assert Version(pinned[name]) in req.specifier, (
                f"{name} 鎖在 {pinned[name]},但宣告是 {req.specifier}"
            )


def test_the_lock_generation_command_is_pinned_and_consistent() -> None:
    """鎖定檔的產生過程本身必須可重現。

    生成工具不釘版本的話,鎖定檔的內容會取決於「那天的 uv 是哪一版」——
    一個為了可重現而存在的檔案,自己的產生過程卻不可重現。
    """
    header = LOCK_FILE.read_text(encoding="utf-8").splitlines()[1]
    assert "uv pip compile" in header, "鎖定檔標頭沒有記錄產生指令"
    for flag in ("--generate-hashes", "--python-version 3.12", "--python-platform linux"):
        assert flag in header, f"鎖定檔不是用 {flag} 產生的"

    canary_text = _code(WORKFLOWS / "dependency-canary.yml")
    assert re.search(r'uv==\d+\.\d+\.\d+', canary_text), "canary 沒有釘住 uv 版本"
    for flag in ("--generate-hashes", "--python-version 3.12", "--python-platform linux"):
        assert flag in canary_text, f"canary 重新產生鎖定檔時少了 {flag}"
    policy = POLICY_DOC.read_text(encoding="utf-8")
    assert re.search(r'uv==\d+\.\d+\.\d+', policy), "DEPENDENCIES.md 沒有記錄工具版本"


# ══════════════════════════════════════════════════════════════════════════
# 4. canary:獨立,而且不自動合併
# ══════════════════════════════════════════════════════════════════════════

def test_the_canary_is_a_separate_workflow_with_no_dependency_on_l1(canary: dict) -> None:
    """canary 失敗不得改變每日健檢的結論。

    上游發新版不是「我們的資料壞了」,而一條會為了別人的發布而亮紅燈的警報,
    幾週後就會被無視 —— 這個 repo 實測過連紅 54 天的後果。
    """
    assert canary.get("needs") is None, "canary 掛了 needs → 它的結果會綁到別的 job"
    text = _code(WORKFLOWS / "dependency-canary.yml")
    assert "needs.health" not in text, "canary 讀了 health 的 output"
    assert "steps.check.outputs.code" not in text
    assert "ops-data" not in text, "canary 不該碰 ops-data(那是 L1 的資料分支)"


def test_the_canary_does_not_reuse_the_health_fail_label(canary: dict) -> None:
    """預警與故障必須是兩個通道。混在一起,使用者就分不出
    「線上壞了」和「上游發了新版」。"""
    text = _code(WORKFLOWS / "dependency-canary.yml")
    assert "dependency-canary" in text
    assert "health-fail" not in text, "canary 用了健檢的標籤 → 兩種事件混成一種"


def test_the_canary_dedupes_by_version_combination(canary: dict) -> None:
    text = _code(WORKFLOWS / "dependency-canary.yml")
    assert "depcombo:" in text, "沒有用版本組合當去重 key"
    assert "dedupe_key" in text
    assert "gh issue comment" in text and "gh issue edit" in text, (
        "只會新建不會更新 → 同一個組合每週一張新 Issue"
    )


def test_nothing_in_any_workflow_auto_merges_or_pushes_main() -> None:
    """`main` 直接部署到公開 Pages,而那是一個會給出投資部位建議的工具。

    「所有必要檢查通過」是合併的必要條件,不是授權。這條邊界寫在 CLAUDE.md,
    這裡確保沒有任何 workflow 偷偷繞過它。
    """
    files = sorted(WORKFLOWS.glob("*.yml"))
    assert len(files) >= 4, (
        f"只找到 {len(files)} 個 workflow —— 這條測試會變成恆真"
    )
    for wf in files:
        text = _code(wf)
        assert "gh pr merge" not in text, f"{wf.name} 會自動合併 PR"
        assert "--auto" not in text, f"{wf.name} 開了 auto-merge"
        assert "--admin" not in text, f"{wf.name} 用 admin 繞過分支保護"
        assert not re.search(r"git push\s+(-\S+\s+)*origin\s+main\b", text), \
            f"{wf.name} 直接推 main"
        assert not re.search(r"git push\s+(-\S+\s+)*origin\s+HEAD:main\b", text), \
            f"{wf.name} 直接推 main"


# ══════════════════════════════════════════════════════════════════════════
# 5. L2 與 canary 必須跑同一組檢查
# ══════════════════════════════════════════════════════════════════════════

def test_both_pipelines_use_the_same_test_entrypoint() -> None:
    """兩邊各寫一套 pytest 指令,日後一定會漂 ——

    而漂掉的症狀是「canary 綠了,但它跑的不是 L2 在跑的那組檢查」,
    也就是一個看起來在把關、實際上在量別的東西的閘門。
    """
    assert ENTRYPOINT.exists(), "共用入口不見了"
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert "python -m pytest tests/" in body
    assert 'cd "$(dirname "$0")/.."' in body, "入口沒有自己切到 ops/,呼叫端的 cwd 會影響結果"
    rel = "ops/scripts/ci_checks.sh"
    for wf in ("ci.yml", "dependency-canary.yml"):
        assert rel in _code(WORKFLOWS / wf), f"{wf} 沒有用共用入口"


def test_the_policy_document_exists_and_marks_its_evidence() -> None:
    """決策必須被記錄,而且每一條要標明證據等級。

    2026-10-19 的交接文件把一條**沒驗過**的機制用跟實測相同的語氣寫進前提,
    而它是反的。量過的與推論的混在同一種字體裡,讀者沒有辦法分辨。
    """
    text = POLICY_DOC.read_text(encoding="utf-8")
    for tag in ("實測", "靜態檢查", "推論"):
        assert tag in text, f"DEPENDENCIES.md 沒有用到證據等級「{tag}」"
    for decision in ("D1", "D2", "D3", "D4"):
        assert decision in text, f"DEPENDENCIES.md 少了 {decision}"
    assert "已知的限制" in text, "沒有列出沒被解決的事 —— 那是這份文件最該有的一節"


def test_the_canary_does_not_fake_a_test_exit_code(canary: dict) -> None:
    """測試那一步被跳過時,不得硬塞一個 exit code 進 manifest。

    第一版用 `${{ steps.tests.outputs.code || '99' }}`,於是「沒跑」會被記成
    「失敗,exit 99」—— 而 99 看起來像一個真的測試框架回的碼。
    """
    text = _code(WORKFLOWS / "dependency-canary.yml")
    assert "|| '99'" not in text, "又把空字串變成假的 exit code 了"
    assert "steps.tests.outcome\" != \"skipped\"" in text or \
           "steps.tests.outcome }}\" != \"skipped\"" in text, (
        "沒有判斷測試那一步到底有沒有執行"
    )
