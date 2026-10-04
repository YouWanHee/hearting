# Lab 실행 저장소와 명시적 데이터 접근

`autopilot-lab` owner를 시작하거나 재개하면 초기화된 compute-hosts inventory의
정확한 `run_root`가 기존 `execution_access_v1` 요청에 포함된다. Inventory는
`COMPUTE_HOSTS_CONFIG`, 또는 `${XDG_CONFIG_HOME:-~/.config}/hearting/compute-hosts.yaml`에서
기존 loader로 읽는다. GPU 작업과 compute-hosts resource 실행의 로그·PID 저장소를
owner의 정상 접근 범위 안에 두기 위한 기본값이다. 일반 code owner와 frame에는
이 기본값을 추가하지 않는다. Resource child는 실제 부모 grant 안에서만 접근한다.

Inventory가 없거나 주석 템플릿 상태라면 경로를 추측하지 않는다. 잘못된 inventory,
홈 전체 같은 넓은 root, symlink로 넓은 root에 도달하는 요청은 기존 검증에서 거부한다.
준비 과정은 inventory를 읽고 요청 파일만 만들며 실제 `run_root`를 만들거나 작업을
시작하지 않는다.

## 외부 데이터 경로 지정

사용자가 승인한 정확한 경로를 기존 JSON 형식으로 지정한다. 다음 경로는 예시이며
실제 승인된 경로로 바꿔야 한다. 자유로운 작업 설명에 나온 경로는 grant로 추정하지 않는다.

```json
{
  "schema_version": 1,
  "writable_roots": ["/data/project/train-valid"],
  "read_roots": [],
  "network": {"required": false, "reason": "", "hosts": []},
  "enforcement_required": "os-sandbox",
  "justification": {
    "/data/project/train-valid": "사용자가 승인한 데이터 처리 및 결과 저장 경로"
  }
}
```

파일을 저장한 뒤 기존 start 또는 resume 명령을 실행하는 세션에 전달한다.

```sh
export AGENT_DISPATCH_EXECUTION_ACCESS_FILE=/absolute/path/lab-access.json
# 기존 preflight.sh compose --start ... 또는 그 receipt의 resume_command 실행
```

Lab 준비는 `load_request`로 이 파일을 검증하고 inventory의 `run_root`만 더한 사본을
기존 dispatch state의 route별 `execution-access` 폴더에 저장한다. 원본 파일은 수정하지
않는다. 시작·재개에 같은 입력을 유지하면 같은 준비 파일을 재사용한다. 승인 입력이
바뀌면 기존 route binding 검사가 변경을 알린다. 명시적 JSON은 preview ROOTS 입력보다
우선한다. 낮은 수준의 직접 dispatch에서도 기존 `--execution-access-file <JSON>`을
쓸 수 있다. 이 직접 경로에서는 필요한 모든 root를 해당 JSON에 명시한다.

Owner 전달과 child 전달은 기존 환경 변수·CLI·effective-grant 경로를 사용한다.
Child 요청이 부모의 실제 파일 또는 네트워크 범위를 넘으면 모델을 시작하기 전에
거부된다. 별도의 승인 절차나 전역 permission 설정은 필요하지 않다.

## 적용 수준과 현재 한계

Codex의 `workspace-write`에서는 writable root를 OS sandbox에 적용한다. 로컬
`codex-cli 0.160.0`의 exec는 `--add-dir`, Hearting App Server runner는
`--writable-root`로 같은 검증 결과를 전달한다. 일반 code 실행의 기존 기본 경로와
request가 없는 builder argv는 유지한다. 공식 Codex 설정도 파일 경로별 permission과
workspace 확장을 지원한다. [Codex permissions 설정](https://learn.chatgpt.com/docs/config-file/config-reference)

현재 Hearting builder는 추가 `read_roots`를 읽기 전용 grant로 투영하지 않는다.
그 입력을 보존하되 `read-roots-unprojected`로 보고하며, `os-sandbox`를 요구하면 거부한다.
현재 지원되는 외부 데이터 grant가 필요하면 승인된 최소 writable root를 명시한다.

SSH 등 네트워크가 필요하면 기존 `network.required=true`와 `reason`을 명시하고 기존
owner 네트워크 정책을 따른다. `network.hosts`는 현재 호스트별 격리를 보장하지 않는다.
`any`에서는 미적용 상태를 보고하고, 호스트 제한과 `os-sandbox`를 함께 요구하면 거부한다.
Codex 공식 문서 역시 호스트별 네트워크 규칙을 적용하려면 proxy enforcement가 필요하다고
설명한다. [Codex 네트워크 설정](https://learn.chatgpt.com/docs/config-file/config-reference)

Claude·OpenCode에서는 같은 요청을 기존 tool-permission 수준으로 적용하며 OS sandbox와
동등하다고 보고하지 않는다. Claude의 additional directory 권한과 sandbox는 별도
설정이다. [Claude permissions](https://code.claude.com/docs/en/permissions)
