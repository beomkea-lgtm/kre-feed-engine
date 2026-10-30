"""상호·단지명 정규화와 매칭.

- short_name: '주식회사미스터홈즈부동산중개' → '미스터홈즈', '고덕그라시움공인중개사사무소' → '고덕그라시움'
- complex_tokens: '래미안대치팰리스' → {'래미안대치팰리스', '대치팰리스'}, '헬리오시티' → {'헬리오시티'}
- lead_matches_complex: 상호/주소에 단지 토큰이 들어가면 ('name'|'address', 토큰)
"""
from __future__ import annotations

import re

_PREFIX = ["주식회사", "(주)", "㈜", "유한회사", "법무법인", "부동산"]
_SUFFIX = [
    "공인중개사사무소", "공인중개사무소", "공인중개사", "부동산중개법인그룹", "부동산중개법인", "부동산중개",
    "중개법인", "중개사무소", "공인", "부동산", "사무소", "컨설팅", "리얼티", "랜드", "하우징",
]
# 단지명 앞에 붙는 브랜드. 떼고 남은 핵심어로도 매칭한다.
_BRANDS = [
    "래미안", "자이", "힐스테이트", "푸르지오", "e편한세상", "이편한세상", "아이파크", "롯데캐슬", "더샵", "SK뷰", "에스케이뷰",
    "센트럴", "리버", "파크", "데시앙", "위브", "꿈에그린", "호반베르디움", "한양수자인", "우미린", "중흥S클래스",
    "포레나", "써밋", "디에이치", "아크로", "트리마제", "두산위브", "벽산", "현대", "삼성", "대우", "한신", "쌍용", "동부",
]
_GENERIC = {"강남", "서초", "송파", "강동", "마포", "서울", "한국", "대한", "우리", "하나", "행복", "믿음", "신뢰", "으뜸",
            "제일", "중앙", "현대", "삼성", "대우", "미래", "희망", "좋은", "스마트", "프라임", "베스트", "탑", "원", "일등",
            "부자", "명품", "황금", "금성", "태양", "다온", "리치", "굿", "더", "뉴", "성실", "정직", "착한"}


def _strip(s: str, parts: list[str]) -> str:
    changed = True
    while changed:
        changed = False
        for p in parts:
            if s.endswith(p) and len(s) > len(p):
                s = s[: -len(p)]
                changed = True
            if s.startswith(p) and len(s) > len(p):
                s = s[len(p):]
                changed = True
    return s


def normalize(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"[\s\-_.,·()\[\]【】]", "", s)
    return s.lower()


def short_name(name: str | None) -> str:
    """상호의 핵심어. 2자 이하거나 너무 일반적인 말이면 ''."""
    if not name:
        return ""
    s = re.sub(r"\s+", "", name)
    s = _strip(s, _PREFIX)
    s = _strip(s, _SUFFIX)
    s = re.sub(r"[()\[\]㈜·.\-_]", "", s)
    if len(s) < 2 or s in _GENERIC:
        return ""
    return s


def complex_tokens(cname: str | None) -> set[str]:
    """단지명 매칭 토큰. 브랜드 떼고 남은 핵심어(3자 이상)도 포함."""
    if not cname:
        return set()
    base = normalize(cname)
    base = re.sub(r"(아파트|apt|오피스텔|\d+단지|\d+차)$", "", base)
    toks = {base} if len(base) >= 2 else set()
    core = base
    for b in sorted(_BRANDS, key=len, reverse=True):
        bb = b.lower()
        if core.startswith(bb) and len(core) - len(bb) >= 3:
            core = core[len(bb):]
        if core.endswith(bb) and len(core) - len(bb) >= 3:
            core = core[: -len(bb)]
    if core != base and len(core) >= 3:
        toks.add(core)
    # 숫자 떼기 (상계주공7 → 상계주공)
    nod = re.sub(r"\d+$", "", core)
    if nod != core and len(nod) >= 3:
        toks.add(nod)
    return {t for t in toks if len(t) >= 2}


def lead_matches_complex(lead_name: str | None, lead_address: str | None, cname: str | None) -> tuple[str, str] | None:
    """('address'|'name', token) — 주소 매칭이 더 강하므로 먼저."""
    toks = complex_tokens(cname)
    if not toks:
        return None
    addr = normalize(lead_address)
    nm = normalize(lead_name)
    for t in sorted(toks, key=len, reverse=True):
        if addr and t in addr:
            return ("address", t)
    for t in sorted(toks, key=len, reverse=True):
        if nm and t in nm:
            return ("name", t)
    return None


def phone_digits(p: str | None) -> str:
    if not p:
        return ""
    d = re.sub(r"\D", "", p.split(" ")[0] if " " in p.strip() else p)
    if len(d) in (7, 8):  # 국번 없는 서울 번호 → 02 붙임
        d = "02" + d
    return d
