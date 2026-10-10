"""User vocabulary shared by Fleet's group and process views; no state judgments."""
from .model import project_of

LABELS = {
    "code": "개발", "autopilot-code": "개발", "lab": "실험", "autopilot-lab": "실험",
    "spec": "설계", "research": "조사", "draft": "작성", "refine": "교정",
    "design": "디자인", "apply": "적용", "ship": "배포", "audit": "감사",
    "plan": "계획", "code-plan": "계획", "plan-check": "계획 검토",
    "execute": "구현", "exec": "구현", "code-execute": "구현",
    "test": "검증", "code-test": "검증", "verify": "검증", "run-verify": "실행 검증",
    "report": "결과 전달", "code-report": "결과 전달", "setup": "준비",
    "route-frame": "방향 검토", "frame": "방향 검토", "frame-replica": "방향 교차검토",
    "scaffold": "구조 준비", "smoke": "동작 검증", "aggregate": "결과 취합",
    "session-tidy": "세션 정리", "session-tidy-memory": "기억 정리",
    "working": "작업 중", "idle": "대기", "unknown": "미확인", "blocked": "입력 대기",
    "done": "종료", "running": "실행 중", "queued": "차례 대기",
    "preparing": "준비 중", "steer": "지시", "watch": "관찰", "notice": "알림",
    "message": "메시지", "retire": "정리", "delay": "예약", "owner": "담당",
    "stage": "단계", "support": "도우미", "review": "검토",
    "resource-exit": "실행 종료 확인", "lab-run-verify": "실험 실행 검증",
    "lab-setup-handoff": "실험 준비 전달", "lab-setup": "실험 준비",
}


def label(value):
    return LABELS.get(str(value), str(value)) if value else ""


def project(cwd):
    return project_of(cwd) if cwd else "프로젝트 미확인"
