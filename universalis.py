#!/usr/bin/env python3
"""Universalis 客户端：负责世界/数据中心列表、多世界价格拉取、按世界汇总。

接口文档：https://docs.universalis.app/
拉价采用「单世界 + 多个物品 ID」的批量评分接口，一次请求可覆盖数百个物品，
对 Universalis 最友好（官方推荐 bulk 而不是逐物品请求）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

API = "https://universalis.app/api/v2"
UA = "WhatToSell/1.0 (FFXIV currency-to-gil helper)"
BATCH = 100  # 单次请求的物品数量上限（Universalis 建议 <=100）

# 国服 / 繁中 / 韩服大区的中文显示名。
# 注意：「陸行鳥」是繁体中文服（台服），不是日服——日服的大区是 Elemental / Gaia /
# Mana / Meteor 这些英文名，本身不需要加注。
REGION_NAMES = {
    "Elemental": "Elemental", "Gaia": "Gaia", "Mana": "Mana", "Meteor": "Meteor",
    "Aether": "Aether", "Primal": "Primal", "Crystal": "Crystal", "Dynamis": "Dynamis",
    "Chaos": "Chaos", "Light": "Light", "Materia": "Materia",
    "陆行鸟": "陆行鸟（国服）", "莫古力": "莫古力（国服）",
    "猫小胖": "猫小胖（国服）", "豆豆柴": "豆豆柴（国服）",
    "한국": "한국（韩服）", "陸行鳥": "陸行鳥（繁中）",
}


class Unavailable(Exception):
    """该世界/该物品没有可用的市场数据。"""


def _get(url: str, retries: int = 2, timeout: int = 25):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise Unavailable("not found") from None
            last = exc
            # 429/5xx 才值得重试
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except Exception as exc:  # noqa: BLE001 - 网络抖动一律重试
            last = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"请求失败：{url}（{last}）")


def fetch_worlds() -> dict[int, str]:
    """世界 ID -> 世界名。"""
    data = _get(f"{API}/worlds")
    return {int(w["id"]): w["name"] for w in data}


def fetch_data_centers() -> list[dict]:
    """[{区域/大区名, 世界ID列表}]"""
    data = _get(f"{API}/data-centers")
    out = []
    for dc in data:
        out.append({"region": dc["name"], "worldIds": [int(w) for w in dc["worlds"]]})
    return out


def world_catalog() -> list[dict]:
    """给页面下拉框用的完整服务器清单（按大区分组）。

    返回 [{"dc": 大区名, "label": 显示名, "worlds": [世界名...]}]
    顺序：国服四大区在最前（本工具主要面向国服），其余按 Universalis 给出的顺序。
    """
    names = fetch_worlds()
    dcs = fetch_data_centers()
    cn = {"陆行鸟", "莫古力", "猫小胖", "豆豆柴"}
    dcs.sort(key=lambda d: (0 if d["region"] in cn else 1))
    out = []
    for dc in dcs:
        worlds = [names[i] for i in dc["worldIds"] if i in names]
        if worlds:
            out.append({"dc": dc["region"], "label": REGION_NAMES.get(dc["region"], dc["region"]),
                        "worlds": worlds})
    return out


def market(world: str, item_ids: Iterable[int], listings: int = 6, concurrency: int = 4) -> dict[int, dict]:
    """拉取单个世界下多个物品的当前市场数据。

    返回 {itemID: {minPrice, listings:[{price, qty}], sales, listingsCount, ...}}
    没有列表的物品会被跳过（该世界无人挂单）。

    分批并发发出：Universalis 对部分批次会挂很久（甚至超时），串行拉 94 批要十几分钟；
    并发既能显著缩短时间，单批失败也只影响那一批。
    """
    from concurrent.futures import ThreadPoolExecutor

    ids = list(dict.fromkeys(int(i) for i in item_ids))
    result: dict[int, dict] = {}
    batches = [ids[s:s + BATCH] for s in range(0, len(ids), BATCH)]

    def fetch_one(chunk: list[int]):
        path = ",".join(str(i) for i in chunk)
        # entries 决定成交记录的采样量：页面统一按「近 7 天」统计成交，
        # 而 7 天窗口要按 recentHistory 的时间戳自己切，所以采样必须足够大，
        # 太少会让窗口内记录缺失（热门物品尤其明显）。取 500 笔。
        # 注意不要用 unitsSold：那是「返回的最近 N 笔」的合计，可能横跨数月。
        query = urllib.parse.urlencode({"listings": listings, "entries": 500})
        try:
            return chunk, _get(f"{API}/{urllib.parse.quote(world)}/{path}?{query}")
        except Exception as exc:  # noqa: BLE001 - 单批失败不影响整体
            print(f"    ! 跳过批次 {chunk[0]}…（{exc.__class__.__name__}）")
            return chunk, None

    failed = 0
    done = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for chunk, data in pool.map(fetch_one, batches):
            done += 1
            if data is None:
                failed += 1
                continue
            items = data.get("items") if isinstance(data, dict) and "items" in data else {str(chunk[0]): data}
            for iid, payload in items.items():
                if not payload or not payload.get("hasData"):
                    continue
                prices = sorted(
                    (float(l["pricePerUnit"]), int(l["quantity"]))
                    for l in payload.get("listings") or []
                    if l.get("pricePerUnit")
                )
                result[int(iid)] = {
                    "minPrice": prices[0][0] if prices else 0,
                    "minLot": prices[0][1] if prices else 0,
                    "listingsCount": int(payload.get("listingsCount") or len(prices)),
                    "unitsForSale": int(payload.get("unitsForSale") or sum(q for _, q in prices)),
                    "unitsSold": int(payload.get("unitsSold") or 0),
                    "recentHistoryCount": int(payload.get("recentHistoryCount") or 0),
                    "regularSaleVelocity": float(payload.get("regularSaleVelocity") or 0),
                    # 成交记录压平成 [时间戳(秒), 件数]，页面据此切 7 天窗口。
                    # 上面三项是 Universalis 的原生统计，保留给调试用；页面不读它们，
                    # 因为 unitsSold 是「返回的最近 N 笔」合计，可能横跨数月，不等于 7 天。
                    "sales": [
                        [int(h.get("timestamp") or 0), int(h.get("quantity") or 0)]
                        for h in (payload.get("recentHistory") or [])
                        if int(h.get("timestamp") or 0) > 0 and int(h.get("quantity") or 0) > 0
                    ],
                    "averagePrice": float(payload.get("averagePrice") or 0),
                    "listings": [{"price": p, "qty": q} for p, q in prices[:listings]],
                }
            if done % 10 == 0:
                print(f"    …{done}/{len(batches)} 批，已拿到 {len(result)} 个物品")
    if failed:
        print(f"    ! 共 {failed}/{len(batches)} 批失败，这些物品本次没有价格")
    return result


def market_many(worlds: list[str], item_ids: list[int], workers: int = 4) -> dict[str, dict[int, dict]]:
    """并发拉取多个世界；失败的世界返回空字典，不影响整体。"""
    out: dict[str, dict[int, dict]] = {}

    def job(w: str):
        try:
            return w, market(w, item_ids)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {w}: {exc}")
            return w, {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for w, data in pool.map(job, worlds):
            out[w] = data
    return out
