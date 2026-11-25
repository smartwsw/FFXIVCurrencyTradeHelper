"""商店名多语言：按术语做翻译（词典 + 结构规则）。

Garland 的商店名在所有语言文档里都是英文，国服 SpecialShop 的中文名又和 Garland
的商店划分粒度不一致（国服按品级拆成几十个，Garland 一个笼统名），
无法一一对应。所以这里对商店名做「术语级」翻译：
  - 用词典翻译高频词（Scrips / Materia / Gear / Weapons …）
  - 保留罗马数字、品级、Lv/IL 等结构信息
  - 词典没覆盖的部分保留英文，保证不会出现空白
"""
from __future__ import annotations

import re

# 英文 -> 中文（按词组优先匹配，长的放前面）
ZH_PHRASES = [
    ("Allagan Tomestones of Poetics", "亚拉戈诗学神典石"),
    ("Allagan Tomestones of Mathematics", "亚拉戈数理神典石"),
    ("Allagan Tomestones of Mnemonics", "亚拉戈记忆神典石"),
    ("Purple Scrip Exchange", "紫票交易"),
    ("Orange Scrip Exchange", "橙票交易"),
    ("Crafters' Scrip Exchange", "巧手票据交易"),
    ("Gatherers' Scrip Exchange", "大地票据交易"),
    ("Skybuilders' Scrips", "天穹街振兴票"),
    ("Sky Pirate Spoils", "空贼战利品"),
    ("Sacks of Nuts Exchange", "战利品交易"),
    ("Sacks of Nuts", "怪物狩猎的战利品"),
    ("Bicolor Gemstone", "双色宝石"),
    ("Cosmocredit Exchange", "宇宙信用点交易"),
    ("Cosmocredits", "宇宙信用点"),
    ("Centurio Seals", "兵团徽章"),
    ("Trophy Crystal", "战利水晶"),
    ("Materia", "魔晶石"),
    ("Master Recipes", "工匠秘籍"),
    ("Materials", "素材"),
    ("Bait", "鱼饵"),
    ("Tokens", "票据"),
    ("Furnishings", "家具"),
    ("Misc.", "杂货"),
    ("Miscellany", "杂项"),
    ("Music", "乐谱"),
    ("Gear", "装备"),
    ("Weapons", "武器"),
    ("Tools", "工具"),
    ("Armor", "防具"),
    ("Accessories", "饰品"),
    ("DoW", "战斗精英"),
    ("DoM", "魔法导师"),
    ("DoH", "能工巧匠"),
    ("DoL", "大地使者"),
    ("Folklore", "传承录"),
    ("Augmentation", "强化"),
    ("Augmented", "改良型"),
    ("Exchange", "兑换"),
    ("Repair", "修理"),
    ("Glamour", "幻化"),
    ("Disposal", "收购"),
    ("Special Arms", "特殊武器"),
    ("Other", "其他"),
    ("Shop", "商店"),
]

# 单独出现的词（前后要有边界）
ZH_WORDS = [
    ("Purple", "紫票"), ("Orange", "橙票"), ("Blue", "蓝票"), ("Red", "红票"),
    ("Crafter", "巧手"), ("Crafters", "巧手"), ("Gatherer", "大地"), ("Gatherers", "大地"),
    ("Scrip", "票据"), ("Scrips", "票据"),
    ("Allowance", "兑换"), ("Vendor", "商人"), ("Trader", "商人"),
    ("Clan", "部队"), ("Grand Company", "军队"),
]

ROMAN = {"I": "一", "II": "二", "III": "三", "IV": "四", "V": "五",
         "VI": "六", "VII": "七", "VIII": "八", "IX": "九", "X": "十"}

ZH_ENUM = {"ZH": "中文", "JA": "日文", "EN": "英文", "DE": "德文", "FR": "法文"}


def localize_shop(name: str, lang: str) -> str:
    """把 Garland 的英文商店名翻成目标语言。

    lang = 'zh' 时按词典翻译；'ja'/'en' 直接返回原名（Garland 本身就是英文/
    日文文档里的商店名也是英文，没有更好的日文来源）。
    """
    if not name:
        return ""
    if lang != "zh":
        return name

    s = name
    # 1) 先替换长词组（避免 "Scrip Exchange" 被拆开）。
    #    必须加字母边界：否则 "Materia" 会先吃掉 "Materials" 的前缀，变成「魔晶石ls」
    for en, zh in ZH_PHRASES:
        s = re.sub(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])", zh, s, flags=re.IGNORECASE)
    # 2) 再替换单词。用一个「先匹配全部、再统一替换」的写法，
    #    避免「Materials → 素材」之后又被「Materia」二次替换成「魔晶石ls」这种串味
    for en, zh in sorted(ZH_WORDS, key=lambda kv: -len(kv[0])):
        s = re.sub(r"(?<![A-Za-z])" + re.escape(en) + r"(?![A-Za-z])", zh, s)
    # 3) Lv. 90 / IL 570 换个写法
    s = re.sub(r"\bLv\.?\s*(\d+)", r"\1级", s, flags=re.IGNORECASE)
    s = re.sub(r"\bIL\s*(\d+)", r"\1品级", s, flags=re.IGNORECASE)
    # 4) 括号内用中文全角括号，并清掉括号内外的多余空格
    s = s.replace("(", "（").replace(")", "）")
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"\s*（\s*", "（", s)
    s = re.sub(r"\s*）", "）", s)
    return s
