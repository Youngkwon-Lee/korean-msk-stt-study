"""Adapters reconstructed from the historical experiment; optional local runtimes only.

No model weights are downloaded by this module. Run each family in its own
already-provisioned environment. GPU inference has not been rerun for this release.
"""
import wave
from pathlib import Path

from metrics import strip_nemotron_metadata


def whisper_audio(path):
    import numpy as np
    from scipy.signal import resample_poly

    with wave.open(str(path)) as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
            raise ValueError("Expected mono 16-bit PCM WAV")
        rate = audio.getframerate()
        pcm = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
    samples = pcm.astype(np.float32) / 32768.0
    if rate != 16000:
        samples = resample_poly(samples, 16000, rate).astype(np.float32)
    return samples


def load_backend(family, model_path, device="cuda", vad=False):
    path = Path(model_path).resolve(strict=True)
    if family == "whisper":
        if not path.is_dir():
            raise ValueError("Whisper requires a local model directory")
        from faster_whisper import WhisperModel
        model = WhisperModel(str(path), device=device,
                             compute_type="float16" if device == "cuda" else "int8",
                             local_files_only=True)

        def transcribe(wav):
            segments, _ = model.transcribe(whisper_audio(wav), language="ko",
                                          beam_size=5, vad_filter=vad)
            return " ".join(segment.text.strip() for segment in segments)
        return transcribe

    if device != "cuda":
        raise ValueError("This reconstruction supports Qwen/Nemotron only on CUDA")
    import torch

    if family == "qwen":
        if not path.is_dir():
            raise ValueError("Qwen requires a local model directory")
        from qwen_asr import Qwen3ASRModel
        model = Qwen3ASRModel.from_pretrained(
            str(path), dtype=torch.float16, device_map="cuda:0",
            attn_implementation="eager", max_inference_batch_size=1,
            max_new_tokens=256, local_files_only=True)

        def transcribe(wav):
            with torch.inference_mode():
                text = model.transcribe(audio=str(wav), language="Korean")[0].text
            torch.cuda.synchronize()
            return text
        return transcribe

    if family == "nemotron":
        if not path.is_file() or path.suffix != ".nemo":
            raise ValueError("Nemotron requires a local .nemo checkpoint")
        from nemo.collections.asr.models import ASRModel
        from omegaconf import OmegaConf
        from nemo.collections.asr.models.rnnt_bpe_models_prompt import RNNTPromptTranscribeConfig
        model = ASRModel.restore_from(str(path), map_location="cpu").to("cuda:0").eval()
        original_loader = model._setup_dataloader_from_config

        def korean_loader(config):
            config = OmegaConf.create(OmegaConf.to_container(config, resolve=True))
            config.use_lhotse = False
            config.default_lang = "ko-KR"
            config.default_prompt_mode = "langID"
            return original_loader(config=config)
        model._setup_dataloader_from_config = korean_loader
        config = RNNTPromptTranscribeConfig(use_lhotse=False, target_lang="ko-KR",
                                           batch_size=1, num_workers=0, verbose=False)

        def transcribe(wav):
            with torch.inference_mode():
                output = model.transcribe([str(wav)], override_config=config)[0]
            torch.cuda.synchronize()
            text = output.text if hasattr(output, "text") else str(output)
            return strip_nemotron_metadata(text)
        return transcribe
    raise ValueError("Unknown backend family")
