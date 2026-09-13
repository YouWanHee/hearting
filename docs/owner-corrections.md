# 실행 중 owner 교정 전달

기존 감독자는 실행과 자식 완료만 입력으로 받았다. 부모에게 도착한 사용자 교정은
별도 headless owner에게 전달되지 않았고, `start` 재호출이나 파일 수정도 수신 증거가
아니었다. 이제 실행 감독자가 동일 owner의 교정 접수와 전송 영수증까지 소유한다.

## 사용

`capability-route.py start` 결과의 `correction_command`로 기존 owner의 수신 상태를
읽는다. 그 명령에 `--message-file <교정문.txt>`를 붙이면 같은 owner에게 전달한다.
직접 호출할 때는 다음과 같다.

```sh
python3 "$AGENT_HOME/utilities/capability-route.py" correct \
  --jobs "$AGENT_DISPATCH_JOBS" --attempt-id <owner-attempt> \
  --message-file <교정문.txt>
```

파일 인자를 빼면 읽기 전용 상태 조회이다. 같은 본문은 같은 요청 ID로 재조회되며
중복 전달되지 않는다. 의도적으로 별개의 요청을 보낼 때만 새 `--request-id`를 지정한다.
이 명령은 새 route, owner, child를 만들지 않는다. 부모/운영자가 전달한 문맥이며
사용자 승인이나 완료 증거를 새로 만들어 주지 않는다.

## 누가 무엇을 끝까지 처리하는가

| 책임 | 소유자 |
|---|---|
| exact owner·route 결속, 중복 접수, 영속 기록 | 공통 `dispatch_owner_input` |
| 활성 Codex turn 전송 | 기존 App Server 감독자의 동일 연결 |
| Claude/OpenCode CLI 교정 전송 | 기존 감독자의 다음 동일 세션 turn |
| 전송 불명·미전달 통보 | 기존 supervision notice와 parent carrier |
| 감독자 사망 뒤 통보 복구 | 기존 terminal-close 및 registry reconcile 경로 |
| 제품 적용 및 검증 | 교정을 받은 owner; 전송 영수증으로 대신하지 않음 |

교정이 대기하면 감독자는 다음 자동 stage/serial slice 전진 및 terminal 마감 전에
그 입력을 소비한다. 이미 실행한 자식을 교정 수신만으로 중단하거나 재실행하지 않는다.
자식 대기 중 입력은 다음 기존 resume에 합류한다. CLI 운송을 활성 turn steer라고
표시하지 않으며, 실행 방식 차이는 이 내부 경계에 남는다.

`queued`는 접수, `accepted`는 runtime 전송 확인, `turn-completed`는 해당 입력을 받은
turn의 종료다. 모두 제품 적용 PASS를 의미하지 않는다(`applied=not-verified`).
`delivery-unknown`은 전송 여부가 불명확하고 `undelivered`는 아직 전달하지 못했다는 뜻이다.
오너 사망 시 원래 전송 기록을 보존하며 관측 상태를 별도로 보여 준다. 감독자가 재시작해도
전송 중이던 요청은 자동 재전송하지 않고, 다른 thread에 기존 입력을 조용히 넘기지 않는다.
보내지 않은 입력은 동일 thread로 복구될 때만 재사용한다.

기존 peer 메시지는 대화형 부모 사이의 전달에 그대로 쓰인다. 부모 수신과 owner 수신을
같다고 취급하던 가정을 제거했고, 새 peer daemon이나 완료·재시도 판정기를 만들지 않았다.
terminal 결과와 교정 통보는 별도이므로 입력 영수증 저장 실패가 이미 확정된 PASS를 뒤집지 않는다.

## 완료 단계 재사용

이미 있는 `capability-route.py continuation`이 source prefix의 현재 completion 증거를
검증하고 suffix만 발급한다. 이를 사용하지 않고 full recipe를 선택하면 계획을 반복할 수 있다.
과거 파일을 입력으로 재사용하는 것과 과거 completion gate를 재사용하는 것은 다르다.
교정 전달은 기존 route/marker/cleanup proof를 바꾸거나 이 검증을 생략하는 기능이 아니다.

## 검증과 한계

- 격리된 관련 8 suite 통과: input, Codex/Claude supervisor, OpenCode transport,
  completion join, registry, supervision, work_start.
- 신규 검사는 실제 프로세스 파이프/CLI, 같은 세션의 Claude/OpenCode 다음 turn,
  중복 제출, 전송 거절, 전송 중 감독자 사망, 재시작, 대상 thread 변경, terminal 경쟁,
  직렬 successor 전진 보류와 기존 parent notice를 다룬다.
- 실제 Codex App Server + `gpt-5.6-luna`에서 활성 turn에 교정을 보내 원래 출력 대신
  교정된 출력이 나왔고, 같은 turn의 accepted 및 turn-completed 영수증을 확인했다.
  이는 native 운송 검증이며 제품 변경의 정확성이나 등록 dispatch 전체 실측은 아니다.
- Claude/OpenCode는 실제 subprocess fixture로 운송 경계를 확인했다. 이번에 해당 모델을
  새로 호출한 native 실측으로 주장하지 않는다.
- 기존 serial_chain_supervisor 전체 suite는 300초 timeout. 수정 전 main에서도 동일
  16-slice fixture의 child-settlement 대기가 재현됐다. 전체 chain PASS로 기록하지 않는다.

공식 runtime 근거: [Codex App Server](https://learn.chatgpt.com/docs/app-server#steer-an-active-turn),
[Claude 프로그램 실행](https://code.claude.com/docs/en/headless),
[OpenCode server](https://opencode.ai/docs/server/). native 지원과 현재 CLI 연결 범위를 구분한다.

새 입력 계약이 없는 기존 실행 중 supervisor는 접수 전에 unsupported를 반환한다.
설치 업데이트가 이미 실행 중인 pinned 프로세스를 바꾸지는 않는다. 운영 home-os owner,
제품 파일, route, 원장, marker, proof를 이번 검증으로 변경하지 않았다.
