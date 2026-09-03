# Camera Ninja OSM

Camera Ninja의 대한민국 전역 도로 매칭용 OSM 데이터 파이프라인입니다.

## 목표

- OpenStreetMap 대한민국 전체 원본에서 **도로 관련 객체만 추출**
- 특정 도로 등급(motorway/trunk 등)만 남기지 않고, OSM에서 `highway=*`로 표현되는 도로를 우선 모두 보존
- 도로 제한 관계(`type=restriction`)와 도로 노선 관계(`route=road`)도 함께 보존
- 이후 Camera Ninja 전용 Road Graph / R-tree / 카메라 매칭 DB의 원천 데이터로 사용
- 주행 중에는 외부 지도 API 없이 완전 로컬 판정

> OSM에서 `highway`는 '고속도로'만 뜻하지 않습니다. 일반도로, 주택가 도로, 서비스 도로, 골목길, 보행로 등 다양한 길이 `highway=*` 태그로 표현됩니다. 이 저장소의 1단계 스냅샷은 특정 등급만 추리는 것이 아니라 `highway=*` 전체를 보존합니다. 실제 자동차 주행 가능 여부는 다음 단계의 Road Graph 생성 시 별도로 판정합니다.

## 원본 데이터

- Provider: Geofabrik OpenStreetMap extracts
- Region: South Korea
- Source: `https://download.geofabrik.de/asia/south-korea-latest.osm.pbf`
- License: Open Database License (ODbL) 1.0
- Attribution: © OpenStreetMap contributors

## 현재 파이프라인

GitHub Actions가 다음 작업을 수행합니다.

1. Geofabrik에서 최신 대한민국 OSM PBF 다운로드
2. 공식 MD5 체크섬 검증
3. `highway=*` way + 도로 제한/노선 relation 추출
4. 참조 node를 포함한 독립적인 `south-korea-roads.osm.pbf` 생성
5. `osmium check-refs`로 참조 무결성 검사
6. SHA-256, 파일 크기, 생성 시각을 `manifest.json`에 기록
7. GitHub Actions artifact로 결과 보관

첫 스냅샷이 정상 생성되는 것을 확인한 뒤 GitHub Releases에 Full Snapshot을 발행하고, 다음 단계에서 OSM 변경분 기반 Delta Update를 추가합니다.

## 저장소 구조

```text
.github/workflows/   GitHub Actions 자동화
config/              OSM 필터 정의
scripts/             다운로드/추출/검증 스크립트
docs/                데이터 구조 및 업데이트 설계
LICENSES/            OSM 라이선스/출처 고지
```

## 설계 원칙

- 대용량 PBF/DB 바이너리는 Git commit history에 직접 넣지 않음
- 전체 스냅샷과 diff는 Release/Artifact로 배포
- 앱은 OSM 매칭 실패 시 GPS heading/bearing 방식으로 fallback 가능하도록 설계
- 카메라 공공데이터는 OSM 도로망과 별도 버전 관리 후 Road Edge에 매칭

## 다음 단계

1. 대한민국 전체 도로 스냅샷 최초 빌드 및 크기 확인
2. 자동차 주행 가능한 Road Edge 분류 규칙 확정
3. Road Graph + R-tree 경량 DB 생성
4. 공공데이터포털 카메라 좌표를 Road Edge에 사전 매칭
5. 주간 OSM diff + 월간 Full Snapshot 업데이트 체계 구축
