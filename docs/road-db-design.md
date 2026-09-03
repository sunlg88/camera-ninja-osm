# Camera Ninja Road DB 설계 초안

## 목적

전국 OSM 도로망과 공공데이터포털 무인교통단속카메라 데이터를 하나의 로컬 판정 체계로 연결한다.

## 런타임 처리 순서

1. 최근 GPS 위치 수집
2. Road R-tree에서 주변 OSM edge 후보 조회
3. 최근 이동 궤적과 edge geometry/방향을 비교해 현재 주행 edge 결정
4. 현재 edge에서 진행 방향으로 road graph 탐색
5. 탐색 경로에 연결된 카메라 중 설정 거리 이내 후보만 선택
6. 카메라 방향/접근 추세/GPS heading을 추가 검증
7. 경고 또는 무시
8. OSM 매칭 신뢰도가 낮으면 GPS 기반 fallback 사용

## 권장 테이블

### road_node

- node_id INTEGER PRIMARY KEY
- lat_e7 INTEGER
- lon_e7 INTEGER

### road_edge

- edge_id INTEGER PRIMARY KEY
- osm_way_id INTEGER
- from_node INTEGER
- to_node INTEGER
- length_m REAL
- road_class TEXT
- name TEXT
- ref TEXT
- oneway INTEGER
- access_flags INTEGER
- layer INTEGER
- bridge INTEGER
- tunnel INTEGER

### road_geometry

- edge_id INTEGER
- seq INTEGER
- lat_e7 INTEGER
- lon_e7 INTEGER

### road_rtree

SQLite R-tree virtual table.

- edge_id
- min_lat
- max_lat
- min_lon
- max_lon

### camera

카메라 공공데이터는 별도 원본을 보존하면서 Road Edge 매칭 결과를 추가한다.

- camera_id TEXT PRIMARY KEY
- latitude REAL
- longitude REAL
- speed_limit INTEGER
- camera_type TEXT
- source_direction TEXT
- road_name TEXT
- installation_place TEXT
- matched_edge_id INTEGER NULL
- position_on_edge REAL NULL
- match_confidence REAL NULL
- data_date TEXT

## 공간검색

- 차량 위치 주변 road candidate 검색: 약 100~200 m bounding box부터 시작
- 카메라 후보는 가능하면 `matched_edge_id` 관계로 탐색
- 진단/fallback을 위해 camera R-tree를 별도 유지할 수 있음

## 방향 판정

OSM의 edge 방향 및 oneway 정보와 GPS 이동벡터를 결합한다. 공공데이터의 `도로노선방향` 값은 원시 코드 자체를 나침반 방위각으로 해석하지 않고 보조 정보로만 사용한다.

## OSM 매칭 실패 처리

다음 중 하나면 Road Match confidence를 낮춘다.

- 가까운 적절한 edge가 없음
- GPS 진행 방향과 edge 방향이 지속적으로 크게 불일치
- 연속 GPS 샘플이 동일 도로망 위에서 설명되지 않음
- 신설도로 등으로 GPS가 OSM 도로 밖에서 지속 이동

낮은 신뢰도에서는 기존 Camera Ninja GPS 방식으로 자동 fallback한다.
