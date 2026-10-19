#!/usr/bin/env bash
# L2 與 dependency canary **共用的唯一測試入口**。
#
# 為什麼要有這個檔案:兩邊各寫一套 pytest 指令,日後一定會漂 ——
# 而漂掉的症狀是「canary 綠了但它跑的不是 L2 在跑的那組檢查」,
# 也就是一個看起來在把關、實際上在量別的東西的閘門。
# 這和 config 的代理清單與 index.html 漂掉是同一個形狀。
#
# 參數會原封不動傳給 pytest(L2 用 -v --tb=short,canary 用 -q)。
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pytest tests/ "$@"
