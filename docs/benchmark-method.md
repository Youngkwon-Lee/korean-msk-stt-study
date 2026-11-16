# 비교 코드의 범위와 실행 방법

이 코드는 2026-11-06 기록에서 복원한 비교 방법의 구현입니다. 원격 서버의 원래 실행 파일을 그대로 보관한 아카이브가 아닙니다. GPU 모델을 다시 실행해 역사적 집계값이 재현됐다고 주장하지 않습니다.

## 입력과 출력

- 입력은 같은 이름의 `.wav`와 UTF-8 `.txt` 쌍입니다. 이용 권한·연구 동의가 확보된 자료를 저장소 밖이나 `private-data/`에 둡니다.
- 입력의 `staff_`와 `patient_` 접두어는 그룹 집계에만 사용합니다. 그 외 접두어는 `other`로 처리합니다.
- 기본 출력은 집계 JSON이며 음성·문장별 전사·파일명 목록을 출력하지 않습니다. 기존 결과 파일은 덮어쓰지 않으며 새 JSON 권한은 600입니다.
- 추론 라이브러리 자체의 진단 로그에는 경로나 식별정보가 포함될 수 있으므로 로그도 승인된 비공개 환경에서 관리합니다.

## 설치와 가중치

새 설치를 자동으로 수행하지 않습니다. 모델별로 이미 준비된 독립 환경에서 실행합니다.

- Whisper 환경: `faster-whisper`, `numpy`, `scipy` 및 호환 CUDA 라이브러리. 원래 실험은 16-bit mono WAV를 float32로 변환하고 필요하면 `resample_poly`로 16 kHz로 변환했습니다.
- Qwen 환경: `qwen_asr`, `torch` 및 호환 Transformers/CUDA 구성.
- Nemotron 환경: NeMo ASR, `torch`, `omegaconf` 및 호환 CUDA 구성. `.nemo` 체크포인트를 직접 지정합니다.

정확한 과거 환경 잠금파일이 이 저장소에 없으므로 실행한 패키지 버전과 가중치 revision·해시를 별도 manifest로 보존해야 합니다. 상호 호환되는 버전을 임의로 최신 설치하거나 드라이버를 바꾸지 마세요. CUDA 라이브러리 탐색 경로도 기존 환경에 맞춰야 합니다.

## 7개 비교 조건

가중치 위치는 예시입니다. 실제 원음·경로·서버 주소를 Git에 넣지 마세요. 먼저 해당 GPU의 기존 작업과 사용 권한을 별도로 확인합니다. 코드는 GPU 자원을 예약하지 않습니다.

```sh
# large-v3와 turbo 각각 --vad 없이, 그리고 --vad를 붙여 실행: 네 조건.
"$WHISPER_PYTHON" -B scripts/benchmark.py \
  --family whisper --model /path/to/local/whisper-model \
  --label large-v3-turbo --data /path/to/authorized/pairs \
  --gpu 0 --out private-runs/turbo-vad-off.json

# Qwen 0.6B와 1.7B 각각: 두 조건.
"$QWEN_PYTHON" -B scripts/benchmark.py \
  --family qwen --model /path/to/local/qwen-model \
  --label Qwen3-ASR-1.7B --data /path/to/authorized/pairs \
  --gpu 0 --out private-runs/qwen-1.7b.json

# Nemotron 0.6B: 한 조건.
"$NEMOTRON_PYTHON" -B scripts/benchmark.py \
  --family nemotron --model /path/to/local/model.nemo \
  --label Nemotron-3.5-ASR-0.6B --data /path/to/authorized/pairs \
  --gpu 0 --out private-runs/nemotron.json
```

Whisper는 한국어·beam=5·CUDA float16, Qwen은 Korean·float16·eager attention·batch=1·max_new_tokens=256을 사용합니다. Nemotron은 기존 실험처럼 인스턴스에만 legacy loader와 ko-KR/langID 설정을 적용하며 설치 패키지를 변경하지 않습니다. 해당 private API는 다른 NeMo 버전에서 달라질 수 있습니다. Nemotron 정밀도는 별도 강제하지 않습니다.

## 계산 방법과 역사적 비교

CER = (대체+삭제+삽입) / 정답 글자 수. 전체 CER는 문장별 CER의 평균이 아니라 오류 수와 정답 글자 수를 각각 합쳐 계산합니다. 완전 일치는 같은 정규화를 적용한 문자열이 일치한 문장 수입니다.

역사적 정규화는 `(철자)/(발음)`에서 뒤쪽을 선택한 뒤 숫자·영문자·완성형 한글만 남기고 소문자로 변환합니다. Nemotron의 끝부분 `<ko-KR>`만 발화가 아닌 출력 메타데이터로 먼저 제거합니다. 이 필터는 단위와 기호를 제거할 수 있으므로 향후 임상 중요 오류 판정은 원형 전사·원음을 기준으로 해야 합니다.

배속은 음성 길이/전사시간이고 모델 적재시간은 별도입니다. GPU 메모리는 로딩과 추론 동안 1초 간격으로 관측한 물리 GPU 전체 메모리입니다. 관측이 불가능하면 `null`이며 0으로 위조하지 않습니다. 실제 프로세스 최대 메모리나 다른 작업과 분리한 순수 모델 메모리가 아닙니다.

복원 코드에는 입력 검증, 집계 전용 출력, 덮어쓰기 방지와 각 추론 호출의 CUDA 동기화가 추가돼 있습니다. 과거 측정은 모델별 스크립트와 외부 메모리 관측을 사용했으므로 시간·메모리 계측도 완전히 같은 실행물이 아닙니다. 새로운 실행은 기존 결과를 대체하지 않고 별도 날짜·버전의 결과로 기록해야 합니다.

## 검증 범위

현재 공개 코드에서 검증하는 것은 정규화·편집거리·가중 집계, 끝 언어 표식 처리, 비공개 파일 권한·덮어쓰기 방지, 합성 WAV→모의 backend→집계 흐름과 CSV/JSON 수치의 일치입니다. 모델 다운로드, 실제 GPU 추론, 긴 상담 정확도, 새 환경 호환성 검증은 이번 공개 준비에 포함되지 않습니다.
