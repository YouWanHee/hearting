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

명령은 goal, criterion, 각 cycle의 disposition(completed/abandoned/sealed-unproven)과
manifest 근거를 보여 준다. 승인 가능한 상태에서만 `approval_statement`를 출력한다.
에이전트는 이 내용과 `approval_statement` 문장을 **그대로** 보여 주는 글로 턴을 마친다.
사용자는 문장 전체를 입력하거나, 짧은 **닫기 동의**로 답한다. 닫기 동의는 닫는 동사가
있는 짧은 답 전체다("응 닫아", "닫아", "네 닫아주세요", "승인", "승인합니다", "close it",
"approve"). "응"·"ok"·"네"만으로는 무엇에 대한 동의인지 알 수 없어 승인이 아니다.
Claude 세션에서만, 사용자가 **입력한 시각** 직전의 마지막 assistant 글에 정확한 문장이
있고 그 뒤 첫 사람 입력일 때 그 snapshot의 승인으로 읽는다. 질문("?", "~하나", "어떻게")은
판정하지 않는다. 짧은 동의는 `campaign-close` 직전의 **마지막** 사용자 입력이어야 하며,
그 뒤에 무엇이든 입력하면 무효가 된다. 부정("아니", "안 닫아", "잠깐", "취소", "거부", "아직",
"no")은 길이와 상관없이 앞선 승인을 거둔다 — 에이전트가 `campaign-close`를 실행하는 중에
쳐서 아직 대기열에만 있는 부정도 포함한다(정확한 문장으로 한 승인은 60자 이하의 부정이나
`campaign-reject` 문장으로만 거둔다).
에이전트가 바쁜 중에 입력해 `queued_command`(origin human)로 저장된 입력도 사용자 입력이다.
Codex·OpenCode 세션은 정확한 문장만 받는다. `codex exec`, 다른 프로그램이 앱 서버로 구동한
Codex 세션(예: Claude Code 플러그인, originator `Claude Code`), Codex 하위 에이전트, OpenCode 하위
세션(`parentID`)은 사용자로 보지 않는다. headless `opencode run` 세션은 대화형과 구분할 수 없어
아래 신뢰 경계에 속한다.
에이전트가 사용자 대신 전송하거나 `actor.kind=user` JSON을 만드는 경로는 없다.
일반적인 "수정해 달라" 지시, 모델·도구 출력, 빈 답변은 승인이 아니다. Claude Code가
사람 입력으로 기록하지 않은 행(headless `sdk`, 프롬프트 제안 수락, task-notification,
도구 결과)도 승인이 아니다. 다른 세션이 프롬프트 창에 주입한 입력은 저장 모양이 사람
입력과 같으므로 peer trailer, peer 원장의 본문 digest, 또는 최근 원장 행의 요약 일치로
걸러 낸다. 원장을 읽지 못하면 짧은 동의는 꺼지고 정확한 문장만 받는다. 원장에 남지 않는
주입(`herdr agent prompt` 직접 호출, tmux 입력)은 사람 입력과 구분할 수 없으며, 이는 아래
native 저장소 신뢰 경계에 속한다.

실제 답변이 기록된 뒤 운영자가 다음을 실행한다. Claude/Codex는 native 세션
ID(UUID), OpenCode는 `ses_…`를 사용한다. 부모 세션의 현재 환경으로 읽는다.

```sh
python3 "$AGENT_HOME/utilities/artifact_producer.py" campaign-close \
  --artifact-root /absolute/project/.agent_reports \
  --campaign /absolute/project/.agent_reports/campaigns/example/campaign.json \
  --approval-harness claude --approval-session ACTUAL_NATIVE_SESSION_ID
```

현재 snapshot에 결속한 user 승인(정확한 문장, 또는 그 문장을 제시한 assistant 턴 직후의 짧은 동의)을 읽고 actor를 도출한다. Codex는
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
옛 `cycles/<cycle_id>` 배치와 `.cycle.json` 이전 봉인도 manifest·producer·index의
일치로 검증하며, 종료를 위해 디렉터리를 이관하거나 봉인을 다시 쓰게 하지 않는다.

`abandoned`는 봉인 상태로 집계되며 성공으로 바뀌지 않는다. 미종결 route의
실패 marker도 그대로다. `residual=0`이나 모든 cycle 성공 조건은 추가하지 않는다.
자유문 criterion의 의미적 충족은 사용자가 명시적으로 수락하며, 코드가 임의로
자연어 목표를 달성했다고 판정하지 않는다.

`finalize --allow-open-route`로 route가 열린 채 봉인된 cycle은 manifest에
`state: active`로 남는다(D-6). 그 route가 나중에 닫히면 — 증명 있게든 없게든
— 서명 행에 사실 4개가 결속된다: `route_closed`, `terminal_gate_proven`,
`terminal_gate_reasons`, `route_outcome_digest`(route 닫힘 sidecar의 그 시점
값이며 다시 관측하지 않는다). 표시 이름표 `sealed-unproven`은 이 서명 밖의
계산값이라 나중에 바꿔도 이미 나간 승인 문장을 깨지 않는다. route가 아직
열려 있으면 종료 자체가 `campaign-cycle-provisional-active`로 거부되며, 안내는
`capability-route.py complete` 뒤 `close`를, 또는 증명 없이 닫으려면
`close --allow-unproven`을 가리킨다 — 이미 봉인된 cycle의 재-`finalize`는
가리키지 않는다(`finalize-state-conflict`이기 때문). route 닫힘 sidecar가
기록과 다른 정체를 가리키면 `campaign-cycle-route-outcome-mismatch`다. 이
장치는 campaign 종료 화면의 표시일 뿐이며, lifecycle·Fleet·Cairn 화면은
manifest의 `state: active`를 그대로 보여 준다는 한계가 남는다.

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

검증 기록: 신규 집중 19건 PASS. isolated runner에서 producer, manifest,
lifecycle, index, reader, relayout 및 신규 campaign suite가 통과했다. 생성
20그룹·적응 경계·표면 예산 검사도 통과했으며 기존 예산 경고의 상한을
올리지 않았다. 실제 BC 경로의 승인 없는 close는 exit 65로 거부됐고
campaign·cycle·manifest·route 기록 65개가 그대로였다. 로컬 근거는
`/tmp/campaign-close*-suites.tsv`, `/tmp/bc-campaign-close-readonly.json`,
`/tmp/bc-campaign-no-approval-observation.json`이다.

## 배포·적용 결과

구현 `9ad69506`과 옛 배치 호환 `27171dbf`를 main에 반영·푸시했다.
[v2.141.0](https://github.com/dmlguq456/hearting/releases/tag/v2.141.0)의 정확한
source commit은 `27171dbf7763df0470878e345bcc574941b126bb`다.
[Release 작업](https://github.com/dmlguq456/hearting/actions/runs/34756670855)의
입력 검증·패키징·게시·실제 게시본 설치 smoke가 모두 통과했다.

2026-09-13 관리형 `harness update --version v2.141.0 --yes --json`으로
Claude/Codex/OpenCode를 설치했다. strict runtime doctor와 verify는 exit 0,
세 하네스 fresh, drift 0이다. 설치본의 변경 파일 7개가 release source와 일치했고
설치본 집중 19건도 통과했다. 사용자 설정·인증 파일 11개는 설치 전후 내용이 같다.
archive SHA256은 `226073ade1a217d2eee441372862f6d9c6fa24edeb012b36982988c9800f7c91`이다.

설치본의 실제 BC 조회도 16 cycle 검증을 통과해 `awaiting-user-acceptance`다.
BC campaign을 종료하지 않았다. 기존 세션은 이전 release에 고정돼 있을 수 있으므로
명령을 즉시 쓸 때는 `/home/Uihyeop/.local/share/hearting/releases/v2.141.0/utilities/artifact_producer.py`
를 직접 지정한다. 새 Claude/Codex 세션 또는 OpenCode 재시작부터 새 설치본을 쓴다.
설치 근거는 `/tmp/campaign-installed-evidence.json`,
`/tmp/campaign-runtime-{update,doctor,verify}.json`,
`/tmp/bc-campaign-installed-status.json`에 보존했다.
