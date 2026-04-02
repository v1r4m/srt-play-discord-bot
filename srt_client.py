"""SRTplay API 클라이언트 - srt_monitor.py에서 추출한 핵심 로직"""

import html
import json
import os
import re
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── 상수 ──────────────────────────────────────────────────────

URL = "https://srtplay.com/ticket/reservation/schedule/proc"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

WEEKDAYS_KR = ["(월)", "(화)", "(수)", "(목)", "(금)", "(토)", "(일)"]

SOLD_OUT_KEYWORDS = {"매진", "0", "", "soldout", "N"}


def _load_stations():
    path = os.path.join(SCRIPT_DIR, "station.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["codeNm"]: item["codeVal"] for item in data if item.get("isUse") == "Y"}


STATIONS = _load_stations()


# ─── 세션 관리 ─────────────────────────────────────────────────

_session_value = None


def get_session():
    global _session_value
    if _session_value is None:
        _session_value = os.getenv("SESSION")
    return _session_value


def set_session(new_session):
    global _session_value
    _session_value = new_session


def get_cookies():
    token = os.getenv("XSRF_TOKEN")
    remember = os.getenv("REMEMBER_ME")
    session = get_session()
    if not all([token, remember, session]):
        raise ValueError(".env 파일에 XSRF_TOKEN, REMEMBER_ME, SESSION을 설정해주세요.")
    return {
        "XSRF-TOKEN": token,
        "remember-me": remember,
        "SESSION": session,
    }


# ─── API ───────────────────────────────────────────────────────

def build_form_data(dpt_name, arv_name, date_str, dpt_tm="0", passengers=None):
    if passengers is None:
        passengers = [1, 0, 0, 0, 0]

    dpt_code = STATIONS.get(dpt_name)
    arv_code = STATIONS.get(arv_name)
    if not dpt_code or not arv_code:
        raise ValueError(f"역 이름을 확인해주세요. 사용 가능: {', '.join(STATIONS.keys())}")

    dt = datetime.strptime(date_str, "%Y%m%d")
    day_of_week = WEEKDAYS_KR[dt.weekday()]
    dpt_dt_txt = f"{dt.year}.+{dt.month}.+{dt.day}."

    return {
        "_csrf": os.getenv("XSRF_TOKEN"),
        "passenger1": str(passengers[0]),
        "passenger2": str(passengers[1]),
        "passenger3": str(passengers[2]),
        "passenger4": str(passengers[3]),
        "passenger5": str(passengers[4]),
        "handicapSeatType": "015",
        "selectScheduleData": "",
        "psrmClCd": "",
        "isGroup": "",
        "isCash": "",
        "dptRsStnCd": dpt_code,
        "dptRsStnNm": dpt_name,
        "arvRsStnCd": arv_code,
        "arvRsStnNm": arv_name,
        "dptDt": date_str,
        "dptTm": dpt_tm,
        "dptDtTxt": dpt_dt_txt,
        "dptDayOfWeekTxt": day_of_week,
    }


def _do_request(dpt_name, arv_name, date_str, dpt_tm="0", passengers=None):
    headers = {
        "Accept": "*/*",
        "Accept-Language": "ko,en-US;q=0.9,en;q=0.8,ja;q=0.7",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    cookies = get_cookies()
    data = build_form_data(dpt_name, arv_name, date_str, dpt_tm, passengers)
    return requests.post(URL, headers=headers, cookies=cookies, data=data,
                         allow_redirects=False, timeout=10)


def _extract_session_from_response(resp):
    for cookie in resp.cookies:
        if cookie.name == "SESSION":
            return cookie.value
    return None


def _fetch_page(dpt_name, arv_name, date_str, dpt_tm="0", passengers=None):
    resp = _do_request(dpt_name, arv_name, date_str, dpt_tm, passengers)

    if resp.status_code == 302:
        new_session = _extract_session_from_response(resp)
        if new_session:
            set_session(new_session)
            resp = _do_request(dpt_name, arv_name, date_str, dpt_tm, passengers)
        else:
            raise RuntimeError("세션이 만료되었고 자동 갱신에 실패했습니다. .env 쿠키를 갱신해주세요.")

    if resp.status_code == 302:
        raise RuntimeError("세션 갱신 후에도 302 응답. .env 쿠키를 모두 갱신해주세요.")

    if resp.status_code != 200:
        raise RuntimeError(f"요청 실패: HTTP {resp.status_code}")

    return resp.text


def _get_next_page_info(html_text):
    fllw = re.search(r'class="fllwPgExt"[^>]*>(\w+)<', html_text)
    last_tm = re.search(r'class="lastDptTm"[^>]*>(\d+)<', html_text)
    has_next = fllw and fllw.group(1) == "Y"
    next_tm = last_tm.group(1) if last_tm else None
    return has_next, next_tm


def fetch_schedule(dpt_name, arv_name, date_str, passengers=None):
    all_html = []
    dpt_tm = "0"
    max_pages = 10

    for _ in range(max_pages):
        html_text = _fetch_page(dpt_name, arv_name, date_str, dpt_tm, passengers)
        all_html.append(html_text)

        has_next, next_tm = _get_next_page_info(html_text)
        if not has_next or not next_tm:
            break
        dpt_tm = next_tm

    return "\n".join(all_html)


# ─── 파싱 ──────────────────────────────────────────────────────

def _parse_java_map(s):
    s = s.strip().strip("{}")
    result = {}
    parts = re.split(r",\s*(?=\w+=)", s)
    for part in parts:
        eq = part.find("=")
        if eq == -1:
            continue
        key = part[:eq].strip()
        val = part[eq + 1:].strip()
        result[key] = val
    return result


def parse_trains_from_html(html_text):
    decoded = html.unescape(html_text)
    pattern = r"setSchedule\('(\{[^}]+\})'\s*,\s*'[12]'\)"
    matches = re.findall(pattern, decoded)

    if not matches:
        return None

    seen = set()
    trains = []
    for m in matches:
        data = _parse_java_map(m)
        key = data.get("trnNo", "") + "_" + data.get("dptTm", "")
        if key in seen:
            continue
        seen.add(key)
        trains.append(data)

    return trains


def fmt_time(tm):
    if not tm or len(str(tm)) < 4:
        return str(tm) if tm else "?"
    s = str(tm)
    return f"{s[:2]}:{s[2:4]}"


def is_seat_available(status_str):
    s = str(status_str).strip()
    return "매진" not in s and s not in SOLD_OUT_KEYWORDS


def parse_train_list(trains):
    """열차 리스트를 파싱하여 구조화된 dict 리스트 반환."""
    if not trains:
        return []

    parsed = []
    for i, item in enumerate(trains):
        trn_no = item.get("trnNo", "?").lstrip("0") or "?"
        dpt_tm = item.get("dptTm", "")
        arv_tm = item.get("arvTm", "")
        gnrm = item.get("gnrmRsvPsbCdNm", "?")
        sprm = item.get("sprmRsvPsbCdNm", "?")
        duration = item.get("timeDuration", "")

        gnrm_avail = is_seat_available(gnrm)
        sprm_avail = is_seat_available(sprm)

        parsed.append({
            "index": i,
            "trainNo": trn_no,
            "dptTm": dpt_tm,
            "arvTm": arv_tm,
            "duration": duration,
            "gnrm": gnrm,
            "sprm": sprm,
            "gnrm_avail": gnrm_avail,
            "sprm_avail": sprm_avail,
            "raw": item,
        })

    return parsed
