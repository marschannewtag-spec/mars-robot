"""報告層的回歸測試 —— 釘住 2026-08-09 debug 找到的兩個 bug。

兩個都有同一個特徵:**Linux 上的 CI 永遠不會發現,只有使用者會踩到。**
一個是平台差異(Windows 代碼頁),一個是只在手動觸發後才浮現(每天多列)。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
SRC = OPS_ROOT / "src"

# 這些字元中文代碼頁 cp950 一個都印不出來,而報告與健檢輸出到處都是
GLYPHS = "✅❌⚠⏭✓✗∈"


def _cp950_hostile(ch: str) -> bool:
    try:
        ch.encode("cp950")
    except UnicodeEncodeError:
        return True
    return False


# --------------------------------------------------------------------------
# Bug 1 — Windows 的 stdout 預設是 ANSI 代碼頁,印 ✅ 直接 UnicodeEncodeError
#
# 踩到的路徑:健檢偵測到異常 → 自動開 Issue → Issue 內文叫使用者跑
# `python ops/src/health_check.py --verbose` → 在他的機器上當場崩潰。
# --------------------------------------------------------------------------


def _run_with_cp950_console(code: str) -> subprocess.CompletedProcess:
    """在子行程裡把主控台編碼強制成 cp950 —— 重現使用者的 Windows 環境。

    cp950 的 codec 是 CPython 內建的,Linux runner 上也跑得起來,
    所以這條測試在 CI 上一樣有效,不是只有在 Windows 才會動。
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONIOENCODING": "cp950"},
        capture_output=True,
    )


def test_cp950_console_really_does_break_without_the_fix() -> None:
    """先證明這個測試環境真的重現得出那個 bug。

    少了這條,下面那條就可能只是「在一個根本不會壞的環境裡通過」——
    綠燈卻什麼都沒保證。
    """
    r = _run_with_cp950_console(f"print({GLYPHS!r})")
    assert r.returncode != 0, "預期會崩潰卻沒有 —— 這個環境重現不出該 bug,下面的測試沒有意義"
    assert b"UnicodeEncodeError" in r.stderr


def test_force_utf8_survives_a_cp950_console() -> None:
    """套上修正之後,同樣的環境要印得出來。"""
    code = (
        f"import sys; sys.path.insert(0, {str(SRC)!r});"
        f"from console import force_utf8; force_utf8();"
        f"print({GLYPHS!r})"
    )
    r = _run_with_cp950_console(code)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert GLYPHS.encode("utf-8") in r.stdout


def test_every_entrypoint_that_prints_non_ascii_calls_force_utf8() -> None:
    """任何會印非 ASCII 的 `__main__` 腳本都必須呼叫 force_utf8()。

    刻意寫成掃描而非列舉:以後新增的腳本會自動被納入。
    列舉版本只會保護今天已知的那幾個,而這個 bug 的教訓正是
    「沒人會想到去檢查新腳本在別的作業系統上印不印得出來」。

    ⚠ 2026-08-12 放寬判準:原本只檢查「cp950 印不出的符號」,於是
    `fetch_data.py`(印「列」)與 `compute_golden.py` 兩個入口通過了測試,
    卻在 Windows 上把中文寫成 cp950 位元組 —— 管線接到 UTF-8 消費端就是亂碼。
    不會崩潰,只會安靜地產生垃圾,比崩潰更難發現。
    判準改成「有非 ASCII 就要 force_utf8()」,整類問題一次消掉。
    """
    offenders = []
    for path in sorted(list(SRC.glob("*.py")) + list((OPS_ROOT / "scripts").glob("*.py"))):
        text = path.read_text(encoding="utf-8")
        if "__main__" not in text:
            continue
        if any(ord(ch) > 127 for ch in text) and "force_utf8()" not in text:
            offenders.append(path.relative_to(OPS_ROOT).as_posix())
    assert not offenders, (
        f"這些入口會印非 ASCII,但沒呼叫 force_utf8():{offenders}\n"
        f"在 main() 開頭加一行 force_utf8()(見 src/console.py 的說明)"
    )


def test_the_scan_would_actually_catch_a_new_offender(tmp_path: Path) -> None:
    """上面那條是掃描式的 —— 先證明它抓得到東西,否則它可能只是恆真。"""
    bad = tmp_path / "newscript.py"
    bad.write_text('print("中文")\nif __name__ == "__main__":\n    pass\n', encoding="utf-8")
    text = bad.read_text(encoding="utf-8")
    assert "__main__" in text
    assert any(ord(ch) > 127 for ch in text)
    assert "force_utf8()" not in text  # -> 會被判為違規


# --------------------------------------------------------------------------
# Bug 2 — health_log.csv 是每次執行一列,不是每天一列
#
# 手動觸發 / drill / 重跑都會讓同一天出現多列,年度報告卻把列數當成天數。
# 實測:兩天的紀錄被印成「10 天」。
# --------------------------------------------------------------------------


@pytest.fixture()
def log_with_repeated_days(tmp_path: Path) -> Path:
    """08-07 跑了四次(其中兩次紅),08-08 跑了兩次(都綠)。"""
    p = tmp_path / "health_log.csv"
    p.write_text(
        "date,ok,n_checks,failed\n"
        "2026-08-07,1,15,\n"
        "2026-08-07,0,15,資料新鮮度 · shiller\n"
        "2026-08-07,0,15,資料新鮮度 · shiller\n"
        "2026-08-07,1,15,\n"
        "2026-08-08,1,15,\n"
        "2026-08-08,1,15,\n",
        encoding="utf-8",
    )
    return p


def test_day_count_is_days_not_runs(log_with_repeated_days: Path, monkeypatch) -> None:
    import annual_review

    monkeypatch.setattr(annual_review, "HEALTH_LOG", log_with_repeated_days)
    body = annual_review.section_data_health().body

    assert "2 天 · 共 6 次執行" in body, f"天數被執行次數灌水了:\n{body}"


def test_a_day_with_any_failure_is_not_green(log_with_repeated_days: Path, monkeypatch) -> None:
    """08-07 有紅過,就算後來重跑通過,那天仍然算紅 —— 資料源當天確實出過問題。"""
    import annual_review

    monkeypatch.setattr(annual_review, "HEALTH_LOG", log_with_repeated_days)
    body = annual_review.section_data_health().body

    assert "**1/2**" in body, f"全綠天數算錯:\n{body}"


def test_failed_items_are_deduped_per_day(log_with_repeated_days: Path, monkeypatch) -> None:
    """同一天失敗兩次的同一項,只能算一天。"""
    import annual_review

    monkeypatch.setattr(annual_review, "HEALTH_LOG", log_with_repeated_days)
    body = annual_review.section_data_health().body

    assert "— 1 天" in body, f"同日重複失敗被重複計數:\n{body}"
    assert "— 2 天" not in body


# --------------------------------------------------------------------------
# 參數敏感度網格:「現行」那一格必須真的是現行設定
#
# 2026-08-12 確認原始的 `lvl1.2` 定義已不可考,網格的兩個軸是本專案自訂的。
# 軸選得對不對無法驗證,但「現行那一格 == 現在跑的東西」可以 —— 而那才是
# 百分位與鄰域讀數會不會指錯對象的關鍵。
# --------------------------------------------------------------------------


def test_current_grid_cell_is_the_running_config(sp_daily, vix_daily, monkeypatch) -> None:
    import annual_review
    import engine
    import fetch_data

    frames = {"sp_daily": sp_daily, "vix": vix_daily}
    monkeypatch.setattr(fetch_data, "load", lambda name, **kw: frames[name].copy())

    grid_cell = annual_review.build_signals(
        annual_review.CURRENT_MA, annual_review.CURRENT_MULT
    )
    truth = engine.signal_frame(
        engine.sp_monthly(sp_daily.copy()), engine.build_vix_monthly(vix_daily.copy())
    )

    merged = grid_cell.merge(truth[["ym", "cash"]], on="ym", suffixes=("_grid", "_engine"))
    assert len(merged) > 400, "重疊月份太少,這條測試沒有涵蓋到東西"
    diff = merged[merged["cash_grid"] != merged["cash_engine"]]
    assert diff.empty, (
        f"網格的「現行」那一格與 engine 實際跑的結果不同,{len(diff)} 個月不一致 —— "
        f"年度報告的百分位/鄰域讀數會在描述一個你沒在用的參數:\n{diff.head()}"
    )


def test_current_params_are_inside_the_grid() -> None:
    """現行參數若不在網格上,`cur` 會是空的,Calmar 變 NaN,整節靜靜失去意義。"""
    import annual_review

    assert annual_review.CURRENT_MA in annual_review.MA_GRID
    assert annual_review.CURRENT_MULT in annual_review.MULT_GRID


def test_sensitivity_section_states_the_axes_are_unverified() -> None:
    """那段誠實標示不得被「順手」刪掉 —— 它是這一節唯一的邊界說明。"""
    src = (SRC / "annual_review.py").read_text(encoding="utf-8")
    assert "不是原始參數化方式的重現" in src
    assert "原始定義已不可考" in src
    assert "沒有被掃到" in src


# --------------------------------------------------------------------------
# 不准再出現「鋪好了但沒接上出海口」的資料管線
#
# FRED 就是這樣存在了很久:來源定義在、workflow 傳了金鑰、文件宣稱有信用利差,
# 但 refresh() 預設排除它、沒有任何 load() 呼叫、報告也沒有對應章節。
# 比沒有更糟 —— 它讓人以為信用面已經被涵蓋了。2026-08-12 移除。
# --------------------------------------------------------------------------


def test_every_declared_source_is_actually_consumed() -> None:
    import fetch_data

    code = "\n".join(p.read_text(encoding="utf-8") for p in SRC.glob("*.py")
                     if p.name != "fetch_data.py")
    orphans = [name for name in fetch_data.SOURCES
               if f'load("{name}"' not in code and f"load('{name}'" not in code]
    assert not orphans, (
        f"這些來源有定義卻沒有任何人 load():{orphans}\n"
        f"要嘛接上消費端,要嘛刪掉。留著的死管線會讓文件宣稱它有在做事。"
    )


def test_refresh_default_covers_every_source() -> None:
    """預設不抓的來源 = 每日流程根本不會去碰的來源。FRED 當年就死在這一行。"""
    import fetch_data

    import inspect
    src = inspect.getsource(fetch_data.refresh)
    assert "needs_fred_key" not in src, "又出現了「預設排除某些來源」的過濾條件"
    assert set(fetch_data.refresh.__defaults__ or ()) <= {None}


def test_no_credentialed_source_without_a_consumer() -> None:
    """任何需要金鑰的來源都必須有人消費,否則設了 secret 也不會改變任何輸出。"""
    import fetch_data

    fields = set(fetch_data.Source.__dataclass_fields__)
    assert "needs_fred_key" not in fields and "fred_series" not in fields, (
        "FRED 管線被加回來了。要加就要同時接上消費端與測試,"
        "否則會重演「設了金鑰卻什麼都沒變」的狀況(見 SETUP.md 第 2 項)"
    )


def test_workflows_do_not_pass_unused_secrets() -> None:
    wf_dir = OPS_ROOT.parent / ".github" / "workflows"
    offenders = [p.name for p in wf_dir.glob("*.yml")
                 if "FRED_API_KEY" in p.read_text(encoding="utf-8")]
    assert not offenders, f"這些 workflow 還在傳已移除的 FRED_API_KEY:{offenders}"


def test_empty_log_does_not_divide_by_zero(tmp_path: Path, monkeypatch) -> None:
    """只有表頭的 log 不該讓整份年度報告掛掉。"""
    import annual_review

    p = tmp_path / "health_log.csv"
    p.write_text("date,ok,n_checks,failed\n", encoding="utf-8")
    monkeypatch.setattr(annual_review, "HEALTH_LOG", p)

    body = annual_review.section_data_health().body  # 不得拋例外
    assert "沒有可判讀的紀錄" in body


def test_a_zero_byte_log_is_not_reported_as_having_no_records(
        tmp_path: Path, monkeypatch) -> None:
    """0 bytes 的軌跡檔 ≠ 只有表頭 ≠ 檔案不存在,而且都不該讓整份報告掛掉。

    0 bytes 是 workflow 的「取回健檢軌跡」失敗時留下的東西:
    `git show ... > 檔案` 的重導向在 git show 執行【之前】就把目的檔建好了。
    這種檔案 `exists()` 為真,而 `pd.read_csv` 對它拋 `EmptyDataError` ——
    **一個輔助檔足以讓整份年度報告產不出來**。

    三種狀態的訊息必須互不相同:報告的讀者要據此決定去查哪裡。
    """
    import annual_review

    missing = tmp_path / "nope.csv"
    monkeypatch.setattr(annual_review, "HEALTH_LOG", missing)
    body_missing = annual_review.section_data_health().body

    empty = tmp_path / "empty.csv"
    empty.write_bytes(b"")
    monkeypatch.setattr(annual_review, "HEALTH_LOG", empty)
    body_zero = annual_review.section_data_health().body   # 不得拋例外

    header = tmp_path / "header.csv"
    header.write_text("date,ok,n_checks,failed\n", encoding="utf-8")
    monkeypatch.setattr(annual_review, "HEALTH_LOG", header)
    body_header = annual_review.section_data_health().body

    assert "0 bytes" in body_zero, (
        f"0 bytes 那一種沒有被說出來:{body_zero!r}"
    )
    assert len({body_missing, body_zero, body_header}) == 3, (
        "「檔案不存在」/「0 bytes」/「只有表頭」有兩種以上長得一樣 —— "
        "讀者會被指去查錯的地方:\n"
        f"  不存在:{body_missing!r}\n  0 bytes:{body_zero!r}\n"
        f"  只有表頭:{body_header!r}"
    )


# --------------------------------------------------------------------------
# workflow 的 git 用法 —— 兩個已知陷阱,掃全部的檔案而不是只修觸發它的那一個
# --------------------------------------------------------------------------


def _workflow_shells() -> dict[str, str]:
    """每個 workflow 的所有 `run:` 內容合起來(YAML 註解已經不在裡面)。

    刻意讀整個檔案的 run 區塊而不是逐步驟 —— 這幾條掃描問的是
    「這個 repo 裡還有沒有這種寫法」,不是某一步的行為。
    """
    wf_dir = OPS_ROOT.parent / ".github" / "workflows"
    out: dict[str, str] = {}
    for path in sorted(wf_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        runs = []
        for job in (data.get("jobs") or {}).values():
            for step in (job.get("steps") or []):
                if step.get("run"):
                    runs.append(step["run"])
        out[path.name] = "\n".join(runs)
    return out


def _uncommented(shell: str) -> list[str]:
    return [ln for ln in shell.splitlines() if not ln.lstrip().startswith("#")]


def test_every_cross_branch_fetch_uses_an_explicit_refspec() -> None:
    """`git fetch origin <分支>` 不保證建立 `origin/<分支>`。

    `actions/checkout` 把 `remote.origin.fetch` 縮窄成只有當前分支,
    所以那個指令會 **exit 0** 卻只寫 FETCH_HEAD —— 接著任何
    `origin/<分支>` 的參照都會失敗,而 exit 0 讓 `if` 走了成功那一支。
    本機實測過(單分支複製):refspec 是
    `+refs/heads/main:refs/remotes/origin/main`,`origin/ops-data` 不存在。

    `daily-health.yml` 的心跳修過這一點,`annual-review.yml` 漏掉 ——
    所以這條掃全部,不是只修觸發它的那一個檔案。
    """
    offenders: list[str] = []
    seen = 0
    for name, shell in _workflow_shells().items():
        for line in _uncommented(shell):
            if "git fetch" not in line:
                continue
            seen += 1
            if "refs/remotes/origin/" not in line:
                offenders.append(f"{name}: {line.strip()}")
    assert seen > 0, "掃不到任何 git fetch —— 這條測試會變成恆真"
    assert not offenders, (
        "這些 `git fetch` 沒有明確 refspec,不保證建立 remote-tracking ref:\n  "
        + "\n  ".join(offenders)
        + "\n寫成 `git fetch origin +refs/heads/X:refs/remotes/origin/X`。"
    )


def test_no_workflow_uses_a_checkout_as_an_or_fallback() -> None:
    """`A && B || C` 讀起來像 if/else,但 B 失敗時 C 也會跑。

    這裡的 C 一旦是「開一條新分支」,失敗的路徑就會從目前的 HEAD
    (通常是 main)另起一條,接下來的 push 是非快轉而被拒。
    """
    offenders: list[str] = []
    for name, shell in _workflow_shells().items():
        for line in _uncommented(shell):
            if re.search(r"\|\|\s*git\s+(checkout|switch)", line):
                offenders.append(f"{name}: {line.strip()}")
    assert not offenders, (
        "這些地方把「切分支」當成 `||` 的退路:\n  " + "\n  ".join(offenders)
        + "\n`A && B || C` 不是 if/else —— B 失敗時 C 也會跑。寫成 if/else。"
    )


def test_git_show_never_redirects_straight_onto_its_destination() -> None:
    """`git show X > 檔案` 的重導向在 git show 執行【之前】就截斷目的檔。

    失敗時留下的是一個 0 bytes 的空殼,而 `exists()` 為真 ——
    下游會把它當成「有這個檔」去讀。先寫暫存檔、成功才搬過去。
    """
    offenders: list[str] = []
    seen = 0
    for name, shell in _workflow_shells().items():
        for line in _uncommented(shell):
            if "git show" not in line or ">" not in line:
                continue
            seen += 1
            target = line.split(">", 1)[1].strip().strip('"')
            # 判準是「目的地不是版控樹裡那個固定路徑」。寫死的 repo 相對路徑
            # (ops/data/...)就是那個陷阱;帶 shell 變數的一律是暫存位置,
            # 取回成功才會被 mv/cp 到定位。刻意不去列舉變數名 ——
            # 列舉的話下一個人換個變數名就繞過了。
            if "$" not in target:
                offenders.append(f"{name}: {line.strip()}")
    assert seen > 0, "掃不到任何 `git show ... >` —— 這條測試會變成恆真"
    assert not offenders, (
        "這些 `git show` 直接重導向到目的檔,失敗時會留下 0 bytes 的空殼:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# Bug 3 — 文件叫使用者跑的指令,在乾淨環境跑不起來(2026-10-14)
#
# `CLAUDE.md` 的覆蓋率指令用 `--cov`,而 `pytest-cov` 原本【不在】
# requirements.txt 裡。乾淨 clone 照文件跑那行會得到
# `error: unrecognized arguments: --cov`。
#
# 這和 Bug 1 是完全同一類,只是換了個缺口:cp950 那次缺的是 force_utf8(),
# 這次缺的是一個依賴。歸納出來的規則都一樣 ——
# **凡是文件叫使用者執行的指令,都要真的在乾淨環境跑一次再寫進文件。**
#
# 這條測試【不是】檢查「pytest-cov 在不在 requirements.txt」(那只會釘住這一個
# 實例),而是把文件裡每一條 pytest 指令真的拿去 `--collect-only` 跑一次,
# 看它會不會以 usage error 收場。任何未來新增的旗標都自動納入。
# --------------------------------------------------------------------------

PYTEST_USAGE_ERROR = 4  # pytest 的 ExitCode.USAGE_ERROR(無法辨識的參數)


def _documented_pytest_commands() -> list[tuple[str, str]]:
    """從 CLAUDE.md / SETUP.md 的 ```bash 區塊撈出所有 pytest 指令。"""
    import re

    out: list[tuple[str, str]] = []
    for doc in ("CLAUDE.md", "SETUP.md"):
        path = OPS_ROOT / doc
        if not path.exists():
            continue
        for block in re.findall(r"```bash\n(.*?)```", path.read_text(encoding="utf-8"), re.S):
            for line in block.splitlines():
                if "pytest" not in line:
                    continue
                # 去掉 `cd ops && ` 這種前綴,只留 pytest 本身那一段
                cmd = line.split("&&")[-1].strip()
                if cmd.startswith(("python -m pytest", "pytest")):
                    out.append((doc, cmd))
    return out


def test_documentation_contains_pytest_commands() -> None:
    """掃描式測試要先證明它掃得到東西,否則下一條可能只是恆真。"""
    assert _documented_pytest_commands(), (
        "在 CLAUDE.md / SETUP.md 的 bash 區塊裡找不到任何 pytest 指令 —— "
        "下一條測試會變成恆真"
    )


# 2026-10-19:宣告拆成兩份(runtime / test-only),所以凡是「掃宣告」的測試
# 都必須掃**兩份**。只掃一份的話,它會在套件搬家的那天安靜地少看一個地方 ——
# 而這條測試存在的理由正是「宣告與事實不符」。
DECLARATION_FILES = ("requirements.txt", "requirements-test.txt")


def _declared_requirement_lines() -> list[str]:
    """兩份宣告檔裡的所有需求行(去註解、去空行)。"""
    out: list[str] = []
    for name in DECLARATION_FILES:
        path = OPS_ROOT / name
        assert path.exists(), f"宣告檔不見了:{name}"
        # utf-8-sig:宣告檔開頭有 UTF-8 BOM(給 pip 的,見 dependency policy 測試)
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.split("#")[0].strip()
            if line:
                out.append(line)
    assert out, "兩份宣告檔都解析不出任何需求 —— 掃描式測試會變成恆真"
    return out


def _declared_pytest_plugins() -> list[str]:
    """宣告檔裡的 pytest 外掛,轉成模組名(pytest-cov -> pytest_cov)。"""
    import re

    out = []
    for line in _declared_requirement_lines():
        name = re.split(r"[<>=!~\[]", line)[0].strip()
        if name.startswith("pytest-"):
            out.append(name.replace("-", "_"))
    return out


@pytest.mark.parametrize("doc,cmd", _documented_pytest_commands(),
                         ids=lambda v: v if isinstance(v, str) else str(v))
def test_documented_pytest_flags_work_with_only_declared_deps(doc: str, cmd: str) -> None:
    """文件裡的每條 pytest 指令,都必須只靠 requirements.txt 宣告的東西就跑得起來。

    ⚠ 判準刻意**不是**「在我這台機器上跑得起來」。第一版就是那樣寫的,
    而它驗不出任何東西 —— 我本機早就手動裝了 pytest-cov,所以就算
    requirements.txt 漏掉它也一樣綠燈。**那是用錯的環境在量。**

    做法:`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 關掉所有自動載入的第三方外掛,
    再用 `-p` 明確只載入 requirements.txt 宣告的那幾個。於是這條測試問的是
    「乾淨 clone 照文件做會不會成功」,和本機裝了什麼無關。

    用 `--collect-only` 是因為我們只要知道參數解析過不過,不需要再跑一輪測試。
    """
    args = cmd.replace("python -m pytest", "", 1).replace("pytest", "", 1).split()
    plugin_args: list[str] = []
    for mod in _declared_pytest_plugins():
        plugin_args += ["-p", mod]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", *plugin_args, *args],
        cwd=OPS_ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    assert proc.returncode != PYTEST_USAGE_ERROR, (
        f"{doc} 裡的這條指令,在只有 requirements.txt 宣告的依賴時無法執行:\n"
        f"  {cmd}\n"
        f"pytest 回報參數錯誤:\n{(proc.stderr or proc.stdout)[-400:]}\n"
        "→ 把它需要的套件加進 ops/requirements.txt,或修掉文件裡的指令。"
    )


# --------------------------------------------------------------------------
# fetch_data.load() 的快取不得被消費端的原地改動污染(2026-10-19)
#
# 年度健檢的 144 格網格原本每格都重讀三個 CSV —— 432 次讀檔、26 秒。
# 加 lru_cache 之後快了,但引入一個比慢嚴重得多的風險:
# `total_return_perf()` 對取回來的 DataFrame 做 `sh["ym"] = ...`(**原地改**)。
# 直接回快取物件的話,那個原地改會污染快取,之後每一格拿到的都是被改過的東西,
# 結果會**依呼叫順序而改變** —— 而年度報告的數字正是靠它們算出來的。
# --------------------------------------------------------------------------


def test_load_is_cached_but_callers_cannot_poison_it(tmp_path: Path, monkeypatch) -> None:
    import fetch_data

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / fetch_data.SOURCES["shiller"].filename).write_text(
        "Date,SP500,Dividend\n1990-01-01,100,2\n1990-02-01,101,2\n",
        encoding="utf-8", newline="\n")
    monkeypatch.setattr(fetch_data, "RAW_DIR", raw)

    first = fetch_data.load("shiller", refresh_if_missing=False)
    first["ym"] = "汙染"                       # 消費端的原地改(annual_review 真的這樣做)
    first.iloc[0, first.columns.get_loc("SP500")] = -999

    second = fetch_data.load("shiller", refresh_if_missing=False)
    assert "ym" not in second.columns, (
        "消費端新增的欄位跑進了下一次 load() 的結果 —— 快取被污染,"
        "之後每一格拿到的東西會依呼叫順序而不同"
    )
    assert second["SP500"].iloc[0] == 100, "消費端改掉的值污染了快取"


def test_load_really_is_cached(tmp_path: Path, monkeypatch) -> None:
    """控制組:證明快取真的有生效,否則上面那條只是在測一個沒開的功能。"""
    import fetch_data

    raw = tmp_path / "raw"
    raw.mkdir()
    path = raw / fetch_data.SOURCES["vix"].filename
    path.write_text("DATE,CLOSE\n1990-01-02,17.2\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(fetch_data, "RAW_DIR", raw)

    calls = {"n": 0}
    real = fetch_data.pd.read_csv

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(fetch_data.pd, "read_csv", counting)
    fetch_data.load("vix", refresh_if_missing=False)
    fetch_data.load("vix", refresh_if_missing=False)
    fetch_data.load("vix", refresh_if_missing=False)
    assert calls["n"] == 1, f"同一個檔讀了 {calls['n']} 次 —— 快取沒有生效"


def test_rewriting_the_file_invalidates_the_cache(tmp_path: Path, monkeypatch) -> None:
    """`refresh()` 寫入新檔之後必須讀得到新內容。

    快取鍵含 mtime 就是為了這件事。少了它,每日健檢抓到新資料卻仍然
    讀到舊的 —— 而那會是一個完全無聲的錯誤。
    """
    import fetch_data

    raw = tmp_path / "raw"
    raw.mkdir()
    path = raw / fetch_data.SOURCES["vix"].filename
    monkeypatch.setattr(fetch_data, "RAW_DIR", raw)

    path.write_text("DATE,CLOSE\n1990-01-02,17.2\n", encoding="utf-8", newline="\n")
    assert len(fetch_data.load("vix", refresh_if_missing=False)) == 1

    os.utime(path, None)   # 確保 mtime 一定不同(同一秒內寫兩次也要分得開)
    path.write_text("DATE,CLOSE\n1990-01-02,17.2\n1990-01-03,18.0\n",
                    encoding="utf-8", newline="\n")
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 1))
    assert len(fetch_data.load("vix", refresh_if_missing=False)) == 2, (
        "檔案換了,load() 還是回舊內容 —— 抓到新資料卻用舊的,無聲錯誤"
    )


# --------------------------------------------------------------------------
# 執行環境 vs requirements.txt(2026-10-19)
# --------------------------------------------------------------------------

def test_the_running_environment_matches_requirements() -> None:
    """跑這些測試的環境,必須符合 requirements.txt 的宣告。

    ## 為什麼這條測試值得存在

    2026-10-19 實測:`requirements.txt` 寫 `pytest>=8.0,<9`,而開發機裝的是
    **9.1.1**。CI 跑 `pip install -r ops/requirements.txt`,所以拿到 8.x ——
    也就是說「我在本機報告的 252 passed」和「CI 的 252 passed」**從來不是
    同一個 major**,而整個專案期間沒有任何東西發現這件事。

    這和 `pytest-cov` 那次是完全同一個形狀:文件宣稱的環境與實際跑的環境
    不一致,而驗證本身站在錯的那一邊,於是驗證什麼都沒驗到。
    CLAUDE.md 的推論條目:**量測本身也會站錯環境**。

    ## 它擋住的不只是打錯字

    相依策略目前是「釘到下一個 major」而不是完整 lock
    (取捨見 `reports/2026-10-19-dependency-locking-for-chatgpt.md`)。
    範圍的意思是「我們會跑到還沒測過的未來版本」—— 那個取捨要成立,
    **至少**得保證此刻跑的東西在宣告範圍內。不然範圍只是註解。

    ## 刻意是硬失敗,不是警告

    一個「環境不符」的警告會被滑過去,而它造成的後果(本機綠、CI 紅,
    或更糟:本機綠、CI 綠、但兩邊測的不是同一件事)極難歸因。
    修法很短:`pip install -r ops/requirements.txt -r ops/requirements-test.txt`。
    """
    from importlib.metadata import PackageNotFoundError, version

    from packaging.requirements import Requirement
    from packaging.version import Version

    # ⚠ **兩份都要讀。** 2026-10-19 把宣告拆成 runtime / test-only 之後,
    # 這裡一度只讀 requirements.txt —— 於是 pytest、pytest-cov、packaging
    # 的版本完全沒有被檢查,而「pytest 的宣告與事實不符」正是這條測試
    # 存在的原因。拆檔案的時候最容易留下這種洞:掃描還在,但它少看了一個地方。
    specs = _declared_requirement_lines()

    problems = []
    for spec in specs:
        r = Requirement(spec)
        try:
            installed = version(r.name)
        except PackageNotFoundError:
            problems.append(f"{r.name}:宣告 {r.specifier},但【沒有安裝】")
            continue
        if not r.specifier.contains(Version(installed), prereleases=True):
            problems.append(f"{r.name}:已裝 {installed},不在宣告的 {r.specifier} 內")

    assert not problems, (
        "執行環境與宣告不符(requirements.txt + requirements-test.txt):\n  "
        + "\n  ".join(problems)
        + "\n\n本機綠而 CI 紅(或兩邊測的根本不是同一件事)就是從這裡開始的。"
        + "\n修法:pip install -r ops/requirements.txt -r ops/requirements-test.txt"
    )


# --------------------------------------------------------------------------
# 全 repo 只能有一個「今天」(2026-10-20)
#
# `date.today()` 是**執行機器的本地日期**。這個 repo 其他每一個時間戳都是
# UTC:心跳 commit、Issue 內文的 `at=` 標記、報告檔名、workflow 排程。
# 在 UTC+8 的機器上,本地午夜到早上八點之間兩者差一整天。
#
# 這個差的現形方式最難處理:**CI(UTC)永遠綠,開發機每天紅八小時**,
# 看起來像「測試不穩」—— 最容易被當成雜訊關掉的那種紅燈。
# 2026-10-20 `check_published_mirror` 就是這樣跟 `check_freshness` 分岔的,
# 在本地凌晨跑整組測試才看見。
#
# 與 `markets.ok` 門檻分岔同一個形狀:同一個判斷不能有兩個來源。
# --------------------------------------------------------------------------

def _code_only(path: Path) -> str:
    """把一份 .py 的註解與字串字面值拿掉,只留真正會執行的 token。

    ⚠ 一定要剝掉 docstring。`health_check.py` 的說明裡**正好**寫著
    `date.today()`(在講為什麼不要用它),純 grep 會把那段說明判成違規,
    於是這條測試第一次寫出來就是紅的 —— 然後最自然的反應是放寬判準,
    而放寬之後它就再也抓不到真的違規了。
    """
    import io
    import tokenize

    kept: list[str] = []
    src = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        kept.append(tok.string)
    return " ".join(kept)


_LOCAL_CLOCK = (
    ("date.today()", "本地日期 —— 請改用 health_check.today_utc()"),
    ("datetime.today()", "本地日期 —— 請改用 health_check.today_utc()"),
    ("datetime.utcnow()", "naive datetime,Python 3.12 起已棄用"),
)


def test_no_production_module_measures_time_against_the_local_clock() -> None:
    """src/ 裡不得出現本地時鐘。"""
    files = sorted(SRC.glob("*.py"))
    assert len(files) >= 5, f"掃描沒有掃到東西,只找到 {files}"

    offenders: list[str] = []
    for f in files:
        code = _code_only(f)
        for needle, why in _LOCAL_CLOCK:
            if needle.replace("(", " ( ").replace(")", " ) ") in code \
                    or needle in code.replace(" ", ""):
                offenders.append(f"{f.name}: {needle} —— {why}")

    assert not offenders, (
        "這些地方用了執行機器的本地時鐘:\n  " + "\n  ".join(offenders)
        + "\n\nCI 跑在 UTC,所以本地時鐘的分岔在 CI 上永遠看不見 ——"
        "\n它只會在 UTC+N 的開發機上每天紅 N 小時,看起來像測試不穩。"
    )


def test_the_utc_helper_is_actually_what_the_checks_use() -> None:
    """安全網:上面那條是掃描式的,必須證明它掃的東西真的存在且被用著。

    少了這條,把 `today_utc()` 整個刪掉會讓掃描測試**變綠**(沒有違規了),
    而所有日期判斷同時失效 —— 掃描式測試最典型的空轉方式。
    """
    import health_check as hc

    assert callable(getattr(hc, "today_utc", None)), "today_utc() 不見了"

    code = _code_only(SRC / "health_check.py")
    n = code.replace(" ", "").count("today_utc()")
    assert n >= 3, (
        f"health_check.py 只呼叫了 today_utc() {n} 次 —— "
        "新鮮度、鏡像、以及它自己的定義各算一次,少於 3 就代表有判斷繞過了它"
    )

    from datetime import datetime, timezone
    assert hc.today_utc() == datetime.now(timezone.utc).date()
