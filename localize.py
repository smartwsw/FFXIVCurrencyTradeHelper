"""多语言物品名：在中文名之外补充日文（ja）与英文（en）。

来源：
  - 英文：Garland 物品文档的 item.name（已有缓存）
  - 日文：Garland 同文档的 /ja/ 版本（国服数据源没有日文文本，只能从这里取）
抓到的日文名单独缓存在 .cache/garland-item-ja/，只抓一次。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import garland


def fetch_ja_names(ids: list[str], workers: int = 8,
                   load_cache=None, save_cache=None, verbose: bool = True) -> dict[str, str]:
    """抓取物品的日文名。返回 {id: 日文名}，失败的不出现在结果里。"""
    out: dict[str, str] = {}
    done = 0
    fresh = 0
    t0 = time.time()

    def job(iid):
        cached = load_cache(f"garland-item-ja/{iid}") if load_cache else None
        if cached is not None:
            return iid, cached.get("ja") or ""
        try:
            doc = garland._get_json(
                f"https://www.garlandtools.org/db/doc/item/ja/3/{iid}.json", timeout=45)
        except Exception:  # noqa: BLE001
            if save_cache:
                save_cache(f"garland-item-ja/{iid}", {"ja": ""})
            return iid, ""
        name = (doc.get("item") or {}).get("name") or ""
        if save_cache:
            save_cache(f"garland-item-ja/{iid}", {"ja": name})
        return iid, name

    if verbose:
        print(f"抓取日文名：{len(ids)} 个物品…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for iid, name in pool.map(job, ids):
            done += 1
            if name:
                out[iid] = name
            if verbose and done % 1000 == 0:
                print(f"  …{done}/{len(ids)}，用时 {time.time() - t0:.0f}s")
    if verbose:
        print(f"日文名：{len(out)}/{len(ids)} 个，用时 {time.time() - t0:.0f}s")
    return out
