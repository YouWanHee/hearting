# Review closure across a continuation

An owner can finish a review disposition after the original review cycle has
ended. The original review remains FAIL; the disposition is the owner's
decision, never an independent PASS. Requiring the new record beside the old
review conflicts with the producer's current-cycle write boundary.

The completion writer owns the handoff. An official continuation can name an
ancestor's exact blocking review in the existing command:

```text
capability-route.py complete --route <continuation.json> --node <review-node> \
  --jobs <canonical-jobs> --attempt-id <source-review-attempt> \
  --evidence <current-cycle>/<round>.owner-closure.md --check
```

The check is read-only. The same command without `--check` revalidates and
publishes only the continuation's gate. This is an operator completion action;
it starts no model, grants no artifact write access, and does not replace an
existing route's runtime pin.

The shared admission proves:

- The destination is an official continuation of the review's exact route and
  hash, with the same artifact root, worktree and review-node contract. Merely
  sharing a campaign, filename or repository is insufficient.
- Review attempts throughout the relevant lineage have settled. Terminated
  rounds exhaust the inherited budget; a continuation does not create a new
  budget. A dead worker is not a completed blocking review.
- The exact source review log still proves FAIL and identifies a readable
  artifact. The owner record names every blocking attempt and its review
  artifact, and states the disposition of its findings.
- The destination producer issued the cycle containing the record. The source
  cycle does not acquire write permission. Current dependencies must be proven
  before the destination gate can be completed.
- The regular `.owner-closure.md` record has flat, nonduplicated frontmatter
  `verdict: closed-by-owner`, matching `node`, and matching `gate` if present.
  Its path is in the artifact root, differs from the reviewed artifact, and
  contains no registry delimiters (comma, equals, tab, newline or control byte).

Publication records the exact review provenance and owner-overridden assurance
without manufacturing a continuation review worker. It preserves source rows,
source verdicts, source markers and sealed payload bytes. Existing completion
history and node locking own idempotent replay; a conflicting disposition is
not silently substituted. Same-route owner closure retains its existing
registered-attempt completion contract and typed `owner-closure-*` refusals.

This recovery concerns review gate authority only. It does not establish product
test adequacy, physical-device coverage, release readiness or deployment success.

## 검증과 책임

완료 writer가 원본 리뷰와 현재 해결 기록의 인수를 소유한다. `complete --check`,
실제 발행, 다음 단계의 start gate가 같은 proof 검증을 사용한다. 기존 회차를
세는 중복 구현을 하나로 합쳐 단일 분절·fallback·batch·closure에 연결했다.
운영자가 원 cycle에 예외로 쓰거나 source FAIL 행을 다시 완료 처리할 필요가 없다.

격리 회귀는 실제 producer가 발급한 두 cycle, 공식 continuation, 실제 종료·회수한
프로세스의 정체성을 사용한다. 원 FAIL 보존, 현재 gate 발행, 공통 start consumer,
정확한 terminal consumer, 회차 미초기화, 읽기 전용 CLI, 반복 호출을 검증했다.
의존 단계 미완료, 열린 review, 관측 불가, terminal conflict, 원 review 내용 변경,
다른 cycle 증거와 변경된 계보는 진행을 허가하지 않는다. marker/history/link 중간
발행 실패도 같은 기록으로 복구하며 link가 없는 동안에는 소비를 보류한다.

검증 기록:

- 완료 marker·dispatch contract 최종 격리 runner exit 0:
  `/tmp/continuation-closure-publication-final.tsv`.
- dispatch-node·fallback·contract 3개 suite 및 batch suite 통과:
  `/tmp/continuation-closure-consumers.tsv`, `/tmp/continuation-closure-batch.tsv`.
- 과거 baseline이 실패로 분류하던 marker 검사 2건도 통과했다(XPASS).
  baseline/기대값을 삭제하지 않았고 `--xpass-nonfatal`로 결과를 구분했다.
- generated projection 20개, adaptation boundary, surface budget 통과.
  기존 bytes 초과는 advisory이며 지시문 상한을 높이지 않았다.
- 실제 home-os exact read-only 검사 exit 0/ready, 원 review 두 행·파일,
  두 route·closure·marker 상태 불변:
  `/tmp/continuation-owner-closure-exact-check.json`.
  이는 기동이나 운영 gate 발행 실측이 아니다. 운영 복구는 제품 담당자가 수행한다.

SD-124/104 연결은 정식 spec transaction으로 PRD v89에 반영했다. 도구가 배정한
직전 v88 snapshot은 원본과 byte 일치하며 다른 component 358개 파일을 보존했다.
새 SD 번호를 만들지 않았다. 유지보수 설계 기준은
[Loop Engineering](../core/LOOP_ENGINEERING.md)에 분리했다.

이 변경으로 과거의 모든 감독·resource 통보 문제가 해결됐다고 주장하지 않는다.
이번 완료 범위는 continuation review disposition의 인수와 소비 경계다.
