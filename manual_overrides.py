"""对 Garland 数据的**手动修正**。

Garland 偶尔会把某种兑换的货币标错，或者漏掉某条兑换。这个文件用来记录
「以我们的人工核对为准」的条目，build.py 在自动汇总之后统一应用它们，
所以不会被下一次自动构建覆盖。

每条修正的形态：

    {
        "currency": "33914",              # 要挂到哪个货币下（Garland 物品 id）
        "item": {
            "id": "38808",                # 物品 id
            "c": 2,                       # 兑换该物品需要的货币数量（单价）
            "a": 1,                       # 一次兑换产出多少个
            "s": "Bait/Miscellany",       # 商店名（Garland 的英文名，页面会再本地化）
            "note": "……",                 # 修正原因，只给人看，不写进 data.json
        },
    }

字段含义与 build.py 里 build_market_map 生成的条目一致：
c = 货币花费，a = 兑换数量，s = 商店名；可选的 o = 额外材料列表（"id x amount"）。
物品的名字 / 图标 / 是否可交易 / 日文名仍然取自 Garland 的物品文档，
这里**只描述兑换关系本身**，避免重复维护。

唯一的例外是物品**还没被自动抓取过**（例如某条兑换 Garland 完全没有收录）时，
可以在 item 里用 n / en / ja / k / ic 直接给出显示信息；这些字段只在
自动数据缺失时兜底使用，不会覆盖 Garland 已有的属性。

应用后每个条目都会带上 "m": 1 字段（manual），页面据此显示「手动修正」标记。
"""
from __future__ import annotations

import json

# 用户要求的修正：卓越丸子（Select Bait Ball）
#   Garland 把它挂在「亚拉戈诗学神典石」（id 47，2 个）下，实际应为
#   「大地紫票」（id 33914，2 个换 1 个）。物品本身的名字、图标、可交易、
#   稀有度等属性在 Garland 里都是对的，所以只需要修正货币归属。
#   https://garlandtools.cn/db/#item/38808
OVERRIDES: list[dict] = [
    {
        "currency": "33914",          # 大地紫票
        "drop_from": ["47"],          # 顺手把挂错货币下的同一条移除
        "item": {
            "id": "38808",
            "c": 2,
            "a": 1,
            "s": "Bait/Miscellany",
            # 兜底显示信息：取自 https://garlandtools.org/db/doc/item/ja/3/38808.json
            # （自动抓取拿不到时会用到，拿到时以自动数据为准）
            "n": "卓越丸子",
            "en": "Select Bait Ball",
            "ja": "モーエンボール",
            "k": 33,
            "ic": 27011,
            "note": "Garland 标成诗学神典石×2，实际是大地紫票×2（2026-11 手工核对）",
        },
    },
]

# 票据类货币的多语言名兜底（按国服中文名索引）。
# 紫票 / 橙票的兑换表是从物品页的 tradeCurrency 读出来的，这些货币在 Garland 的
# item 文档里常常没有英语 / 日语名（数据里 en / ja 为空），界面切到 EN / JA 就会
# 露出中文。这里补上官方译名（取自 Garland 的 /en/ 与 /ja/ item 文档）。
CURRENCY_NAME_FALLBACK: dict[str, dict[str, str]] = {
    "巧手紫票": {"en": "Purple Crafters' Scrip", "ja": "クラフタースクリップ:紫貨"},
    "大地紫票": {"en": "Purple Gatherers' Scrip", "ja": "ギャザラースクリップ:紫貨"},
    "巧手橙票": {"en": "Orange Crafters' Scrip", "ja": "クラフタースクリップ:橙貨"},
    "大地橙票": {"en": "Orange Gatherers' Scrip", "ja": "ギャザラースクリップ:橙貨"},
}


def describe() -> str:
    """给 build.py 打印一行摘要用。"""
    return "；".join(
        f"{o['item']['id']} → 货币 {o['currency']} ×{o['item']['c']}" for o in OVERRIDES
    )


def apply(result: dict) -> int:
    """把 OVERRIDES 应用到 build_market_map 的结果上，返回应用条数。

    有则覆盖、无则新增；条目标记 "m": 1。应用后按原有排序规则重排受影响货币。
    重复调用结果不变（幂等）。
    """
    by_currency = {c["id"]: c for c in result.get("currencies") or []}
    applied = 0
    for rule in OVERRIDES:
        cobj = by_currency.get(rule["currency"])
        if cobj is None:
            print(f"  ! 手动修正跳过：数据里没有货币 {rule['currency']}")
            continue
        want = rule["item"]
        iid = str(want["id"])
        items = cobj.get("items") or []
        entry = next((e for e in items if str(e.get("id")) == iid), None)
        if entry is None:
            entry = {"id": iid, "n": "", "en": "", "ja": "", "k": None, "ic": None}
            cobj.setdefault("items", []).append(entry)
        # 只修正兑换关系，不改物品自身的属性（名字 / 图标 / 日文名 等都保留 Garland 的）
        entry["c"] = want["c"]
        entry["a"] = want.get("a", 1)
        entry["s"] = want["s"]
        entry["sZh"] = _shop_zh(want["s"])
        if want.get("o"):
            entry["o"] = list(want["o"])
        # 自动数据里缺的显示字段才用这里给的兜底值
        for k in ("n", "en", "ja", "k", "ic"):
            if want.get(k) not in (None, "") and entry.get(k) in (None, ""):
                entry[k] = want[k]
        entry["m"] = 1
        applied += 1

        # 清掉挂错货币下的同一条（例如诗学神典石下的 38808）
        for wrong in rule.get("drop_from") or []:
            wobj = by_currency.get(str(wrong))
            if not wobj:
                continue
            before = len(wobj["items"])
            wobj["items"] = [e for e in wobj["items"] if str(e.get("id")) != iid]
            if len(wobj["items"]) != before:
                print(f"  手动修正：从货币 {wrong} 移除物品 {iid}")

        shop = want["s"]
        if shop not in cobj.get("shops", []):
            cobj.setdefault("shops", []).append(shop)
            cobj["shops"].sort()

    # 受影响货币重排：与 build_market_map 的排序保持一致
    for rule in OVERRIDES:
        cobj = by_currency.get(rule["currency"])
        if cobj:
            cobj["items"].sort(key=lambda e: (e.get("c", 0), e.get("n") or ""))
    return applied


def _shop_zh(shop: str) -> str:
    try:
        import shopnames
        return shopnames.localize_shop(shop, "zh")
    except Exception:  # noqa: BLE001 - 本地化失败就退回英文店名
        return shop


if __name__ == "__main__":
    print(json.dumps(OVERRIDES, ensure_ascii=False, indent=2))
