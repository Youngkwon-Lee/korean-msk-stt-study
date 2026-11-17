# 고정 표본으로 확대 비교 실행하기

[계획 v0.2](publication-plan-v0.2.md)와 [현재 준비 집계](../results/2026-11-18-preparation/README.md)를 먼저 읽는다. 검증한 범위는 합성 입력의 선택·해시·재개·집계 계약이다. 확대 GPU/API 실행을 완료했다는 증거가 아니다.

## 선택과 원음 검증

`prepare_corpus.py`는 WAV/TXT를 파일명으로 연결하고 기존 개발 정답과 같은 정규화 문구를 전 코드에서 제외한다. 폴더 코드는 화자가 아닌 출처 계층이다. 코드 내부 중복 문구는 제거하고 코드 간 같은 문구는 같은 `text_group`으로 표시한다. 정답 캐시는 전체 TXT 파일명 집합과 일치해야 한다. 원본 경로·ID·정답·선택 목록은 비공개다.

```sh
python3 -B scripts/prepare_corpus.py \
  --audio-root /path/to/authorized/audio --text-root /path/to/authorized/labels \
  --exclude-metadata /path/to/private/development-metadata.json \
  --reference-cache /path/to/private/reference-cache.json \
  --per-stratum 300 --seed 20261117 --private-out private-runs/new-selection
```

캐시는 `[{"name":"synthetic.txt","reference":"합성 문장"}]` 형식이다. 개발 메타데이터는 `kind: labels`와 정확한 `source_name`을 포함한다. `--per-stratum 0`은 적격 개수만 점검한다. 기존 고정 표본은 다시 추첨하지 않는다.

권리·정답 검토 뒤 원음의 크기·WAV 헤더·SHA-256을 확인한다. 승인된 경로로 복사한 원음을 사용할 경우 동일 WAV 파일명을 유지한다. 크기가 달라지거나 파일이 없으면 중단한다. 새 명세에 원래 선택 지문을 기록하며 선택 파일을 덮어쓰지 않는다.

```sh
python3 -B scripts/seal_manifest.py \
  --manifest /path/to/private/selection/manifest.json \
  --expected-selection-sha256 SELECTION_SHA256 \
  --audio-root /path/to/authorized/materialized-wav \
  --private-out private-runs/run-ready-manifest.json
```

지원하지 않는 원음은 자동 변환하지 않는다. 전처리가 필요하면 두 모델에 동일 규칙과 입력 지문을 고정한다. 첫 음성 해시 생성이 과거 실행 원음과의 동일성을 소급 증명하지 않는다.

## 직렬 실행과 재개

실행기는 모델을 설치하거나 GPU 서버를 시작·예약하지 않는다. 기존 작업 소유권·GPU 잠금·서버 버전을 실행 환경에서 확인한다. 실행 메타데이터 JSON에 revision/가중치·패키지·GPU/드라이버·디코딩·언어·워밍업·호출 위치를 기록하고 자격증명은 넣지 않는다.

```sh
python3 -B scripts/run_manifest.py \
  --manifest private-runs/run-ready-manifest.json \
  --expected-manifest-sha256 RUN_MANIFEST_SHA256 \
  --provider gpt-transcribe --condition-id gpt-transcribe-frozen-condition \
  --execution-metadata /path/to/private/execution-metadata.json \
  --external-audio-use-authorized --private-out private-runs/gpt-expanded
```

키는 승인된 프로세스 환경에서만 읽는다. 외부 이용 플래그는 확대 범위의 권리·처리 조건을 확인했을 때 사용한다. Qwen은 `--provider qwen-service --service-source /path/to/service-source --callers /path/to/private/callers.json --base-url APPROVED_BASE_URL`과 동일 실행용 명세를 사용한다.

같은 명령으로 재개하면 성공 항목은 재호출하지 않는다. 코드·설정·명세가 바뀌면 새 실행 폴더가 필요하다. 요청 전 intent와 결과를 flush/fsync/원자적 교체로 남기고 실행 폴더에 단일 작성자 잠금을 둔다. `.pending` 또는 `in_flight`가 남으면 자동 삭제·재호출하지 않고 실제 응답 여부를 조정한다. 명시적 실패는 자동 재시도하지 않으며 근거 확인 후 `--retry-failed`로 한 번만 추가 시도한다. 원래 실패도 보존한다.

공급자 응답 본문·예외 문자열은 오류 로그에 남기지 않는다. 정답·전사·개별 시간은 비공개 영수증에 보존한다. 성공한 API 요청 직후 로컬 기록 이전에 프로세스가 중단될 가능성을 없앤 것은 아니며, 모호한 요청을 자동 재호출하지 않는 설계다.

## 대응 집계

```sh
python3 -B scripts/analyze_manifest.py \
  --manifest private-runs/run-ready-manifest.json \
  --expected-manifest-sha256 RUN_MANIFEST_SHA256 \
  --run-a private-runs/gpt-expanded --run-b private-runs/qwen-expanded \
  --repetitions 2000 --seed 20261117 \
  --public-out private-runs/review-before-publication.json
```

누락·처리 중인 항목은 완료 집계로 내보낼 수 없다. 전체 실패율, 공통 성공 집합의 대응 CER, 실패를 정답 전체 삭제로 가정한 민감도 값을 구분한다. 문구별 대응 재표집은 미확인 화자 의존성을 보정하지 못한다. 모집단 p값·임상 안전성·미평가 용어 오류 0건을 생성하지 않는다. 출력은 공개 후보이므로 내용 검토 뒤 집계만 명시적으로 공개한다.

현재 미완료: 화자 메타데이터·확대 이용 조건·300개 원음–정답 검토·1,800개 원음 해시와 추론·용어/임상 의미 주석·실제 모델 환경의 재개 동작. 합성 테스트는 모델 정확도나 공급자 과금 내역을 검증하지 않는다.
