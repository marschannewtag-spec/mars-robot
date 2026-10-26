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
    # ⚠ utf-8-sig:BOM 是給 pip 看的(見 test_pip_can_read_every_declaration_file),
    # 用 utf-8 讀會讓第一行剩下一個 ﻿,變成一個名字叫 BOM 的假套件。
    for line in path.read_text(encoding="utf-8-sig").splitlines():
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


def test_autofix_opens_only_for_dependency_failures() -> None:
    """2026-10-20 **反轉**:只對相依解析/鎖定檔錯誤開門。

    舊版剛好相反 —— 排除 90(依賴安裝失敗)、接受其他所有異常。
    反轉的理由有兩個方向:

    1. **舊版讓 agent 在資料源異常時被叫起來**,而提示裡又寫著「資料源故障
       不要動程式碼」。每次上游抽風都燒一次額度去得到「不用改」。
    2. 舊版排除 90 的兩個技術理由(自己也裝不起來、health_report.md 不存在)
       **都是真的,但都可以解掉** —— 下面兩條測試就是在釘那兩個解法。
       它們不是裝飾:任何一條被改回去,這條路就又走不通了。

    開門清單本身(哪些錯誤分類算數)在 `src/autofix_gate.py`,有自己的測試。
    這裡只釘「workflow 真的去問它了」。
    """
    data = yaml.safe_load((WORKFLOWS / "daily-health.yml").read_text(encoding="utf-8"))
    cond = data["jobs"]["autofix"]["if"]
    assert "needs.health.outputs.autofix" in cond, (
        f"autofix 的條件沒有去問開門決策:{cond}")
    assert "code != '0'" not in cond, (
        f"還留著「所有非零健康碼都叫 Claude」的舊條件:{cond}")


def test_autofix_survives_its_own_install_failing() -> None:
    """解法一:安裝步驟必須 continue-on-error。

    這個 job 現在只在「相依裝不起來」時被叫起來,所以它自己多半也裝不起來。
    少了這個旗標,job 會在 agent 開口之前就死掉 —— 而那正是舊版排除 90 的
    理由。**拿掉它就等於把這整條路再關一次,而且不會有任何測試以外的症狀。**
    """
    autofix = _job("daily-health.yml", "autofix")
    step = _step(autofix, "pip install -r ops/requirements.txt")
    assert step.get("continue-on-error") is True, (
        "安裝步驟不是 continue-on-error —— 裝不起來的那天 job 會在 agent "
        "開口前就結束,而那天正是唯一會叫它的日子")


def test_autofix_reads_an_artifact_that_exists_on_the_failure_path() -> None:
    """解法二:輸入必須是 always() 上傳的那一份。

    `health-report` 的上傳條件是 `steps.install.outcome == 'success'` ——
    安裝失敗那天它**不存在**,而那是唯一會叫 autofix 的日子。
    `dependency-manifest` 才是 always() 上傳的。
    """
    autofix = _job("daily-health.yml", "autofix")
    names = [str((s.get("with") or {}).get("name", ""))
             for s in autofix["steps"]
             if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert "dependency-manifest" in names, (
        f"autofix 下載的 artifact 是 {names} —— health-report 在安裝失敗那天"
        "不存在,下載會失敗或拿到空的")

    health = _job("daily-health.yml", "health")
    up = [s for s in health["steps"]
          if str(s.get("uses", "")).startswith("actions/upload-artifact")
          and (s.get("with") or {}).get("name") == "dependency-manifest"]
    assert len(up) == 1 and up[0].get("if") == "always()", (
        "dependency-manifest 的上傳條件不是 always() —— 那它在失敗那天也不存在")


def test_autofix_always_reports_what_happened() -> None:
    """**「沒有 token 而安靜跳過」和「修好了」不可以長得一樣。**

    兩者在舊版都是「沒有紅燈」。這是 SKIP 與 PASS 的 exit code 相同
    那個老問題,換到自動化這一層再出現一次。

    三件事一起才成立,所以一起釘:
      1. 回報步驟 `if: always()`
      2. checkout 與 setup-python **無條件** —— 否則沒有 token 時那個
         步驟自己也跑不起來,而那正是最需要它的場合
      3. 回報走 `autofix_gate.py report`(去重規則在那裡,有測試)
    """
    autofix = _job("daily-health.yml", "autofix")

    report = _step(autofix, "autofix_gate.py report")
    assert report.get("if") == "always()", (
        f"回報步驟的條件是 {report.get('if')!r},不是 always()")

    for prefix in ("actions/checkout", "actions/setup-python"):
        hits = [s for s in autofix["steps"]
                if str(s.get("uses", "")).startswith(prefix)]
        assert hits, f"autofix 沒有 {prefix}"
        assert all(s.get("if") is None for s in hits), (
            f"{prefix} 有條件 —— 沒有 token 時它不會跑,於是回報步驟也跑不了"
            "(它需要 repo 裡的 autofix_gate.py 和 python),"
            "「該跑卻沒跑」就會變成唯一報不出來的狀態")


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
    text = LOCK_FILE.read_text(encoding="utf-8-sig")
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

    text = LOCK_FILE.read_text(encoding="utf-8-sig")
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
    header = LOCK_FILE.read_text(encoding="utf-8-sig").splitlines()[1]
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


# ══════════════════════════════════════════════════════════════════════════
# 6. pip 真的讀得懂這些檔案嗎(2026-10-19,第四次同一個形狀)
#
# `pip install -r <檔案>` 在沒有 BOM 時會退回用
# `locale.getpreferredencoding(False)` 解碼。中文版 Windows 是 cp950,
# 於是帶中文註解的 requirements 檔會讓 pip **直接 exit 2**。
# 實測(開發機 cp950):原始檔 exit 2、加 BOM exit 0、純 ASCII exit 0。
#
# CI 跑在 UTF-8 的 Linux 上,所以這個 bug 在 CI 永遠不會出現 ——
# 和 cp950 終端機、pytest-cov、pytest major 是同一個形狀的第四次:
# **只在 CI 驗過,等於只在一種環境驗過。**
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    "requirements.txt", "requirements-test.txt", "requirements-ci.lock",
])
def test_pip_can_read_every_declaration_file(name: str) -> None:
    """判準:**要嘛有 UTF-8 BOM,要嘛整份是純 ASCII。**

    那正好是 pip 在任何 locale 下都讀得懂的集合。

    ⚠ 刻意不寫成「在這台機器上 pip 跑得起來」—— CI 的 locale 是 UTF-8,
    那樣寫在 CI 上永遠綠,而這個 bug 只在非 UTF-8 的 locale 出現。
    判準要測在事實產生的地方:**檔案的位元**,不是今天這台機器的 locale。
    """
    import codecs

    raw = (OPS_ROOT / name).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return  # pip 看到 BOM 就會用 UTF-8 解碼,任何 locale 都安全
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise AssertionError(
            f"{name} 有非 ASCII 內容卻沒有 UTF-8 BOM —— 在中文版 Windows 上"
            f"`pip install -r` 會 exit 2(UnicodeDecodeError: cp950)。"
            f"修法:在檔案開頭加 UTF-8 BOM,或把註解改成純 ASCII。"
            f"\n第一個非 ASCII 位元組在 offset {exc.start}"
        ) from None


def test_the_bom_does_not_leak_into_the_first_requirement() -> None:
    """對照組:BOM 不得被當成需求行的一部分。

    加 BOM 的代價是第一行會多一個 `\ufeff`。如果那一行剛好是需求而不是註解,
    套件名就會變成 `\ufeffpandas` —— 一個 pip 裝得起來(它會剝掉 BOM)、
    但我們自己的解析器看不懂的名字。
    """
    for name in ("requirements.txt", "requirements-test.txt"):
        for pkg in _requirements(OPS_ROOT / name):
            assert pkg.isascii() and pkg == pkg.strip(), f"{name} 解析出髒名字:{pkg!r}"


def test_the_canary_tests_the_lock_it_would_propose(canary: dict) -> None:
    """canary 必須**先產生候選鎖定檔、裝它、再測它**,順序不可以反。

    反過來(用範圍裝 → 測 → 事後重新產生鎖定檔)會留下一個洞:
    pip 與 uv 是兩個解析器,**被測的組合和被釘住的組合可能不是同一個**,
    而 PR 說明會寫著「全綠組合」。2026-10-19 實測兩者在同平台解析結果相同
    (20 個套件、零版本差異),所以那不是正在發作的故障 —— 但它是在賭兩個
    第三方工具永遠一致,而這個 repo 的歷史就是一連串「賭別人的預設值」。
    """
    steps = canary["steps"]

    def idx(needle: str) -> int:
        hits = [i for i, s in enumerate(steps) if needle in (s.get("run") or "")]
        assert len(hits) == 1, f"跑 `{needle}` 的步驟有 {len(hits)} 個,預期 1 個"
        return hits[0]

    compile_at = idx("uv pip compile")
    # 需求寫完整:PR 說明的文案裡也提到了 `pip install --require-hashes`,
    # 只比對那個片段會找到兩個步驟。斷言要指向**真的那一行指令**。
    install_at = idx("pip install --require-hashes -r ops/requirements-ci.lock")
    tests_at = idx("ci_checks.sh")
    assert compile_at < install_at < tests_at, (
        f"順序不對:compile={compile_at} install={install_at} tests={tests_at} —— "
        f"必須是「產生候選 → 裝候選 → 測候選」"
    )
    # 裝的必須是鎖定檔,不是範圍
    install_run = steps[install_at]["run"]
    assert "requirements-ci.lock" in install_run
    assert "pip install -r ops/requirements.txt" not in install_run, (
        "canary 又改回用範圍安裝了 —— 那會讓「測到的」與「釘住的」分家"
    )
    # 開 PR 的判準必須是「工作目錄裡那一份」的 diff,不是再產生一次
    relock = steps[idx("git diff --quiet -- requirements-ci.lock")]
    assert "uv pip compile" not in relock["run"], (
        "在判斷要不要開 PR 的時候又重新產生了一次 —— 那一份沒有被測過"
    )


def test_the_ci_trigger_covers_every_file_the_tests_read() -> None:
    """測試會讀的每一個 repo 檔案,都必須能觸發 L2。

    `實測`(2026-10-26,改之前)L2 的 `paths` 是
    `index.html` / `ops/**` / `.github/workflows/ci.yml`,而測試會讀的是
    `index.html` / `ops/**` / `sw.js` / `README.md` / `.github`。

    漏掉的三個都有測試在看,而且漏得很安靜 —— 只改 `sw.js` 不會跑
    `test_sw.py`,只改 `daily-health.yml` 不會跑那三組 workflow 契約測試。
    `ops/CLAUDE.md` 甚至明文把「單獨 +1 sw.js」寫成一個安全的動作。

    另一個後果是 PR 會卡死:**必要檢查有 paths 過濾時,沒碰到那些路徑的 PR
    永遠拿不到那個檢查**,於是 mergeStateStatus 永遠 BLOCKED(PR #14 實測)。

    判準不是「有沒有 paths」,是**測試讀得到的檔案,觸發條件涵不涵蓋它** ——
    所以把 paths 加回來也可以,只要它是完整的。
    """
    import re as _re

    root = Path(__file__).resolve().parents[2]
    wanted = set()
    for f in sorted((root / "ops/tests").glob("test_*.py")):
        src = f.read_text(encoding="utf-8")
        wanted |= set(_re.findall(r'REPO_ROOT\s*/\s*"([^"]+)"', src))
    wanted = {w for w in wanted if not w.startswith("ops/")}   # ops/** 必定涵蓋
    assert len(wanted) >= 3, f"只掃到 {wanted} —— 掃描可能壞了"

    doc = yaml.safe_load((root / ".github/workflows/ci.yml")
                         .read_text(encoding="utf-8"))
    # ⚠ PyYAML 走 YAML 1.1,`on:` 會被解析成布林 **True**,不是字串 "on"。
    #   直接 `doc["on"]` 會 KeyError —— 而那是 GitHub workflow 的經典陷阱
    #   (寫這條測試時當場踩到)。兩個鍵都查。
    on = doc.get("on", doc.get(True))
    assert on is not None, "ci.yml 沒有觸發條件 —— 前提變了,請重寫這條"
    for event in ("push", "pull_request"):
        cfg = on.get(event) or {}
        paths = cfg.get("paths") if isinstance(cfg, dict) else None
        if paths is None:
            continue                       # 沒有過濾 = 全部都會觸發
        missing = []
        for w in sorted(wanted):
            ok = any(p == w or (p.endswith("/**") and
                                (w == p[:-3] or w.startswith(p[:-2])))
                     for p in paths)
            if not ok:
                missing.append(w)
        assert not missing, (
            f"`on.{event}.paths` 沒有涵蓋 {missing} —— 但有測試在讀它們。\n"
            f"目前的 paths:{paths}\n"
            "只改那些檔案時 L2 不會跑,而那不會有任何症狀:沒有紅燈、"
            "沒有訊息,看起來就像通過了。\n"
            "順帶:必要檢查有 paths 過濾時,沒碰到那些路徑的 PR 永遠拿不到"
            "那個檢查,會一直停在 BLOCKED。")
