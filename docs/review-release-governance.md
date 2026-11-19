# STT 300건 — 검수 품질·이용권·내보내기 게이트

**상태:** 로컬 품질 게이트 도구 구현/합성 검증. 실제 자료의 외주 제공·학습 허가·독립 2인 검수·전문가 승인 **미완료**. 기술적 게이트가 법률·IRB·계약상 승인 자체를 부여하지 않는다.

## 목표와 기존 흐름

기존 [300건 원음 검수](reference-review.md)는 제공 정답과 모델 출력을 분리하고 검수자의 원음 청취·수정·보류·이력을 별도 SQLite에 보존한다. 기존 [검토 정답 재채점](reference-rescoring.md)은 단일 검수자 300건 완료 후 고정된 모델 결과를 비교하는 **연구 분석**이다. 이번 게이트는 그 분석을 대체하거나 기존 연구를 무효화하지 않고, **별도 이용 목적을 위한 재사용/반출**에 더 엄격한 조건을 적용한다.

### 목적별 경계

| 목적 | v1 조건 | 구현 상태 |
| --- | --- | --- |
| 품질 현황 보고 | 고정 manifest SHA, 기존 DB 형식 확인. 집계 수·해시만 출력 | 로컬 읽기 전용 가능 |
| 승인된 로컬 평가용 정답 묶음 | 서로 다른 검수자 2명 모두 완료, 전 항목 정확한 텍스트 일치, 승인된 이용권·개인정보 검토·독립성·전문가 QA, 승인 파일의 manifest 및 현재 DB 상태 해시 일치 | 사전 게이트와 비공개 **참조 텍스트 JSONL** 내보내기 가능. 별도 수동 승인 전에는 차단 |
| 외부 프리랜서 검수/자료 전송 | 실제 원천별 위탁·제공/보안 법적 근거, 계약·접근통제·기기 제한·감사·회수·철회 체계 | **차단.** 기존 검토자 ID는 인증이 아니며 로컬 UI를 외부에 노출하지 않음 |
| 모델 재학습용 자료 반출 | 학습 목적 사용권, 개인정보/기관·IRB 확인, 대상자·화자·센터 단위 분할 및 고정 홀드아웃, 독립 QA | **차단.** train/validation/test 분할·학습 exporter 미구현 |

**승인 전 기본값은 `deny`.** 공개 코퍼스를 내려받아 연구하는 것, 검수 작업을 위탁하는 것, 서비스 모델을 추가 학습시키는 것, 전체 전사를 공개하는 것은 서로 다른 권한이다. 기존 [연구 참가 동의서 초안](forms/recording_consent.md)은 모델 재학습을 허용하지 않으며 실제 환자 동의/기관 승인의 증거가 아니다. 공개 코퍼스의 약관 또한 별도 확인해야 한다.

## 1. 읽기 전용 품질 현황 확인

보호된 장치에서 기존 manifest·DB를 그대로 사용한다. 이 도구는 원음·개별 전사·검수 메모·파일 경로·개인 식별자를 **표준 출력으로 출력하지 않는다**. 사용자/자료가 확인되지 않은 실험 환경이나 공유 로그에 원문을 넣지 않는다.

```sh
python3 -B scripts/review_release_gate.py \
  --manifest /path/to/private/run-ready-manifest.json \
  --expected-manifest-sha256 EXPECTED_64_HEX \
  --review-db /path/to/private/reviews.sqlite3 \
  --reviewer reviewer-1 --second-reviewer reviewer-2 \
  --expected-count 300
```

출력은 `first_review`, `second_review`, `both_reviewed`, `exact_agreements`, `disagreements`의 **집계**와 `review_state_sha256`, 목적별 `ready`/`blockers`이다. 누락·보류·초안은 완료로 세지 않는다. 첫 검수자 ID만 입력하면 두 번째 독립 검수 조건은 계속 미충족이다. 한 사람이 다른 검수자 ID로 접속해도 독립성이 입증되지 않는다. 실제 두 사람이 독립 검수했다는 검증은 별도 관리자 책임이다.

**현재 실행 시점(2026-11-19):** 300건 중 1차 검수 1건 완료, 2차 검수 0건, 동일 항목에 대한 2인 완료 0건. 공식 승인 파일 없음. 평가용 정답 내보내기·외부 검수·모델 학습이 모두 차단되는 것이 정상이다. 이 수치는 당시 비공개 로컬 DB 읽기 결과이며 이후 작업으로 변경될 수 있다.

## 2. 별도 승인 기록 계약 (비공개)

실제 관리자·기관/데이터 권리자가 증거를 확인한 **후에만** 아래 속성으로 만든다. 임의로 `true`를 채우거나 형식만 맞춰 실제 승인을 주장하지 않는다. 승인 원본과 감사자료는 Git/Second Brain/일반 공유 폴더에 올리지 않는다. 소유자는 이 파일의 **실제 바이트 SHA-256**도 별도로 확인·고정해야 한다. 이 해시는 파일 변경 감지 수단이지 서명이나 법적 권한 인증이 아니다.

```json
{
  "schema": "review-release-approval/v1",
  "approval_id": "PLACEHOLDER_PRIVATE_APPROVAL_ID",
  "approved_by_role": "dataset-steward",
  "evidence_record_id": "PLACEHOLDER_PRIVATE_EVIDENCE_ID",
  "approved_on": "YYYY-MM-DD",
  "expires_on": "YYYY-MM-DD",
  "manifest_sha256": "MANIFEST_SHA256",
  "review_state_sha256": "REVIEW_STATE_SHA256_FROM_PREFLIGHT",
  "source_reuse_rights_verified": false,
  "privacy_review_passed": false,
  "reviewer_independence_verified": false,
  "expert_qa_signed_off": false,
  "external_processor_contract_verified": false,
  "model_training_rights_verified": false,
  "scopes": {
    "local_evaluation": false,
    "external_review": false,
    "model_training": false
  }
}
```

- 권한 기록은 데이터 원천/버전, 목적, 승인 책임자, 원천 계약·동의/기관 절차 증거, 위탁 대상과 처리 장소, 보관 기간·철회/삭제 절차를 **비공개 시스템에서 별도로** 연결한다. 이 도구는 참조 ID의 실재/권한과 실제 위탁 계약을 자동 검증하지 않는다.
- 고정 manifest의 내용이 바뀌거나 검토문/상태/버전이 수정되면 `review_state_sha256`이 바뀌므로 이전 승인 재사용을 거부한다. 서명 후 실데이터 변경이 있을 경우 새 검토·새 승인으로 진행한다.
- 확인된 완전 일치도 임상적 정확성의 보증이 아니다. 의학용어·수치·좌우·부정·화자·난청 구간은 원음과 대조하여 전문 QA에서 검증한다. 두 검수자가 불일치하면 판정과 근거를 별도 승인 기록에 남기고 재확인한다. 단순히 두 텍스트를 복사해 같게 만드는 것은 독립 검수가 아니다.

## 3. 승인된 **로컬 평가용** 비공개 내보내기

2인 전건 완료·전건 동일·실제 승인 확인 후에만 아래 수동 명령을 사용할 수 있다. `private-runs/` 아래 이미 존재하는 부모 폴더를 지정하고 **새로운** 하위 목적지를 쓴다. 기존 결과를 덮어쓰지 않는다.

```sh
python3 -B scripts/review_release_gate.py \
  --manifest /path/to/private/run-ready-manifest.json \
  --expected-manifest-sha256 EXPECTED_64_HEX \
  --review-db /path/to/private/reviews.sqlite3 \
  --reviewer reviewer-1 --second-reviewer reviewer-2 \
  --expected-count 300 \
  --approval-file /path/to/private/approval.json \
  --expected-approval-sha256 EXPECTED_APPROVAL_64_HEX \
  --purpose local_evaluation \
  --audio-root /path/to/private/selected-audio \
  --private-out private-runs/approved-local-evaluation-NEW
```

반출 직전 모든 선택 WAV와 제공 정답의 바이트 SHA-256을 고정 manifest와 재대조한다. 자료가 누락되거나 변경되면 반출을 거부한다.

생성되는 `references.jsonl`에는 고정 검토 순서 ID·원음 SHA-256·**승인된 수정 정답 텍스트**만 포함한다. 원음은 복제/전송하지 않으며 `release-manifest.json`은 manifest/검수/승인 및 JSONL 해시를 보존한다. 이 묶음도 원문을 포함하므로 외부 공개 불가. 해당 JSONL은 **학습용 데이터셋이 아니며**, 샘플별 대상자/화자 단위 분할도 포함하지 않는다. 실제 모델 재평가는 기존 동결된 Qwen/GPT 영수증과 정답을 비교하는 [재채점 절차](reference-rescoring.md)를 별도로 따른다. 이 새 도구는 모델을 실행하거나 평가 점수를 계산하지 않는다.

## 4. 외주를 시작하기 전에 확인할 운영 기준

1. 실제 원천 데이터별 학습/검수 목적의 이용권·기관 반출/위탁 승인 상태를 별도로 증빙한다. 어떤 근거가 실제로 유효한지는 승인권자·법률/기관 담당자가 판단한다.
2. 사람별 **인증된 계정**, 최소 권한, 작업 할당, 접속 로그, 다운로드·제3자 전송 제한과 종료 시 권한 회수 경로를 먼저 구현한다. 기존 `reviewer-1`/`reviewer-2` 문자열과 `localhost` 앱은 외주 접속 수단이 아니다.
3. 작업자별 자격 평가·오디오 1분당 실제 검수 시간·수정률·QA 불일치율·재작업 비용을 측정한다. 비용·정확도 개선을 검증하기 전 300건 일괄 외주 계약을 확정하지 않는다.
4. 2인 독립 검수와 임상가 판정, 변환 이력, 데이터셋 버전, 사용 범위/만료/철회, 누락·이탈 관리 정책을 구성한다. 임상용어/수치 의미 평가와 단순 문자 일치율을 혼동하지 않는다.
5. 학습을 결정하면 원천 라이선스·개인/화자/센터 그룹 분할·고정 평가셋, 모델 버전·추론 비용·WER/CER 및 의미 오류, 모델 배포 승인 조건을 별도 문서로 확정한다. 그 전에는 재학습을 시작하지 않는다.

## 검증 및 금지 경계

합성 데이터 테스트는 독립 2인 전건 일치/불일치, 승인 누락·만료·파일 SHA 불일치·수정 후 상태 SHA 변화, 목적별 불허, 원문 미노출, 비공개 출력과 기존 출력 덮어쓰기 금지를 확인한다. 테스트 성공이 환자 데이터 적법성·실제 외주 계약·임상 QA 완료·모델 재학습 허가를 뜻하지 않는다.

**이 개발의 범위:** 별도 읽기 전용 QA와 명시적 승인에 종속된 로컬 평가용 정답 release. 외부 프리랜서 접속/데이터 제공, 모델 학습, 실제 개인정보 처리 승인, 기존 검토 이력·제공 정답 변경은 수행하지 않음.
