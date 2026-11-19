"""한국 기상청 단기예보 조회서비스로 날씨를 알려 주는 MCP 서버 — Streamable HTTP로 배포하는 원격 MCP 서버.

도구 2개
- get_korea_now(latitude, longitude): 지금 날씨 (초단기실황)
- get_korea_forecast(latitude, longitude, days): 앞으로 며칠 예보 (단기예보)

기상청 API는 위경도가 아니라 5km 격자 번호(nx, ny)를 받는다. 변환과 「가장 최근 발표 시각」 계산은 이 서버가 한다.

서비스키: 공공데이터포털 「기상청_단기예보 조회서비스」 활용신청으로 받은 키를 환경 변수 KMA_SERVICE_KEY로 넣는다
(배포 플랫폼의 시크릿 · 비밀 환경 변수로 넣는다). 인코딩 키·디코딩 키 모두 받는다.
키가 없어도 서버는 뜨고 도구 목록도 보인다 — 도구를 부르면 키가 없다고 알린다.

배포 설정: Streamable HTTP · 경로 /mcp · 포트 8000(MCP_PORT, PORT는 예약 키) ·
Host 헤더 검사 끔 · 읽기 전용 표시(readOnlyHint).
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("kr-weather")
# httpx는 INFO에서 요청 URL을 통째로 남긴다 — URL에 서비스키가 들어가므로 끈다
logging.getLogger("httpx").setLevel(logging.WARNING)

KMA_BASE = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
USER_AGENT = "kr-weather-mcp/1.0"
PORT = int(os.getenv("MCP_PORT", "8000"))
TIMEOUT = httpx.Timeout(15.0)
KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없다

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

mcp = FastMCP(
    "kr-weather",
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

SKY = {"1": "맑음", "3": "구름많음", "4": "흐림"}
PTY = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "4": "소나기", "5": "빗방울", "6": "빗방울눈날림", "7": "눈날림"}
DIRS = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동",
        "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서", "북"]
WEEKDAYS = "월화수목금토일"
RESULT_MSG = {
    "03": "자료가 없습니다",
    "10": "요청 값이 잘못됐습니다",
    "12": "서비스가 없거나 폐기됐습니다",
    "20": "서비스 접근이 거부됐습니다",
    "22": "하루 호출 한도를 넘었습니다",
    "30": "등록되지 않은 서비스키입니다",
    "31": "서비스키 사용 기한이 끝났습니다",
    "32": "등록되지 않은 IP입니다",
    "401": "서비스키 인증에 실패했습니다",
}


# ── 위경도 → 기상청 격자 (기상청 동네예보 격자 변환, Lambert Conformal Conic) ──────────────
def to_grid(lat: float, lon: float) -> tuple[int, int]:
    re_, grid = 6371.00877, 5.0
    slat1, slat2, olon, olat = 30.0, 60.0, 126.0, 38.0
    xo, yo = 43, 136
    rad = math.pi / 180.0

    r = re_ / grid
    s1, s2, ol, oa = slat1 * rad, slat2 * rad, olon * rad, olat * rad
    sn = math.log(math.cos(s1) / math.cos(s2)) / math.log(
        math.tan(math.pi * 0.25 + s2 * 0.5) / math.tan(math.pi * 0.25 + s1 * 0.5)
    )
    sf = math.tan(math.pi * 0.25 + s1 * 0.5) ** sn * math.cos(s1) / sn
    ro = r * sf / math.tan(math.pi * 0.25 + oa * 0.5) ** sn
    ra = r * sf / math.tan(math.pi * 0.25 + lat * rad * 0.5) ** sn
    theta = lon * rad - ol
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn
    nx = math.floor(ra * math.sin(theta) + xo + 0.5)
    ny = math.floor(ro - ra * math.cos(theta) + yo + 0.5)
    return nx, ny


def grid_or_error(lat: float, lon: float) -> tuple[tuple[int, int] | None, str | None]:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, "위도는 -90~90, 경도는 -180~180 범위로 넣어 주세요."
    nx, ny = to_grid(lat, lon)
    if not (1 <= nx <= 149 and 1 <= ny <= 253):
        return None, f"좌표 ({lat}, {lon})은(는) 한국 기상청 예보 범위 밖이라 조회할 수 없습니다."
    return (nx, ny), None


# ── 발표 시각 ──────────────────────────────────────────────────────────────────
def ncst_bases(now: datetime) -> list[datetime]:
    """초단기실황: 매시 정시 자료, 10분쯤 뒤부터 조회된다. 최근 시각부터 3개."""
    t = (now - timedelta(minutes=10)).replace(minute=0, second=0, microsecond=0)
    return [t - timedelta(hours=i) for i in range(3)]


VILAGE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)


def vilage_bases(now: datetime) -> list[datetime]:
    """단기예보: 02·05·08·11·14·17·20·23시 발표, 10분쯤 뒤부터 조회된다. 최근 발표부터 3개."""
    t = now - timedelta(minutes=10)
    day = t.replace(minute=0, second=0, microsecond=0)
    hour = max((h for h in VILAGE_HOURS if h <= t.hour), default=None)
    base = (day - timedelta(days=1)).replace(hour=23) if hour is None else day.replace(hour=hour)
    return [base - timedelta(hours=3 * i) for i in range(3)]


# ── 기상청 호출 ────────────────────────────────────────────────────────────────
class KmaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def service_key() -> str | None:
    key = os.getenv("KMA_SERVICE_KEY", "").strip()
    if not key:
        return None
    return unquote(key) if "%" in key else key  # 인코딩 키를 넣어도 두 번 인코딩되지 않게


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


async def kma_get(client: httpx.AsyncClient, operation: str, params: dict[str, str]) -> list[dict[str, Any]]:
    key = service_key()
    if not key:
        raise KmaError("NO_KEY", "기상청 서비스키(KMA_SERVICE_KEY)가 설정되지 않아 조회할 수 없습니다. 이 MCP 서버를 배포한 사람에게 알려 주세요.")
    query = {"serviceKey": key, "pageNo": "1", "numOfRows": "2000", "dataType": "JSON", **params}
    try:
        resp = await client.get(f"{KMA_BASE}/{operation}", params=query)
    except httpx.HTTPError as exc:
        raise KmaError("NET", f"기상청에 연결하지 못했습니다 ({type(exc).__name__}).") from exc

    try:
        data = resp.json()
    except ValueError:
        # 키 오류 등은 dataType=JSON이어도 XML이나 평문으로 온다
        text = resp.text
        code_m = re.search(r"<returnReasonCode>(\d+)</returnReasonCode>", text) or re.search(
            r"<resultCode>(\d+)</resultCode>", text
        )
        code = code_m.group(1) if code_m else str(resp.status_code)
        msg_m = re.search(r"<returnAuthMsg>([^<]+)</returnAuthMsg>", text) or re.search(
            r"<resultMsg>([^<]+)</resultMsg>", text
        )
        detail = msg_m.group(1) if msg_m else (text.strip()[:80] or f"HTTP {resp.status_code}")
        raise KmaError(code, f"기상청 응답 오류: {RESULT_MSG.get(code, detail)} (코드 {code}).")

    gateway = (data.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader")
    if gateway:  # 공공데이터포털 게이트웨이 오류(키 미등록·한도 초과 등) — HTTP 403 등과 함께 JSON으로 온다
        code = str(gateway.get("returnReasonCode") or resp.status_code)
        detail = gateway.get("returnAuthMsg") or gateway.get("errMsg") or f"HTTP {resp.status_code}"
        raise KmaError(code, f"기상청 응답 오류: {RESULT_MSG.get(code, detail)} (코드 {code}).")
    header = (data.get("response") or {}).get("header") or {}
    if not header:
        raise KmaError(str(resp.status_code), f"기상청 응답 오류: 알 수 없는 응답 형식 (HTTP {resp.status_code}).")
    code = str(header.get("resultCode", ""))
    if code != "00":
        reason = RESULT_MSG.get(code, header.get("resultMsg", "알 수 없음"))
        raise KmaError(code, f"기상청 응답 오류: {reason} (코드 {code}).")
    body = (data.get("response") or {}).get("body") or {}
    return ((body.get("items") or {}).get("item")) or []


async def fetch_latest(
    client: httpx.AsyncClient, operation: str, bases: list[datetime], nx: int, ny: int
) -> tuple[datetime, list[dict[str, Any]]]:
    """가장 최근 발표부터 차례로 불러 자료가 있는 첫 발표를 돌려준다."""
    last: KmaError | None = None
    for base in bases:
        try:
            items = await kma_get(
                client,
                operation,
                {"base_date": base.strftime("%Y%m%d"), "base_time": base.strftime("%H%M"), "nx": str(nx), "ny": str(ny)},
            )
        except KmaError as exc:
            if exc.code != "03":
                raise
            last = exc
            continue
        if items:
            return base, items
    raise last or KmaError("03", "기상청 자료가 아직 없습니다. 잠시 후 다시 시도해 주세요.")


# ── 표시 도우미 ────────────────────────────────────────────────────────────────
def num(value: Any) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "-"


def wind(vec: Any, wsd: Any) -> str:
    try:
        idx = int((float(vec) + 22.5 * 0.5) / 22.5)
        direction = DIRS[idx] if 0 <= idx < len(DIRS) else ""
    except (TypeError, ValueError):
        direction = ""
    return f"{direction}풍 {num(wsd)} m/s" if direction else f"{num(wsd)} m/s"


def day_label(yyyymmdd: str) -> str:
    d = datetime.strptime(yyyymmdd, "%Y%m%d")
    return f"{d:%Y-%m-%d} ({WEEKDAYS[d.weekday()]})"


# ── 도구 ───────────────────────────────────────────────────────────────────────
async def get_korea_now(latitude: float, longitude: float) -> str:
    """한국 지역의 지금 날씨를 기상청 초단기실황에서 가져온다: 기온, 강수 형태, 1시간 강수량, 습도, 바람.

    Args:
        latitude: 위도 (예: 서울시청 37.5665, 판교역 37.3948, 부산역 35.1151, 제주공항 33.5104)
        longitude: 경도 (예: 서울시청 126.9780, 판교역 127.1112, 부산역 129.0415, 제주공항 126.4914)
    """
    grid, err = grid_or_error(latitude, longitude)
    if err:
        return err
    nx, ny = grid
    async with new_client() as client:
        try:
            base, items = await fetch_latest(client, "getUltraSrtNcst", ncst_bases(datetime.now(KST)), nx, ny)
        except KmaError as exc:
            return str(exc)

    v = {it.get("category"): it.get("obsrValue") for it in items}
    rn1 = v.get("RN1")
    rain = "없음" if rn1 in (None, "", "0", "0.0") else f"{num(rn1)} mm"
    return "\n".join(
        [
            f"관측 기준: {base:%Y-%m-%d %H:00} (한국 시간) · 기상청 격자 {nx},{ny}",
            f"기온: {num(v.get('T1H'))}°C",
            f"강수 형태: {PTY.get(str(v.get('PTY')), '-')} · 1시간 강수량: {rain}",
            f"습도: {num(v.get('REH'))}%",
            f"바람: {wind(v.get('VEC'), v.get('WSD'))}",
            "출처: 기상청 단기예보 조회서비스(초단기실황)",
        ]
    )


@mcp.tool(annotations=READ_ONLY)
async def get_korea_forecast(latitude: float, longitude: float, days: int = 3) -> str:
    """한국 지역의 날씨 예보를 기상청 단기예보에서 가져온다: 날짜별 최저·최고 기온, 하늘 상태, 강수확률, 3시간 간격 상세.

    Args:
        latitude: 위도 (예: 서울시청 37.5665, 판교역 37.3948, 부산역 35.1151, 제주공항 33.5104)
        longitude: 경도 (예: 서울시청 126.9780, 판교역 127.1112, 부산역 129.0415, 제주공항 126.4914)
        days: 오늘부터 며칠치를 볼지 1~5, 기본 3. 기상청이 주는 범위(발표 시각에 따라 약 3~5일)까지만 나온다
    """
    grid, err = grid_or_error(latitude, longitude)
    if err:
        return err
    nx, ny = grid
    days = max(1, min(5, days))
    async with new_client() as client:
        try:
            base, items = await fetch_latest(client, "getVilageFcst", vilage_bases(datetime.now(KST)), nx, ny)
        except KmaError as exc:
            return str(exc)

    slots: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for it in items:
        slots[it["fcstDate"]][it["fcstTime"]][it["category"]] = str(it.get("fcstValue"))

    rows = [f"발표: {base:%Y-%m-%d %H:%M} (한국 시간) · 기상청 격자 {nx},{ny}"]
    for date in sorted(slots)[:days]:
        day = slots[date]
        temps = [float(v["TMP"]) for v in day.values() if "TMP" in v]
        tmn = next((v["TMN"] for v in day.values() if "TMN" in v), None)
        tmx = next((v["TMX"] for v in day.values() if "TMX" in v), None)
        partial = tmn is None or tmx is None
        low = num(tmn) if tmn is not None else (num(min(temps)) if temps else "-")
        high = num(tmx) if tmx is not None else (num(max(temps)) if temps else "-")
        pops = [int(v["POP"]) for v in day.values() if v.get("POP", "").isdigit()]
        daytime = Counter(v["SKY"] for t, v in day.items() if "SKY" in v and "0600" <= t <= "1800")
        sky_label = "낮 하늘"
        if not daytime:  # 오늘 저녁처럼 낮 시간대가 남지 않았으면 남은 시간 전체로 본다
            daytime = Counter(v["SKY"] for v in day.values() if "SKY" in v)
            sky_label = "하늘"
        sky = SKY.get(daytime.most_common(1)[0][0], "-") if daytime else "-"
        precip = sorted({PTY.get(v["PTY"], v["PTY"]) for v in day.values() if v.get("PTY", "0") != "0"})

        rows.append(
            f"■ {day_label(date)} 최저 {low}°C / 최고 {high}°C"
            + (" (남은 시간 기준)" if partial else "")
            + f" · {sky_label} {sky} · 최대 강수확률 {max(pops) if pops else 0}%"
            + f" · 강수 {', '.join(precip) if precip else '없음'}"
        )
        for t in sorted(day):
            if t[2:] != "00" or int(t[:2]) % 3:
                continue
            v = day[t]
            parts = [f"  {t[:2]}시 {num(v.get('TMP'))}°C {SKY.get(v.get('SKY', ''), '')}".rstrip()]
            if v.get("PTY", "0") != "0":
                parts.append(PTY.get(v["PTY"], v["PTY"]))
            parts.append(f"강수확률 {v.get('POP', '-')}%")
            if v.get("PCP") and v["PCP"] != "강수없음":
                parts.append(f"강수량 {v['PCP']}")
            if v.get("SNO") and v["SNO"] != "적설없음":
                parts.append(f"적설 {v['SNO']}")
            parts.append(wind(v.get("VEC"), v.get("WSD")))
            parts.append(f"습도 {v.get('REH', '-')}%")
            rows.append(" · ".join(parts))
    if len(rows) == 1:
        return "기상청 예보 자료가 비어 있습니다. 잠시 후 다시 시도해 주세요."
    rows.append("출처: 기상청 단기예보 조회서비스(단기예보)")
    return "\n".join(rows)


@mcp.tool(annotations=READ_ONLY)
async def get_korea_summary(latitude: float, longitude: float) -> str:
    """(v2 새 기능) 한국 지역의 지금 날씨를 한 줄로 요약한다. v1의 get_korea_now를 대신한다.

    Args:
        latitude: 위도 (예: 서울시청 37.5665)
        longitude: 경도 (예: 서울시청 126.9780)
    """
    text = await get_korea_now(latitude, longitude)
    return " / ".join(line.strip() for line in str(text).splitlines() if line.strip())


if __name__ == "__main__":
    log.info("kr-weather MCP 서버 시작 — 0.0.0.0:%s/mcp (서비스키 %s)", PORT, "있음" if service_key() else "없음")
    mcp.run(transport="streamable-http")
