# OSM 업데이트 전략

## 기본 원칙

Camera Ninja는 자체 서버를 운영하지 않는다. GitHub Actions가 OSM 데이터를 주기적으로 가공하고 GitHub Releases/Artifacts가 정적 배포소 역할을 한다.

## 단계

### 1단계: Full Snapshot 검증

- 최신 South Korea OSM PBF 다운로드
- 전체 `highway=*` 도로 객체 추출
- 참조 무결성 검증
- 실제 결과 파일 크기/처리시간 측정

### 2단계: Camera Ninja Road DB

- 자동차 주행 가능 여부 분류
- road graph 생성
- SQLite R-tree 생성
- 공공데이터 카메라를 road edge에 사전 매칭

### 3단계: Delta Update

OSM 변경분을 그대로 앱에 전달하지 않고, 가공 전후 Camera Ninja Road DB를 비교해 논리 patch를 만든다.

예시:

```json
{
  "fromVersion": 104,
  "toVersion": 105,
  "deleteEdges": [123, 456],
  "upsertEdges": [],
  "deleteNodes": [],
  "upsertNodes": [],
  "cameraRematches": []
}
```

실제 포맷은 JSON보다 CBOR/Protocol Buffers + zstd 같이 작은 바이너리 포맷을 우선 검토한다.

## 권장 주기

- OSM 원본 확인/가공: 주 1회
- Delta patch: 주 1회
- Full Snapshot: 월 1회

## 앱 측 업데이트

1. 작은 manifest 조회
2. 현재 버전과 최신 버전 비교
3. 가까운 버전이면 patch chain 다운로드
4. 너무 오래된 버전이면 최신 Full Snapshot 다운로드
5. 임시 DB에 적용
6. SHA-256 및 SQLite integrity check
7. sanity check 통과 후 원자적 교체
8. 실패 시 기존 DB 유지

## 신설도로 대응

OSM 데이터가 아직 갱신되지 않은 구간은 업데이트 시스템으로 해결할 수 없다. 해당 구간에서는 Road Match confidence가 낮아지고 Camera Ninja의 GPS heading/bearing/distance-trend fallback이 자동으로 동작해야 한다.
