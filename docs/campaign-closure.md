# Campaign 행정 종료

`campaign-status`, `campaign-close`, `campaign-recover`가 기존 D-10 campaign
satisfaction과 D-11 append-only event의 공식 writer/복구 경로다. cycle 완료,
route 종료, campaign goal 수락은 각각 다르다. 모든 cycle이 sealed여도 사용자의
goal·completion criterion 수락 없이 campaign을 자동 종료하지 않는다.

## 운영 순서

먼저 현재 설치본에서 읽기 검사한다. `--campaign`은 ID 또는 `campaign.json`
경로다. 경로를 주면 큰 저장소의 ID 탐색을 줄일 수 있다.

```sh
python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-status \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json
```

명령은 goal, criterion, 각 cycle의 실제 completed/abandoned 상태와 manifest
근거를 보여 준다. 승인 가능한 상태에서만 `approval_statement`를 출력한다.
사용자가 이 내용을 확인하고 **그 문장 전체를 자신의 원 native 세션에 직접
전송**한다. 예시의 ID/hash를 복사해 쓰지 말고 해당 status의 실제 출력을 쓴다.
에이전트가 사용자 대신 전송하거나 `actor.kind=user` JSON을 만드는 경로는 없다.
일반적인 "수정해 달라" 지시, 모델·도구 출력, peer 전달문, 빈 답변은 승인이 아니다.

실제 답변이 기록된 뒤 운영자가 다음을 실행한다. Claude/Codex는 native 세션
ID(UUID), OpenCode는 `ses_…`를 사용한다. 부모 세션의 현재 환경으로 읽는다.

```sh
python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-close \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json \
  --approval-harness claude --approval-session ACTUAL_NATIVE_SESSION_ID
```

현재 snapshot에 결속한 exact user message를 읽고 actor를 도출한다. Codex는
`CODEX_HOME/sessions`의 session_meta와 user message, Claude는
`CLAUDE_CONFIG_DIR/projects`의 sessionId/user message를 검증한다. 각 변수의
기본 runtime home도 지원한다. OpenCode는 [공식 `opencode export <sessionID>`](https://opencode.ai/docs/cli/#export)의
native user 메시지를 읽는다. 임의 approval 파일·임시 export·모델 handoff를
받지 않는다. metadata/sidechain/tool-result/synthetic 메시지는 수락하지 않는다.
지원하지 않는 native 형식이나 없는 기록은 typed 거부로 남고, 같은 실제 사용자가
읽을 수 있는 native 세션에 정확한 문장을 남기는 것이 fallback이다.

`campaign-reject`로 시작하고 나머지가 같은 명시 거부가 해당 세션에 더 늦게
기록되면 이전 수락은 소비하지 않는다. 목표·criterion·멤버·봉인 digest가 바뀌면
이전 문장이 새 snapshot을 승인하지 않는다. 이미 commit된 만족 상태의 취소나
재개는 이 명령의 범위가 아니다. native 저장소는 기존 로컬 신뢰 경계이며,
동일 사용자 권한으로 그 저장소 자체를 위조하는 프로세스를 암호학적으로
식별한다는 보장은 하지 않는다.

## 검증과 복구 책임

공통 `artifact_campaign` API의 `status`, `close`, `fold_campaign`이 판정과
복구를 소유한다. 승인 전후에 같은 admission lock 아래 snapshot을 대조한다.
campaign 멤버 목록, producer cycle 기록, `.cycle.json`, 봉인된 manifest의
schema/ID/root/producer, 색인과 파일 digest·size를 검증한다. 승인된 옛 병합의
비정규 JSON은 실제 바이트에 결속한 producer seal과 정규화된 내용에 결속한
index digest를 각각 검증한다. 현재 포맷에 맞추려고 원본을 재작성하지 않는다.

`abandoned`는 봉인 상태로 집계되며 성공으로 바뀌지 않는다. 미종결 route의
실패 marker도 그대로다. `residual=0`이나 모든 cycle 성공 조건은 추가하지 않는다.
자유문 criterion의 의미적 충족은 사용자가 명시적으로 수락하며, 코드가 임의로
자연어 목표를 달성했다고 판정하지 않는다.

`campaigns/<locator>/campaign.satisfied.json`이 no-replace·fsync로
발행되는 immutable commit point다. D-11 event envelope에 snapshot과 실제 승인
근거를 결속한다. `campaign.json`은 이 event의 복구 가능한 상태 projection이다.
manifest나 route terminal 파일에는 쓰지 않는다. producer reader/begin도 같은
event를 소비하므로 event 발행 후 projection 전에 종료돼도 새 cycle을 허용하지
않는다. 종료된 key의 명시 재사용이 조용히 새 campaign을 만드는 것도 막는다.

commit 전 실패는 종료를 주장하지 않고 같은 close를 재시도할 수 있다. commit 후
projection 실패는 `campaign-close-committed-recovery-required`와 정확한 복구
명령을 출력한다. 추가 승인이 필요하지 않다.

```sh
python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-recover \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json
```

recover는 기존 event가 없으면 아무것도 발급하지 않는다. 동일 호출과 동시 호출은
event 하나로 수렴하며 foreign successor·손상·심볼릭 링크는 보존하고 typed conflict로
보고한다. 원 campaign.json이나 manifest의 수동 수정을 복구 방법으로 안내하지 않는다.

## 이번 BC 관측

2026-09-13 읽기 검사에서 대상 campaign의 cycle 16개가 봉인돼 있었다.
`cyc_cd71974e9c13b8ff1d13f121a41a7334`는 completed,
`cyc_c962f7ba3d69ede0257740f85f2c4413`는 abandoned다. 다른 abandoned 1개를 포함해
completed 14/abandoned 2로 나타났다. 전체 manifest·색인·파일 검증을 통과한
상태는 `awaiting-user-acceptance`이며, 이번 하팅 정비 요청을 해당 campaign의
종료 승인으로 소비하지 않았다. BC canonical 데이터와 cnn에는 쓰기·접속·probe가 없다.

회귀 테스트는 실제 producer/CLI, 승인 출처·역할·snapshot, 중복·동시 호출,
commit 전후 실패, 복구, 이후 begin 차단, 봉인 파일 보존, legacy seal/index와
세 하네스 native 입력 parser를 포함한다. native parser fixture 통과를 실제
사용자 승인이나 세 하네스 실모델 canary로 주장하지 않는다.

검증 기록: 신규 집중 18건 PASS. isolated runner에서 producer, manifest,
lifecycle, index, reader, relayout 및 신규 campaign suite가 통과했다. 생성
20그룹·적응 경계·표면 예산 검사도 통과했으며 기존 예산 경고의 상한을
올리지 않았다. 실제 BC 경로의 승인 없는 close는 exit 65로 거부됐고
campaign·cycle·manifest·route 기록 65개가 그대로였다. 로컬 근거는
`/tmp/campaign-close*-suites.tsv`, `/tmp/bc-campaign-close-readonly.json`,
`/tmp/bc-campaign-no-approval-observation.json`이다.
