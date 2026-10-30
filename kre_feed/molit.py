"""국토교통부 실거래가 공개 API (공공데이터포털) 클라이언트 + 파서.

4종: apt(아파트) / rh(연립다세대) / sh(단독·다가구) / offi(오피스텔) 매매.
파라미터: serviceKey, LAWD_CD(시군구 5자리), DEAL_YMD(YYYYMM), pageNo, numOfRows.
응답 필드명은 2024년 개편 후 영문(camelCase)이지만, 구 API의 한글 필드명도 함께 매핑한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Iterator

import requests

log = logging.getLogger(__name__)

ENDPOINTS = {
    "apt_dev": "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev/getRTMSDataSvcAptTradeDev",
    "apt_basic": "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade",
    "rh": "https://apis.data.go.kr/1613000/RTMSDataSvcRHTrade2/getRTMSDataSvcRHTrade2",   # 2025 개편: 끝에 2
    "sh": "https://apis.data.go.kr/1613000/RTMSDataSvcSHTrade/getRTMSDataSvcSHTrade",
    "offi": "https://apis.data.go.kr/1613000/RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade",
}

OK_CODES = {"000", "00"}
NODATA_CODES = {"03", "NODATA_ERROR"}
QUOTA_CODES = {"22", "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR"}
AUTH_CODES = {"30", "31", "32", "33", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "DEADLINE_HAS_EXPIRED_ERROR",
              "UNREGISTERED_IP_ERROR", "UNSIGNED_CALL_ERROR"}

# 캐노니컬 필드 → 후보 태그명 (영문 신규 / 한글 구버전)
FIELD_MAP: dict[str, list[str]] = {
    "sgg_cd": ["sggCd", "지역코드"],
    "umd_cd": ["umdCd", "법정동읍면동코드"],
    "umd_nm": ["umdNm", "법정동"],
    "jibun": ["jibun", "지번"],
    "apt_nm": ["aptNm", "아파트"],
    "rh_nm": ["mhouseNm", "연립다세대"],
    "offi_nm": ["offiNm", "단지"],
    "house_type": ["houseType", "주택유형"],
    "dong": ["aptDong", "동"],
    "exclu_use_ar": ["excluUseAr", "전용면적"],
    "land_ar": ["landAr", "대지권면적", "plottageAr", "대지면적"],
    "total_floor_ar": ["totalFloorAr", "연면적"],
    "floor": ["floor", "층"],
    "build_year": ["buildYear", "건축년도"],
    "deal_year": ["dealYear", "년"],
    "deal_month": ["dealMonth", "월"],
    "deal_day": ["dealDay", "일"],
    "deal_amount": ["dealAmount", "거래금액"],
    "dealing_gbn": ["dealingGbn", "거래유형"],
    "agent_sgg_nm": ["estateAgentSggNm", "중개사소재지"],
    "seller_gbn": ["slerGbn", "sellerGbn", "매도자"],
    "buyer_gbn": ["buyerGbn", "매수자"],
    "rgst_date": ["rgstDate", "등기일자"],
    "cancel_type": ["cdealType", "해제여부"],
    "cancel_day": ["cdealDay", "해제사유발생일"],
    "apt_seq": ["aptSeq", "일련번호"],
}


class MolitError(Exception):
    pass


class QuotaExceeded(MolitError):
    pass


class AuthError(MolitError):
    pass


@dataclass
class Trade:
    trade_key: str
    prop_type: str
    sgg_cd: str
    umd_cd: str | None
    umd_nm: str | None
    region_key: str
    jibun: str | None
    name: str | None
    dong: str | None
    exclu_use_ar: float | None
    land_ar: float | None
    total_floor_ar: float | None
    floor: int | None
    build_year: int | None
    deal_date: date
    deal_amount: int
    dealing_gbn: str | None
    agent_sgg_nm: str | None
    seller_gbn: str | None
    buyer_gbn: str | None
    rgst_date: date | None
    cancel_flag: bool
    cancel_date: date | None
    apt_seq: str | None
    raw: dict

    def as_row(self) -> tuple:
        return (
            self.trade_key, self.prop_type, self.sgg_cd, self.umd_cd, self.umd_nm, self.region_key,
            self.jibun, self.name, self.dong, self.exclu_use_ar, self.land_ar, self.total_floor_ar,
            self.floor, self.build_year, self.deal_date, self.deal_amount, self.dealing_gbn,
            self.agent_sgg_nm, self.seller_gbn, self.buyer_gbn, self.rgst_date, self.cancel_flag,
            self.cancel_date, self.apt_seq, json.dumps(self.raw, ensure_ascii=False),
        )


# ---------- 파싱 유틸 ----------

def _pick(item: dict, field: str) -> str | None:
    for tag in FIELD_MAP[field]:
        v = item.get(tag)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def _num(v: str | None) -> float | None:
    if v is None:
        return None
    s = v.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _int(v: str | None) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None


def _amount(v: str | None) -> int | None:
    """'12,500' → 12500 (만원)."""
    if v is None:
        return None
    s = re.sub(r"[^\d]", "", v)
    return int(s) if s else None


def parse_loose_date(v: str | None) -> date | None:
    """'24.03.15' | '2024-03-15' | '20240315' | '2024.03.15' → date."""
    if not v:
        return None
    s = v.strip()
    m = re.fullmatch(r"(\d{2})\.(\d{1,2})\.(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return date(2000 + y, mo, d)
    m = re.fullmatch(r"(\d{4})[.\-/]?(\d{1,2})[.\-/]?(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return date(y, mo, d)
    return None


def item_to_dict(item: ET.Element) -> dict:
    return {child.tag: (child.text or "").strip() for child in item}


def parse_item(prop_type: str, item: dict, sgg_cd_hint: str | None = None) -> Trade | None:
    sgg_cd = _pick(item, "sgg_cd") or sgg_cd_hint
    if not sgg_cd:
        return None
    y, m, d = _int(_pick(item, "deal_year")), _int(_pick(item, "deal_month")), _int(_pick(item, "deal_day"))
    amount = _amount(_pick(item, "deal_amount"))
    if not (y and m and d and amount):
        return None
    deal_date = date(y, m, d)

    if prop_type == "apt":
        name = _pick(item, "apt_nm")
    elif prop_type == "rh":
        name = _pick(item, "rh_nm")
    elif prop_type == "offi":
        name = _pick(item, "offi_nm")
    else:  # sh: 단독·다가구는 이름이 없고 주택유형만 있다
        name = None

    umd_nm = _pick(item, "umd_nm")
    region_key = f"{sgg_cd}|{umd_nm or ''}"
    jibun = _pick(item, "jibun")
    dong = _pick(item, "dong") if prop_type == "apt" else None
    exclu = _num(_pick(item, "exclu_use_ar"))
    floor = _int(_pick(item, "floor"))
    cancel_type = (_pick(item, "cancel_type") or "").upper()
    cancel_flag = cancel_type in {"O", "Y", "해제"}

    ident = "|".join(str(x) for x in (
        prop_type, sgg_cd, umd_nm, jibun, name, dong, exclu,
        _num(_pick(item, "total_floor_ar")), deal_date.isoformat(), amount, floor,
    ))
    trade_key = hashlib.sha1(ident.encode("utf-8")).hexdigest()

    return Trade(
        trade_key=trade_key,
        prop_type=prop_type,
        sgg_cd=sgg_cd,
        umd_cd=_pick(item, "umd_cd"),
        umd_nm=umd_nm,
        region_key=region_key,
        jibun=jibun,
        name=name,
        dong=dong,
        exclu_use_ar=exclu,
        land_ar=_num(_pick(item, "land_ar")),
        total_floor_ar=_num(_pick(item, "total_floor_ar")),
        floor=floor,
        build_year=_int(_pick(item, "build_year")),
        deal_date=deal_date,
        deal_amount=amount,
        dealing_gbn=_pick(item, "dealing_gbn"),
        agent_sgg_nm=_pick(item, "agent_sgg_nm"),
        seller_gbn=_pick(item, "seller_gbn"),
        buyer_gbn=_pick(item, "buyer_gbn"),
        rgst_date=parse_loose_date(_pick(item, "rgst_date")),
        cancel_flag=cancel_flag,
        cancel_date=parse_loose_date(_pick(item, "cancel_day")),
        apt_seq=_pick(item, "apt_seq"),
        raw=item,
    )


def parse_response(xml_text: str) -> tuple[str, str, list[dict], int]:
    """→ (resultCode, resultMsg, items, totalCount)"""
    root = ET.fromstring(xml_text)
    # 정상 응답은 response/header/resultCode, 게이트웨이 오류는 OpenAPI_ServiceResponse/cmmMsgHeader/returnReasonCode
    code = (root.findtext("./header/resultCode") or root.findtext(".//resultCode")
            or root.findtext(".//returnReasonCode") or "").strip()
    msg = (root.findtext("./header/resultMsg") or root.findtext(".//resultMsg")
           or root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or "").strip()
    items = [item_to_dict(it) for it in root.iter("item")]
    total_raw = root.findtext("./body/totalCount") or root.findtext(".//totalCount") or "0"
    total = _int(total_raw) or 0
    return code, msg, items, total


# ---------- 클라이언트 ----------

class MolitClient:
    def __init__(self, service_key: str, *, apt_endpoint: str = "dev", num_rows: int = 1000,
                 timeout: float = 20, sleep: float = 0.15, max_calls: int = 900,
                 session: requests.Session | None = None):
        if not service_key:
            raise AuthError("DATA_GO_KR_KEY 비어 있음")
        self.key = service_key
        self.apt_endpoint = ENDPOINTS["apt_dev" if apt_endpoint == "dev" else "apt_basic"]
        self.num_rows = num_rows
        self.timeout = timeout
        self.sleep = sleep
        self.max_calls = max_calls
        self.calls = 0
        self.session = session or requests.Session()

    def endpoint(self, prop_type: str) -> str:
        return self.apt_endpoint if prop_type == "apt" else ENDPOINTS[prop_type]

    def budget_left(self) -> int:
        return self.max_calls - self.calls

    def _get(self, prop_type: str, lawd_cd: str, deal_ymd: str, page: int) -> str:
        if self.calls >= self.max_calls:
            raise QuotaExceeded(f"일일 호출 예산 {self.max_calls} 소진")
        params = {
            "serviceKey": self.key,
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "pageNo": page,
            "numOfRows": self.num_rows,
        }
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                self.calls += 1
                r = self.session.get(self.endpoint(prop_type), params=params, timeout=self.timeout)
                if r.status_code >= 500:
                    raise MolitError(f"HTTP {r.status_code}")
                r.encoding = "utf-8"
                return r.text
            except (requests.RequestException, MolitError) as e:  # 재시도
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise MolitError(f"요청 실패 {prop_type} {lawd_cd} {deal_ymd} p{page}: " + re.sub(r"serviceKey=[^&\s]+", "serviceKey=***", str(last_err)))   # 키는 로그에 남기지 않는다

    def fetch(self, prop_type: str, lawd_cd: str, deal_ymd: str) -> Iterator[Trade]:
        """한 (유형, 시군구, 년월) 조합의 전 페이지를 순회하며 Trade를 낸다."""
        page = 1
        seen = 0
        while True:
            text = self._get(prop_type, lawd_cd, deal_ymd, page)
            code, msg, items, total = parse_response(text)
            if code in QUOTA_CODES:
                raise QuotaExceeded(f"공공데이터포털 쿼터 초과: {msg}")
            if code in AUTH_CODES:
                raise AuthError(f"인증 오류({code}): {msg}")
            if code in NODATA_CODES or (code in OK_CODES and not items):
                return
            if code not in OK_CODES:
                raise MolitError(f"API 오류({code}): {msg}")
            for it in items:
                t = parse_item(prop_type, it, sgg_cd_hint=lawd_cd)
                if t:
                    yield t
            seen += len(items)
            if seen >= total or len(items) < self.num_rows:
                return
            page += 1
            time.sleep(self.sleep)

    def fetch_many(self, combos: Iterable[tuple[str, str, str]]) -> Iterator[tuple[tuple[str, str, str], list[Trade] | None]]:
        """combos: (prop_type, lawd_cd, deal_ymd). 예산 소진 시 None을 내고 멈춘다.
        특정 유형 API가 인증 오류(키 미등록·승인 전)면 그 유형만 건너뛰고 계속 간다 → self.auth_failed."""
        self.auth_failed: set[str] = getattr(self, "auth_failed", set())
        self.net_failed: list[tuple[str, str, str]] = getattr(self, "net_failed", [])
        consecutive = 0
        for combo in combos:
            prop_type = combo[0]
            if prop_type in self.auth_failed:
                continue
            if self.budget_left() <= 0:
                log.warning("호출 예산 소진 — %s 부터 다음 실행으로", combo)
                yield combo, None
                return
            try:
                yield combo, list(self.fetch(*combo))
            except QuotaExceeded as e:
                log.warning("%s", e)
                yield combo, None
                return
            except AuthError as e:
                log.warning("%s API 인증 오류 — 이 유형은 오늘 건너뜀 (승인·키 동기화 확인): %s", prop_type, e)
                self.auth_failed.add(prop_type)
                continue
            except MolitError as e:   # 네트워크·5xx: 그 조합만 건너뛰고 계속. 연속 8번이면 API가 내려간 것 → 여기서 멈춤
                log.warning("%s — 건너뜀", e)
                self.net_failed.append(combo)
                consecutive += 1
                if consecutive >= 8:
                    log.error("data.go.kr 연속 %d회 실패 — 이 유형 수집 중단, 다음 실행에 이어서", consecutive)
                    yield combo, None
                    return
                time.sleep(5)
                continue
            consecutive = 0
            time.sleep(self.sleep)


def deal_ymds(today: date, months_back: int) -> list[str]:
    """이번 달 포함 months_back개월의 YYYYMM. 신고 지연(30일)과 해제 반영을 위해 매일 재수집."""
    out = []
    y, m = today.year, today.month
    for _ in range(months_back):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out
