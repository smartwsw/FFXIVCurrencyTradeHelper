#!/usr/bin/env python3
"""FF14 货币 → 可兑换物品 → 市场价 数据管道。

流程：
  1. Garland Tools：根据 NPC 商店（doc/npc）汇总「每种货币能换哪些物品」，
     同时读取 Garland 全局物品索引与 Teamcraft 国服中文名；
  2. Universalis：批量拉取所选服务器的当前最低挂单价、挂单量与成交活跃度；
  3. 计算「每单位货币的卖价」并写出 site/data.json 供静态页面读取。

用法：
  python build.py --worlds 神意之地 晨曦王座           # 指定服务器
  python build.py --dc 陆行鸟                        # 整个大区
  python build.py --worlds 神意之地 --workers 16      # 提高 NPC 抓取并发
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import garland
import localize
import secondhop
import universalis

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "docs", "data")
CACHE_DIR = os.path.join(HERE, ".cache")
TZ = timezone(timedelta(hours=8))  # 国服时间


# ------------------------------------------------------------------ 缓存

def cache_path(name: str) -> str:
    return os.path.join(CACHE_DIR, f"{name}.json.gz")


def load_cache(name: str, max_age_hours: float | None = None):
    path = cache_path(name)
    if not os.path.exists(path):
        return None
    if max_age_hours is not None:
        age = (time.time() - os.path.getmtime(path)) / 3600
        if age > max_age_hours:
            return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def save_cache(name: str, obj) -> None:
    path = cache_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(cache_path(name), "wt", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


# ------------------------------------------------------------ Garland 扫描

def scan_npc_shops(workers: int = 10, no_second_hop: bool = False) -> dict:
    """抓取所有兑换 NPC 的商店，汇总成「货币 → 可兑换物品」。

    Garland 的全局物品索引里没有 NPC，但有全量浏览索引
    doc/browse/en/2/npc.json（5 万多个 NPC 的编号/名字/称号）。
    兑换商店只挂在有称号的 NPC 上，所以先把候选缩小到一千多个再逐个抓取，
    比按编号段暴力扫描快两个数量级，也不会漏掉新版本 NPC。
    每个 NPC 单独缓存，日常刷新只重抓新增/改动的那几十个。
    """
    from concurrent.futures import ThreadPoolExecutor

    meta = load_cache("garland-meta", max_age_hours=6) or {}
    patch = meta.get("patch")
    if not patch:
        patch = garland.fetch_core().get("patch", {}).get("current")
        meta["patch"] = patch
        save_cache("garland-meta", meta)

    core = load_cache("garland-core", max_age_hours=24)
    if core is None:
        print("读取 Garland 全局索引…")
        core = garland.fetch_core()
        save_cache("garland-core", core)
    index = garland.item_index(core)

    zh = load_cache("zh-items", max_age_hours=24)
    if zh is None:
        print("读取国服中文物品名…")
        zh = garland.fetch_zh_names()
        save_cache("zh-items", zh)
    print(f"中文名表：{len(zh)} 条")

    browse = load_cache("garland-browse-npc", max_age_hours=24)
    if browse is None:
        print("读取 Garland NPC 总表…")
        browse = garland.fetch_browse("npc", garland.BROWSE_VERSION)
        save_cache("garland-browse-npc", browse)
    cands = garland.candidate_npcs(browse)
    print(f"NPC 总数 {len(browse)}，候选兑换 NPC {len(cands)}（带称号/交易关键词）")

    cache_key = f"garland-npc-{patch}"

    def job(entry):
        nid = entry["i"]
        cached = load_cache(f"{cache_key}/{nid}")
        if cached is not None:
            return cached
        try:
            doc = garland.fetch_npc(nid)
        except Exception:  # noqa: BLE001 - 404 表示该编号没有 NPC 文档
            save_cache(f"{cache_key}/{nid}", [])
            return []
        npc = doc.get("npc") or {}
        found = []
        for shop in npc.get("shops") or []:
            entries = [e for e in (shop.get("entries") or [])
                       if isinstance(e, dict) and e.get("currency")]
            if entries:
                found.append({"name": shop.get("name") or npc.get("name"), "entries": entries})
        save_cache(f"{cache_key}/{nid}", found)
        return found

    npcs: list[dict] = []
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for entry, found in zip(cands, pool.map(job, cands)):
            done += 1
            if found:
                npcs.append({"id": entry["i"], "name": entry.get("n"), "shops": found})
            if done % 200 == 0:
                print(f"  …{done}/{len(cands)}，已命中兑换 NPC {len(npcs)} 个，用时 {time.time() - t0:.0f}s")
    print(f"抓取完成：{len(npcs)} 个兑换 NPC，用时 {time.time() - t0:.0f}s")

    # 物品的名字、类别、图标都在 item 文档里（全局索引只收录常用物品，不能依赖）
    need = set()
    for npc in npcs:
        for shop in npc["shops"]:
            for e in shop["entries"]:
                for o in e.get("item") or []:
                    if isinstance(o, dict) and o.get("id"):
                        need.add(str(o["id"]))
                for o in e.get("currency") or []:
                    if isinstance(o, dict) and o.get("id"):
                        need.add(str(o["id"]))
    details = fetch_item_details(sorted(need), workers)
    print(f"物品详情：{len(details)}/{len(need)} 个")

    # 第二路来源：票据类货币的兑换表挂在物品页的 tradeCurrency 上，NPC 商店里没有。
    # （紫票/橙票就是这样：它们的 npcs 字段为空，只能从这里读）
    scrip_shops = fetch_tradecurrency_shops(SHOP_CURRENCIES, details, zh, index, workers)
    if scrip_shops:
        print(f"票据类货币（tradeCurrency）：{len(scrip_shops)} 种 / "
              f"{sum(len(v) for v in scrip_shops.values())} 条")

    # 第三路：一级兑换物若不可交易（独占），可能还能再兑换一次（二级兑换）。
    second_hop = {}
    if not no_second_hop:
        preliminary = build_market_map(npcs, index, zh, details, patch, scrip_shops)
        first_ids = sorted({
            str(it["id"])
            for c in preliminary["currencies"] if c["n"] in WANTED_CURRENCIES
            for it in c["items"] if it.get("x") == 1
        })
        second_hop = secondhop.scan_second_hop(first_ids, workers, load_cache, save_cache)
        extra = set()
        for rec in second_hop.values():
            for shop in rec["next"]:
                for listing in shop["listings"]:
                    for o in listing["item"]:
                        if o.get("id"):
                            extra.add(str(o["id"]))
        missing = sorted(extra - set(details))
        if missing:
            more = fetch_item_details(missing, workers)
            details.update(more)
            print(f"  补充抓取二级兑换产物 {len(more)} 个")
        # 判断二级产物能不能卖，依赖 tr（是否可交易）；旧缓存可能没有这个字段
        ensure_details(details, sorted(extra), workers)

    # 日文名（国服数据源没有日文文本，只能从 Garland 的 /ja/ 文档取，之后走缓存）
    all_needed = set(details)
    ja_names = localize.fetch_ja_names(sorted(all_needed), 10, load_cache, save_cache)

    return build_market_map(npcs, index, zh, details, patch, scrip_shops, second_hop, ja_names)


def ensure_details(details: dict, ids: list[str], workers: int = 16) -> None:
    """确保 ids 的详情里带有「是否可交易」标记。

    早期版本的缓存只有 n/t/icon/p，没有 tr 字段，会让二级兑换产物被一律当成
    不可交易而过滤掉。这里发现缺字段就强制重抓，只涉及被引用的少数物品。
    """
    bad = [i for i in ids if (details.get(i) or {}).get("tr") is None]
    if not bad:
        return
    print(f"  重新校验 {len(bad)} 个物品的可交易标记…")
    more = fetch_item_details_force(bad, workers)
    details.update(more)
    print(f"  已更新 {len(more)} 个")


def fetch_item_details_force(ids: list[str], workers: int = 16) -> dict:
    """不管缓存，强制重抓。"""
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, dict] = {}

    def job(iid):
        try:
            doc = garland.fetch_item(iid)
        except Exception:  # noqa: BLE001
            return iid, {}
        it = doc.get("item") or {}
        rec = {"n": it.get("name") or "", "t": it.get("category"),
               "icon": it.get("icon"), "p": it.get("patch"),
               "ex": 0 if it.get("tradeable") else 1,
               "tr": 1 if it.get("tradeable") else 0,
               "rar": it.get("rarity")}
        save_cache(f"garland-item/{iid}", rec)
        return iid, rec

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for iid, rec in pool.map(job, ids):
            if rec:
                out[iid] = rec
    return out


def fetch_tradecurrency_shops(currency_ids: dict[str, str], details: dict, zh: dict,
                              index: dict, workers: int = 16) -> dict[str, list]:
    """从物品页的 tradeCurrency 读取「货币 → 兑换商店 → 物品」。

    返回 {currencyId: [{"name": 商店名, "entries": [...]}]}，结构与 NPC 商店一致，
    方便 build_market_map 统一处理。
    """
    from concurrent.futures import ThreadPoolExecutor

    def job(cid):
        try:
            doc = garland.fetch_item(cid)
        except Exception:  # noqa: BLE001
            return cid, []
        item = doc.get("item") or {}
        groups = item.get("tradeCurrency") or []
        shops = []
        for g in groups:
            entries = []
            for listing in g.get("listings") or []:
                cur = [c for c in (listing.get("currency") or []) if isinstance(c, dict)]
                if not any(str(c.get("id")) == cid for c in cur):
                    continue
                items = [i for i in (listing.get("item") or []) if isinstance(i, dict)]
                if items:
                    entries.append({"item": items, "currency": cur})
            if entries:
                shops.append({"name": g.get("shop") or item.get("name") or cid, "entries": entries})
        return cid, shops

    out: dict[str, list] = {}
    ids = list(currency_ids)
    with ThreadPoolExecutor(max_workers=min(workers, len(ids) or 1)) as pool:
        for cid, shops in pool.map(job, ids):
            out[cid] = shops

    # 这些兑换里出现的物品也要有名字/图标
    extra = set()
    for shops in out.values():
        for shop in shops:
            for e in shop["entries"]:
                for o in e.get("item") or []:
                    if o.get("id"):
                        extra.add(str(o["id"]))
    missing = sorted(extra - set(details))
    if missing:
        more = fetch_item_details(missing, workers)
        details.update(more)
        print(f"  补充抓取票据兑换物品 {len(more)} 个")
    return out


def fetch_item_details(ids: list[str], workers: int = 16) -> dict:
    """抓 item 文档，补齐名字/类别/图标。按 ID 缓存，日常刷新只重抓新增物品。"""
    from concurrent.futures import ThreadPoolExecutor

    out: dict[str, dict] = {}

    def job(iid):
        cached = load_cache(f"garland-item/{iid}")
        # 旧缓存没有 icon 字段，需要重抓（图标文件名用的是图标 id，不是物品 id）
        if cached is not None and (not cached or "icon" in cached):
            return iid, cached
        try:
            doc = garland.fetch_item(iid)
        except Exception:  # noqa: BLE001
            save_cache(f"garland-item/{iid}", {})
            return iid, {}
        it = doc.get("item") or {}
        rec = {"n": it.get("name") or "", "t": it.get("category"),
               "icon": it.get("icon"), "p": it.get("patch"),
               # 国服界面里的「独占」属性 = 不可交易 / 不可上架；Garland 用 tradeable 表示
               "ex": 0 if it.get("tradeable") else 1,
               "rar": it.get("rarity")}
        save_cache(f"garland-item/{iid}", rec)
        return iid, rec

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for iid, rec in pool.map(job, ids):
            done += 1
            if rec:
                out[iid] = rec
            if done % 1000 == 0:
                print(f"  …物品详情 {done}/{len(ids)}，用时 {time.time() - t0:.0f}s")
    return out


# 这些类别属于「货币/代币/票据」，其余（装备、材料……）在兑换里只作为商品出现
CURRENCY_KINDS = {63, 61, 58, 60, 83, 85, 100, 54, 62, 64}

# 兑换表只挂在物品页 tradeCurrency 上、NPC 商店里查不到的货币（按 Garland 物品 id）。
# 紫票/橙票的 tradeCurrency.npcs 为空，只能走这条路径。
SHOP_CURRENCIES = {
    "33913": "巧手紫票",
    "33914": "大地紫票",
    "41784": "巧手橙票",
    "41785": "大地橙票",
}

# 页面上真正展示的货币（按国服中文名）。二级兑换只针对这些货币里的一级兑换物扫描。
WANTED_CURRENCIES = {
    "亚拉戈诗学神典石", "怪物狩猎的战利品", "天穹街振兴票", "双色宝石",
    "宇宙信用点", "巧手紫票", "大地紫票", "巧手橙票", "大地橙票",
}
KIND_LABEL = {
    58: "魔晶石", 59: "水晶", 60: "触媒", 61: "杂货", 62: "灵魂水晶",
    63: "其他", 64: "资材", 83: "亚拉戈魔晶石", 85: "季节杂货", 100: "货币",
}


def build_market_map(npcs: list[dict], index: dict, zh: dict,
                     details: dict[str, dict], patch: str,
                     scrip_shops: dict[str, list] | None = None,
                     second_hop: dict[str, dict] | None = None,
                     ja_names: dict[str, str] | None = None) -> dict:
    """把 NPC 商店 + 票据的 tradeCurrency 压平成 货币 -> 唯一物品 的映射。

    second_hop 给出「一级兑换物 → 它能再兑换成什么」。一级兑换物本身不可交易时，
    这条路就是唯一的变现途径，所以要作为独立条目计入，并把路径写清楚。
    """
    currencies: dict[str, dict] = {}
    ja_names = ja_names or {}

    def add_entries(shop_name: str, entries: list) -> None:
        for entry in entries:
            cur = [c for c in entry["currency"] if isinstance(c, dict)]
            items = [i for i in (entry.get("item") or []) if isinstance(i, dict)]
            if not items or not cur:
                continue
            for c in cur:
                cid = str(c.get("id"))
                cost = float(c.get("amount") or 0)
                if cost <= 0:
                    continue
                others = [
                    f"{o['id']}x{o.get('amount')}" for o in cur
                    if str(o.get("id")) != cid and isinstance(o.get("amount"), (int, float))
                ]
                cobj = currencies.setdefault(cid, {"items": {}, "shops": set()})
                cobj["shops"].add(shop_name)
                for it in items:
                    iid = str(it.get("id"))
                    prev = cobj["items"].get(iid)
                    cand = {"c": cost, "a": it.get("amount") or 1,
                            "o": others, "s": shop_name}
                    if prev is None or cand["c"] < prev["c"]:
                        cobj["items"][iid] = cand

    for npc in npcs:
        for shop in npc["shops"]:
            add_entries(shop["name"], shop["entries"])

    # 票据类：来自物品页的 tradeCurrency（只给 SHOP_CURRENCIES 里的货币用）
    for cid, shops in (scrip_shops or {}).items():
        for shop in shops:
            add_entries(shop["name"], shop["entries"])

    # 二级兑换：一级兑换物不可交易（独占）时，它可能还能再兑换一次。
    # 关键点：二级兑换消耗的是「一级换来的中间物」本身，不再消耗原货币。
    # 所以一次二级兑换折算成原货币的成本 = 换得多少中间物 × 每个中间物花多少原货币。
    # 只保留「二级产物本身可交易」的路径——不可交易的产物还是卖不掉，没有意义。
    second_raw: dict[str, list] = {}   # 货币id -> 二级条目（先收集，下面统一格式化）
    if second_hop:
        for cid, cobj in currencies.items():
            for first_id, first_info in cobj["items"].items():
                hop = second_hop.get(first_id)
                if not hop:
                    continue
                per_unit_cost = first_info["c"] / max(1, first_info["a"])   # 每个中间物花的原货币
                for shop in hop["next"]:
                    for listing in shop["listings"]:
                        cur = listing["currency"]
                        # 二级兑换必须用本中间物作成本
                        mine = next((c for c in cur if str(c.get("id")) == first_id), None)
                        if not mine:
                            continue
                        need = float(mine.get("amount") or 0)
                        if need <= 0:
                            continue
                        others2 = [
                            f"{o['id']}x{o.get('amount')}" for o in cur
                            if str(o.get("id")) != first_id
                        ]
                        for target in listing["item"]:
                            tid = str(target.get("id"))
                            tinfo = details.get(tid) or {}
                            # 二级产物必须自己能上架，否则换出来照样卖不掉
                            # 新缓存有 tr；老缓存只有 ex（独占标记），两者互补
                            is_tradeable = tinfo.get("tr")
                            if is_tradeable is None:
                                is_tradeable = not tinfo.get("ex", 0)
                            if not is_tradeable:
                                continue
                            second_raw.setdefault(cid, []).append({
                                "id": tid,
                                # 折算成本：消耗的中间物 × 每个中间物的原货币成本
                                "c": per_unit_cost * need,
                                "a": target.get("amount") or 1,
                                "s": first_info["s"],
                                "o": first_info.get("o") or [],
                                "h": [{
                                    "id": first_id,
                                    "need": need,
                                    "s": shop["shop"],
                                    "ta": target.get("amount") or 1,
                                    **({"o": others2} if others2 else {}),
                                }],
                            })

    def info(iid: str) -> dict:
        d = details.get(iid) or {}
        g = index.get(iid) or {}
        return {
            "name": zh.get(iid) or d.get("n") or g.get("n") or f"#{iid}",
            "en": d.get("n") or g.get("n") or "",
            "ja": ja_names.get(iid, ""),
            "kind": d.get("t") if d.get("t") is not None else g.get("t"),
            "icon": d.get("icon") or g.get("c"),
            "patch": d.get("p") or g.get("p") or "",
            # 「独占」属性（国服界面用语）：不可交易、不可上架、不可出售
            "exclusive": bool(d.get("ex")),
            "rarity": d.get("rar"),
        }
    def name(iid: str) -> str:
        return info(iid)["name"]

    cur_out = []
    dropped = 0
    used_ids: set[str] = set()
    for cid, obj in currencies.items():
        cinfo = info(cid)
        kind = cinfo["kind"]
        # 只保留真正的货币：类别对不上、又只换得起一件东西的，多半是配方材料
        if kind is not None and kind not in CURRENCY_KINDS and len(obj["items"]) < 2:
            dropped += 1
            continue
        if kind is None and len(obj["items"]) < 2:
            dropped += 1
            continue

        items = sorted(obj["items"].items(), key=lambda kv: (kv[1]["c"], name(kv[0])))
        entries = []
        for iid, o in items:
            used_ids.add(iid)
            iinfo = info(iid)
            entries.append({
                "id": iid,
                "n": iinfo["name"],
                "en": iinfo["en"],
                "ja": iinfo["ja"],
                "k": iinfo["kind"],
                # 图标 id：Garland 的图标文件名对应图标（= Icon ID），不是物品 id
                "ic": iinfo["icon"],
                "c": o["c"],
                "a": o["a"],
                "s": o["s"],
                **({"o": o["o"]} if o["o"] else {}),
                # 只给「独占」物品打标，普通物品不写这个字段，省体积
                **({"x": 1} if iinfo["exclusive"] else {}),
            })

        # 并入二级兑换条目：同一件商品可能既有一级也有二级路径，取总成本更低的
        by_id = {e["id"]: e for e in entries}
        extra_count = 0
        for raw in second_raw.get(cid, []):
            tid = raw["id"]
            hop = raw["h"][0]
            # raw["c"] 已经是「整条路径折算成原货币的成本」，不再额外加
            total_cost = raw["c"]
            cand = {
                "id": tid,
                "n": info(tid)["name"],
                "en": info(tid)["en"],
                "k": info(tid)["kind"],
                "ic": info(tid)["icon"],
                "c": total_cost,
                "a": raw["a"],
                "s": raw["s"],
                "h": [hop],
                **({"o": raw["o"]} if raw["o"] else {}),
            }
            old = by_id.get(tid)
            if old is None or total_cost < old["c"]:
                by_id[tid] = cand
                extra_count += 1
        if extra_count:
            entries = sorted(by_id.values(), key=lambda e: (e["c"], e["n"]))
        cur_out.append({
            "id": cid,
            "n": cinfo["name"],
            "en": cinfo["en"],
            "ja": cinfo["ja"],
            "p": cinfo["patch"],
            "icon": cinfo["icon"],
            "k": kind,
            "kl": KIND_LABEL.get(kind, ""),
            "shops": sorted(obj["shops"] | {r["h"][0]["s"] for r in second_raw.get(cid, [])}),
            "items": entries,
        })

    cur_out.sort(key=lambda c: (-len(c["items"]), c["n"]))
    total = sum(len(c["items"]) for c in cur_out)
    print(f"汇总完成：{len(cur_out)} 种货币 / {total} 条兑换 + {len(used_ids)} 个独立物品"
          f"（剔除疑似配方的单件条目 {dropped} 个）")
    return {"patch": patch, "currencies": cur_out}


# ------------------------------------------------------------ Universalis

def pick_worlds(args) -> list[str]:
    if args.worlds:
        return list(dict.fromkeys(args.worlds))
    if args.dc:
        names = universalis.fetch_worlds()
        dcs = universalis.fetch_data_centers()
        for dc in dcs:
            if dc["region"] in (args.dc, universalis.REGION_NAMES.get(args.dc)):
                return [names[i] for i in dc["worldIds"] if i in names]
        raise SystemExit(f"找不到大区：{args.dc}")
    return ["神意之地"]


def collect_prices(worlds: list[str], item_ids: list[int], max_age_hours: float) -> dict:
    """按世界拉价：最近 max_age_hours 小时内抓过的物品跳过。"""
    key = "prices"
    store = load_cache(key) or {"worlds": {}}
    now = time.time()
    for w in worlds:
        wstore = store["worlds"].setdefault(w, {"fetched": {}, "data": {}})
        todo = [i for i in item_ids if now - wstore["fetched"].get(str(i), 0) > max_age_hours * 3600]
        if not todo:
            print(f"  {w}: 全部命中缓存")
            continue
        print(f"  {w}: 拉取 {len(todo)} 个物品…")
        t0 = time.time()
        got = universalis.market(w, todo)
        wstore["data"].update({str(k): v for k, v in got.items()})
        for i in todo:
            wstore["fetched"][str(i)] = now
        print(f"  {w}: 有挂单 {len(got)}/{len(todo)}，用时 {time.time() - t0:.0f}s")
        save_cache(key, store)
    return store


# ------------------------------------------------------------------ 输出

def main() -> int:
    ap = argparse.ArgumentParser(description="FF14 货币兑换收益表")
    ap.add_argument("--worlds", nargs="*", help="服务器名，如 神意之地 晨曦王座")
    ap.add_argument("--dc", help="大区名，如 陆行鸟（覆盖 --worlds）")
    ap.add_argument("--workers", type=int, default=10, help="抓取 Garland NPC 的并发数，默认 10")
    ap.add_argument("--price-age", type=float, default=0.5, help="价格缓存有效期（小时），默认 0.5")
    ap.add_argument("--garland-age", type=float, default=6,
                    help="Garland 商店缓存有效期（小时），默认 6")
    ap.add_argument("--no-second-hop", action="store_true",
                    help="跳过「二级兑换」扫描（一级兑换物不可交易时，再去兑换一次）")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    t_start = time.time()

    garland_data = load_cache("garland", max_age_hours=args.garland_age)
    if garland_data is None:
        print("== 1/2 Garland：货币与兑换物品")
        garland_data = scan_npc_shops(args.workers, args.no_second_hop)
        save_cache("garland", garland_data)
    else:
        print("Garland 数据命中缓存（--garland-age 内未过期）")

    worlds = pick_worlds(args)
    item_ids = sorted({
        int(it["id"]) for c in garland_data["currencies"] for it in c["items"]
        if str(it["id"]).isdigit()
    })
    print(f"== 2/2 Universalis：{', '.join(worlds)}，共 {len(item_ids)} 个物品")
    prices = collect_prices(worlds, item_ids, args.price_age)

    worlds_out = []
    for w in worlds:
        wstore = prices["worlds"].get(w) or {}
        worlds_out.append({
            "name": w,
            "markets": {k: v for k, v in (wstore.get("data") or {}).items()},
            "fetchedAt": max((wstore.get("fetched") or {}).values(), default=0),
        })

    # 服务器清单（Universalis 全量），把已经抓过价的服务器排到最前，方便直接选中
    try:
        catalog = universalis.world_catalog()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 服务器清单获取失败：{exc}")
        catalog = []
    priced = set(worlds)
    if catalog:
        pref = [{"dc": "已抓价", "label": "已抓价", "worlds": [w for w in worlds]}]
        catalog = pref + [
            {**dc, "worlds": [w for w in dc["worlds"] if w not in priced]}
            for dc in catalog
        ]
        catalog = [dc for dc in catalog if dc["worlds"]]
        total = sum(len(dc["worlds"]) for dc in catalog)
        print(f"服务器清单：{len(catalog)} 组 / {total} 个服务器")

    payload = {
        "generatedAt": datetime.now(TZ).isoformat(timespec="seconds"),
        "patch": garland_data["patch"],
        "worlds": worlds_out,
        # 页面下拉框用的完整服务器清单（来自 Universalis），预抓价格的服务器在最前
        "worldCatalog": catalog,
        "currencies": garland_data["currencies"],
        "sources": {
            "garland": "https://www.garlandtools.org/",
            "universalis": "https://universalis.app/",
            "zhNames": "https://github.com/ffxiv-teamcraft/ffxiv-teamcraft",
        },
    }
    out = os.path.join(DATA_DIR, "data.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(out) / 1e6
    print(f"写出 {out}（{size:.2f} MB），总用时 {time.time() - t_start:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
