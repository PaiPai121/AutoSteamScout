"""Conservative product identity matching with optional user-maintained aliases."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .domain import Offer


PROMO = re.compile(r"[【\[](?:特惠|促销|史低|秒杀|优惠|限时)[^】\]]*[】\]]", re.I)
CHANNEL = re.compile(r"(?<![a-z0-9])(?:steam\s*(?:key|版)?|cd\s*key|digital\s*key)(?![a-z0-9])|激活码|数字版|现货|秒发", re.I)
EDITION_PATTERNS = (
    ("ultimate", re.compile(r"(?<![a-z0-9])(?:ultimate\s+edition|edition\s+ultimate)(?![a-z0-9])|(?<![a-z0-9])ultimate\s*$|终极版", re.I)),
    ("deluxe", re.compile(r"(?<![a-z0-9])(?:deluxe\s+edition|edition\s+deluxe)(?![a-z0-9])|(?<![a-z0-9])deluxe\s*$|豪华版", re.I)),
    ("gold", re.compile(r"(?<![a-z0-9])(?:gold\s+edition|edition\s+gold)(?![a-z0-9])|(?<![a-z0-9])gold\s*$|黄金版", re.I)),
    ("complete", re.compile(r"(?<![a-z0-9])(?:complete\s+edition|edition\s+complete)(?![a-z0-9])|(?<![a-z0-9])complete\s*$|完整版", re.I)),
    ("directors_cut", re.compile(r"director['’]?s\s+cut|导演剪辑版", re.I)),
    ("goty", re.compile(r"game\s+of\s+the\s+year(?:\s+edition)?|(?<![a-z0-9])goty(?![a-z0-9])|年度版", re.I)),
    ("definitive", re.compile(r"definitive(?:\s+edition)?|最终版|决定版", re.I)),
    ("remastered", re.compile(r"remaster(?:ed)?|高清版", re.I)),
    ("remake", re.compile(r"(?<![a-z0-9])remake(?![a-z0-9])|重制版", re.I)),
    ("standard", re.compile(r"(?<![a-z0-9])(?:standard\s+edition|edition\s+standard)(?![a-z0-9])|(?<![a-z0-9])standard\s*$|标准版", re.I)),
    ("royal", re.compile(r"(?<![a-z0-9])(?:royal\s+edition|edition\s+royal)(?![a-z0-9])|皇家版", re.I)),
    ("premium", re.compile(r"(?<![a-z0-9])(?:premium\s+edition|edition\s+premium)(?![a-z0-9])|高级版", re.I)),
)
NON_BASE = re.compile(r"(?<![a-z0-9])(?:dlc|soundtrack|bundle|season\s*pass)(?![a-z0-9])|原声|合集|季票|升级包|扩展包", re.I)
DLC = re.compile(r"(?<![a-z0-9])dlc(?![a-z0-9])|追加内容|扩展包", re.I)
SOUNDTRACK = re.compile(r"(?<![a-z0-9])soundtrack(?![a-z0-9])|原声", re.I)
BUNDLE = re.compile(r"(?<![a-z0-9])bundle(?![a-z0-9])|合集", re.I)
SEASON_PASS = re.compile(r"(?<![a-z0-9])season\s*pass(?![a-z0-9])|季票", re.I)
UPGRADE = re.compile(r"(?<![a-z0-9])upgrade(?![a-z0-9])|升级包", re.I)
BILINGUAL_SUFFIX = re.compile(r"^(.+?)\s*[（(]([^()（）]+)[）)]\s*$")
SUBTITLE_BREAK = re.compile(r"\s+[-–—:：]\s*|\s*[-–—:：]\s+")
PUNCT = re.compile(r"[\W_]+", re.UNICODE)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _clean_title(title: str) -> str:
    text = CHANNEL.sub(" ", PROMO.sub(" ", _fold(title)))
    return re.sub(r"[（(]\s*[）)]", " ", text).strip(" -:：|()（） ")


def search_name(title: str) -> str:
    text = _clean_title(title)
    for _, pattern in EDITION_PATTERNS:
        text = pattern.sub(" ", text)
    return " ".join(text.split()).strip(" -:：|()（）")


def identity(title: str) -> tuple[str, str, bool]:
    text = _clean_title(title)
    special = bool(NON_BASE.search(text))
    editions = [label for label, pattern in EDITION_PATTERNS if pattern.search(text)]
    edition = editions[0] if len(editions) == 1 else ("mixed" if editions else "standard")
    core = PUNCT.sub("", search_name(title))
    return core, edition, special


def _content_kind(title: str) -> str:
    text = _fold(title)
    for kind, pattern in (("soundtrack", SOUNDTRACK), ("bundle", BUNDLE),
                          ("season_pass", SEASON_PASS), ("upgrade", UPGRADE),
                          ("dlc", DLC)):
        if pattern.search(text):
            return kind
    return "base"


def _historical_core(title: str) -> str:
    if _content_kind(title) == "dlc":
        return PUNCT.sub("", DLC.sub(" ", search_name(title)))
    return identity(title)[0]


def _bilingual_parts(title: str) -> tuple[str, str] | None:
    match = BILINGUAL_SUFFIX.fullmatch(title.strip())
    if not match:
        return None
    outer, inner = (part.strip() for part in match.groups())
    outer_chinese, inner_chinese = bool(re.search(r"[\u3400-\u9fff]", outer)), bool(re.search(r"[\u3400-\u9fff]", inner))
    if outer_chinese == inner_chinese or not re.search(r"[a-zA-Z]{3}", outer + inner):
        return None
    if identity(outer)[1:] != identity(inner)[1:]:
        return None
    return outer, inner


def _has_specific_subtitle(title: str) -> bool:
    parts = SUBTITLE_BREAK.split(search_name(title), maxsplit=1)
    return len(parts) == 2 and len(PUNCT.sub("", parts[1])) >= 3


class TitleCatalog:
    def __init__(self, groups: dict[str, list[str]] | None = None):
        self._canonical: dict[str, str] = {}
        self._historical_canonical: dict[tuple[str, str], str] = {}
        self._groups: dict[str, tuple[str, ...]] = {}
        for canonical, aliases in (groups or {}).items():
            names = (canonical, *aliases)
            canonical_key = identity(canonical)[0]
            if not canonical_key:
                raise ValueError("别名组的标准名称不能为空")
            for name in names:
                key = identity(name)[0]
                previous = self._canonical.get(key)
                if not key or (previous is not None and previous != canonical_key):
                    raise ValueError(f"别名冲突: {name}")
                self._canonical[key] = canonical_key
                historical_key = (_content_kind(name), _historical_core(name))
                historical_previous = self._historical_canonical.get(historical_key)
                if not historical_key[1] or (historical_previous is not None
                                              and historical_previous != canonical_key):
                    raise ValueError(f"别名冲突: {name}")
                self._historical_canonical[historical_key] = canonical_key
            self._groups[canonical_key] = names

    @classmethod
    def from_file(cls, path: Path) -> "TitleCatalog":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(
            not isinstance(key, str) or not isinstance(value, list) or
            not all(isinstance(item, str) for item in value)
            for key, value in data.items()
        ):
            raise ValueError("别名文件需要 {\"标准名称\": [\"别名\"]} 格式")
        return cls(data)

    def compare(self, left: str, right: str) -> tuple[bool, str]:
        left_core, left_edition, left_special = identity(left)
        right_core, right_edition, right_special = identity(right)
        if not left_core or not right_core:
            return False, "商品名称为空，无法核对"
        if left_special or right_special:
            return False, "DLC、原声、合集或升级内容需要人工核对"
        if left_edition != right_edition or left_edition == "mixed":
            return False, f"版本不一致：{left_edition} / {right_edition}"
        left_key = self._canonical.get(left_core, left_core)
        right_key = self._canonical.get(right_core, right_core)
        if left_core == right_core and re.findall(r"\d+", search_name(left)) != re.findall(r"\d+", search_name(right)):
            return False, "商品名称中的编号不一致"
        if left_key != right_key:
            return False, "商品名称不同，未配置可信别名"
        return True, "名称和版本一致" if left_core == right_core else "已按配置的别名核对名称和版本"

    def compare_historical(self, left: str, right: str) -> tuple[bool, str]:
        """Match purchased and sold products; retain the exact add-on subtitle."""
        left_core, left_edition, _ = identity(left)
        right_core, right_edition, _ = identity(right)
        if not left_core or not right_core:
            return False, "商品名称为空，无法核对"
        if left_edition != right_edition or left_edition == "mixed":
            return False, "版本不一致"
        left_kind, right_kind = _content_kind(left), _content_kind(right)
        if left_kind != right_kind and {left_kind, right_kind} != {"base", "dlc"}:
            return False, "商品内容类型不一致"
        left_core = _historical_core(left)
        right_core = _historical_core(right)
        if not left_core or not right_core:
            return False, "商品名称为空，无法核对"
        left_alias = self._historical_canonical.get((left_kind, left_core))
        right_alias = self._historical_canonical.get((right_kind, right_core))
        if left_core != right_core and (not left_alias or left_alias != right_alias):
            return False, "完整商品名称不同"
        if left_kind != right_kind:
            unmarked = left if left_kind == "base" else right
            if not _has_specific_subtitle(unmarked):
                return False, "DLC 标记不一致，缺少可核对的完整副标题"
            return True, "完整副标题一致；一侧省略了 DLC 标记"
        if left_kind == "dlc":
            if not _has_specific_subtitle(left) or not _has_specific_subtitle(right):
                return False, "DLC 缺少可核对的完整副标题"
            return True, "DLC 完整名称和版本一致"
        return True, "名称和版本一致" if left_core == right_core else "已按确认的别名核对名称和版本"

    @staticmethod
    def _historical_names(primary: str, alternatives: tuple[str, ...]) -> tuple[str, ...]:
        names = [primary]
        for name in (primary, *alternatives):
            if name != primary and not TitleCatalog._compatible_historical_official_name(primary, name):
                continue
            parts = _bilingual_parts(name)
            names.append(parts[0] if parts else name)
            if name != primary:
                names.append(name)
        return tuple(dict.fromkeys(names))

    def compare_historical_products(self, buy_title: str, buy_alternatives: tuple[str, ...],
                                    sale_title: str, sale_alternatives: tuple[str, ...]) -> tuple[bool, str]:
        buy_names = self._historical_names(buy_title, buy_alternatives)
        sale_names = self._historical_names(sale_title, sale_alternatives)
        for buy_name in buy_names:
            for sale_name in sale_names:
                matched, reason = self.compare_historical(buy_name, sale_name)
                if not matched:
                    continue
                # A platform-supplied parenthetical translation is useful only if
                # it does not contradict an official name in the same script.
                for parts, other_names in ((_bilingual_parts(buy_title), sale_names),
                                           (_bilingual_parts(sale_title), buy_names)):
                    if not parts:
                        continue
                    outer, inner = parts
                    other_script_names = [name for name in other_names
                                          if bool(re.search(r"[\u3400-\u9fff]", name))
                                          == bool(re.search(r"[\u3400-\u9fff]", inner))]
                    if other_script_names and not any(
                            self.compare_historical(inner, name)[0]
                            for name in other_script_names):
                        break
                else:
                    if _bilingual_parts(buy_title) or _bilingual_parts(sale_title):
                        reason = f"双语括号名称已核对；{reason}"
                    return True, reason
        return False, "没有名称和版本一致的杉果购买明细"

    @staticmethod
    def _compatible_historical_official_name(primary: str, alternate: str) -> bool:
        return (identity(primary)[1] == identity(alternate)[1] != "mixed"
                and _content_kind(primary) == _content_kind(alternate))

    def search_terms(self, title: str) -> tuple[str, ...]:
        name = search_name(title)
        key = identity(title)[0]
        canonical = self._canonical.get(key)
        group = self._groups.get(canonical, ()) if canonical else ()
        terms = [name, *(search_name(item) for item in group)]
        return tuple(dict.fromkeys(term for term in terms if term))

    def offer_search_terms(self, offer: "Offer") -> tuple[str, ...]:
        terms = [*self.search_terms(offer.title)]
        for alternate in offer.alternate_titles:
            if self._compatible_official_name(offer.title, alternate):
                terms.extend(self.search_terms(alternate))
        return tuple(dict.fromkeys(terms))

    def compare_offer(self, offer: "Offer", market_title: str) -> tuple[bool, str]:
        if any(not self._compatible_official_name(offer.title, alternate)
               for alternate in offer.alternate_titles):
            return False, "杉果官方名称的版本或内容类型不一致，需要人工核对"
        same, reason = self.compare(offer.title, market_title)
        if same:
            return same, reason
        for alternate in offer.alternate_titles:
            if not self._compatible_official_name(offer.title, alternate):
                continue
            matched, _ = self.compare(alternate, market_title)
            if matched:
                return True, "杉果官方多语言名称与市场名称、版本一致"
        return False, reason

    @staticmethod
    def _compatible_official_name(primary: str, alternate: str) -> bool:
        _, primary_edition, primary_special = identity(primary)
        _, alternate_edition, alternate_special = identity(alternate)
        return (primary_edition == alternate_edition != "mixed"
                and not primary_special and not alternate_special)
