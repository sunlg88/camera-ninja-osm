#!/usr/bin/env python3
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.data.go.kr/openapi/tn_pubr_public_unmanned_traffic_camera_api"
FIELD_MAP = {
    "mnlssRegltCameraManageNo": "무인교통단속카메라관리번호",
    "ctprvnNm": "시도명",
    "signguNm": "시군구명",
    "roadKnd": "도로종류",
    "roadRouteNo": "도로노선번호",
    "roadRouteNm": "도로노선명",
    "roadRouteDrc": "도로노선방향",
    "rdnmadr": "소재지도로명주소",
    "lnmadr": "소재지지번주소",
    "latitude": "위도",
    "longitude": "경도",
    "itlpc": "설치장소",
    "regltSe": "단속구분",
    "lmttVe": "제한속도",
    "regltSctnLcSe": "단속구간위치구분",
    "ovrspdRegltSctnLt": "과속단속구간길이",
    "prtcareaType": "보호구역구분",
    "installationYear": "설치연도",
    "institutionNm": "관리기관명",
    "phoneNumber": "관리기관전화번호",
    "referenceDate": "데이터기준일자",
    "instt_code": "제공기관코드",
    "instt_nm": "제공기관명",
}


def fetch_page(service_key, page, rows, attempts=6, timeout_s=45):
    query = urllib.parse.urlencode({
        "serviceKey": service_key,
        "pageNo": page,
        "numOfRows": rows,
        "type": "json",
    })
    url = API_URL + "?" + query
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Camera-Ninja-CI/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                payload = json.load(response)

            wrapped = payload.get("response", payload)
            header = wrapped.get("header", {})
            code = str(header.get("resultCode", "00"))
            if code not in {"00", "0", "NORMAL_CODE"}:
                raise RuntimeError(f"data.go.kr error {code}: {header.get('resultMsg')}")
            body = wrapped.get("body", wrapped)
            items = body.get("items", body.get("data", []))
            if isinstance(items, dict):
                items = items.get("item", [])
            return items or [], int(body.get("totalCount", len(items or [])))
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionResetError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay = min(5 * (2 ** (attempt - 1)), 40)
            print(
                f"page {page} fetch failed (attempt {attempt}/{attempts}): {exc}; retrying in {delay}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)

    raise RuntimeError(f"page {page} failed after {attempts} attempts: {last_error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--rows", type=int, default=1000)
    args = parser.parse_args()
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY")
    if not key:
        print("DATA_GO_KR_SERVICE_KEY is required", file=sys.stderr)
        return 2

    records = []
    page = 1
    total = None
    while total is None or len(records) < total:
        items, total = fetch_page(key, page, args.rows)
        if not items:
            break
        for item in items:
            records.append({FIELD_MAP.get(k, k): v for k, v in item.items()})
        print(f"fetched {len(records)}/{total}", flush=True)
        page += 1

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"records": records}, f, ensure_ascii=False, separators=(",", ":"))
    print(json.dumps({"records": len(records)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
