# K-Equity Research

> **공시(DART)·재무·시장 데이터와 AI 에이전트를 결합한 한국 주식 이벤트 기반 리서치 자동화 플랫폼**

![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)
![Package Manager](https://img.shields.io/badge/Manager-uv-brightgreen.svg)
![Architecture](https://img.shields.io/badge/Architecture-Contract_Guarded-blueviolet.svg)
![Quality](https://img.shields.io/badge/Quality-Strict_Typing-success.svg)

---

## 1. 개요 (Overview)

`k-equity-research`는 DART 공시, 기업 재무와 KRX 시장 데이터를 결합해 근거가 있는 이벤트 리서치 메모를 생성하려는 프로젝트입니다. V1은 자기주식 취득 결정 공시에 집중하며, 컨센서스와 어닝 서프라이즈는 후속 단계에서 검토합니다.

현재는 초기 설계 단계입니다. V1 범위와 평가 기준은 [프로젝트 개발 계획](docs/project-development-plan.md)에, 실제 데이터·API 검증 결과는 [probe findings](docs/probe-findings.md)에, 구현 순서와 함수별 계약은 [V1 구현 명세](docs/specs/README.md)에 정리했습니다.

---

## 2. 개발 및 검증 환경 (Quick Start)

### 가상환경 및 의존성 동기화
```bash
# uv를 통한 가상환경 구축 및 패키지 동기화
uv sync
```

### 코드 맵 갱신
```bash
# 아키텍처 및 소스 모듈 맵 동기화
uv run python tools/agent_skills/gen_code_map.py
```

### 린트, 타입, 테스트 및 Diff 커버리지 통합 검증
```bash
# Lean Check 실행
uv run python tools/agent_skills/lean_check.py
```

### 단위 테스트 실행
```bash
# pytest 병렬 실행
uv run pytest
```

---

## 3. 프로젝트 구조 (Directory Structure)

```text
k-equity-research/
├── .agents/            # AI 에이전트 핵심 행동 지침 및 도메인 룰 (rules, skills)
├── .claude/            # Claude Code 호환 심볼릭 링크 및 설정
├── .codex/             # Codex CLI 및 Serena MCP 서버 설정
├── docs/               # 아키텍처, 코드 맵, 기술 명세서
├── src/                # 핵심 프로덕션 코드
│   ├── config/         # 환경 설정 (Settings, pydantic)
│   └── core/           # 핵심 비즈니스 및 도메인 로직
├── tests/              # 테스트 스위트 (unit, integration)
├── tools/              # 에이전트 검증 스크립트 (lean_check, gen_code_map 등)
└── pyproject.toml      # 프로젝트 메타데이터 및 의존성 명세
```
