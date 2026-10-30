"""환경변수 기반 설정. .env 파일이 있으면 읽는다 (덮어쓰지 않음)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from .regions import ALL_SGG


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


_load_dotenv(Path(os.environ.get("KRE_ENV_FILE", ".env")))


def _list(env: str, default: list[str]) -> list[str]:
    raw = os.environ.get(env, "").strip()
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # 공공데이터포털 인증키. 인코딩된 키를 넣어도 디코딩해서 쓴다.
    data_go_kr_key: str = field(default_factory=lambda: unquote(os.environ.get("DATA_GO_KR_KEY", "")))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

    # 수집 범위
    regions: list[str] = field(default_factory=lambda: _list("REGIONS", list(ALL_SGG.keys())))   # 실거래 수집: 서울+경기 전부
    prop_types: list[str] = field(default_factory=lambda: _list("PROP_TYPES", ["apt", "rh", "sh", "offi"]))
    months_back: int = field(default_factory=lambda: int(os.environ.get("MONTHS_BACK", "2")))
    # 아파트 엔드포인트: dev(상세) | basic
    apt_endpoint: str = field(default_factory=lambda: os.environ.get("MOLIT_APT_ENDPOINT", "dev"))

    # 호출 예산 (개발계정 일 1,000건). 넘으면 남은 조합은 다음 날로.
    max_calls: int = field(default_factory=lambda: int(os.environ.get("MAX_CALLS", "900")))
    num_rows: int = field(default_factory=lambda: int(os.environ.get("NUM_ROWS", "1000")))
    request_timeout: float = field(default_factory=lambda: float(os.environ.get("REQUEST_TIMEOUT", "20")))
    sleep_between_calls: float = field(default_factory=lambda: float(os.environ.get("SLEEP_BETWEEN_CALLS", "0.15")))
    collect_workers: int = field(default_factory=lambda: int(os.environ.get("COLLECT_WORKERS", "1")))  # 유형별 병렬 스레드 — 동시 연결이 많으면 data.go.kr 이 연결을 끊는다(시간초과) → 1

    # 이슈 규칙
    tx_spike_min: int = field(default_factory=lambda: int(os.environ.get("TX_SPIKE_MIN", "5")))
    tx_spike_pct: float = field(default_factory=lambda: float(os.environ.get("TX_SPIKE_PCT", "30")))
    tx_spike_max: int = field(default_factory=lambda: int(os.environ.get("TX_SPIKE_MAX", "10")))      # 하루 거래 급증 카드 상한
    complex_min: int = field(default_factory=lambda: int(os.environ.get("COMPLEX_MIN", "5")))
    issue_ttl_hours: int = field(default_factory=lambda: int(os.environ.get("ISSUE_TTL_HOURS", "24")))
    related_leads_max: int = field(default_factory=lambda: int(os.environ.get("RELATED_LEADS_MAX", "20")))
    # 첫 수집 직후엔 모든 계약이 '오늘 뜬 계약'이라 신호가 가짜다. 이 기간은 tx/단지 이슈를 만들지 않는다.
    bootstrap_quiet_days: int = field(default_factory=lambda: int(os.environ.get("BOOTSTRAP_QUIET_DAYS", "7")))
    # 활성 지역 (카드·리드·순위): 서울 전부 + 경기 타겟. TARGET_SGG 비면 거래 상위 GYEONGGI_TARGET_N개 자동
    target_sgg: list[str] = field(default_factory=lambda: _list("TARGET_SGG", []))
    gyeonggi_target_n: int = field(default_factory=lambda: int(os.environ.get("GYEONGGI_TARGET_N", "8")))
    recent_top_dongs_gg: int = field(default_factory=lambda: int(os.environ.get("RECENT_TOP_DONGS_GG", "6")))       # 경기 타겟 리더보드 동네 수
    recent_top_complexes_gg: int = field(default_factory=lambda: int(os.environ.get("RECENT_TOP_COMPLEXES_GG", "4")))
    # 계약일 기준 '최근 2주 거래 많은 동네/단지' (첫날부터 유효한 신호). 상위 N개, 카드 수명, 같은 카드 재등장 간격
    recent_top_dongs: int = field(default_factory=lambda: int(os.environ.get("RECENT_TOP_DONGS", "8")))
    recent_top_complexes: int = field(default_factory=lambda: int(os.environ.get("RECENT_TOP_COMPLEXES", "5")))
    recent_complex_min: int = field(default_factory=lambda: int(os.environ.get("RECENT_COMPLEX_MIN", "3")))
    recent_ttl_days: int = field(default_factory=lambda: int(os.environ.get("RECENT_TTL_DAYS", "7")))
    recent_repeat_days: int = field(default_factory=lambda: int(os.environ.get("RECENT_REPEAT_DAYS", "7")))
    issues_refresh: bool = field(default_factory=lambda: os.environ.get("ISSUES_REFRESH", "").lower() in ("1", "true", "yes"))

    # 메일 (Gmail 앱 비밀번호)
    gmail_user: str = field(default_factory=lambda: os.environ.get("GMAIL_USER", ""))
    gmail_app_password: str = field(default_factory=lambda: os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", ""))
    gmail_sender_name: str = field(default_factory=lambda: os.environ.get("GMAIL_SENDER_NAME", ""))
    mail_daily_limit: int = field(default_factory=lambda: int(os.environ.get("MAIL_DAILY_LIMIT", "100")))
    # true면 실제 발송 없이 로그만. 자격 정보가 없으면 자동으로 dry-run.
    mail_dry_run: bool = field(default_factory=lambda: os.environ.get("MAIL_DRY_RUN", "").lower() in ("1", "true", "yes")
                               or not (os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD")))
    mail_gaps_days: list[int] = field(default_factory=lambda: [int(x) for x in _list("MAIL_GAPS_DAYS", ["3", "4", "7"])])

    # 외부 신호 수집 (enrich)
    kakao_rest_key: str = field(default_factory=lambda: os.environ.get("KAKAO_REST_KEY", ""))
    naver_client_id: str = field(default_factory=lambda: os.environ.get("NAVER_CLIENT_ID", ""))
    naver_client_secret: str = field(default_factory=lambda: os.environ.get("NAVER_CLIENT_SECRET", ""))
    # NAVER API HUB(네이버클라우드) 키면 true(기본). 개발자센터 구키면 NAVER_HUB=false
    naver_hub: bool = field(default_factory=lambda: os.environ.get("NAVER_HUB", "true").lower() not in ("0", "false", "no"))
    kakao_daily_budget: int = field(default_factory=lambda: int(os.environ.get("KAKAO_DAILY_BUDGET", "3000")))
    naver_daily_budget: int = field(default_factory=lambda: int(os.environ.get("NAVER_DAILY_BUDGET", "1500")))      # 블로그 노출 수
    naver_local_budget: int = field(default_factory=lambda: int(os.environ.get("NAVER_LOCAL_BUDGET", "1500")))      # 지역검색 상위 5
    enrich_extra_regions: int = field(default_factory=lambda: int(os.environ.get("ENRICH_EXTRA_REGIONS", "15")))  # 이슈 없는 날 미리 채울 상위 동네 수
    enrich_refresh_days: int = field(default_factory=lambda: int(os.environ.get("ENRICH_REFRESH_DAYS", "90")))
    # 제휴·금융 리드 수집 (places): 동네마다 세무사·법무사·은행 지점 — 카카오 호출 예산, 갱신 주기, 검색 반경
    places_daily_budget: int = field(default_factory=lambda: int(os.environ.get("PLACES_DAILY_BUDGET", "6000")))
    places_refresh_days: int = field(default_factory=lambda: int(os.environ.get("PLACES_REFRESH_DAYS", "365")))
    places_radius_m: int = field(default_factory=lambda: int(os.environ.get("PLACES_RADIUS_M", "1200")))
    # 공개정보 메일 수집 (emails): 하루 리드 수, 페이지 fetch 예산, 재시도 주기, 자동 채택/후보 점수
    email_daily_leads: int = field(default_factory=lambda: int(os.environ.get("EMAIL_DAILY_LEADS", "600")))
    email_page_budget: int = field(default_factory=lambda: int(os.environ.get("EMAIL_PAGE_BUDGET", "2400")))
    email_refresh_days: int = field(default_factory=lambda: int(os.environ.get("EMAIL_REFRESH_DAYS", "365")))
    email_min_auto: int = field(default_factory=lambda: int(os.environ.get("EMAIL_MIN_AUTO", "60")))
    email_min_candidate: int = field(default_factory=lambda: int(os.environ.get("EMAIL_MIN_CANDIDATE", "40")))
    # proposal(기본): 제안·문의 맥락 메일만 · off: 수집 안 함 · quarantine: 자동으로 붙은 메일 격리
    email_mode: str = field(default_factory=lambda: os.environ.get("EMAIL_MODE", "proposal").lower())
    email_workers: int = field(default_factory=lambda: int(os.environ.get("EMAIL_WORKERS", "12")))
    email_daily_regions: int = field(default_factory=lambda: int(os.environ.get("EMAIL_DAILY_REGIONS", "40")))
    # 초기 스윕: 리드를 N 조각으로 나눠 동시에 (EMAIL_SHARDS, EMAIL_SHARD), EMAIL_LIGHT=1 이면 리드당 네이버 검색 3회로 줄임
    email_shards: int = field(default_factory=lambda: int(os.environ.get("EMAIL_SHARDS", "1")))
    email_shard: int = field(default_factory=lambda: int(os.environ.get("EMAIL_SHARD", "0")))
    email_naver_daily_cap: int = field(default_factory=lambda: int(os.environ.get("EMAIL_NAVER_DAILY_CAP", "10000")))   # 하루 2.5만 중 메일 수집 몫
    email_light: bool = field(default_factory=lambda: os.environ.get("EMAIL_LIGHT", "").lower() in ("1", "true", "yes"))
    rank_leads_max: int = field(default_factory=lambda: int(os.environ.get("RANK_LEADS_MAX", "20")))        # 중개
    rank_partner_max: int = field(default_factory=lambda: int(os.environ.get("RANK_PARTNER_MAX", "10")))    # 제휴 (세무사·조합·법무사·플랫폼·인플루언서)
    # 2금융 지점을 본점 하나로 묶기 (끄려면 RANK_FINANCE_HQ=0 — .trigger 에 nohq)
    rank_finance_hq: bool = field(default_factory=lambda: os.environ.get("RANK_FINANCE_HQ", "1").lower() not in ("0", "false", "no"))
    rank_finance_max: int = field(default_factory=lambda: int(os.environ.get("RANK_FINANCE_MAX", "10")))    # 금융 (은행 지점 등)

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = {"data_go_kr_key": "DATA_GO_KR_KEY", "database_url": "DATABASE_URL"}
            raise SystemExit("환경변수 누락: " + ", ".join(env_names.get(m, m.upper()) for m in missing))


settings = Settings()
