# Fleet의 담당 범위와 전체 route 분리

2026-09-14. frame은 사전 검토를 수행하고 이후 단계는 별도 owner가 맡는다.
기존 Fleet은 두 역할 모두 dispatch depth 1이라는 이유로 같은 전체 진행표를
그렸다. frame 카드에 미래의 test/report까지 나타나 담당 범위를 잘못 전달했다.

공통 work projection에 `scope_node_ids`를 추가했다. 정확한 node binding을
가진 worker는 그 노드만 표시하고 진행률도 같은 범위에서 계산한다. owner는
봉인된 route가 depth 1/frame으로 선언한 사전 검토 노드를 제외한 실행 단계를
표시한다. 옛 route의 depth 2 frame은 그 owner의 담당으로 유지한다.

부모 세션은 첫 자식의 진행표를 복사하지 않고 자기 자식들을 집계한다. 따라서
frame 준비 중에는 두 검토가 함께 보이고, 일반 병렬 작업의 다른 자식도 빠지지
않는다. 이 범위 결정은 projection이 맡으며 화면별 필터를 따로 만들지 않았다.
원본 route view는 그대로 보존하여 route 개요와 JSON의 전체 노드·전체 진행률은
개별 worker의 범위에 영향받지 않는다. 실행, 완료 판정, 소유권 원장은 변경하지 않는다.

## 검증

- 새 회귀는 세 하네스의 frame/alternative, wide/narrow/stack 화면, owner 담당
  단계, 노드에 결속된 일반 stage, 두 frame의 부모 집계, 전체 route 개요,
  옛 depth 2 frame을 검사한다. 기존 코드에서는 frame에 후속 단계가 나타나 실패했다.
- 실제 과거 frame 행과 봉인된 route를 읽기 전용으로 재생했다. 설치본 v2.142.2는
  `frame(2-way) → test → report`, 카드 진행률 `4/4`를 표시했다. 수정본은 그
  작업이 맡은 `frame`, `1/1`만 표시하며 전체 route의 네 노드는 보존했다.
  이 행은 이미 완료된 이력이며 실행 당시 화면 snapshot이라고 주장하지 않는다.
- 공식 격리 runner에서 관련 12개 묶음이 통과했다. 별도 기존
  `test_v20_dispatch_contract.py`는 `No module named 'tools'`로 KNOWN-FAIL이다.
  새 6개 회귀를 포함한 work projection 전체 36개 테스트가 통과했다.
- 생성 projection 20개 그룹, adapter 경계, surface budget을 확인했다.
  기존 바이트 초과 권고는 유지하며 한도를 늘리지 않았다.

세 하네스 검증은 공통 Fleet 데이터와 렌더링 경로의 검증이다. 이를 위해 새 모델
작업이나 제품 테스트를 기동하지 않았으며, 등록된 작업의 depth와 parent는 그대로다.
