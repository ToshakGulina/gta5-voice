#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gta5_f5_tts.py — Zero-Shot Russian TTS & Voice Cloning for GTA V using Misha24-10/F5-TTS_RUSSIAN.

How it works:
  1. Uses the original GTA 5 English audio (`ref_en_wav`) directly as the Zero-Shot
     acoustic prompt (preserving the original actor's voice timbre, pitch register,
     arousal, and delivery).
  2. Transcribes the English reference clip (`ref_text`) and the Russian target clip
     (`gen_text`, from Nerual Dreming's Russian audio) using `openai/whisper-small`.
  3. Runs `Misha24-10/F5-TTS_RUSSIAN` (`F5TTS_v1_Base_v2/model_last_inference.safetensors`
     + `F5TTS_v1_Base/vocab.txt` + `charactr/vocos-mel-24khz` 24 kHz vocoder) on GPU
     with Flow Matching (`CFM` + `DiT`) to synthesize Russian speech in the original
     English actor's voice and emotional tone, paced to the exact phrase window.
"""

import argparse
import io
import json
import math
import os
import re
import struct
import sys
import tempfile
import warnings

os.environ["TQDM_DISABLE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
warnings.filterwarnings("ignore")

import numpy as np
import soundfile as sf
from scipy.signal import butter, filtfilt, resample_poly

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
F5_CORE_ZIP = os.path.join(SCRIPT_DIR, "f5_tts_core.zip")
if os.path.isfile(F5_CORE_ZIP) and F5_CORE_ZIP not in sys.path:
    sys.path.insert(0, F5_CORE_ZIP)

import torch
import torchaudio
from huggingface_hub import hf_hub_download
from safetensors.torch import load_file
from vocos import Vocos

from f5_tts.model import CFM, DiT
from f5_tts.model.utils import get_tokenizer

F5_TARGET_SR = 24000
F5_HOP_LENGTH = 256
F5_TARGET_RMS = 0.1


def make_xing_header_mp3(mp3_bytes: bytes, total_frames: int) -> bytes:
    """Prepend a valid Xing VBR info frame so libsndfile reads full duration of VBR MP3."""
    if len(mp3_bytes) < 4 or mp3_bytes[0] != 0xFF or (mp3_bytes[1] & 0xE0) != 0xE0:
        return mp3_bytes
    b1, b2, b3 = mp3_bytes[1], mp3_bytes[2], mp3_bytes[3]
    mpeg_ver = (b1 >> 3) & 0x03
    sr_idx = (b2 >> 2) & 0x03
    ch_mode = (b3 >> 6) & 0x03
    sr_map = {
        3: [44100, 48000, 32000],
        2: [22050, 24000, 16000],
        0: [11025, 12000, 8000],
    }
    if mpeg_ver not in sr_map or sr_idx >= 3:
        return mp3_bytes
    sr = sr_map[mpeg_ver][sr_idx]
    is_mpeg1 = (mpeg_ver == 3)
    samples_per_frame = 1152 if is_mpeg1 else 576
    side_info_len = (17 if ch_mode == 3 else 32) if is_mpeg1 else (9 if ch_mode == 3 else 17)
    br_idx = 9
    bitrate = (128 if is_mpeg1 else 80) * 1000
    frame_len = (samples_per_frame * (bitrate // 8)) // sr
    if frame_len < side_info_len + 20:
        return mp3_bytes
    frame = bytearray(frame_len)
    frame[0] = 0xFF
    frame[1] = b1 | 0x01
    frame[2] = (br_idx << 4) | (sr_idx << 2)
    frame[3] = (ch_mode << 6)
    off = 4 + side_info_len
    frame[off : off + 4] = b"Xing"
    struct.pack_into(">I", frame, off + 4, 0x0003)
    struct.pack_into(">I", frame, off + 8, max(1, total_frames))
    struct.pack_into(">I", frame, off + 12, len(mp3_bytes) + frame_len)
    return bytes(frame) + mp3_bytes


def count_mp3_frames(mp3_bytes: bytes) -> int:
    pos = 0
    n = len(mp3_bytes)
    frames = 0
    br_mpeg1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    br_mpeg2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
    sr_map = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}
    while pos + 4 <= n:
        if mp3_bytes[pos] != 0xFF or (mp3_bytes[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        b1, b2 = mp3_bytes[pos + 1], mp3_bytes[pos + 2]
        ver = (b1 >> 3) & 0x03
        br_i = (b2 >> 4) & 0x0F
        sr_i = (b2 >> 2) & 0x03
        pad = (b2 >> 1) & 0x01
        if ver not in sr_map or sr_i >= 3 or br_i == 0 or br_i == 15:
            pos += 1
            continue
        sr = sr_map[ver][sr_i]
        br = (br_mpeg1[br_i] if ver == 3 else br_mpeg2[br_i]) * 1000
        spf = 1152 if ver == 3 else 576
        flen = (spf * (br // 8)) // sr + pad
        if flen < 24:
            pos += 1
            continue
        frames += 1
        pos += flen
    return frames


def load_audio_mono(path: str):
    """Load WAV or VBR MP3 cleanly into mono float32 numpy array + sample_rate."""
    if path.lower().endswith(".mp3"):
        with open(path, "rb") as f:
            raw = f.read()
        if b"Xing" not in raw[:512] and b"Info" not in raw[:512]:
            nframes = count_mp3_frames(raw)
            if nframes > 0:
                raw = make_xing_header_mp3(raw, nframes)
        wav, sr = sf.read(io.BytesIO(raw), dtype="float32")
    else:
        wav, sr = sf.read(path, dtype="float32")
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    return wav.astype(np.float32), int(sr)


def resample_audio(wav: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    if orig_sr == target_sr or len(wav) == 0:
        return wav.astype(np.float32)
    g = math.gcd(int(orig_sr), int(target_sr))
    up = int(target_sr) // g
    down = int(orig_sr) // g
    return resample_poly(wav, up, down).astype(np.float32)


def bandpass_speech(wav: np.ndarray, sr: int) -> np.ndarray:
    if len(wav) < 64:
        return wav
    low = 80.0 / (0.5 * sr)
    high = min(7500.0, 0.45 * sr) / (0.5 * sr)
    b, a = butter(4, [low, high], btype="band")
    return filtfilt(b, a, wav).astype(np.float32)


def trim_silence_edges(wav: np.ndarray, sr: int, db_thresh: float = -40.0, pad_sec: float = 0.08):
    """Trim leading and trailing silence while keeping natural breath padding."""
    if len(wav) == 0:
        return wav, 0, 0
    hop = max(1, int(0.01 * sr))
    win = max(hop, int(0.025 * sr))
    peak = float(np.max(np.abs(wav))) + 1e-8
    thr = peak * (10.0 ** (db_thresh / 20.0))
    active = []
    for i in range(0, max(1, len(wav) - win + 1), hop):
        rms = float(np.sqrt(np.mean(wav[i : i + win] ** 2)))
        active.append(rms >= thr)
    if not any(active):
        return wav, 0, len(wav)
    first_idx = active.index(True)
    last_idx = len(active) - 1 - active[::-1].index(True)
    pad = int(pad_sec * sr)
    s = max(0, first_idx * hop - pad)
    e = min(len(wav), (last_idx + 1) * hop + pad)
    return wav[s:e], s, e


def normalize_russian_text_for_f5(text: str) -> str:
    """Clean Russian ASR transcript and mark unambiguous stress (`ё` -> `+ё`, single-vowel words)."""
    text = text.strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    # Replace 'ё'/'Ё' with '+е'/'+Е' or keep '+ё' if in vocab (F5TTS_v1_Base_v2 uses '+е' or 'ё')
    vowels = set("аеёиоуыэюяАЕЁИОУЫЭЮЯ")

    def stress_word(match):
        w = match.group(0)
        if "+" in w:
            return w
        if "ё" in w:
            idx = w.index("ё")
            return w[:idx] + "+е" + w[idx + 1 :]
        if "Ё" in w:
            idx = w.index("Ё")
            return w[:idx] + "+Е" + w[idx + 1 :]
        v_indices = [i for i, ch in enumerate(w) if ch in vowels]
        if len(v_indices) == 1 and len(w) >= 2:
            idx = v_indices[0]
            return w[:idx] + "+" + w[idx:]
        return w

    text = re.sub(r"[а-яА-ЯёЁ]+", stress_word, text)
    if text and text[-1] not in ".!?…":
        text += "."
    return text


def normalize_english_text_for_f5(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if text and text[-1] not in ".!?…":
        text += " "
    elif text:
        text += " "
    return text


class GTA5F5Engine:
    """Holds Whisper ASR + Misha24-10/F5-TTS_RUSSIAN (`F5TTS_v1_Base_v2`) + Vocos vocoder."""

    def __init__(self, cache_dir: str = None, device: str = None):
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self.device = torch.device(device)
        self.cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            os.environ["HF_HOME"] = cache_dir

        print(f"[F5-TTS] Initializing on device: {self.device}", flush=True)

        # 1. Download Misha24-10/F5-TTS_RUSSIAN vocab & v2 safetensors checkpoint
        print("[F5-TTS] Fetching Misha24-10/F5-TTS_RUSSIAN (F5TTS_v1_Base_v2)...", flush=True)
        vocab_path = hf_hub_download(
            repo_id="Misha24-10/F5-TTS_RUSSIAN",
            filename="F5TTS_v1_Base/vocab.txt",
            cache_dir=cache_dir,
        )
        ckpt_path = hf_hub_download(
            repo_id="Misha24-10/F5-TTS_RUSSIAN",
            filename="F5TTS_v1_Base_v2/model_last_inference.safetensors",
            cache_dir=cache_dir,
        )

        vocab_char_map, vocab_size = get_tokenizer(vocab_path, "custom")
        print(f"[F5-TTS] Loaded vocab ({vocab_size} tokens). Building DiT + CFM...", flush=True)

        model_cfg = dict(
            dim=1024,
            depth=22,
            heads=16,
            ff_mult=2,
            text_dim=512,
            conv_layers=4,
        )
        mel_spec_kwargs = dict(
            n_fft=1024,
            hop_length=F5_HOP_LENGTH,
            win_length=1024,
            n_mel_channels=100,
            target_sample_rate=F5_TARGET_SR,
            mel_spec_type="vocos",
        )
        model = CFM(
            transformer=DiT(
                **model_cfg,
                text_num_embeds=vocab_size,
                mel_dim=100,
            ),
            mel_spec_kwargs=mel_spec_kwargs,
            odeint_kwargs=dict(method="euler"),
            vocab_char_map=vocab_char_map,
        ).to(self.device)

        self.dtype = torch.float16 if self.device.type == "cuda" else torch.float32
        model = model.to(self.dtype)

        state = load_file(ckpt_path, device=str(self.device))
        # Strip 'ema_model.' prefix if present and remove legacy mel_spec keys
        cleaned_state = {}
        for k, v in state.items():
            if k in ("initted", "step"):
                continue
            nk = k[len("ema_model.") :] if k.startswith("ema_model.") else k
            if nk in ("mel_spec.mel_stft.mel_scale.fb", "mel_spec.mel_stft.spectrogram.window"):
                continue
            cleaned_state[nk] = v
        missing, unexpected = model.load_state_dict(cleaned_state, strict=False)
        print(
            f"[F5-TTS] Checkpoint loaded (missing={len(missing)}, unexpected={len(unexpected)}).",
            flush=True,
        )
        model = model.to(self.device, dtype=self.dtype).eval()
        self.model = model

        # 2. Load Vocos 24 kHz vocoder
        print("[F5-TTS] Loading charactr/vocos-mel-24khz vocoder...", flush=True)
        self.vocoder = Vocos.from_pretrained("charactr/vocos-mel-24khz").to(self.device).eval()

        # 3. Load Whisper-small for English & Russian transcription
        print("[F5-TTS] Loading openai/whisper-small ASR...", flush=True)
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self.whisper_proc = WhisperProcessor.from_pretrained(
            "openai/whisper-small", cache_dir=cache_dir
        )
        self.whisper_model = (
            WhisperForConditionalGeneration.from_pretrained(
                "openai/whisper-small", cache_dir=cache_dir
            )
            .to(self.device)
            .eval()
        )
        print("[F5-TTS] Engine ready!", flush=True)

    @torch.inference_mode()
    def transcribe(self, wav: np.ndarray, sr: int, language: str) -> str:
        """Transcribe an audio array in English or Russian using Whisper-small."""
        if len(wav) < int(0.15 * sr):
            return ""
        wav_16k = resample_audio(wav, sr, 16000)
        peak = float(np.max(np.abs(wav_16k)))
        if peak < 1e-4:
            return ""
        wav_16k = wav_16k / max(peak, 0.05)
        inputs = self.whisper_proc(
            wav_16k, sampling_rate=16000, return_tensors="pt"
        ).input_features.to(self.device)
        forced_ids = self.whisper_proc.get_decoder_prompt_ids(
            language=language, task="transcribe"
        )
        gen_ids = self.whisper_model.generate(
            inputs, forced_decoder_ids=forced_ids, max_new_tokens=128
        )
        text = self.whisper_proc.batch_decode(gen_ids, skip_special_tokens=True)[0].strip()
        return text

    @torch.inference_mode()
    def synthesize_phrase(
        self,
        ref_en_wav: np.ndarray,
        ref_sr: int,
        gen_ru_text: str,
        ref_en_text: str = None,
        target_dur_sec: float = None,
        nfe_step: int = 32,
        cfg_strength: float = 2.0,
        sway_sampling_coef: float = -1.0,
    ) -> np.ndarray:
        """
        Synthesize Russian speech `gen_ru_text` in the voice & emotion of `ref_en_wav`.
        Returns 24 kHz mono float32 numpy array.
        """
        # Clean & trim reference audio so trailing silence doesn't bleed into generation
        ref_trimmed, _, _ = trim_silence_edges(ref_en_wav, ref_sr, db_thresh=-38.0, pad_sec=0.06)
        if len(ref_trimmed) < int(0.6 * ref_sr):
            ref_trimmed = ref_en_wav

        # Cap reference prompt to 6.5s max so F5-TTS leaves plenty of context window
        max_ref_samples = int(6.5 * ref_sr)
        if len(ref_trimmed) > max_ref_samples:
            ref_trimmed = ref_trimmed[:max_ref_samples]

        # If reference is very short (< 1.4s), pad a brief 0.12s pause at the end
        ref_24k = resample_audio(ref_trimmed, ref_sr, F5_TARGET_SR)
        ref_24k = np.concatenate([ref_24k, np.zeros(int(0.10 * F5_TARGET_SR), dtype=np.float32)])

        if not ref_en_text:
            ref_en_text = self.transcribe(ref_trimmed, ref_sr, language="english")
        ref_en_text = normalize_english_text_for_f5(ref_en_text or "Hey.")
        gen_ru_norm = normalize_russian_text_for_f5(gen_ru_text)

        rms = float(np.sqrt(np.mean(ref_24k**2)))
        if rms > 1e-5:
            ref_24k_norm = ref_24k * (F5_TARGET_RMS / max(rms, 0.02))
        else:
            ref_24k_norm = ref_24k

        audio_cond = torch.from_numpy(ref_24k_norm).unsqueeze(0).to(self.device, dtype=torch.float32)
        ref_frames = audio_cond.shape[-1] // F5_HOP_LENGTH

        # Calculate natural Russian generation duration in mel frames (avoid UTF-8 2x bug!)
        ru_chars = max(4, len(gen_ru_norm.replace("+", "")))
        en_chars = max(6, len(ref_en_text.strip()))
        ref_sec = len(ref_24k) / float(F5_TARGET_SR)
        chars_per_sec = np.clip(en_chars / max(ref_sec, 0.8), 11.0, 17.5)
        natural_gen_sec = max(0.85, ru_chars / chars_per_sec + 0.18)

        if target_dur_sec is not None and target_dur_sec > 0.5:
            # Let F5-TTS pace itself naturally within [0.70x .. 1.02x] of the slot duration
            gen_sec = float(np.clip(natural_gen_sec, target_dur_sec * 0.72, target_dur_sec * 1.02))
        else:
            gen_sec = natural_gen_sec

        gen_frames = max(20, int(gen_sec * F5_TARGET_SR / F5_HOP_LENGTH))
        total_duration = ref_frames + gen_frames

        full_text = [ref_en_text + gen_ru_norm]
        generated, _ = self.model.sample(
            cond=audio_cond,
            text=full_text,
            duration=total_duration,
            steps=nfe_step,
            cfg_strength=cfg_strength,
            sway_sampling_coef=sway_sampling_coef,
        )
        generated = generated.to(torch.float32)[:, ref_frames:, :].permute(0, 2, 1)
        gen_wave = self.vocoder.decode(generated).squeeze().detach().cpu().numpy()

        # Trim any leading click/breath artifact from prompt boundary (first 25ms fade-in)
        fade_in = min(int(0.025 * F5_TARGET_SR), len(gen_wave) // 4)
        if fade_in > 1:
            gen_wave[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)

        # Match RMS loudness of the original reference
        gen_rms = float(np.sqrt(np.mean(gen_wave**2))) + 1e-8
        target_rms = max(rms, 0.04)
        gen_wave = gen_wave * (target_rms / gen_rms)
        peak = float(np.max(np.abs(gen_wave)))
        if peak > 0.96:
            gen_wave = gen_wave * (0.96 / peak)

        return gen_wave.astype(np.float32), ref_en_text.strip(), gen_ru_norm


def fit_wave_to_slot(
    gen_wave_24k: np.ndarray,
    out_sr: int,
    slot_samples: int,
    max_speedup: float = 1.18,
) -> np.ndarray:
    """Resample 24 kHz generated speech to `out_sr` and fit cleanly into `slot_samples`."""
    wav = resample_audio(gen_wave_24k, F5_TARGET_SR, out_sr)
    trimmed, _, _ = trim_silence_edges(wav, out_sr, db_thresh=-42.0, pad_sec=0.04)
    if len(trimmed) > 0:
        wav = trimmed

    if len(wav) > slot_samples:
        ratio = len(wav) / float(slot_samples)
        if ratio <= max_speedup:
            wav = resample_poly(wav, slot_samples, len(wav)).astype(np.float32)
        else:
            compressed_len = int(len(wav) / max_speedup)
            wav = resample_poly(wav, compressed_len, len(wav)).astype(np.float32)
            wav = wav[:slot_samples]

    fade = min(int(0.015 * out_sr), len(wav) // 4)
    if fade > 1:
        wav[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
        wav[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)

    out = np.zeros(slot_samples, dtype=np.float32)
    n = min(len(wav), slot_samples)
    out[:n] = wav[:n]
    return out


def segment_dialogue_windows(
    ru_wav: np.ndarray,
    sr: int,
    min_silence_sec: float = 0.28,
    min_phrase_sec: float = 0.55,
    max_phrase_sec: float = 5.5,
):
    """Detect active speech phrase windows `[s_sec, e_sec]` in Russian dialogue."""
    speech = bandpass_speech(ru_wav, sr)
    hop = int(0.02 * sr)
    win = int(0.04 * sr)
    n_frames = max(1, (len(speech) - win) // hop + 1)
    rms = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        s = i * hop
        rms[i] = float(np.sqrt(np.mean(speech[s : s + win] ** 2)))

    nz = rms[rms > 1e-4]
    if len(nz) == 0:
        return []
    thr = max(float(np.percentile(nz, 22)) * 1.35, float(np.max(nz)) * 0.055)
    active = rms >= thr

    # Bridge short gaps < min_silence_sec
    min_gap_frames = max(1, int(min_silence_sec / 0.02))
    i = 0
    while i < n_frames:
        if not active[i]:
            j = i
            while j < n_frames and not active[j]:
                j += 1
            if 0 < i and j < n_frames and (j - i) < min_gap_frames:
                active[i:j] = True
            i = j
        else:
            i += 1

    raw_segs = []
    i = 0
    while i < n_frames:
        if active[i]:
            j = i
            while j < n_frames and active[j]:
                j += 1
            s_sec = max(0.0, (i * hop) / float(sr) - 0.06)
            e_sec = min(len(ru_wav) / float(sr), (j * hop + win) / float(sr) + 0.08)
            if (e_sec - s_sec) >= min_phrase_sec:
                raw_segs.append((s_sec, e_sec))
            i = j
        else:
            i += 1

    # Split any segment longer than max_phrase_sec at its local energy minimum
    final_segs = []
    for s_sec, e_sec in raw_segs:
        cur_s = s_sec
        while (e_sec - cur_s) > max_phrase_sec:
            search_s = cur_s + 2.0
            search_e = min(cur_s + max_phrase_sec, e_sec - 0.8)
            f0 = max(0, int(search_s / 0.02))
            f1 = min(n_frames, int(search_e / 0.02))
            if f1 > f0 + 2:
                min_f = f0 + int(np.argmin(rms[f0:f1]))
                split_t = (min_f * hop) / float(sr)
            else:
                split_t = cur_s + max_phrase_sec
            final_segs.append((cur_s, split_t))
            cur_s = split_t
        if (e_sec - cur_s) >= min_phrase_sec:
            final_segs.append((cur_s, e_sec))

    return final_segs


def find_best_english_prompt(en_wav: np.ndarray, en_sr: int, s_sec: float, e_sec: float):
    """
    Extract the cleanest voiced English slice corresponding to `[s_sec, e_sec]`
    so F5-TTS gets the exact speaker who is talking in that moment.
    """
    total_sec = len(en_wav) / float(en_sr)
    # First check exact window [s_sec - 0.25, e_sec + 0.25]
    w0 = max(0.0, s_sec - 0.25)
    w1 = min(total_sec, e_sec + 0.25)
    direct = en_wav[int(w0 * en_sr) : int(w1 * en_sr)]
    direct_speech = bandpass_speech(direct, en_sr)
    direct_rms = float(np.sqrt(np.mean(direct_speech**2))) if len(direct_speech) > 0 else 0.0

    # If the direct window has good speech energy and is at least 1.5s, use it directly
    if direct_rms > 0.012 and (w1 - w0) >= 1.5:
        return direct

    # Otherwise search within +/- 2.0s for the highest-energy 2.5s window of the same speaker
    search_s = max(0.0, s_sec - 1.8)
    search_e = min(total_sec, e_sec + 1.8)
    win_sec = min(3.0, max(1.8, e_sec - s_sec + 0.6))
    best_slice = direct
    best_rms = direct_rms
    t = search_s
    while t + win_sec <= search_e + 0.01:
        sl = en_wav[int(t * en_sr) : int(min(total_sec, t + win_sec) * en_sr)]
        if len(sl) >= int(1.0 * en_sr):
            r = float(np.sqrt(np.mean(bandpass_speech(sl, en_sr) ** 2)))
            if r > best_rms:
                best_rms = r
                best_slice = sl
        t += 0.25
    return best_slice


def process_single(
    engine: GTA5F5Engine,
    en_wav_path: str,
    ru_audio_path: str,
    out_wav_path: str,
    en_text_override: str = None,
    ru_text_override: str = None,
):
    en_wav, en_sr = load_audio_mono(en_wav_path)
    ru_wav, ru_sr = load_audio_mono(ru_audio_path)

    target_sr = en_sr
    target_samples = len(en_wav)
    target_dur = target_samples / float(target_sr)

    ru_text = ru_text_override or engine.transcribe(ru_wav, ru_sr, language="russian")
    en_text = en_text_override or engine.transcribe(en_wav, en_sr, language="english")

    gen_24k, used_en, used_ru = engine.synthesize_phrase(
        ref_en_wav=en_wav,
        ref_sr=en_sr,
        gen_ru_text=ru_text,
        ref_en_text=en_text,
        target_dur_sec=target_dur,
    )
    out_wav = fit_wave_to_slot(gen_24k, target_sr, target_samples)

    # Match peak/RMS to English original
    en_rms = float(np.sqrt(np.mean(en_wav**2))) + 1e-8
    out_rms = float(np.sqrt(np.mean(out_wav**2))) + 1e-8
    out_wav = out_wav * min(1.8, max(0.6, en_rms / out_rms))
    peak = float(np.max(np.abs(out_wav)))
    if peak > 0.96:
        out_wav *= 0.96 / peak

    os.makedirs(os.path.dirname(os.path.abspath(out_wav_path)), exist_ok=True)
    sf.write(out_wav_path, out_wav, target_sr, subtype="PCM_16")
    return {
        "mode": "single",
        "en_wav": en_wav_path,
        "ru_audio": ru_audio_path,
        "out_wav": out_wav_path,
        "target_sr": target_sr,
        "target_samples": target_samples,
        "duration_sec": round(target_dur, 3),
        "en_transcript": used_en,
        "ru_transcript": used_ru,
    }


def process_cutscene(
    engine: GTA5F5Engine,
    en_wav_path: str,
    ru_audio_path: str,
    out_wav_path: str,
):
    en_wav, en_sr = load_audio_mono(en_wav_path)
    ru_wav, ru_sr = load_audio_mono(ru_audio_path)

    target_sr = en_sr
    target_samples = len(en_wav)
    ru_resampled = resample_audio(ru_wav, ru_sr, target_sr)
    if len(ru_resampled) < target_samples:
        ru_resampled = np.pad(ru_resampled, (0, target_samples - len(ru_resampled)))
    else:
        ru_resampled = ru_resampled[:target_samples]

    # Start from Russian track so non-dialogue gaps / background SFX stay intact
    out_wav = ru_resampled.copy()
    windows = segment_dialogue_windows(ru_resampled, target_sr)
    phrase_logs = []

    for idx, (s_sec, e_sec) in enumerate(windows):
        s_idx = max(0, int(s_sec * target_sr))
        e_idx = min(target_samples, int(e_sec * target_sr))
        slot_samples = e_idx - s_idx
        if slot_samples < int(0.45 * target_sr):
            continue

        ru_slice = ru_resampled[s_idx:e_idx]
        ru_text = engine.transcribe(ru_slice, target_sr, language="russian")
        # Skip pure noise/gunshots where Whisper outputs nothing or hallucinations
        ru_clean = re.sub(r"[^а-яА-ЯёЁ0-9]", "", ru_text)
        if len(ru_clean) < 2:
            continue

        en_prompt = find_best_english_prompt(en_wav, target_sr, s_sec, e_sec)
        en_text = engine.transcribe(en_prompt, target_sr, language="english")

        gen_24k, used_en, used_ru = engine.synthesize_phrase(
            ref_en_wav=en_prompt,
            ref_sr=target_sr,
            gen_ru_text=ru_text,
            ref_en_text=en_text,
            target_dur_sec=(e_sec - s_sec),
        )
        fitted = fit_wave_to_slot(gen_24k, target_sr, slot_samples)

        # Match RMS to the original speech slice
        ref_rms = max(
            float(np.sqrt(np.mean(ru_slice**2))),
            float(np.sqrt(np.mean(en_wav[s_idx:e_idx] ** 2))),
        )
        fit_rms = float(np.sqrt(np.mean(fitted**2))) + 1e-8
        if ref_rms > 1e-4:
            fitted = fitted * min(2.0, max(0.5, ref_rms / fit_rms))

        # Crossfade 15ms at boundaries into out_wav
        cf = min(int(0.015 * target_sr), slot_samples // 6)
        if cf > 1:
            w_in = np.linspace(0.0, 1.0, cf, dtype=np.float32)
            w_out = np.linspace(1.0, 0.0, cf, dtype=np.float32)
            fitted[:cf] = out_wav[s_idx : s_idx + cf] * w_out + fitted[:cf] * w_in
            fitted[-cf:] = fitted[-cf:] * w_out + out_wav[e_idx - cf : e_idx] * w_in

        out_wav[s_idx:e_idx] = fitted
        phrase_logs.append(
            {
                "index": idx + 1,
                "start_sec": round(s_sec, 2),
                "end_sec": round(e_sec, 2),
                "en_transcript": used_en,
                "ru_transcript": used_ru,
            }
        )
        print(
            f"  [Segment {idx+1:02d} {s_sec:5.2f}s..{e_sec:5.2f}s] EN='{used_en}' -> RU='{used_ru}'",
            flush=True,
        )

    peak = float(np.max(np.abs(out_wav)))
    if peak > 0.96:
        out_wav *= 0.96 / peak

    os.makedirs(os.path.dirname(os.path.abspath(out_wav_path)), exist_ok=True)
    sf.write(out_wav_path, out_wav, target_sr, subtype="PCM_16")
    return {
        "mode": "cutscene",
        "en_wav": en_wav_path,
        "ru_audio": ru_audio_path,
        "out_wav": out_wav_path,
        "target_sr": target_sr,
        "target_samples": target_samples,
        "duration_sec": round(target_samples / float(target_sr), 3),
        "segments_synthesized": len(phrase_logs),
        "segments": phrase_logs,
    }


def build_yandex_reel(items, out_wav_path: str, out_map_path: str, reel_sr: int = 24000):
    """
    Stitch multiple English WAV files with 2.0s digital silence separators into a single
    reel WAV + JSON timecode map so Yandex Video Translator ('Живые голоса') can dub
    them in one request with full context and zero boundary bleed.
    """
    gap_sec = 2.0
    lead_sec = 0.8
    chunks = [np.zeros(int(lead_sec * reel_sr), dtype=np.float32)]
    cur_t = lead_sec
    mapping = []

    for item in items:
        name = item["name"]
        wav_path = item["wav_path"]
        wav, sr = load_audio_mono(wav_path)
        orig_dur = len(wav) / float(sr)
        wav_r = resample_audio(wav, sr, reel_sr)
        dur = len(wav_r) / float(reel_sr)
        s_t = cur_t
        e_t = cur_t + dur
        mapping.append(
            {
                "name": name,
                "orig_wav": wav_path,
                "orig_sr": int(sr),
                "orig_samples": int(len(wav)),
                "orig_duration_sec": round(orig_dur, 4),
                "reel_start_sec": round(s_t, 4),
                "reel_end_sec": round(e_t, 4),
            }
        )
        chunks.append(wav_r)
        chunks.append(np.zeros(int(gap_sec * reel_sr), dtype=np.float32))
        cur_t = e_t + gap_sec

    full_reel = np.concatenate(chunks)
    os.makedirs(os.path.dirname(os.path.abspath(out_wav_path)), exist_ok=True)
    sf.write(out_wav_path, full_reel, reel_sr, subtype="PCM_16")
    with open(out_map_path, "w", encoding="utf-8") as f:
        json.dump({"reel_sr": reel_sr, "items": mapping}, f, ensure_ascii=False, indent=2)
    return {"reel_wav": out_wav_path, "map_json": out_map_path, "total_sec": round(cur_t, 2)}


def main():
    parser = argparse.ArgumentParser(description="GTA 5 Zero-Shot Russian F5-TTS Dubber")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_single = sub.add_parser("single")
    p_single.add_argument("en_wav")
    p_single.add_argument("ru_audio")
    p_single.add_argument("out_wav")
    p_single.add_argument("--cache-dir", default=None)
    p_single.add_argument("--en-text", default=None)
    p_single.add_argument("--ru-text", default=None)

    p_cut = sub.add_parser("cutscene")
    p_cut.add_argument("en_wav")
    p_cut.add_argument("ru_audio")
    p_cut.add_argument("out_wav")
    p_cut.add_argument("--cache-dir", default=None)

    p_batch = sub.add_parser("batch")
    p_batch.add_argument("manifest_json")
    p_batch.add_argument("--cache-dir", default=None)

    args = parser.parse_args()

    if args.cmd == "single":
        eng = GTA5F5Engine(cache_dir=args.cache_dir)
        res = process_single(
            eng, args.en_wav, args.ru_audio, args.out_wav, args.en_text, args.ru_text
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))

    elif args.cmd == "cutscene":
        eng = GTA5F5Engine(cache_dir=args.cache_dir)
        res = process_cutscene(eng, args.en_wav, args.ru_audio, args.out_wav)
        print(json.dumps(res, ensure_ascii=False, indent=2))

    elif args.cmd == "batch":
        with open(args.manifest_json, "r", encoding="utf-8-sig") as f:
            spec = json.load(f)
        cache_dir = args.cache_dir or spec.get("cache_dir")
        eng = GTA5F5Engine(cache_dir=cache_dir)
        results = []
        for job in spec.get("jobs", []):
            jtype = job.get("type", "single")
            print(f"\n=== [F5-TTS] Job: {job.get('name')} ({jtype}) ===", flush=True)
            if jtype == "single":
                r = process_single(
                    eng,
                    job["en_wav"],
                    job["ru_audio"],
                    job["out_wav"],
                    job.get("en_text"),
                    job.get("ru_text"),
                )
            else:
                r = process_cutscene(eng, job["en_wav"], job["ru_audio"], job["out_wav"])
            r["name"] = job.get("name")
            results.append(r)

        if "yandex_reel" in spec:
            yr = spec["yandex_reel"]
            reel_info = build_yandex_reel(yr["items"], yr["out_wav"], yr["out_map"])
            print(f"\n=== [Yandex Reel] Built {reel_info['reel_wav']} ({reel_info['total_sec']}s) ===", flush=True)

        out_report = spec.get("out_report")
        if out_report:
            with open(out_report, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
