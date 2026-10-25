#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GTA 5 Offline Neural Voice & Emotion Enhancer (Seed-VC DiT + RMVPE F0 + CAMPPlus)
Transforms flat Russian TTS lines (Nerual Dreming v3.0) using the original
English actor's vocal timbre, emotional pitch contour, and dynamics while
strictly preserving Russian speech duration for in-place .awc/.rpf injection.
"""

import os
import io
import sys
import time
import json
import struct
import zipfile
import argparse
from pathlib import Path

os.environ["TQDM_DISABLE"] = "1"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

import numpy as np
from scipy.signal import medfilt
import soundfile as sf
import torch
import torchaudio
import yaml


def _mp3_frame_len(buf: bytes, pos: int) -> int:
    if pos + 4 > len(buf):
        return 0
    if buf[pos] != 0xFF or (buf[pos + 1] & 0xE0) != 0xE0:
        return 0
    b1, b2 = buf[pos + 1], buf[pos + 2]
    ver = (b1 >> 3) & 0x3
    layer = (b1 >> 1) & 0x3
    if ver == 1 or layer != 1:
        return 0
    br_tab = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    sr_tab = [44100, 48000, 32000, 0]
    br_idx = (b2 >> 4) & 0xF
    sr_idx = (b2 >> 2) & 0x3
    pad = (b2 >> 1) & 0x1
    br = br_tab[br_idx] * 1000
    sr = sr_tab[sr_idx]
    if br == 0 or sr == 0:
        return 0
    div = 144 if ver == 3 else 72
    return (div * br) // sr + pad


def _find_mp3_sync(buf: bytes, start_pos: int = 0) -> int:
    limit = len(buf) - 8
    for pos in range(max(0, start_pos), limit):
        if buf[pos] == 0xFF and (buf[pos + 1] & 0xE0) == 0xE0:
            fl1 = _mp3_frame_len(buf, pos)
            if fl1 > 0:
                if pos + fl1 + 4 <= len(buf):
                    if _mp3_frame_len(buf, pos + fl1) > 0:
                        return pos
                else:
                    return pos
    return start_pos


def make_xing_header_mp3(raw_mp3: bytes) -> bytes:
    """
    Prepend a valid 1-frame MPEG1 Layer3 Xing VBR header if not already present.
    Fixes libsndfile VBR duration estimation on raw MPEG streams extracted from .awc.
    """
    if len(raw_mp3) < 4:
        return b""
    sync0 = _find_mp3_sync(raw_mp3, 0)
    if sync0 > 0 and sync0 < len(raw_mp3):
        raw_mp3 = raw_mp3[sync0:]
    if len(raw_mp3) < 4 or raw_mp3[0] != 0xFF or (raw_mp3[1] & 0xE0) != 0xE0:
        return raw_mp3
    if b"Xing" in raw_mp3[:512] or b"Info" in raw_mp3[:512]:
        return raw_mp3

    br_tab = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    sr_tab = [44100, 48000, 32000, 0]

    pos = 0
    frames = 0
    first_sr_idx = 0
    first_mode = 3
    while pos + 4 <= len(raw_mp3):
        if raw_mp3[pos] != 0xFF or (raw_mp3[pos + 1] & 0xE0) != 0xE0:
            pos += 1
            continue
        b1, b2, b3 = raw_mp3[pos + 1], raw_mp3[pos + 2], raw_mp3[pos + 3]
        br_idx = (b2 >> 4) & 0xF
        sr_idx = (b2 >> 2) & 0x3
        pad = (b2 >> 1) & 0x1
        mode = (b3 >> 6) & 0x3
        br = br_tab[br_idx] * 1000
        sr = sr_tab[sr_idx]
        if br == 0 or sr == 0:
            pos += 1
            continue
        if frames == 0:
            first_sr_idx = sr_idx
            first_mode = mode
        flen = (144 * br) // sr + pad
        frames += 1
        pos += flen

    if frames == 0:
        return raw_mp3

    sr = sr_tab[first_sr_idx]
    br_idx = 1  # 32 kbps
    flen = (144 * 32000) // sr
    hdr = bytearray(flen)
    hdr[0] = 0xFF
    hdr[1] = 0xFB
    hdr[2] = (br_idx << 4) | (first_sr_idx << 2)
    hdr[3] = (first_mode << 6)
    xing_off = 4 + (17 if first_mode == 3 else 32)
    if xing_off + 16 <= flen:
        total_bytes = len(raw_mp3) + flen
        hdr[xing_off : xing_off + 4] = b"Xing"
        struct.pack_into(">III", hdr, xing_off + 4, 0x0003, frames, total_bytes)
        return bytes(hdr) + raw_mp3
    return raw_mp3


def read_audio_complete(path: str):
    """
    Read WAV or raw VBR MP3 without libsndfile's VBR truncation bug.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) < 32:
        raise ValueError(f"Audio file too small ({len(raw)} bytes): {path}")
    if path.lower().endswith(".mp3") or (raw[:4] != b"RIFF" and raw[:4] != b"fLaC" and raw[:4] != b"OggS"):
        raw = make_xing_header_mp3(raw)
    data, sr = sf.read(io.BytesIO(raw), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, sr


def estimate_f0_autocorr(wave: np.ndarray, sr: int, fmin: float = 65.0, fmax: float = 500.0) -> np.ndarray:
    """
    Ground-truth time-domain autocorrelation F0 estimator (immune to mel filterbank scaling).
    """
    win_len = int(0.04 * sr)
    hop_len = int(0.01 * sr)
    min_lag = max(1, int(sr / fmax))
    max_lag = int(sr / fmin)
    f0_list = []
    for start in range(0, len(wave) - win_len, hop_len):
        frame = wave[start : start + win_len]
        frame = frame - np.mean(frame)
        energy = np.sum(frame * frame) / win_len
        if energy < 2e-4:
            continue
        corr = np.correlate(frame, frame, mode="full")[win_len - 1 :]
        if len(corr) <= max_lag:
            continue
        sub = corr[min_lag:max_lag]
        peak_idx = int(np.argmax(sub)) + min_lag
        if corr[peak_idx] > 0.30 * corr[0]:
            f0_list.append(sr / float(peak_idx))
    return np.array(f0_list, dtype=np.float32)


def apply_presence_eq_and_limiter(wave: np.ndarray, sr: int, ref_wave_44k: np.ndarray = None) -> np.ndarray:
    """
    Add natural chest warmth (150-260 Hz) and moderate vocal presence (3.2 kHz)
    without making male voices thin or bright.
    """
    if len(wave) < 256:
        return wave
    spec = np.fft.rfft(wave)
    freqs = np.fft.rfftfreq(len(wave), d=1.0 / sr)

    warmth_gain = 0.28 * np.exp(-0.5 * ((freqs - 185.0) / 85.0) ** 2)
    presence_gain = 0.42 * np.exp(-0.5 * ((freqs - 3300.0) / 1400.0) ** 2)
    eq_curve = 1.0 + warmth_gain + presence_gain
    eq_wave = np.fft.irfft(spec * eq_curve, n=len(wave)).astype(np.float32)

    rms_in = float(np.sqrt(np.mean(wave ** 2))) + 1e-6
    rms_eq = float(np.sqrt(np.mean(eq_wave ** 2))) + 1e-6
    target_rms = rms_in * 1.06
    if ref_wave_44k is not None and len(ref_wave_44k) > 256:
        rms_ref = float(np.sqrt(np.mean(ref_wave_44k ** 2)))
        if rms_ref > rms_in:
            target_rms = min(0.27, rms_in * 0.68 + rms_ref * 0.32)

    eq_wave = eq_wave * (target_rms / rms_eq)
    out = np.tanh(eq_wave * 1.12) / np.tanh(1.12)
    return out.astype(np.float32)


def ensure_seed_vc_extracted(tools_dir: Path) -> Path:
    core_dir = tools_dir / "seed_vc_core"
    zip_path = tools_dir / "seed_vc_core.zip"
    if zip_path.exists():
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(core_dir)
    return core_dir


def find_speech_phrases(wave: np.ndarray, sr: int, min_silence_sec: float = 0.18, min_phrase_sec: float = 0.25):
    win = int(0.02 * sr)
    hop = int(0.01 * sr)
    if len(wave) < win:
        return [(0, len(wave))]

    n_frames = 1 + (len(wave) - win) // hop
    rms = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        seg = wave[i * hop : i * hop + win]
        rms[i] = np.sqrt(np.mean(seg * seg) + 1e-12)

    p95 = float(np.percentile(rms, 95)) if len(rms) > 0 else 0.0
    if p95 < 0.008:
        return []

    thresh = max(0.010, p95 * 0.08)
    active = rms > thresh

    min_sil_frames = int(min_silence_sec / 0.01)
    min_phr_frames = int(min_phrase_sec / 0.01)

    phrases = []
    in_p = False
    start_f = 0
    sil_count = 0

    for i, a in enumerate(active):
        if a:
            if not in_p:
                in_p = True
                start_f = max(0, i - 4)
            sil_count = 0
        else:
            if in_p:
                sil_count += 1
                if sil_count >= min_sil_frames:
                    end_f = min(n_frames - 1, i - sil_count + 4)
                    if (end_f - start_f) >= min_phr_frames:
                        phrases.append((start_f * hop, min(len(wave), end_f * hop + win)))
                    in_p = False
                    sil_count = 0

    if in_p:
        end_f = n_frames - 1
        if (end_f - start_f) >= min_phr_frames:
            phrases.append((start_f * hop, len(wave)))

    return phrases


class GTA5EmotionVC:
    def __init__(self, tools_dir: Path, device: str = None):
        self.tools_dir = Path(tools_dir)
        self.core_dir = ensure_seed_vc_extracted(self.tools_dir)
        if str(self.core_dir) not in sys.path:
            sys.path.insert(0, str(self.core_dir))

        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        else:
            self.device = torch.device(device)

        print(f"[GTA5EmotionVC] Using device: {self.device}")
        if self.device.type == "cuda":
            print(f"[GTA5EmotionVC] GPU: {torch.cuda.get_device_name(0)}")

        from hf_utils import load_custom_model_from_hf
        from modules.commons import build_model, load_checkpoint, recursive_munch
        from modules.audio import mel_spectrogram
        from modules.rmvpe import RMVPE
        from modules.campplus.DTDNN import CAMPPlus
        from modules.bigvgan import bigvgan
        from transformers import AutoFeatureExtractor, WhisperModel

        dit_ckpt, dit_cfg = load_custom_model_from_hf(
            "Plachta/Seed-VC",
            "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
            "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
        )
        config = yaml.safe_load(open(dit_cfg, "r", encoding="utf-8"))
        model_params = recursive_munch(config["model_params"])
        model_params.dit_type = "DiT"
        self.model = build_model(model_params, stage="DiT")
        self.hop_length = config["preprocess_params"]["spect_params"]["hop_length"]
        self.sr = config["preprocess_params"]["sr"]  # 44100

        self.model, _, _, _ = load_checkpoint(
            self.model,
            None,
            dit_ckpt,
            load_only_params=True,
            ignore_modules=[],
            is_distributed=False,
        )
        for key in self.model:
            self.model[key].eval()
            self.model[key].to(self.device)
        self.model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)

        rmvpe_path = load_custom_model_from_hf("lj1995/VoiceConversionWebUI", "rmvpe.pt", None)
        self.f0_extractor = RMVPE(rmvpe_path, is_half=False, device=self.device)

        campplus_ckpt = load_custom_model_from_hf("funasr/campplus", "campplus_cn_common.bin", config_filename=None)
        self.campplus = CAMPPlus(feat_dim=80, embedding_size=192)
        self.campplus.load_state_dict(torch.load(campplus_ckpt, map_location="cpu"))
        self.campplus.eval().to(self.device)

        bigvgan_name = model_params.vocoder.name
        self.vocoder = bigvgan.BigVGAN.from_pretrained(bigvgan_name, use_cuda_kernel=False)
        self.vocoder.remove_weight_norm()
        self.vocoder = self.vocoder.eval().to(self.device)

        whisper_name = model_params.speech_tokenizer.name
        self.whisper_model = WhisperModel.from_pretrained(whisper_name).half().to(self.device).eval()
        del self.whisper_model.decoder
        self.whisper_fe = AutoFeatureExtractor.from_pretrained(whisper_name)

        mel_fn_args = {
            "n_fft": config["preprocess_params"]["spect_params"]["n_fft"],
            "win_size": config["preprocess_params"]["spect_params"]["win_length"],
            "hop_size": self.hop_length,
            "num_mels": config["preprocess_params"]["spect_params"]["n_mels"],
            "sampling_rate": self.sr,
            "fmin": config["preprocess_params"]["spect_params"].get("fmin", 0),
            "fmax": None if config["preprocess_params"]["spect_params"].get("fmax", "None") == "None" else 8000,
            "center": False,
        }
        self.to_mel = lambda x: mel_spectrogram(x, **mel_fn_args)
        print("[GTA5EmotionVC] All models loaded and ready.")

    @torch.no_grad()
    def _semantic_fn(self, waves_16k: torch.Tensor) -> torch.Tensor:
        ori_inputs = self.whisper_fe(
            [waves_16k.squeeze(0).cpu().numpy()],
            return_tensors="pt",
            return_attention_mask=True,
            sampling_rate=16000,
        )
        ori_input_features = self.whisper_model._mask_input_features(
            ori_inputs.input_features, attention_mask=ori_inputs.attention_mask
        ).to(self.device, dtype=self.whisper_model.encoder.dtype)
        ori_outputs = self.whisper_model.encoder(
            ori_input_features,
            head_mask=None,
            output_attentions=False,
            output_hidden_states=False,
            return_dict=True,
        )
        S_ori = ori_outputs.last_hidden_state.to(torch.float32)
        S_ori = S_ori[:, : waves_16k.size(-1) // 320 + 1]
        return S_ori

    @torch.no_grad()
    def convert_phrase(
        self,
        ru_seg_44k: np.ndarray,
        en_ref_44k: np.ndarray,
        diffusion_steps: int = 25,
        inference_cfg_rate: float = 0.75,
        emotion_scale: float = 1.75,
        timbre_blend: float = 0.55,
    ):
        """
        Convert a Russian speech phrase with the true English pitch contour and vocal
        grit while keeping the Russian speaker's deep natural chest timbre & register.
        """
        ru_seg_44k = np.clip(ru_seg_44k, -0.99, 0.99)
        en_ref_44k = np.clip(en_ref_44k, -0.99, 0.99)

        if len(en_ref_44k) < int(1.0 * self.sr):
            repeats = int(np.ceil(int(1.0 * self.sr) / max(1, len(en_ref_44k))))
            en_ref_44k = np.tile(en_ref_44k, repeats)[: int(1.6 * self.sr)]
        elif len(en_ref_44k) > int(4.5 * self.sr):
            en_ref_44k = en_ref_44k[: int(4.5 * self.sr)]

        ac_ru_f0s = estimate_f0_autocorr(ru_seg_44k, self.sr)
        ac_ru_med = float(np.median(ac_ru_f0s)) if len(ac_ru_f0s) > 0 else 130.0
        is_male = ac_ru_med < 185.0

        ac_en_f0s = estimate_f0_autocorr(en_ref_44k, self.sr)
        ac_en_med = float(np.median(ac_en_f0s)) if len(ac_en_f0s) > 0 else ac_ru_med

        # If the English reference speaker is much higher-pitched than the Russian male voice
        # (e.g. 223 Hz vs 134 Hz in 0x1227C3A3), formant/pitch-shift the acoustic prompt anchor
        # down into the Russian speaker's chest register so DiT & CAMPPlus never make the timbre high/thin!
        en_for_timbre = en_ref_44k
        eff_timbre_blend = timbre_blend
        if is_male and ac_en_med > ac_ru_med * 1.25:
            shift_ratio = max(0.68, min(0.88, (ac_ru_med / ac_en_med) ** 0.70))
            shifted_sr = int(round(self.sr / shift_ratio))
            en_for_timbre = (
                torchaudio.functional.resample(
                    torch.from_numpy(en_ref_44k).unsqueeze(0).float(),
                    shifted_sr,
                    self.sr,
                )
                .squeeze(0)
                .numpy()
            )
            eff_timbre_blend = min(timbre_blend, 0.35)

        ru_anchor = ru_seg_44k[: min(len(ru_seg_44k), int(1.2 * self.sr))]
        en_anchor = en_for_timbre[: min(len(en_for_timbre), int(1.2 * self.sr))]
        ru_rms = float(np.sqrt(np.mean(ru_anchor ** 2))) + 1e-5
        en_rms = float(np.sqrt(np.mean(en_anchor ** 2))) + 1e-5
        en_anchor_norm = np.clip(en_anchor * (ru_rms / en_rms), -0.98, 0.98)
        prompt_44k = np.concatenate([ru_anchor, en_anchor_norm]).astype(np.float32)

        src_t = torch.from_numpy(ru_seg_44k).unsqueeze(0).float().to(self.device)
        prm_t = torch.from_numpy(prompt_44k).unsqueeze(0).float().to(self.device)
        ref_t = torch.from_numpy(en_ref_44k).unsqueeze(0).float().to(self.device)
        tim_t = torch.from_numpy(en_for_timbre).unsqueeze(0).float().to(self.device)
        ru_a_t = torch.from_numpy(ru_anchor).unsqueeze(0).float().to(self.device)

        src_16k = torchaudio.functional.resample(src_t, self.sr, 16000)
        prm_16k = torchaudio.functional.resample(prm_t, self.sr, 16000)
        ref_16k = torchaudio.functional.resample(ref_t, self.sr, 16000)
        tim_16k = torchaudio.functional.resample(tim_t, self.sr, 16000)
        ru_a_16k = torchaudio.functional.resample(ru_a_t, self.sr, 16000)

        S_alt = self._semantic_fn(src_16k)
        S_prm = self._semantic_fn(prm_16k)

        mel_src = self.to_mel(src_t)
        mel_prm = self.to_mel(prm_t)

        target_lengths = torch.LongTensor([mel_src.size(2)]).to(self.device)
        target2_lengths = torch.LongTensor([mel_prm.size(2)]).to(self.device)

        feat_ru = torchaudio.compliance.kaldi.fbank(ru_a_16k, num_mel_bins=80, dither=0, sample_frequency=16000)
        feat_ru = feat_ru - feat_ru.mean(dim=0, keepdim=True)
        style_ru = self.campplus(feat_ru.unsqueeze(0))

        feat_en = torchaudio.compliance.kaldi.fbank(tim_16k, num_mel_bins=80, dither=0, sample_frequency=16000)
        feat_en = feat_en - feat_en.mean(dim=0, keepdim=True)
        style_en = self.campplus(feat_en.unsqueeze(0))

        style_mix = (1.0 - eff_timbre_blend) * style_ru + eff_timbre_blend * style_en
        ru_norm = torch.norm(style_ru, dim=-1, keepdim=True) + 1e-6
        mix_norm = torch.norm(style_mix, dim=-1, keepdim=True) + 1e-6
        style2 = style_mix * (ru_norm / mix_norm)

        F0_prm_np = self.f0_extractor.infer_from_audio(prm_16k[0], thred=0.03)
        F0_ru_np = self.f0_extractor.infer_from_audio(src_16k[0], thred=0.03)
        F0_en_np = self.f0_extractor.infer_from_audio(ref_16k[0], thred=0.03)

        # Fold octave errors on Russian TTS relative to its own autocorrelation median
        v_ru = F0_ru_np > 1.0
        if np.any(v_ru):
            while np.any(F0_ru_np > ac_ru_med * 1.80):
                F0_ru_np[F0_ru_np > ac_ru_med * 1.80] *= 0.5

        # Clean English F0 contour without folding peaks across 240/420 Hz:
        # remove sub-harmonic creak glitches (<0.48 * med) and apply 5-frame median filter
        v_en_raw = F0_en_np > 1.0
        if np.sum(v_en_raw) >= 6:
            en_raw_med = float(np.median(F0_en_np[v_en_raw]))
            F0_en_np[F0_en_np < 0.48 * en_raw_med] = 0.0
            v_en_valid = F0_en_np > 1.0
            if np.sum(v_en_valid) >= 5:
                F0_en_np[v_en_valid] = medfilt(F0_en_np[v_en_valid], kernel_size=5)

        voiced_ru = F0_ru_np > 1.0
        voiced_en = F0_en_np > 1.0

        ru_med_hz = float(np.median(F0_ru_np[voiced_ru])) if np.any(voiced_ru) else ac_ru_med
        en_med_hz = float(np.median(F0_en_np[voiced_en])) if np.any(voiced_en) else ac_en_med
        out_med_hz = ru_med_hz

        if np.any(voiced_ru):
            log_f0_ru = np.log(np.maximum(F0_ru_np, 1e-5))
            med_ru = float(np.median(log_f0_ru[voiced_ru]))

            # Keep base register locked closely to the Russian speaker's natural chest pitch
            # (at most +1.0 semitone upward shift when the English voice is high-pitched)
            if np.sum(voiced_en) >= 6:
                log_f0_en_all = np.log(np.maximum(F0_en_np, 1e-5))
                med_en = float(np.median(log_f0_en_all[voiced_en]))
                semi_diff = float(np.clip((med_en - med_ru) * (12.0 / np.log(2.0)) * 0.18, -1.5, 1.0))
            else:
                semi_diff = float(np.clip((en_rms / ru_rms - 0.85) * 1.2, 0.0, 1.0))

            target_med = med_ru + semi_diff * (np.log(2.0) / 12.0)
            if is_male:
                target_med = min(target_med, np.log(max(ru_med_hz * 1.06, 142.0)))

            max_ceil_hz = 218.0 if is_male else 420.0

            # Normalize the two halves of the hybrid prompt [ru_anchor, en_anchor] separately
            ru_p_frames = int(round(len(ru_anchor) / float(self.sr) * 100.0))
            for sl in [slice(0, ru_p_frames), slice(ru_p_frames, len(F0_prm_np))]:
                sub = F0_prm_np[sl]
                v_sub = sub > 1.0
                if np.any(v_sub):
                    m_sub = float(np.median(np.log(np.maximum(sub[v_sub], 1e-5))))
                    sub[v_sub] = np.clip(
                        sub[v_sub] * np.exp(target_med - m_sub),
                        65.0,
                        max_ceil_hz,
                    )

            # 50ms (5-frame) Macro/Micro Pitch Decomposition
            t_ru = np.arange(len(log_f0_ru))
            ru_idx = np.where(voiced_ru)[0]
            v_ru_0, v_ru_1 = int(ru_idx[0]), int(ru_idx[-1])

            dev_ru_voiced = np.clip(log_f0_ru[voiced_ru] - med_ru, -0.45, 0.48)
            interp_ru = np.interp(t_ru, t_ru[voiced_ru], dev_ru_voiced)
            k5 = np.ones(5) / 5.0
            macro_ru = np.convolve(interp_ru, k5, mode="same")
            micro_ru = interp_ru - macro_ru

            if np.sum(voiced_en) >= 6:
                log_f0_en = np.log(np.maximum(F0_en_np, 1e-5))
                med_en = float(np.median(log_f0_en[voiced_en]))

                t_en = np.arange(len(log_f0_en))
                en_idx = np.where(voiced_en)[0]
                v_en_0, v_en_1 = int(en_idx[0]), int(en_idx[-1])

                dev_en_voiced = np.clip(log_f0_en[voiced_en] - med_en, -0.50, 0.52)
                interp_en = np.interp(t_en, t_en[voiced_en], dev_en_voiced)
                macro_en = np.convolve(interp_en, k5, mode="same")
                macro_en_span = macro_en[v_en_0 : v_en_1 + 1]
                if len(macro_en_span) >= 4 and (v_ru_1 > v_ru_0):
                    macro_en_span = macro_en_span - float(np.median(macro_en_span))
                    ru_span = macro_ru[v_ru_0 : v_ru_1 + 1]
                    ru_iqr_raw = float(np.percentile(ru_span, 75) - np.percentile(ru_span, 25))
                    en_iqr_raw = float(np.percentile(macro_en_span, 75) - np.percentile(macro_en_span, 25))
                    gain = 0.92
                    if en_iqr_raw > 0.02 and ru_iqr_raw > en_iqr_raw * 0.90:
                        gain = min(1.55, max(0.95, (ru_iqr_raw / en_iqr_raw) * 1.15))

                    macro_en_aligned = np.zeros_like(log_f0_ru)
                    ru_span_len = v_ru_1 - v_ru_0 + 1
                    macro_en_aligned[v_ru_0 : v_ru_1 + 1] = np.interp(
                        np.linspace(0, len(macro_en_span) - 1, ru_span_len),
                        np.arange(len(macro_en_span)),
                        macro_en_span,
                    )
                    shifted_log = target_med + gain * macro_en_aligned + 0.28 * micro_ru
                else:
                    shifted_log = target_med + interp_ru * 1.50
            else:
                shifted_log = target_med + interp_ru * 1.50

            max_ceil_log = np.log(max_ceil_hz)
            shifted_log = np.clip(shifted_log, target_med - 0.42, min(target_med + 0.46, max_ceil_log))
            F0_shifted_np = np.where(voiced_ru, np.exp(shifted_log), 0.0).astype(np.float32)
            out_med_hz = float(np.median(F0_shifted_np[voiced_ru]))
            out_iqr_semi = float(
                12.0
                * np.log2(
                    np.percentile(F0_shifted_np[voiced_ru], 75)
                    / max(1.0, np.percentile(F0_shifted_np[voiced_ru], 25))
                )
            )
            ru_iqr_semi = float(
                12.0
                * np.log2(
                    np.percentile(F0_ru_np[voiced_ru], 75)
                    / max(1.0, np.percentile(F0_ru_np[voiced_ru], 25))
                )
            )
        else:
            F0_shifted_np = F0_ru_np.astype(np.float32)
            ru_iqr_semi = 0.0
            out_iqr_semi = 0.0

        F0_prm = torch.from_numpy(F0_prm_np.astype(np.float32)).to(self.device)[None]
        shifted_f0_alt = torch.from_numpy(F0_shifted_np).to(self.device)[None]

        cond, _, _, _, _ = self.model.length_regulator(
            S_alt, ylens=target_lengths, n_quantizers=3, f0=shifted_f0_alt
        )
        prompt_condition, _, _, _, _ = self.model.length_regulator(
            S_prm, ylens=target2_lengths, n_quantizers=3, f0=F0_prm
        )

        cat_condition = torch.cat([prompt_condition, cond], dim=1)
        vc_target = self.model.cfm.inference(
            cat_condition,
            torch.LongTensor([cat_condition.size(1)]).to(self.device),
            mel_prm,
            style2,
            None,
            diffusion_steps,
            inference_cfg_rate=inference_cfg_rate,
        )
        vc_target = vc_target[:, :, mel_prm.size(-1) :]
        vc_wave = self.vocoder(vc_target.float()).squeeze()[None]
        out_np = vc_wave.squeeze(0).cpu().numpy()

        out_np = apply_presence_eq_and_limiter(out_np, self.sr, ref_wave_44k=en_for_timbre)
        return out_np, (ru_med_hz, en_med_hz, out_med_hz, ru_iqr_semi, out_iqr_semi)

    def enhance_phrase(
        self,
        ru_path: str,
        en_path: str,
        out_path: str,
        target_sr: int = None,
        diffusion_steps: int = 25,
        inference_cfg_rate: float = 0.75,
        emotion_scale: float = 1.75,
    ) -> dict:
        t0 = time.time()
        ru_wave, ru_sr = read_audio_complete(ru_path)
        en_wave, en_sr = read_audio_complete(en_path)

        out_sr = target_sr if target_sr is not None else en_sr
        orig_ru_dur = len(ru_wave) / float(ru_sr)
        target_out_samples = int(round(orig_ru_dur * out_sr))

        ru_44k = (
            torchaudio.functional.resample(torch.from_numpy(ru_wave).unsqueeze(0), ru_sr, self.sr)
            .squeeze(0)
            .numpy()
        )
        en_44k = (
            torchaudio.functional.resample(torch.from_numpy(en_wave).unsqueeze(0), en_sr, self.sr)
            .squeeze(0)
            .numpy()
        )

        phrases = find_speech_phrases(ru_44k, self.sr)
        en_phrases = find_speech_phrases(en_44k, self.sr)

        if len(phrases) == 0:
            phrases = [(0, len(ru_44k))]

        is_cutscene = len(ru_44k) > int(5.0 * self.sr)
        out_44k = ru_44k.copy()
        f0_stats = []

        for idx, (s, e) in enumerate(phrases):
            seg_len = e - s
            if seg_len < int(0.18 * self.sr):
                continue

            en_ref = None
            if is_cutscene:
                # In GTA 5 cutscenes, Russian and English timelines are synchronized:
                # take the exact matching timestamp window [s-0.15s .. e+0.25s] first!
                s_en = max(0, int(round(s * len(en_44k) / float(len(ru_44k)))) - int(0.15 * self.sr))
                e_en = min(len(en_44k), int(round(e * len(en_44k) / float(len(ru_44k)))) + int(0.25 * self.sr))
                if e_en - s_en >= int(0.25 * self.sr):
                    cand = en_44k[s_en:e_en]
                    if float(np.sqrt(np.mean(cand ** 2))) > 0.008:
                        en_ref = cand

            if en_ref is None:
                if len(en_phrases) > 0:
                    best_en = min(
                        en_phrases,
                        key=lambda ep: abs(0.5 * (ep[0] + ep[1]) / len(en_44k) - 0.5 * (s + e) / len(ru_44k)),
                    )
                    en_s, en_e = best_en
                    en_ref = en_44k[en_s:en_e]
                else:
                    rel_s = int((s / len(ru_44k)) * len(en_44k))
                    rel_e = min(len(en_44k), rel_s + max(seg_len, int(1.5 * self.sr)))
                    en_ref = en_44k[rel_s:rel_e]

            chunk_max = int(8.0 * self.sr)
            if seg_len <= chunk_max:
                conv_seg, f0_info = self.convert_phrase(
                    ru_44k[s:e],
                    en_ref,
                    diffusion_steps=diffusion_steps,
                    inference_cfg_rate=inference_cfg_rate,
                    emotion_scale=emotion_scale,
                )
                f0_stats.append(
                    f"P{idx+1}({s/self.sr:.2f}..{e/self.sr:.2f}s): "
                    f"F0 {f0_info[0]:.0f}->{f0_info[2]:.0f}Hz, IQR {f0_info[3]:.1f}->{f0_info[4]:.1f}st"
                )
            else:
                conv_parts = []
                for cs in range(s, e, chunk_max):
                    ce = min(e, cs + chunk_max)
                    if ce - cs < int(0.2 * self.sr):
                        conv_parts.append(ru_44k[cs:ce])
                    else:
                        cp, f0_info = self.convert_phrase(
                            ru_44k[cs:ce],
                            en_ref,
                            diffusion_steps=diffusion_steps,
                            inference_cfg_rate=inference_cfg_rate,
                            emotion_scale=emotion_scale,
                        )
                        f0_stats.append(
                            f"P{idx+1}({cs/self.sr:.2f}..{ce/self.sr:.2f}s): "
                            f"F0 {f0_info[0]:.0f}->{f0_info[2]:.0f}Hz, IQR {f0_info[3]:.1f}->{f0_info[4]:.1f}st"
                        )
                        if len(cp) >= (ce - cs):
                            cp = cp[: ce - cs]
                        else:
                            cp = np.pad(cp, (0, (ce - cs) - len(cp)))
                        conv_parts.append(cp)
                conv_seg = np.concatenate(conv_parts)

            if len(conv_seg) > seg_len:
                conv_seg = conv_seg[:seg_len]
            elif len(conv_seg) < seg_len:
                conv_seg = np.pad(conv_seg, (0, seg_len - len(conv_seg)))

            fade = min(int(0.015 * self.sr), seg_len // 4)
            if fade > 0:
                w_in = np.linspace(0.0, 1.0, fade, dtype=np.float32)
                w_out = np.linspace(1.0, 0.0, fade, dtype=np.float32)
                conv_seg[:fade] = ru_44k[s : s + fade] * w_out + conv_seg[:fade] * w_in
                conv_seg[-fade:] = conv_seg[-fade:] * w_out + ru_44k[e - fade : e] * w_in

            out_44k[s:e] = conv_seg

        out_wave = (
            torchaudio.functional.resample(torch.from_numpy(out_44k).unsqueeze(0), self.sr, out_sr)
            .squeeze(0)
            .numpy()
        )
        if len(out_wave) > target_out_samples:
            out_wave = out_wave[:target_out_samples]
        elif len(out_wave) < target_out_samples:
            out_wave = np.pad(out_wave, (0, target_out_samples - len(out_wave)))

        peak = float(np.max(np.abs(out_wave))) if len(out_wave) > 0 else 0.0
        if peak > 0.98:
            out_wave = out_wave * (0.98 / peak)

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        sf.write(out_path, out_wave, out_sr, subtype="PCM_16")
        dt = time.time() - t0
        return {
            "ru_path": ru_path,
            "en_path": en_path,
            "out_path": out_path,
            "out_sr": out_sr,
            "duration_sec": round(len(out_wave) / float(out_sr), 3),
            "phrases_enhanced": len(phrases),
            "f0_summary": "; ".join(f0_stats),
            "elapsed_sec": round(dt, 2),
        }

    def enhance_item(
        self,
        ru_path: str,
        en_path: str,
        out_path: str,
        target_sr: int = None,
        diffusion_steps: int = 25,
        inference_cfg_rate: float = 0.75,
        emotion_scale: float = 1.75,
    ) -> dict:
        """
        Enhance either a single (ru_file, en_file) pair OR a multichannel unpacked
        directory pair (e.g. 3-channel cutscenes pro_mcs_2 and pro_mcs_3_pt1).
        In 3-channel _mastered_only.awc files, all 3 channels are Left/Right/Center
        surround stems of the SAME dialogue — so we select the single highest-energy
        (Center/primary) stream and run inference ONCE to eliminate chorus/tripling!
        """
        if not os.path.isdir(ru_path):
            return self.enhance_phrase(
                ru_path=ru_path,
                en_path=en_path,
                out_path=out_path,
                target_sr=target_sr,
                diffusion_steps=diffusion_steps,
                inference_cfg_rate=inference_cfg_rate,
                emotion_scale=emotion_scale,
            )

        ru_dir = Path(ru_path)
        en_dir = Path(en_path)
        ru_files = sorted(
            [p for p in list(ru_dir.glob("*.mp3")) + list(ru_dir.glob("*.wav")) if p.stat().st_size >= 64 and not p.name.startswith("enhanced_")]
        )
        en_files = sorted([p for p in en_dir.glob("*.wav") if p.stat().st_size >= 64])
        if not ru_files or not en_files:
            raise ValueError(f"No valid audio streams in {ru_dir} ({len(ru_files)}) or {en_dir} ({len(en_files)})")

        best_ru = None
        best_ru_rms = -1.0
        for rp in ru_files:
            try:
                rw, _ = read_audio_complete(str(rp))
                rrms = float(np.sqrt(np.mean(rw ** 2)))
                if rrms > best_ru_rms:
                    best_ru_rms = rrms
                    best_ru = rp
            except Exception:
                pass

        best_en = None
        best_en_rms = -1.0
        for ep in en_files:
            try:
                ew, _ = read_audio_complete(str(ep))
                erms = float(np.sqrt(np.mean(ew ** 2)))
                if erms > best_en_rms:
                    best_en_rms = erms
                    best_en = ep
            except Exception:
                pass

        if best_ru is None or best_en is None:
            raise ValueError(f"No readable audio stream in {ru_dir} or {en_dir}")

        st = self.enhance_phrase(
            ru_path=str(best_ru),
            en_path=str(best_en),
            out_path=out_path,
            target_sr=target_sr,
            diffusion_steps=diffusion_steps,
            inference_cfg_rate=inference_cfg_rate,
            emotion_scale=emotion_scale,
        )
        st["f0_summary"] = f"[primary {best_ru.stem}/{best_en.stem}] " + st["f0_summary"]
        return st


def main():
    parser = argparse.ArgumentParser(description="GTA 5 Neural Emotion & Timbre Enhancer")
    parser.add_argument("--ru", help="Input Russian WAV/MP3 or unpacked directory")
    parser.add_argument("--en", help="Reference English WAV or unpacked directory")
    parser.add_argument("--out", help="Output enhanced WAV")
    parser.add_argument("--batch-json", help="JSON file with list of {ru, en, out, name} items")
    parser.add_argument("--steps", type=int, default=25, help="CFM diffusion steps")
    parser.add_argument("--cfg", type=float, default=0.75, help="Inference CFG rate")
    parser.add_argument("--emotion-scale", type=float, default=1.75, help="Pitch dynamic range multiplier")
    args = parser.parse_args()

    tools_dir = Path(__file__).resolve().parent
    engine = GTA5EmotionVC(tools_dir=tools_dir)

    if args.batch_json:
        with open(args.batch_json, "r", encoding="utf-8-sig") as f:
            items = json.load(f)
        results = []
        for idx, item in enumerate(items):
            name = item.get("name", f"item_{idx+1}")
            st = engine.enhance_item(
                ru_path=item["ru"],
                en_path=item["en"],
                out_path=item["out"],
                diffusion_steps=args.steps,
                inference_cfg_rate=args.cfg,
                emotion_scale=args.emotion_scale,
            )
            st["name"] = name
            print(f"[{idx+1}/{len(items)}] {name} -> {st['out_path']} ({st['elapsed_sec']}s, {st['f0_summary']})")
            results.append(st)
        print("RESULT_JSON:" + json.dumps(results, ensure_ascii=False))
    else:
        if not (args.ru and args.en and args.out):
            parser.error("Provide either --batch-json or --ru, --en, --out")
        stats = engine.enhance_item(
            ru_path=args.ru,
            en_path=args.en,
            out_path=args.out,
            diffusion_steps=args.steps,
            inference_cfg_rate=args.cfg,
            emotion_scale=args.emotion_scale,
        )
        print("RESULT_JSON:" + json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
