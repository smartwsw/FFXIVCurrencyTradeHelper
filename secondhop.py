"""给抓取管线加「二级兑换」：独占的一级兑换物若本身能再兑换成可交易物品，也计入收益。

放在这里独立成模块，避免 build.py 继续膨胀。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import garland


def scan_second_hop(first_ids: list[str], workers: int = 16,
                    load_cache=None, save_cache=None, verbose=True) -> dict:
    """对每个一级兑换物抓 item 文档，读出它的 tradeCurrency。

    返回 {first_id: {"shops": [...], "tradeable": bool}}，结构尽量精简；
    没有二级兑换的不出现在结果里。
    """
    out: dict[str, dict] = {}
    done = 0
    t0 = time.time()

    def job(iid):
        cached = load_cache(f"garland-item2/{iid}") if load_cache else None
        if cached is not None:
            return iid, cached
        try:
            doc = garland.fetch_item(iid)
        except Exception:  # noqa: BLE001
            rec = {"next": [], "tradeable": 0}
            if save_cache:
                save_cache(f"garland-item2/{iid}", rec)
            return iid, rec
        item = doc.get("item") or {}
        next_shops = []
        for g in item.get("tradeCurrency") or []:
            listings = []
            for listing in g.get("listings") or []:
                cur = [c for c in (listing.get("currency") or []) if isinstance(c, dict)]
                its = [i for i in (listing.get("item") or []) if isinstance(i, dict)]
                if its and cur:
                    listings.append({"item": its, "currency": cur})
            if listings:
                next_shops.append({"shop": g.get("shop") or "", "listings": listings})
        rec = {"next": next_shops, "tradeable": 1 if item.get("tradeable") else 0}
        if save_cache:
            save_cache(f"garland-item2/{iid}", rec)
        return iid, rec

    if verbose:
        print(f"扫描二级兑换：{len(first_ids)} 个一级兑换物…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for iid, rec in pool.map(job, first_ids):
            done += 1
            if rec.get("next"):
                out[iid] = rec
            if verbose and done % 200 == 0:
                print(f"  …{done}/{len(first_ids)}，有二级兑换 {len(out)} 个，用时 {time.time() - t0:.0f}s")
    if verbose:
        print(f"二级兑换：{len(out)}/{len(first_ids)} 个一级兑换物可再兑换，用时 {time.time() - t0:.0f}s")
    return out
