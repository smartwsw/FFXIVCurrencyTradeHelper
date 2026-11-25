#!/usr/bin/env python3
"""Garland Tools 客户端：抓取 FF14 里所有「可兑换货币 → 可兑换物品」的映射。

为什么走 NPC 商店而不是 item.tradeCurrency？
  - tradeCurrency 只覆盖「全新物品本身就是在售货币」的情形，
    很多货币（亚拉戈幻想神典石、古拉尔票据、各种票据）的商品挂在 NPC 商店里；
  - 而且 tradeCurrency 的历史商店会随版本被清理，NPC 商店保留了完整历史。

数据结构：
  npc.shops[] = {"name": 商店名, "entries": [{"item": [{id,amount}...],
                                             "currency": [{id,amount}...]}]}
  entries[].currency 中 id 等于本货币的那一项即单价，其余为同一商品还需的其它材料。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

BASE = "https://www.garlandtools.org/db"
UA = "WhatToSell/1.0 (FFXIV currency-to-gil helper)"
LANG = "en"
CORE_VERSION = 3
ITEM_VERSION = 3
NPC_VERSION = 2
BROWSE_VERSION = 2

# NPC 称号里能看出「用货币换东西」的关键词
TRADE_TITLE = re.compile(
    r"Exchange|Vendor|Trader|Supplier|Provisioner|Purveyor|Broker|Dealer|"
    r"Reclamation|Trading|Token|Tomestone|Scrip|Splendor",
    re.I,
)

_ZH_URL = (
    "https://raw.githubusercontent.com/ffxiv-teamcraft/ffxiv-teamcraft/staging/"
    "libs/data/src/lib/json/zh/zh-items.json"
)
# 国服/繁中资料片名称
PATCH_SERIES_ZH = {
    "A Realm Reborn": "重生之境", "Heavensward": "苍穹之禁城", "Stormblood": "红莲之狂潮",
    "Shadowbringers": "暗影之逆焰", "Endwalker": "晓月之终途", "Dawntrail": "金曦之遗辉",
}


def _get_json(url: str, retries: int = 3, timeout: int = 90):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Garland 请求失败：{url}（{last}）")


# ---------------------------------------------------------------- 基础数据

def fetch_browse(kind: str, version: int = 2) -> list[dict]:
    """Garland 的全量浏览索引（doc/browse/en/<version>/<kind>.json）。

    这是 Garland 网页「NPCs」浏览页用的清单，包含全部 5 万多个 NPC 的
    编号、名字和称号。兑换商店只出现在有称号的 NPC 上，因此可以据此把
    需要逐个抓取的候选缩小到一千多个。
    """
    data = _get_json(f"{BASE}/doc/browse/{LANG}/{version}/{kind}.json", timeout=180)
    return data.get("browse") or []


def candidate_npcs(browse: list[dict]) -> list[dict]:
    """挑出可能有兑换商店的 NPC：带称号的，加上称号里提到交易的。"""
    out = []
    for entry in browse:
        title = (entry.get("t") or "").strip()
        if title or TRADE_TITLE.search(entry.get("n", "")):
            out.append(entry)
    return out


def fetch_core() -> dict:
    return _get_json(f"{BASE}/doc/core/{LANG}/{CORE_VERSION}/data.json")


def fetch_npc(npc_id) -> dict:
    return _get_json(f"{BASE}/doc/npc/{LANG}/{NPC_VERSION}/{npc_id}.json")


def fetch_item(item_id) -> dict:
    return _get_json(f"{BASE}/doc/item/{LANG}/{ITEM_VERSION}/{item_id}.json")


def fetch_zh_names() -> dict[str, str]:
    """国服物品中文名（Teamcraft 的 zh 数据，与盛大/腾讯服务器一致）。失败返回空表。"""
    try:
        raw = _get_json(_ZH_URL, timeout=120)
        return {k: (v.get("zh") if isinstance(v, dict) else v) for k, v in raw.items() if v}
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 中文名加载失败，将使用英文名：{exc}")
        return {}


def item_index(core: dict) -> dict[str, dict]:
    idx = (core.get("item") or {}).get("partialIndex") or {}
    return {str(k): v for k, v in idx.items() if v}


# ---------------------------------------------------------------- 商店发现

def discover_shop_npcs(core: dict) -> list[int]:
    """找出所有「兑换类」NPC。

    Garland 的全局索引里只登记了部分 NPC，所以这里两路并行：
      1) 索引里所有 NPC 条目；
      2) 已知货币物品的 partials 里出现的 NPC（递归一层）。
    """
    npc_index = (core.get("npc") or {}).get("partialIndex") or {}
    ids = {int(k) for k, v in npc_index.items() if v}
    return sorted(ids)


def shop_currencies(doc: dict) -> Counter:
    """一个 NPC 文档里每种货币出现的次数。"""
    count: Counter = Counter()
    npc = doc.get("npc") or {}
    for shop in npc.get("shops") or []:
        for entry in shop.get("entries") or []:
            for cur in entry.get("currency") or []:
                if isinstance(cur, dict):
                    count[str(cur.get("id"))] += 1
    return count


def collect_shops(core: dict, extra_npc_ids: list[int] | None = None,
                  workers: int = 8, verbose: bool = True) -> dict:
    """遍历所有 NPC，把货币兑换信息汇总成 currencies / offers。"""
    index = item_index(core)
    series = ((core.get("patch") or {}).get("partialIndex") or {})
    series_of = {k: v.get("series") for k, v in series.items() if isinstance(v, dict)}
    current_patch = (core.get("patch") or {}).get("current")

    npc_ids = set(discover_shop_npcs(core))
    npc_ids |= set(extra_npc_ids or [])

    def job(nid):
        try:
            return nid, fetch_npc(nid)
        except Exception:  # noqa: BLE001
            return nid, None

    npc_names: dict[int, str] = {}
    currency_shops: dict[str, dict[str, dict]] = {}   # currencyId -> shopName -> {npcs, listings}
    if verbose:
        print(f"扫描 NPC 商店：{len(npc_ids)} 个 NPC…")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for nid, doc in pool.map(job, npc_ids):
            done += 1
            if verbose and done % 200 == 0:
                print(f"  …已处理 {done}/{len(npc_ids)}")
            if not doc:
                continue
            npc = doc.get("npc") or {}
            npc_name = npc.get("name") or f"#{nid}"
            for shop in npc.get("shops") or []:
                entries = shop.get("entries") or []
                if not entries:
                    continue
                shop_name = shop.get("name") or npc_name
                for entry in entries:
                    # 少数商店的 entries 只是物品 id 列表（无价格），跳过
                    if not isinstance(entry, dict):
                        continue
                    cur_list = [c for c in (entry.get("currency") or []) if isinstance(c, dict)]
                    mine_ids = {str(c.get("id")) for c in cur_list}
                    for cur in cur_list:
                        cid = str(cur.get("id"))
                        cost = float(cur.get("amount") or 0)
                        if cost <= 0:
                            continue
                        others = [
                            {"id": str(o.get("id")), "amount": o.get("amount"),
                             "name": index.get(str(o.get("id")), {}).get("n", "")}
                            for o in cur_list if str(o.get("id")) != cid
                        ]
                        items = [i for i in (entry.get("item") or []) if isinstance(i, dict)]
                        if not items:
                            continue
                        bucket = currency_shops.setdefault(cid, {}).setdefault(
                            shop_name, {"npcs": set(), "listings": []})
                        bucket["npcs"].add(nid)
                        for it in items:
                            bucket["listings"].append({
                                "id": str(it.get("id")),
                                "amount": it.get("amount") or 1,
                                "cost": cost,
                                "others": others,
                                "shop": shop_name,
                            })
            if npc.get("shops"):
                npc_names[nid] = npc_name

    # ------------------------------------------------------------ 组装
    currencies = []
    for cid, shops in currency_shops.items():
        info = index.get(cid) or {}
        best: dict[str, dict] = {}
        for shop_name, bucket in shops.items():
            for listing in bucket["listings"]:
                iid = listing["id"]
                # 同一货币对同一物品可能挂多家店，保留最便宜
                if iid not in best or listing["cost"] < best[iid]["cost"]:
                    best[iid] = listing
        offers = [
            {
                "id": iid,
                "name": index.get(iid, {}).get("n") or f"#{iid}",
                "amount": o["amount"],
                "cost": o["cost"],
                "others": o["others"],
                "shop": o["shop"],
            }
            for iid, o in best.items()
        ]
        offers.sort(key=lambda o: (o["cost"], o["name"]))
        if not offers:
            continue
        patch = (info.get("p") or "")
        currencies.append({
            "id": cid,
            "name": info.get("n") or f"#{cid}",
            "patch": patch,
            "series": series_of.get(str(patch)) or "",
            "icon": info.get("c"),
            "kind": info.get("t"),
            "shopCount": len(shops),
            "npcCount": len({n for b in shops.values() for n in b["npcs"]}),
            "npcs": sorted({npc_names.get(n, str(n)) for b in shops.values() for n in b["npcs"]}),
            "shops": sorted(shops.keys()),
            "offers": offers,
        })

    currencies.sort(key=lambda c: (-len(c["offers"]), c["name"]))
    if verbose:
        total = sum(len(c["offers"]) for c in currencies)
        print(f"共 {len(currencies)} 种货币，{total} 条可兑换记录；当前游戏版本 {current_patch}")
    return {"currencies": currencies, "patch": current_patch}
