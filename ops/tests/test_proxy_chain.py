"""PWA 的代理清單 ↔ `config.yml` 的代理清單,必須是同一份。

## 為什麼需要這個檔

健檢探測的是 `config.yml` 的 `proxies`,PWA 實際跑的是 `index.html` 的 `PROXIES`。
兩份清單在 2026-08 到 2026-10 之間漂移了兩個月,沒有任何測試看著:

- `config.yml` 的 `accepted.known_dead_proxies` 從 2026-08 就寫著 `allorigins` 已死。
- `index.html` 完全不知道這件事,每次冷啟動照樣對它等滿 7 秒 × 2 個主機,
  把 12 秒總預算整個燒光才退回 Shiller。實測 4/4 次都是完整逾時,不是間歇。

後果是雙向的:健檢天天探測一個 PWA 沒在用的代理(假訊號),
而 PWA 在用的那個沒人幫它看著(真空窗)。

這和 FRED 死管線是同一個形狀 —— **設定宣稱有、實際沒接上出海口**。
`test_reporting.py::test_every_declared_source_is_actually_consumed` 已經為「資料源」
做了這件事,代理清單一直沒有對應的那一條。現在有了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

OPS_ROOT = Path(__file__).resolve().parents[1]
CONFIG = OPS_ROOT / "config.yml"


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pwa_proxies(index_html: Path) -> list[dict]:
    """從 index.html 解析 `const PROXIES=[...]`。

    解析的是**線上真的在跑的那個檔案**,不是另抄一份 —— 抄一份就等於多一個
    會漂移的實作,而漂移正是這個測試要防的東西。
    """
    src = index_html.read_text(encoding="utf-8")
    m = re.search(r"const PROXIES\s*=\s*\[(.*?)\n\]\s*;", src, re.S)
    assert m, "index.html 裡找不到 `const PROXIES=[...]`(被改名或改寫了?)"
    out: list[dict] = []
    for line in m.group(1).splitlines():
        name = re.search(r'name\s*:\s*"([^"]+)"', line)
        if not name:
            continue
        # wrap 函式裡所有字串字面值接起來 = 去掉 {url} 之後的樣板
        literals = re.findall(r'"([^"]*)"', line.split("wrap", 1)[-1])
        out.append({"name": name.group(1), "template": "".join(literals)})
    return out


def test_the_parse_actually_found_something(pwa_proxies: list[dict], cfg: dict) -> None:
    """掃描式測試若沒有這一層,可能只是恆真而沒人察覺。

    (`test_reporting.py` 的 `force_utf8` 掃描就踩過這個 —— 掃描本身要被驗證。)
    """
    assert pwa_proxies, "從 index.html 解析出 0 個代理 —— 底下的比對會變成恆真"
    assert cfg["proxies"], "config.yml 的 proxies 是空的"


def test_pwa_proxy_list_matches_config(pwa_proxies: list[dict], cfg: dict) -> None:
    pwa = {p["name"] for p in pwa_proxies}
    conf = {p["name"] for p in cfg["proxies"]}
    assert pwa == conf, (
        f"兩份代理清單不一致。PWA 在用 {sorted(pwa)},健檢在探測 {sorted(conf)}。\n"
        "只改一邊的後果:健檢探測一個沒人在用的代理(假訊號),"
        "而真正在用的那個沒人看著(真空窗)。兩邊都要改。"
    )


def test_proxy_templates_are_the_same_url(pwa_proxies: list[dict], cfg: dict) -> None:
    """名字一樣不代表打的是同一個網址 —— 端點換版的話健檢會探測舊的那個。"""
    conf = {p["name"]: p["template"] for p in cfg["proxies"]}
    for p in pwa_proxies:
        if p["name"] not in conf:
            continue  # 名稱不一致由上一條測試負責報,這裡不要再噴 KeyError 噪音
        expected = conf[p["name"]].replace("{url}", "")
        assert p["template"] == expected, (
            f"代理 `{p['name']}` 兩邊的網址樣板不同:\n"
            f"  index.html : {p['template']!r}\n"
            f"  config.yml : {conf[p['name']]!r}\n"
            "健檢會探測到一個 PWA 沒在打的端點 —— 綠燈證明不了 PWA 能用"
        )


def test_known_dead_list_only_names_real_proxies(cfg: dict) -> None:
    """`known_dead_proxies` 指向一個不在 `proxies` 裡的名字 = 又一條死設定。

    那個條目唯一的作用是「讓這個代理失效時不要報警」。若那個代理已經整個被移除,
    條目就永遠不會被比對到 —— 它看起來在做事,實際什麼都沒做。
    FRED 死管線就是這個形狀:設定宣稱有、實際沒接上出海口。
    """
    names = {p["name"] for p in cfg["proxies"]}
    dead = set(cfg["accepted"].get("known_dead_proxies") or [])
    orphan = dead - names
    assert not orphan, (
        f"known_dead_proxies 列了 {sorted(orphan)},但 proxies 裡沒有這些名字 —— "
        "這個條目永遠不會生效,是一條看起來在把關實際沒有的死設定"
    )


def test_a_dead_proxy_is_not_silently_tolerated_forever(cfg: dict) -> None:
    """不得把【全部】代理都列進 known_dead —— 那等於把這項檢查關掉。

    `check_proxies()` 的設計是「全死才算失敗,部分失效只記錄」。
    若所有代理都被標成已知死亡,`dead_unexpected` 永遠是空的,
    而 `alive` 也永遠是空的 → 走的是「全部失效」那條 FAIL 分支。
    這條測試擋的是另一種繞法:有人為了讓紅燈變綠而把名字加進 known_dead。
    """
    names = {p["name"] for p in cfg["proxies"]}
    dead = set(cfg["accepted"].get("known_dead_proxies") or [])
    assert dead != names or not names, (
        "每一個代理都被列進 known_dead_proxies —— 這項檢查已經沒有任何判別力了。"
        "代理真的全死時正確的做法是讓它紅燈(並填 worker_url),不是把它標成已知"
    )
