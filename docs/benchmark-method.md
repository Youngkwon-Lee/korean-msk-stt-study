# 비교 코드의 범위와 실행 방법

이 코드는 2026-11-06 기록에서 복원한 비교 방법의 구현입니다. 원격 서버의 원래 실행 파일을 그대로 보관한 아카이브가 아닙니다. 최초 공개 후 [2026-11-16 실제 GPU 재실행](../results/2026-11-16/README.md)을 완료했으며, 7조건의 CER와 완전 일치 문장 수가 기존 집계와 일치했습니다. 전사 구현 세 파일의 정확한 해시와 실행 조건은 해당 결과의 manifest에 있습니다.

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

## 순차 실행과 실행 증거 보존

Linux의 `scripts/run_suite.py`는 기존 모델 환경을 순차 실행하며 **이미 존재하는 GPU별 파일 잠금**을 사용합니다. 운영자가 현재 점유·작업 소유권을 먼저 확인한 뒤 실행합니다. 잠금이 점유됐거나 카드 전체 메모리 사용량이 2,048 MiB 초과 또는 사용률이 10% 초과면 시작하지 않습니다. 이 시점 검사는 GPU 예약이나 모든 외부 프로세스의 협조를 보장하지 않습니다.

비공개 JSON 설정에는 `expected_input_sha256`과 `models` 배열을 지정합니다. 각 모델 항목은 `family`, 공개용 `label`, 기존 실행 파일 `python`, 가중치 `path`를 가지며 Whisper에는 `vad_conditions: [false, true]`를 넣습니다. 필요한 기존 CUDA 라이브러리 디렉터리는 `library_dirs`에 지정합니다. 같은 Python/NumPy ABI에서 호환성을 확인한 기존 패키지를 작업 전용 경로에서 재사용할 때만 `python_paths`를 지정하고, 그 환경으로 조회한 버전을 manifest에 남깁니다. 설정에는 서버 경로가 있으므로 공개하지 않습니다.

```sh
"$CONTROL_PYTHON" -B scripts/run_suite.py \
  --config /path/to/private-config.json --data /path/to/authorized/pairs \
  --out /path/to/new-private-run --gpu 1 --lock /path/to/existing-slot-1.lock
```

실행기는 입력의 전후 지문, 코드 SHA-256, 모델 파일 SHA-256, 패키지 버전, 시각, 조건별 종료 코드와 집계 결과를 보존합니다. 입력 지문은 이름순 WAV/TXT 파일마다 UTF-8 파일명과 파일 내용의 이진 SHA-256을 이어 붙인 뒤 다시 SHA-256을 계산합니다. 공개되는 것은 이 전체 지문이며 원본 파일명 목록·파일별 정답·전사는 포함하지 않습니다. 모델마다 새 프로세스를 사용하고 자동 재시도하지 않습니다. 로그와 설정은 비공개로 유지하며, manifest도 공개 전에 별도 검토합니다.

## 계산 방법과 역사적 비교

CER = (대체+삭제+삽입) / 정답 글자 수. 전체 CER는 문장별 CER의 평균이 아니라 오류 수와 정답 글자 수를 각각 합쳐 계산합니다. 완전 일치는 같은 정규화를 적용한 문자열이 일치한 문장 수입니다.

역사적 정규화는 `(철자)/(발음)`에서 뒤쪽을 선택한 뒤 숫자·영문자·완성형 한글만 남기고 소문자로 변환합니다. Nemotron의 끝부분 `<ko-KR>`만 발화가 아닌 출력 메타데이터로 먼저 제거합니다. 이 필터는 단위와 기호를 제거할 수 있으므로 향후 임상 중요 오류 판정은 원형 전사·원음을 기준으로 해야 합니다.

배속은 음성 길이/전사시간이고 모델 적재시간은 별도입니다. GPU 메모리는 로딩과 추론 동안 1초 간격으로 관측한 물리 GPU 전체 메모리입니다. 관측이 불가능하면 `null`이며 0으로 위조하지 않습니다. 실제 프로세스 최대 메모리나 다른 작업과 분리한 순수 모델 메모리가 아닙니다.

복원 코드에는 입력 검증, 집계 전용 출력, 덮어쓰기 방지와 각 추론 호출의 CUDA 동기화가 추가돼 있습니다. 과거 측정은 모델별 스크립트와 외부 메모리 관측을 사용했으므로 시간·메모리 계측도 완전히 같은 실행물이 아닙니다. 새로운 실행은 기존 결과를 대체하지 않고 별도 날짜·버전의 결과로 기록해야 합니다.

## 검증 범위

단위 테스트는 정규화·편집거리·가중 집계, 끝 언어 표식 처리, 비공개 파일 권한·덮어쓰기 방지, 합성 WAV→모의 backend→집계 흐름, 공유 잠금 유지와 결과 파일 간 일치를 확인합니다. 모의 테스트는 모델 검증이 아닙니다. 별도의 2026-11-16 자연 환경 증거는 기존 CUDA 환경의 실제 36문장·5모델·7조건 전사입니다. 모델 다운로드, 긴 근골격계 상담 정확도, 새 환경 전체의 호환성을 입증한 것은 아닙니다.
