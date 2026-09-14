# Fleet 실행 관측과 수집 복구

2026-09-14. 실행 중인 owner가 idle로 표시되고, 느린 NAS에서 Fleet 조회가
오래 걸리거나 `workers stuck`에 남는 문제를 함께 수정했다. 운영 owner,
원장, route pin, 산출물과 사용자 설정은 수정하지 않았다.

## 원인과 책임 정리

| 경계 | 확인한 원인 | 수정 후 책임 |
|---|---|---|
| owner 재개 | 완료 전달용 outbox가 있으면 감독자가 `running-turn`을 기록하지 않았다. 실제 turn 동안 과거 `deliverable`이 남았다. | 공통 `begin_supervisor_turn`이 상태 락 안에서 현재 outbox를 보존하고 실행을 기록한다. receiving turn 뒤의 exact acknowledgement와 실패 시 복구는 감독자가 계속 맡는다. |
| 구버전 owner 관측 | Fleet이 감독자의 대기 phase를 현재 활동보다 우선했다. Codex의 supervised usage 이벤트에서도 thread identity를 읽지 않았다. | 정확한 attempt 로그의 thread/turn/시각을 읽는다. 기존 60초 활동 창 안의 증거만 대기 표시를 working으로 보완한다. 결과 확정·정리·재시도 권한은 생기지 않는다. |
| 화면 정보 구성 | `collect_all`이 끝낸 projection을 CLI가 다시 실행했다. | collector가 한 번만 구성한다. demo도 추가된 행만 별도로 구성한다. |
| 불필요한 탐색 | 이름 기반 stage 추정을 위해 전체 campaign inventory를 읽었다. 정확한 실행 신원이 있는 행에도 사용하지 않을 과거 rollout 색인을 만들고, 화면과 관계없는 옛 route를 열었다. | 실제 행과 부모 연결이 참조하는 route만 읽는다. cwd 기반 rollout 추정은 신원이 없는 옛 행에만 남긴다. 명시적 route와 직접 지정된 legacy 경로 외에는 현재 stage를 추측하지 않는다. |
| 느린 수집 복구 | 살아 있는 스레드를 버리고 재수집했다. 버린 횟수는 회복 뒤에도 누적돼 `stalled`를 유지할 수 있었다. | pump당 하나의 살아 있는 producer가 끝까지 결과를 반환한다. 지연은 관측 상태이고 재시도 허가가 아니다. 실제 실패·종료 뒤에만 다시 수집한다. 늦은 정상 결과를 받으면 상태와 갱신 시각이 회복된다. |

상단 문구는 작업자가 멈췄다는 인상을 주는 `workers stuck` 대신
`collection delayed`와 마지막 성공 시각을 표시한다. TUI 입력·그리기는 수집과
분리된다. 명시적 새로고침 여러 번도 현재 수집 뒤 한 번으로 합쳐진다.
스레드 포기·중복 실행·늦은 결과 폐기 경로를 제거했으며 시간 상한은 늘리지 않았다.
기존 health JSON의 `leaked_workers`는 호환을 위해 0으로 남긴다.

Claude와 OpenCode의 owner는 같은 session supervisor를 사용하고 Codex는
App Server supervisor를 사용한다. 세 실행 경계 모두 공통 상태 전이를 쓴다.
새 Codex turn 종료 관측은 owner 전체의 terminal envelope와 구분되며,
terminal reader는 정상 종료와 유효 신원이 있는 해당 이벤트만 통과시킨다.
실패·잘못된 이벤트·다른 turn의 최종 답변은 기존처럼 성공 근거가 되지 않는다.

제보 JSON에 있던 app-server와 후손의 raw session 행은 둘 다 `is_child=true`였다.
JSON은 이 실제 프로세스 관측을 보존하고 기본 작업 화면은 해당 행을 제외한다.
이 사실은 owner 행의 idle 결함을 부정하는 근거가 아니다. 그 결함은 위에서
별도로 재현·수정했다. private 전체 snapshot이나 로그 본문은 이 보고서에 싣지 않는다.

## 검증과 적용 한계

- 격리 runner 17개 관련 suite 통과, 재시도 0. 세 하네스의 실제 Python fixture
  프로세스가 재개 시 `running-turn`과 미확인 outbox를 함께 읽고 정상 종결한다.
  이는 실제 공급자 모델을 다시 호출한 검증이 아니다.
- partial consume 뒤 turn 시작이 기존 소비 기록을 되돌리지 않음, 잘못된 receipt
  거부, 재시작 시 outbox 복원, 정확한 terminal handoff 유지 등을 검사했다.
- Fleet은 오래된·미래 시각·다른 attempt/thread·이미 종료된 turn을 working으로
  승격하지 않는다. 실제 16:22:50 snapshot과 그 시점까지의 로그를 읽기 전용으로
  재생한 결과 해당 owner는 idle에서 working으로 교정됐다.
- 지연된 실제 스레드에 반복 요청을 보내도 producer 하나를 유지하고, IO가 반환하면
  정상 결과와 갱신 시각을 회복한다. 실패한 producer의 후속 재시도도 유지된다.
- 설치본 v2.142.3의 수동 구간 계측: 설치 정보 0.001초, 수집 46.492초,
  중복 projection 37.412초. stack에서 NAS campaign 순회 대기를 확인했다.
  수정본의 일반 수집 계측은 27.846초, 사용하지 않을 rollout 색인은 0초,
  관련 route 읽기 0.115초였다. 표본 시각과 실행 중 행 수가 다르므로 고정 배수의
  성능 보장으로 해석하지 않는다. NAS 지연 자체가 해소됐다는 주장도 아니다.
- 실제 PTY에서 첫 header 0.440초, 종료 키 후 정상 exit 0을 확인했다. 전체
  데이터 수집 완료 시간과 구분한다. 시험 프로세스에만 빈 host 설정 경로를 넘겨
  SSH/GPU probe를 실행하지 않았고 사용자 설정은 유지했다.

소스 수정 전의 실제 owner pin은 그대로다. 새 Fleet은 그 owner의 정확한 로그를
읽어 표시를 보완한다. 새 감독 상태 기록은 수정된 release에서 시작하는 owner에
적용된다. 이미 열린 Fleet 프로세스는 재실행해야 새 수집·복구 코드를 사용한다.
이 작업의 완료 범위는 Fleet과 그 근거가 되는 owner 실행 상태 계약이며,
별도의 resource 실패 통보·외부 후속 인수 작업 완료를 뜻하지 않는다.
