"""依賴安裝的稽核軌跡 —— 成功與失敗都要留下紀錄。

為什麼需要這一層
----------------
2026-10-19 讀 `daily-health.yml` 的失敗路徑得到的結論(靜態檢查,未實跑):

    pip install 失敗 → job 紅
                     → 「執行健檢」被跳過 → steps.check.outputs.code 是空的
                     → 開 Issue 那一步的條件不含 always() → 被跳過,不開 Issue
                     → 心跳有 always() 會跑,但沒有新資料
                        → git diff --cached --quiet → 跳過 commit → 沒有推送

也就是:**依賴裝不起來的那一天,Issue 通道完全安靜,而且 60 天排程停用的
計時器開始跑。** 剩下的通知只有 GitHub 自己的「workflow 失敗」信。
對一個「沒消息就是好消息」的系統,那是最糟的失效模式 ——
故障期間反而失去監控。

⚠ **這個模組只用標準庫。** 它要在「pandas / pytest 裝不起來」的那一刻執行,
所以不能 import 任何需要被安裝的東西。`console.force_utf8` 也是純標準庫
(已由 AST 掃描確認)。加 import 之前請先問:**這一行在安裝失敗之後還跑得動嗎?**

為什麼不寫進 ops/data/manifest.json
-----------------------------------
那一份是 PWA 在讀的資料來源軌跡(`checkOpsHeartbeat()` 取它的 `fetched_at`
最大值)。把依賴診斷塞進去,等於讓一個與使用者無關的欄位有機會改變
PWA 的心跳判斷。兩種 manifest 的消費者不同,schema 就分開。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from console import force_utf8  # noqa: E402

OPS_ROOT = Path(__file__).resolve().parents[1]
DEPS_DIR = OPS_ROOT / "data" / "deps"
INSTALL_MANIFEST = DEPS_DIR / "install.json"

SCHEMA = 1
KIND = "dependency-install"

# 安裝失敗在報告層的名字。必須是一個「檢查項名稱」的形狀,因為它會被寫進
# Issue 內文的 `<!-- failset: ... -->` 標記,而 health_check.parse_failset()
# 會在下一次執行讀回它來判斷「失敗集合有沒有變動」。
# ⚠ 名稱裡不能有 `|` 或 `,` —— 那兩個字元是標記本身的分隔符號。
FAILSET_NAME = "依賴安裝"

# 給 workflow output 用的非零健康碼。
# health_check.py 的 exit code 是「失敗項目數」(目前最多約 19 項),
# 所以 90 不會和它撞,而且一眼看得出「這不是健檢算出來的」。
INSTALL_FAILURE_CODE = 90

_MARKER_PREFIX = "<!-- failset: "
_MARKER_SUFFIX = " -->"

# 錯誤分類 —— 順序有意義,先命中的優先。
# 分類必須是**正規化的小集合**,因為它會進去重 key:
# 同一種失敗每天都報一次新 Issue 就是狼來了。
_ERROR_PATTERNS = (
    ("hash_mismatch", r"THESE PACKAGES DO NOT MATCH THE HASHES|hashes.*do not match|hash mismatch"),
    ("hash_missing", r"--require-hashes|hash is required|missing hash"),
    ("resolution", r"ResolutionImpossible|No matching distribution|could not find a version|resolution-impossible"),
    ("download", r"Connection|timed out|Temporary failure|HTTPSConnectionPool|Network is unreachable|502 Bad Gateway|503"),
    ("build", r"Failed building wheel|error: (?:command|Microsoft Visual)|metadata-generation-failed|subprocess-exited-with-error"),
    ("not_found", r"No such file or directory|is not a valid requirements file|FileNotFoundError"),
)

# 祕密的樣子。寫進 manifest 的東西會被 artifact 保存、也可能被貼進公開 Issue,
# 所以 log tail 必須先過這一層。寧可多遮一點。
_SECRET_PATTERNS = (
    # URL 裡的帳密(https://user:token@host)
    (re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
    # 常見 token 前綴
    (re.compile(r"\b(gh[pousr]_|github_pat_)[A-Za-z0-9_]{10,}"), r"\1***"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), "sk-ant-***"),
    # query string 裡的憑證
    (re.compile(r"([?&](?:token|key|api_key|apikey|access_token|password)=)[^&\s]+", re.I), r"\1***"),
    # Authorization 標頭
    (re.compile(r"(Authorization:\s*\w+\s+)\S+", re.I), r"\1***"),
)

# 這些環境變數的【值】若出現在 log 裡,一律遮掉。名稱比對不區分大小寫。
_SECRET_ENV_HINTS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "APIKEY", "PAT")


def _utc_now(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def redact(text: str, environ: dict[str, str] | None = None) -> str:
    """把 log 裡看起來像祕密的東西換成 `***`。

    兩層:樣式比對(token 前綴、URL 帳密、query string),
    以及**實際環境變數的值**比對 —— 後者才擋得住「某個 secret 剛好不符合
    任何已知樣式」的情況,而那正是 CI 裡最可能出現的那種。
    """
    env = environ if environ is not None else dict(os.environ)
    for name, value in env.items():
        if not value or len(value) < 8:
            continue  # 太短的值會把正常字串打成馬賽克
        if any(h in name.upper() for h in _SECRET_ENV_HINTS):
            text = text.replace(value, "***")
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def log_tail(text: str, limit: int = 4000, environ: dict[str, str] | None = None) -> str:
    """log 的尾巴,先截斷再遮蔽。失敗原因幾乎都在最後面。"""
    tail = text[-limit:] if len(text) > limit else text
    if len(text) > limit:
        tail = f"...(前 {len(text) - limit} 個字元已截斷)\n" + tail
    return redact(tail, environ)


def classify_error(log: str) -> str:
    """把安裝失敗歸成一個正規化的類別,給去重 key 用。"""
    for name, pattern in _ERROR_PATTERNS:
        if re.search(pattern, log, re.I):
            return name
    return "unknown"


def installed_distributions() -> dict[str, str]:
    """目前這個解釋器看得到的所有套件與版本(正規化名稱 → 版本)。

    用 `importlib.metadata` 而不是 `pip list` —— 它在標準庫裡,
    所以在「pip 自己也出問題」的情況下仍然讀得到已經落地的那些。
    """
    from importlib.metadata import distributions

    out: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"] if dist.metadata else None
        if not name:
            continue
        out[re.sub(r"[-_.]+", "-", name).lower()] = dist.version or "unknown"
    return dict(sorted(out.items()))


def file_digest(path: Path) -> str | None:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def combination_key(dists: dict[str, str], python_version: str,
                    platform_tag: str, resolver: str) -> str:
    """**版本組合**的去重 key。

    ⚠ commit SHA 與 run ID 刻意**不在**裡面。放進去的話每次執行都會算出
    新的 key,於是「同一個壞掉的版本組合」每天都會被當成新發現 ——
    那就是這個專案實測過的 52 則留言的狼來了。
    manifest 本身仍然記錄 commit 與 run ID(診斷要用),只是不進 key。
    """
    payload = "\n".join(f"{k}=={v}" for k, v in sorted(dists.items()))
    payload += f"\npython={python_version}\nplatform={platform_tag}\nresolver={resolver}"
    return "combo:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def incomplete_key(input_digests: dict[str, str | None], python_version: str,
                   platform_tag: str, error_class: str) -> str:
    """解析根本沒完成時的去重 key。

    沒有版本組合可用,所以改用「輸入檔的 hash + 環境 + 正規化的錯誤類別」。
    ⚠ 回傳值刻意用 `incomplete:` 開頭 —— 它**不是**一個真實的版本組合,
    兩者在報告上必須分得出來。
    """
    payload = "\n".join(f"{k}={v}" for k, v in sorted(input_digests.items()))
    payload += f"\npython={python_version}\nplatform={platform_tag}\nerror={error_class}"
    return "incomplete:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build(
    *,
    phase: str,
    requested: list[Path],
    source: str,
    exit_code: int = 0,
    log: str = "",
    now: datetime | None = None,
    environ: dict[str, str] | None = None,
    dists: dict[str, str] | None = None,
    tests_exit_code: int | None = None,
    tests_log: str = "",
) -> dict:
    """組出一份 manifest。`phase` 是 `success` 或 `failure`。

    ⚠ **失敗時的清單不得冒稱完整。** `resolution_complete` 永遠要說實話:
    失敗路徑讀到的是「目前已經落地的部分」,把它當成解析結果會讓事後歸因
    指向錯的版本。
    """
    if phase not in ("success", "failure"):
        raise ValueError(f"phase 只能是 success 或 failure,收到 {phase!r}")
    env = environ if environ is not None else dict(os.environ)
    installed = installed_distributions() if dists is None else dict(sorted(dists.items()))
    digests = {p.name: file_digest(p) for p in requested}
    python_version = platform.python_version()
    platform_tag = f"{platform.system()}-{platform.machine()}".lower()
    ok = phase == "success"

    manifest = {
        "schema": SCHEMA,
        "kind": KIND,
        "generated_at": _utc_now(now),
        "phase": phase,
        "resolution_complete": ok,
        "install_exit_code": int(exit_code),
        "source": source,
        "inputs": digests,
        "environment": {
            "python": python_version,
            "platform": platform_tag,
            "runner_os": env.get("RUNNER_OS") or platform.system(),
            "runner_arch": env.get("RUNNER_ARCH") or platform.machine(),
            "pip": env.get("PIP_VERSION") or "unknown",
        },
        "run": {
            "commit": env.get("GITHUB_SHA") or "local",
            "workflow": env.get("GITHUB_WORKFLOW") or "local",
            "run_id": env.get("GITHUB_RUN_ID") or "local",
            "attempt": env.get("GITHUB_RUN_ATTEMPT") or "1",
        },
        "distributions": installed,
        "n_distributions": len(installed),
    }

    resolver = env.get("DEP_RESOLVER") or source
    if ok:
        manifest["dedupe_key"] = combination_key(
            installed, python_version, platform_tag, resolver)
        manifest["error"] = None
    else:
        err = classify_error(log)
        manifest["error"] = {
            "class": err,
            "log_tail": log_tail(log, environ=env),
        }
        manifest["dedupe_key"] = incomplete_key(
            digests, python_version, platform_tag, err)
        # 失敗路徑讀到的是「已落地的部分」,不是解析結果。說清楚。
        manifest["distributions_note"] = (
            "安裝失敗時讀到的是【目前已落地的部分】,不是完整的解析結果"
        )

    # canary 用:解析成功之後測試跑成什麼樣。
    # ⚠ 測試結果**不會**改變 dedupe_key。key 的語意是「這個版本組合」,
    # 而同一個組合連續三天跑出同樣的失敗不該被當成三個新發現。
    if tests_exit_code is not None:
        manifest["tests"] = {
            "ran": True,
            "ok": tests_exit_code == 0,
            "exit_code": int(tests_exit_code),
            "log_tail": log_tail(tests_log, limit=3000, environ=env) if tests_log else "",
        }
    return manifest


def _tests_label(tests: dict) -> str:
    """測試欄位的三種狀態。`{}` = 那一步沒有執行(通常是解析失敗)。"""
    if not tests.get("ran"):
        return "**未執行**(解析失敗,所以沒得判斷)"
    return ("通過" if tests.get("ok") else "失敗") + f"(exit {tests.get('exit_code', '—')})"


def canary_issue_title(manifest: dict) -> str:
    if not manifest.get("resolution_complete"):
        err = (manifest.get("error") or {}).get("class", "unknown")
        return f"依賴 canary · 最新版解析失敗({err})"
    return "依賴 canary · 最新版組合測試失敗"


def canary_issue_body(manifest: dict) -> str:
    """canary 失敗要開的 Issue 內文。

    ⚠ 這是一張**預警**,不是故障通知。L1 的每日健檢完全不受它影響 ——
    正式環境跑的是 `requirements-ci.lock` 的凍結版本組合,而這裡測的是
    「如果現在升上去會怎樣」。把這兩件事混在一起,就會讓一個上游的小改動
    看起來像自己的系統壞了。

    去重 key 是**版本組合**(見 combination_key):同一個組合只出聲一次。
    """
    env = manifest.get("environment", {})
    tests = manifest.get("tests") or {}
    err = manifest.get("error") or {}
    key = manifest.get("dedupe_key", "?")
    lines = [
        "**依賴 canary 失敗。這是預警,不是故障。**",
        "",
        "正式環境(L2 CI)跑的是 `ops/requirements-ci.lock` 的凍結組合,"
        "不受這件事影響;每日 L1 健檢用的是 runtime 範圍,也不受影響。",
        "這張 Issue 說的是:**照現在的宣告範圍解析最新版,會得到下面這個結果。**",
        "",
        "| | |",
        "|---|---|",
        f"| 解析完成 | {manifest.get('resolution_complete')} |",
        # 三種狀態必須分得開:沒跑 / 跑了通過 / 跑了失敗。
        # 把「沒跑」寫成「失敗」,讀的人會去查一個不存在的測試失敗。
        f"| 測試 | {_tests_label(tests)} |",
        f"| Python | {env.get('python', '?')} |",
        f"| 平台 | {env.get('platform', '?')} |",
        f"| 去重 key | `{key}` |",
        "",
    ]
    if not manifest.get("resolution_complete"):
        lines += [
            f"## 解析失敗({err.get('class', 'unknown')})",
            "",
            "⚠ 下面的套件清單是**已落地的部分**,不是完整的解析結果。",
            "",
            "```",
            (err.get("log_tail") or "(沒有 log)").strip(),
            "```",
            "",
        ]
    else:
        lines += [
            "## 解析到的版本組合",
            "",
            "```",
            "\n".join(f"{k}=={v}" for k, v in (manifest.get("distributions") or {}).items()),
            "```",
            "",
        ]
    if tests.get("log_tail"):
        lines += ["## 測試輸出尾段", "", "```", tests["log_tail"].strip(), "```", ""]
    lines += [
        "## 這代表要做什麼",
        "",
        "- **不要**為了讓這張 Issue 消失而放寬黃金基準或調整容差。",
        "- 若失敗來自上游的數值行為改變,正確的反應是**維持 lock 不動**,"
        "並把差異記錄下來 —— lock 的存在就是為了這一刻。",
        "- 若失敗來自我們自己的程式碼跟不上新版 API,那是一個真的待辦。",
        "",
        f"<!-- depcombo: {key} -->",
    ]
    return "\n".join(lines)


def write_atomic(path: Path, manifest: dict) -> Path:
    """先寫暫存檔,成功才 rename —— **不成功就不要碰目的檔。**

    這條紀律是踩過才寫下來的:workflow 原本用
    `git show ... > ops/data/health_log.csv`,而 shell 的重導向是在命令執行
    **之前**就把目的檔截斷成 0 bytes,於是那一行只要失敗,留下的就是一個
    存在但空的檔案 —— 而下游判斷「檔案存在」就以為有內容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)  # 同一個目錄內,POSIX 與 Windows 都是原子的
    return path


def issue_body(manifest: dict) -> str:
    """安裝失敗要開的 Issue 內文。

    刻意帶上 `<!-- failset: 依賴安裝 -->` 標記 —— 既有的去重與「恢復後自動
    關閉」是靠那個標記運作的,少了它,這張 Issue 會變成一張沒人會關的孤兒。
    """
    err = manifest.get("error") or {}
    env = manifest.get("environment", {})
    run = manifest.get("run", {})
    lines = [
        "依賴安裝失敗,**今天的健檢完全沒有執行**。",
        "",
        "也就是說:這份報告沒有說「資料源壞了」,它說的是"
        "**我們今天沒有能力去看資料源**。兩件事不一樣。",
        "",
        "| | |",
        "|---|---|",
        f"| 錯誤類別 | `{err.get('class', 'unknown')}` |",
        f"| 安裝來源 | `{manifest.get('source', '?')}` |",
        f"| 安裝 exit code | `{manifest.get('install_exit_code', '?')}` |",
        f"| Python | {env.get('python', '?')} |",
        f"| Runner | {env.get('runner_os', '?')} / {env.get('runner_arch', '?')} |",
        f"| commit | `{str(run.get('commit', '?'))[:12]}` |",
        f"| run | `{run.get('run_id', '?')}` (attempt {run.get('attempt', '?')}) |",
        f"| 去重 key | `{manifest.get('dedupe_key', '?')}` |",
        "",
        "完整 manifest 在這次 run 的 artifact(`dependency-manifest`)裡,"
        "並會隨心跳推到 `ops-data` 分支的 `ops/data/deps/install.json`。",
        "",
        "## 輸入檔",
        "",
    ]
    for name, digest in (manifest.get("inputs") or {}).items():
        lines.append(f"- `{name}` — `{digest or '讀不到'}`")
    lines += [
        "",
        "## 錯誤尾段(已遮蔽祕密)",
        "",
        "```",
        (err.get("log_tail") or "(沒有 log)").strip(),
        "```",
        "",
        "## 要注意的事",
        "",
        "- **這一天沒有新的資料鏡像。** PWA 會沿用 `ops-data` 上的舊版,"
        "而「舊版有多舊」由健檢的 `PWA 資料鏡像` 兩項負責 —— 但那兩項今天也沒跑。",
        "- 安裝失敗若連續多天,排程被 GitHub 自動停用的 60 天計時器會開始跑"
        "(心跳在沒有新資料時不會 commit)。",
        "",
        _MARKER_PREFIX + FAILSET_NAME + _MARKER_SUFFIX,
    ]
    return "\n".join(lines)


def issue_title(manifest: dict) -> str:
    err = (manifest.get("error") or {}).get("class", "unknown")
    return f"健檢異常(1 項)· {FAILSET_NAME}({err})"


def main(argv: list[str] | None = None) -> int:
    force_utf8()  # Windows 的 cp950 印不出中文訊息,不設會直接 UnicodeEncodeError
    ap = argparse.ArgumentParser(description="寫依賴安裝的 manifest(成功或失敗都寫)")
    ap.add_argument("--phase", choices=("success", "failure"), required=True)
    ap.add_argument("--source", required=True,
                    help="安裝來源,例如 requirements.txt 或 requirements-ci.lock")
    ap.add_argument("--requested", action="append", default=[],
                    help="宣告檔路徑,可重複")
    ap.add_argument("--log", help="安裝輸出的檔案路徑(失敗時用)")
    ap.add_argument("--exit-code", type=int, default=0)
    ap.add_argument("--tests-log", help="測試輸出的檔案路徑(canary 用)")
    ap.add_argument("--tests-exit-code", type=int, default=None)
    ap.add_argument("--out", help=f"輸出路徑(預設 {INSTALL_MANIFEST})")
    ap.add_argument("--emit-issue", action="store_true",
                    help="同時把 Issue 標題/內文寫到 --issue-title / --issue-body")
    ap.add_argument("--issue-kind", choices=("health", "canary"), default="health",
                    help="health=L1 安裝失敗(帶 failset 標記);canary=最新版預警")
    ap.add_argument("--issue-title", default="dep_issue_title.txt")
    ap.add_argument("--issue-body", default="dep_issue_body.md")
    args = ap.parse_args(argv)

    def _read(path: str | None, what: str) -> str:
        if not path:
            return ""
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"(讀不到{what}:{exc})"

    log = _read(args.log, "安裝 log")

    try:
        manifest = build(
            phase=args.phase,
            requested=[Path(p) for p in args.requested],
            source=args.source,
            exit_code=args.exit_code,
            log=log,
            tests_exit_code=args.tests_exit_code,
            tests_log=_read(args.tests_log, "測試 log"),
        )
        out = Path(args.out) if args.out else INSTALL_MANIFEST
        write_atomic(out, manifest)
    except Exception as exc:  # noqa: BLE001
        # ⚠ 寫不出來時【不要】假裝成功。既有的有效檔案也沒有被動到
        # (write_atomic 只在 rename 成功時才取代目的檔)。
        print(f"manifest 寫入失敗:{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"manifest → {out}({manifest['n_distributions']} 個套件,"
          f"resolution_complete={manifest['resolution_complete']},"
          f"key={manifest['dedupe_key']})")

    if args.emit_issue:
        if args.issue_kind == "canary":
            title, body = canary_issue_title(manifest), canary_issue_body(manifest)
        elif args.phase == "failure":
            title, body = issue_title(manifest), issue_body(manifest)
        else:
            # 成功又不是 canary → 沒有 Issue 要開。這不是錯誤。
            return 0
        Path(args.issue_title).write_text(title + "\n", encoding="utf-8", newline="\n")
        Path(args.issue_body).write_text(body, encoding="utf-8", newline="\n")
        print(f"Issue 文案 → {args.issue_title} / {args.issue_body}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
