"""依賴安裝的稽核軌跡 —— `src/dep_manifest.py`(2026-10-19 新增)。

為什麼這個模組需要自己的測試
----------------------------
它的工作是在**最糟的那一刻**還能動:`pip install` 剛剛失敗,所以 pandas、
pytest、yaml 可能都不在。那一刻如果它也壞掉,結果就是
「workflow 紅了但 Issue 安靜、repo 沒有活動」—— 而那會讓 GitHub 的
60 天排程停用計時器開始跑。也就是:**故障期間反而失去監控**。

所以這裡釘的不是「它有跑」,而是四件會讓它變成裝飾的事:

  1. 失敗時的套件清單**不得冒稱完整**(事後歸因會指向錯的版本)
  2. 去重 key 不得含 commit / run ID(否則同一個壞掉的組合每次都算新發現,
     那就是這個 repo 實測過的 52 則留言)
  3. 寫檔失敗**不得**動到既有的有效檔案
  4. log 尾段裡的祕密必須被遮掉(它會被貼進公開 Issue)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import dep_manifest as dm

OPS_ROOT = Path(__file__).resolve().parents[1]
RUNTIME = OPS_ROOT / "requirements.txt"
TESTREQ = OPS_ROOT / "requirements-test.txt"


# --------------------------------------------------------------------------
# 1. 這個模組必須只靠標準庫
# --------------------------------------------------------------------------

def test_the_module_imports_nothing_that_needs_installing() -> None:
    """它要在「安裝失敗之後」執行,所以不能 import 任何需要被安裝的東西。

    用 AST 掃原始碼,不是去 try/except import —— 後者在開發機上永遠成功
    (開發機什麼都裝了),那就是「用錯的環境在量」。
    """
    import ast
    import sys

    tree = ast.parse((OPS_ROOT / "src" / "dep_manifest.py").read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    # console 是同目錄的本地模組,而它自己也必須是純標準庫
    outside = {m for m in mods if m not in sys.stdlib_module_names and m != "console"}
    assert not outside, (
        f"dep_manifest.py import 了需要安裝的東西:{sorted(outside)} —— "
        f"它必須在 pip install 失敗之後仍然跑得動"
    )
    console_tree = ast.parse((OPS_ROOT / "src" / "console.py").read_text(encoding="utf-8"))
    for node in ast.walk(console_tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            for n in names:
                root = n.split(".")[0]
                assert root in sys.stdlib_module_names, (
                    f"console.py import 了 {root} —— dep_manifest 靠它印訊息"
                )


# --------------------------------------------------------------------------
# 2. 成功 / 失敗兩種 manifest
# --------------------------------------------------------------------------

def test_a_successful_install_records_a_complete_set() -> None:
    m = dm.build(phase="success", requested=[RUNTIME, TESTREQ],
                 source="requirements.txt", environ={})
    assert m["resolution_complete"] is True
    assert m["error"] is None
    assert m["n_distributions"] == len(m["distributions"]) > 0
    assert m["dedupe_key"].startswith("combo:")
    assert m["kind"] == dm.KIND and m["schema"] == dm.SCHEMA
    assert set(m["inputs"]) == {RUNTIME.name, TESTREQ.name}
    for digest in m["inputs"].values():
        assert digest and digest.startswith("sha256:")


def test_a_failed_install_never_claims_a_complete_set() -> None:
    """**最重要的一條。** 失敗時讀到的是「已落地的部分」。

    把它當成解析結果,會讓事後歸因指向一組從來沒有被完整解析出來的版本 ——
    而歸因是這整層存在的唯一理由。
    """
    m = dm.build(phase="failure", requested=[RUNTIME],
                 source="requirements.txt", exit_code=1,
                 log="ERROR: ResolutionImpossible", environ={})
    assert m["resolution_complete"] is False
    assert m["dedupe_key"].startswith("incomplete:"), \
        "解析沒完成的 key 必須看得出來它不是一個真實的版本組合"
    assert m["error"]["class"] == "resolution"
    assert "distributions_note" in m, "沒有說明這份清單是不完整的"
    assert m["install_exit_code"] == 1


def test_phase_must_be_one_of_two_values() -> None:
    with pytest.raises(ValueError):
        dm.build(phase="maybe", requested=[], source="x", environ={})


@pytest.mark.parametrize("log,expect", [
    ("ERROR: ResolutionImpossible: for help visit ...", "resolution"),
    ("ERROR: No matching distribution found for numpy==99", "resolution"),
    ("THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE", "hash_mismatch"),
    ("ERROR: Failed building wheel for numpy", "build"),
    ("HTTPSConnectionPool(host='pypi.org', port=443): Read timed out", "download"),
    ("ERROR: Could not open requirements file: No such file or directory", "not_found"),
    ("something nobody has seen before", "unknown"),
])
def test_error_classes_are_a_small_normalised_set(log: str, expect: str) -> None:
    """錯誤類別會進去重 key,所以它必須是正規化的小集合。

    每一種失敗都算成不同的類別,等於每天都是新發現 —— 狼來了。
    """
    assert dm.classify_error(log) == expect


# --------------------------------------------------------------------------
# 3. 去重 key —— 這一組是「不要變成狼來了」的全部依據
# --------------------------------------------------------------------------

def test_the_same_version_combination_gives_the_same_key() -> None:
    a = dm.combination_key({"numpy": "2.5.4", "pandas": "2.3.3"}, "3.12.0", "linux", "pip")
    b = dm.combination_key({"pandas": "2.3.3", "numpy": "2.5.4"}, "3.12.0", "linux", "pip")
    assert a == b, "順序不同就算成不同組合的話,每次執行都會是新發現"


def test_a_different_version_gives_a_different_key() -> None:
    a = dm.combination_key({"numpy": "2.5.4"}, "3.12.0", "linux", "pip")
    b = dm.combination_key({"numpy": "2.6.0"}, "3.12.0", "linux", "pip")
    assert a != b, "版本變了 key 卻一樣 → 升版造成的新故障會被當成舊故障而沉默"


def test_python_and_platform_are_part_of_the_key() -> None:
    base = {"numpy": "2.5.4"}
    k = dm.combination_key(base, "3.12.0", "linux", "pip")
    assert k != dm.combination_key(base, "3.13.0", "linux", "pip")
    assert k != dm.combination_key(base, "3.12.0", "windows", "pip")
    assert k != dm.combination_key(base, "3.12.0", "linux", "canary-latest")


def test_commit_and_run_id_are_not_in_the_key() -> None:
    """⚠ 這條是整個去重機制的承重點。

    把 run ID 放進 key,每次執行都會算出新的 key,於是同一個壞掉的版本組合
    每週都被當成新發現 —— 那正是這個 repo 實測過的 52 則留言。
    manifest 本身仍然要記錄 commit / run ID(診斷要用),只是不進 key。
    """
    env_a = {"GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "111"}
    env_b = {"GITHUB_SHA": "b" * 40, "GITHUB_RUN_ID": "222"}
    dists = {"numpy": "2.5.4"}
    a = dm.build(phase="success", requested=[], source="x", environ=env_a, dists=dists)
    b = dm.build(phase="success", requested=[], source="x", environ=env_b, dists=dists)
    assert a["dedupe_key"] == b["dedupe_key"], "run ID 進了 key → 永遠都是新發現"
    assert a["run"]["commit"] != b["run"]["commit"], "manifest 仍然要記得 commit"
    assert a["run"]["run_id"] == "111" and b["run"]["run_id"] == "222"


def test_test_failures_do_not_change_the_combination_key() -> None:
    """同一個版本組合跑出不同的測試結果,仍然是同一個組合。

    否則 canary 會因為「今天多紅了一條」而把同一個組合重報一次。
    """
    dists = {"numpy": "2.5.4"}
    ok = dm.build(phase="success", requested=[], source="x", environ={},
                  dists=dists, tests_exit_code=0)
    bad = dm.build(phase="success", requested=[], source="x", environ={},
                   dists=dists, tests_exit_code=1, tests_log="1 failed")
    assert ok["dedupe_key"] == bad["dedupe_key"]
    assert ok["tests"]["ok"] is True and bad["tests"]["ok"] is False
    assert bad["tests"]["exit_code"] == 1


def test_incomplete_keys_separate_different_error_classes() -> None:
    digests = {"requirements.txt": "sha256:abc"}
    a = dm.incomplete_key(digests, "3.12.0", "linux", "resolution")
    b = dm.incomplete_key(digests, "3.12.0", "linux", "build")
    assert a != b
    assert a.startswith("incomplete:") and b.startswith("incomplete:")


# --------------------------------------------------------------------------
# 4. 祕密遮蔽 —— manifest 會被貼進【公開】repo 的 Issue
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,must_not_contain", [
    ("cloning https://user:ghp_realsecretvalue@github.com/x.git", "ghp_realsecretvalue"),
    ("token ghp_abcdefghijklmnop1234 used", "ghp_abcdefghijklmnop1234"),
    ("key sk-ant-api03-abcdefghijklmnop", "sk-ant-api03-abcdefghijklmnop"),
    ("GET /x?access_token=supersecretvalue&a=1", "supersecretvalue"),
    ("Authorization: Bearer supersecretbearer", "supersecretbearer"),
])
def test_known_secret_shapes_are_redacted(raw: str, must_not_contain: str) -> None:
    out = dm.redact(raw, environ={})
    assert must_not_contain not in out, f"沒遮掉:{out}"
    assert "***" in out


def test_the_actual_value_of_a_secret_env_var_is_redacted() -> None:
    """樣式比對擋不住「剛好不像任何已知樣式」的 secret —— 那才是 CI 裡最可能的。"""
    env = {"HEARTBEAT_PAT": "zq7_plain_looking_value"}
    out = dm.redact("remote: rejected zq7_plain_looking_value", environ=env)
    assert "zq7_plain_looking_value" not in out and "***" in out


def test_short_env_values_are_not_redacted() -> None:
    """對照組:太短的值會把正常字串打成馬賽克,反而讓 log 讀不懂。"""
    env = {"MY_TOKEN": "abc"}
    assert dm.redact("abc def", environ=env) == "abc def"


def test_the_log_tail_is_truncated_and_says_so() -> None:
    out = dm.log_tail("x" * 9000, limit=100, environ={})
    assert len(out) < 400
    assert "已截斷" in out, "截斷了卻不說,讀的人會以為那就是全部的輸出"


# --------------------------------------------------------------------------
# 5. 原子寫入 —— 不成功就不要碰目的檔
# --------------------------------------------------------------------------

def test_writing_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    out = tmp_path / "install.json"
    dm.write_atomic(out, {"a": 1})
    assert json.loads(out.read_text(encoding="utf-8")) == {"a": 1}
    assert not list(tmp_path.glob("*.tmp")), "暫存檔沒有被 rename 掉"


def test_a_failed_write_does_not_destroy_the_existing_file(tmp_path: Path) -> None:
    """⚠ 這條釘的是這個 repo 踩過的那個坑的反面。

    workflow 原本用 `git show ... > ops/data/health_log.csv`,而 shell 的
    重導向在命令執行**之前**就把目的檔截斷成 0 bytes —— 失敗只要一次,
    留下的就是一個存在但空的檔案,而下游判斷「檔案存在」就以為有內容。
    """
    out = tmp_path / "install.json"
    dm.write_atomic(out, {"good": True})
    before = out.read_text(encoding="utf-8")

    class Unserialisable:
        pass

    with pytest.raises(TypeError):
        dm.write_atomic(out, {"bad": Unserialisable()})
    assert out.read_text(encoding="utf-8") == before, "寫入失敗卻動到了既有的有效檔案"


def test_the_cli_writes_a_failure_manifest_and_issue_text(tmp_path: Path) -> None:
    """整條 CLI 真的跑一次 —— workflow 呼叫的就是這個介面。"""
    log = tmp_path / "pip.log"
    log.write_text("ERROR: Failed building wheel for numpy\n", encoding="utf-8")
    out = tmp_path / "deps" / "install.json"
    title, body = tmp_path / "t.txt", tmp_path / "b.md"
    rc = dm.main([
        "--phase", "failure", "--source", "requirements.txt",
        "--requested", str(RUNTIME), "--log", str(log), "--exit-code", "1",
        "--out", str(out), "--emit-issue",
        "--issue-title", str(title), "--issue-body", str(body),
    ])
    assert rc == 0
    m = json.loads(out.read_text(encoding="utf-8"))
    assert m["error"]["class"] == "build"
    assert dm.FAILSET_NAME in title.read_text(encoding="utf-8")
    text = body.read_text(encoding="utf-8")
    assert f"<!-- failset: {dm.FAILSET_NAME} -->" in text, (
        "少了 failset 標記,這張 Issue 不會被「恢復正常 → 關閉 Issue」關掉,"
        "會變成一張沒人收的孤兒"
    )


def test_a_canary_issue_carries_the_version_combination_key(tmp_path: Path) -> None:
    m = dm.build(phase="success", requested=[], source="latest", environ={},
                 dists={"numpy": "2.6.0"}, tests_exit_code=1, tests_log="1 failed")
    body = dm.canary_issue_body(m)
    assert f"<!-- depcombo: {m['dedupe_key']} -->" in body
    assert "預警" in body, "canary 的 Issue 必須講明它不是正式環境故障"
    assert "numpy==2.6.0" in body, "沒有把實際版本組合寫出來,讀的人無從歸因"


def test_a_canary_resolution_failure_says_the_set_is_partial() -> None:
    m = dm.build(phase="failure", requested=[RUNTIME], source="latest",
                 exit_code=1, log="ResolutionImpossible", environ={})
    body = dm.canary_issue_body(m)
    assert "已落地的部分" in body, "沒說清單不完整,等於冒稱那是解析結果"
