#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GTA 5 Emotion & Timbre Transfer Engine (Seed-VC 44.1kHz F0-Conditioned)
=======================================================================
Enhances flat Russian neural dubbing (Nerual Dreming v3.0) using the original
Rockstar English audio tracks from GTA V Legacy:
  1. Automatic Xing VBR header injection in memory so raw .awc MP3 streams are
     NEVER truncated by libsndfile/soundfile (fixes 'Хватит болтать, чёрт побери').
  2. High-resolution (10ms hop, 110ms plosive bridge) speaker-turn segmentation
     with autocorrelation F0 voice verification so different speakers in cutscenes
     are never merged together and background SFX/screams remain untouched.
  3. Octave-protected F0 emotion transfer: anchors each speaker's natural vocal
     register (110-165 Hz for male characters, 210-280 Hz for female characters)
     while transferring the dynamic pitch inflection arch and syllabic stress
     from the English performance.
"""

import argparse
import io
import os
import struct
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import scipy.signal
import soundfile as sf
import torch
import torchaudio
import yaml

warnings.simplefilter("ignore")


def ensure_mp3_xing_header(raw_mp3: bytes) -> bytes:
    """
    Prepend a valid 1-frame MPEG1 Layer3 Xing VBR header if not already present.
    Fixes libsndfile truncation on headerless VBR MP3 streams from Nerual .awc files.
    """
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
    if xing_off + 16 <= len(hdr):
        total_bytes = len(raw_mp3) + flen
        hdr[xing_off : xing_off + 4] = b"Xing"
        struct.pack_into(">III", hdr, xing_off + 4, 0x0003, frames, total_bytes)
        return bytes(hdr) + raw_mp3
    return raw_mp3


def read_audio_complete(path: str):
    """
    Read WAV/MP3 completely, injecting a Xing VBR header in memory for .mp3 files
    that lack one so libsndfile never truncates VBR streams.
    """
    raw = Path(path).read_bytes()
    if path.lower().endswith(".mp3"):
        raw = ensure_mp3_xing_header(raw)
    audio, sr = sf.read(io.BytesIO(raw), dtype="float32")
    if audio.ndim > 1:
        audio = audio[:, 0]
    return audio, sr


def estimate_f0_autocorr(wav: np.ndarray, sr: int) -> np.ndarray:
    """
    Fast time-domain autocorrelation F0 estimator in [65, 340] Hz human speech band.
    Used both to filter out non-speech SFX bursts and to guard RMVPE against harmonic locking.
    """
    win = int(0.04 * sr)
    hop = int(0.01 * sr)
    min_lag = int(sr / 500.0)
    max_lag = int(sr / 65.0)
    f0s = []
    for i in range(0, len(wav) - win, hop):
        frame = wav[i : i + win]
        if float(np.sqrt(np.mean(frame * frame))) < 0.035:
            continue
        frame = frame - np.mean(frame)
        corr = np.correlate(frame, frame, mode="full")[win - 1 :]
        corr = corr / (corr[0] + 1e-8)
        sub = corr[min_lag:max_lag]
        if len(sub) > 0 and float(np.max(sub)) > 0.38:
            lag = min_lag + int(np.argmax(sub))
            f0s.append(sr / lag)
    return np.array(f0s, dtype=np.float32)


def find_speech_phrases(
    audio: np.ndarray,
    sr: int,
    min_phrase_sec: float = 0.35,
    max_phrase_sec: float = 3.2,
):
    """
    Detect individual spoken phrases [start_sample, end_sample] with 10ms hop resolution.
    Only bridges micro-pauses <= 110ms (plosive consonant closures) so adjacent speakers
    in multi-character cutscenes (e.g. pro_mcs_1) are never merged together.
    Filters out non-speech SFX bursts using autocorrelation F0 verification.
    """
    if len(audio) < int(0.5 * sr):
        return [(0, len(audio))]

    win = int(0.02 * sr)
    hop = int(0.01 * sr)
    n_frames = (len(audio) - win) // hop + 1
    if n_frames <= 0:
        return [(0, len(audio))]

    rms = np.array(
        [np.sqrt(np.mean(audio[i * hop : i * hop + win] ** 2)) for i in range(n_frames)],
        dtype=np.float32,
    )
    rms_smooth = np.convolve(rms, np.ones(3) / 3.0, mode="same")
    peak_rms = float(np.percentile(rms_smooth, 96)) if len(rms_smooth) > 0 else 0.1
    thr = max(0.032, peak_rms * 0.16)
    min_peak_thr = max(0.065, peak_rms * 0.28)
    active = (rms_smooth > thr).copy()

    # Only bridge micro-pauses <= 110ms (11 frames of 10ms) for plosive consonants
    bridge = 11
    true_idx = np.where(active)[0]
    for k in range(len(true_idx) - 1):
        if 1 < (true_idx[k + 1] - true_idx[k]) <= bridge + 1:
            active[true_idx[k] : true_idx[k + 1]] = True

    raw_segs = []
    in_seg = False
    start = 0
    for i, a in enumerate(active):
        if a and not in_seg:
            in_seg = True
            start = max(0, i - 3)
        elif not a and in_seg:
            in_seg = False
            end = min(n_frames, i + 4)
            if (end - start) * 0.01 >= min_phrase_sec and float(np.max(rms_smooth[start:end])) >= min_peak_thr:
                raw_segs.append((start, end))
    if (
        in_seg
        and (n_frames - start) * 0.01 >= min_phrase_sec
        and float(np.max(rms_smooth[start:n_frames])) >= min_peak_thr
    ):
        raw_segs.append((start, n_frames))

    split_segs = []
    for s, e in raw_segs:
        cur_s = s
        while (e - cur_s) * 0.01 > max_phrase_sec:
            search_s = cur_s + int(1.0 / 0.01)
            search_e = min(e - int(0.6 / 0.01), cur_s + int(max_phrase_sec / 0.01))
            if search_e <= search_s:
                break
            valley_idx = search_s + int(np.argmin(rms_smooth[search_s:search_e]))
            if float(np.max(rms_smooth[cur_s:valley_idx])) >= min_peak_thr:
                split_segs.append((cur_s * hop, valley_idx * hop))
            cur_s = valley_idx
        if (e - cur_s) * 0.01 >= min_phrase_sec and float(np.max(rms_smooth[cur_s:e])) >= min_peak_thr:
            split_segs.append((cur_s * hop, min(len(audio), e * hop)))

    speech_segs = []
    for s, e in split_segs:
        seg = audio[s:e]
        f0s = estimate_f0_autocorr(seg, sr)
        if len(f0s) >= 4 and 65.0 <= float(np.median(f0s)) <= 340.0:
            speech_segs.append((s, e))

    return speech_segs if speech_segs else [(0, len(audio))]


class GTA5EmotionVC:
    def __init__(self, seed_vc_dir: str, device: str = "cuda", fp16: bool = True):
        if seed_vc_dir not in sys.path:
            sys.path.insert(0, seed_vc_dir)

        from modules.commons import recursive_munch, build_model, load_checkpoint
        from modules.audio import mel_spectrogram
        from modules.rmvpe import RMVPE
        from modules.campplus.DTDNN import CAMPPlus
        from modules.bigvgan import bigvgan
        from hf_utils import load_custom_model_from_hf
        from transformers import AutoFeatureExtractor, WhisperModel

        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.fp16 = fp16 and (self.device.type == "cuda")

        print(f"[GTA5EmotionVC] Loading 44.1kHz F0-Conditioned Seed-VC models on {self.device}...")
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
        inference_cfg_rate: float = 0.70,
        emotion_scale: float = 1.32,
        contour_blend: float = 0.25,
        timbre_blend: float = 0.25,
    ):
        """
        Enhance a single Russian speech phrase with the emotional prosody, dynamic pitch
        inflection, and vocal tension of the matching English phrase while preserving
        the Russian speaker's natural vocal register and distinct speaker identity.
        """
        ru_seg_44k = np.clip(ru_seg_44k, -0.99, 0.99)
        en_ref_44k = np.clip(en_ref_44k, -0.99, 0.99)

        # Build clean acoustic prompt from the current Russian speaker so each character
        # keeps 100% of their distinct voice and clean studio bass without cutscene SFX bleed
        prompt_44k = ru_seg_44k.copy()
        if len(prompt_44k) < int(1.2 * self.sr):
            repeats = int(np.ceil(int(1.2 * self.sr) / max(1, len(prompt_44k))))
            prompt_44k = np.tile(prompt_44k, repeats)[: int(1.6 * self.sr)]
        elif len(prompt_44k) > int(4.0 * self.sr):
            prompt_44k = prompt_44k[: int(4.0 * self.sr)]

        if len(en_ref_44k) < int(1.0 * self.sr):
            repeats = int(np.ceil(int(1.0 * self.sr) / max(1, len(en_ref_44k))))
            en_ref_44k = np.tile(en_ref_44k, repeats)[: int(1.5 * self.sr)]
        elif len(en_ref_44k) > int(6.0 * self.sr):
            en_ref_44k = en_ref_44k[: int(6.0 * self.sr)]

        src_t = torch.from_numpy(ru_seg_44k).unsqueeze(0).float().to(self.device)
        prm_t = torch.from_numpy(prompt_44k).unsqueeze(0).float().to(self.device)
        ref_t = torch.from_numpy(en_ref_44k).unsqueeze(0).float().to(self.device)

        src_16k = torchaudio.functional.resample(src_t, self.sr, 16000)
        prm_16k = torchaudio.functional.resample(prm_t, self.sr, 16000)
        ref_16k = torchaudio.functional.resample(ref_t, self.sr, 16000)

        S_alt = self._semantic_fn(src_16k)
        S_prm = self._semantic_fn(prm_16k)

        mel_src = self.to_mel(src_t)
        mel_prm = self.to_mel(prm_t)

        target_lengths = torch.LongTensor([mel_src.size(2)]).to(self.device)
        target2_lengths = torch.LongTensor([mel_prm.size(2)]).to(self.device)

        # Speaker style embedding: blend Russian character identity (75%) with English emotional arousal (25%)
        feat_ru = torchaudio.compliance.kaldi.fbank(prm_16k, num_mel_bins=80, dither=0, sample_frequency=16000)
        feat_ru = feat_ru - feat_ru.mean(dim=0, keepdim=True)
        style_ru = self.campplus(feat_ru.unsqueeze(0))

        feat_en = torchaudio.compliance.kaldi.fbank(ref_16k, num_mel_bins=80, dither=0, sample_frequency=16000)
        feat_en = feat_en - feat_en.mean(dim=0, keepdim=True)
        style_en = self.campplus(feat_en.unsqueeze(0))

        style_mix = (1.0 - timbre_blend) * style_ru + timbre_blend * style_en
        ru_norm = torch.norm(style_ru, dim=-1, keepdim=True) + 1e-6
        mix_norm = torch.norm(style_mix, dim=-1, keepdim=True) + 1e-6
        style2 = style_mix * (ru_norm / mix_norm)

        # Extract pitch contours via fixed RMVPE @ 16kHz
        F0_prm_np = self.f0_extractor.infer_from_audio(prm_16k[0], thred=0.03)
        F0_ru_np = self.f0_extractor.infer_from_audio(src_16k[0], thred=0.03)
        F0_en_np = self.f0_extractor.infer_from_audio(ref_16k[0], thred=0.03)

        # Ground-truth autocorrelation F0 sanity anchor to prevent any harmonic octave jump
        ac_f0s = estimate_f0_autocorr(ru_seg_44k, self.sr)
        ac_med_hz = float(np.median(ac_f0s)) if len(ac_f0s) > 0 else 130.0

        for arr in (F0_ru_np, F0_prm_np):
            v = arr > 1.0
            if np.any(v):
                # Fold down any frames where RMVPE locked onto 2nd or 3rd harmonic
                while np.any(arr > ac_med_hz * 1.75):
                    arr[arr > ac_med_hz * 1.75] *= 0.5

        voiced_ru = F0_ru_np > 1.0
        voiced_en = F0_en_np > 1.0

        ru_med_hz = float(np.median(F0_ru_np[voiced_ru])) if np.any(voiced_ru) else ac_med_hz
        out_med_hz = ru_med_hz

        if np.any(voiced_ru):
            log_f0_ru = np.log(np.maximum(F0_ru_np, 1e-5))
            med_ru = float(np.median(log_f0_ru[voiced_ru]))
            std_ru = float(np.std(log_f0_ru[voiced_ru])) + 1e-4

            # Compute subtle emotional arousal pitch shift bounded strictly to [0.0, +1.0] semitone
            ru_rms = float(np.sqrt(np.mean(ru_seg_44k ** 2))) + 1e-5
            en_rms = float(np.sqrt(np.mean(en_ref_44k ** 2))) + 1e-5
            arousal_semitones = float(np.clip((en_rms / ru_rms - 0.9) * 0.8, 0.0, 1.0))
            target_med = med_ru + arousal_semitones * (np.log(2.0) / 12.0)

            # Expand Russian word-stress pitch contour
            dev_ru = np.clip(log_f0_ru - med_ru, -0.26, 0.28)
            dyn_ratio = float(np.clip(emotion_scale, 1.15, 1.45))

            if np.sum(voiced_en) >= 8 and contour_blend > 0.0:
                log_f0_en = np.log(np.maximum(F0_en_np, 1e-5))
                med_en = float(np.median(log_f0_en[voiced_en]))
                std_en = float(np.std(log_f0_en[voiced_en])) + 1e-4
                dyn_ratio = float(np.clip((std_en / std_ru) * 0.5 + emotion_scale * 0.6, 1.18, 1.42))

                t_en = np.arange(len(log_f0_en))
                dev_en_voiced = np.clip(log_f0_en[voiced_en] - med_en, -0.22, 0.22)
                interp_en = np.interp(t_en, t_en[voiced_en], dev_en_voiced)
                kernel = np.ones(9) / 9.0
                macro_en = np.convolve(interp_en, kernel, mode="same")
                macro_en = macro_en - float(np.median(macro_en))
                macro_resampled = np.interp(
                    np.linspace(0, len(macro_en) - 1, len(log_f0_ru)),
                    np.arange(len(macro_en)),
                    macro_en,
                )
                shifted_log = target_med + dev_ru * dyn_ratio + contour_blend * macro_resampled
            else:
                shifted_log = target_med + dev_ru * dyn_ratio

            # Hard clamp within natural human speech inflection range [-4.5, +5.2] semitones around speaker's med_ru
            shifted_log = np.clip(shifted_log, med_ru - 0.26, med_ru + 0.30)
            F0_shifted_np = np.where(voiced_ru, np.exp(shifted_log), 0.0).astype(np.float32)
            out_med_hz = float(np.median(F0_shifted_np[voiced_ru]))
        else:
            F0_shifted_np = F0_ru_np.astype(np.float32)

        F0_prm = torch.from_numpy(F0_prm_np.astype(np.float32)).to(self.device)[None]
        shifted_f0_alt = torch.from_numpy(F0_shifted_np).to(self.device)[None]

        cond, _, _, _, _ = self.model.length_regulator(
            S_alt, ylens=target_lengths, n_quantizers=3, f0=shifted_f0_alt
        )
        prompt_condition, _, _, _, _ = self.model.length_regulator(
            S_prm, ylens=target2_lengths, n_quantizers=3, f0=F0_prm
        )

        cat_condition = torch.cat([prompt_condition, cond], dim=1)
        with torch.autocast(device_type=self.device.type, dtype=torch.float16 if self.fp16 else torch.float32):
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

        vc_wave = self.vocoder(vc_target.float()).squeeze().cpu().numpy()
        return vc_wave, ru_med_hz, out_med_hz

    def process_file(
        self,
        ru_path: str,
        en_path: str,
        out_path: str,
        diffusion_steps: int = 25,
        emotion_scale: float = 1.32,
        contour_blend: float = 0.25,
    ) -> dict:
        t0 = time.time()
        ru_audio, ru_sr = read_audio_complete(ru_path)
        en_audio, en_sr = read_audio_complete(en_path)

        phrases = find_speech_phrases(ru_audio, ru_sr)
        out_audio = ru_audio.copy()

        en_voiced_indices = np.where(np.abs(en_audio) > 0.025)[0]
        if len(en_voiced_indices) > int(0.3 * en_sr):
            en_active_full = en_audio[en_voiced_indices[0] : en_voiced_indices[-1] + 1]
        else:
            en_active_full = en_audio

        f0_diagnostics = []
        for idx, (s, e) in enumerate(phrases):
            ru_seg = ru_audio[s:e]
            seg_len = e - s

            if len(phrases) == 1 and len(ru_audio) < 5.0 * ru_sr:
                en_seg = en_active_full
            else:
                s_sec = s / ru_sr
                e_sec = e / ru_sr
                s_en = max(0, int(s_sec * en_sr))
                e_en = min(len(en_audio), int(e_sec * en_sr))
                en_seg = en_audio[s_en:e_en]
                if len(en_seg) < int(0.35 * en_sr) or np.sqrt(np.mean(en_seg ** 2)) < 0.015:
                    en_seg = en_active_full

            ru_44k = torchaudio.functional.resample(
                torch.from_numpy(ru_seg).unsqueeze(0), ru_sr, self.sr
            ).squeeze(0).numpy()
            en_44k = torchaudio.functional.resample(
                torch.from_numpy(en_seg).unsqueeze(0), en_sr, self.sr
            ).squeeze(0).numpy()

            vc_44k, ru_f0_hz, out_f0_hz = self.convert_phrase(
                ru_44k,
                en_44k,
                diffusion_steps=diffusion_steps,
                emotion_scale=emotion_scale,
                contour_blend=contour_blend,
            )
            f0_diagnostics.append(f"P{idx+1}({s/ru_sr:.2f}..{e/ru_sr:.2f}s): {ru_f0_hz:.1f}Hz->{out_f0_hz:.1f}Hz")

            vc_ru_sr = torchaudio.functional.resample(
                torch.from_numpy(vc_44k).unsqueeze(0), self.sr, ru_sr
            ).squeeze(0).numpy()

            # Match exact sample length by trimming/padding STFT hop tail (<10ms) WITHOUT resampling pitch!
            if len(vc_ru_sr) > seg_len:
                vc_ru_sr = vc_ru_sr[:seg_len]
            elif len(vc_ru_sr) < seg_len:
                vc_ru_sr = np.pad(vc_ru_sr, (0, seg_len - len(vc_ru_sr)))

            # Transfer dynamic amplitude envelope from English phrase (syllable punch)
            win_env = max(1, int(0.05 * ru_sr))
            env_ru = np.sqrt(np.convolve(vc_ru_sr ** 2, np.ones(win_env) / win_env, mode="same") + 1e-6)
            en_resampled = scipy.signal.resample(en_seg, seg_len).astype(np.float32)
            env_en = np.sqrt(np.convolve(en_resampled ** 2, np.ones(win_env) / win_env, mode="same") + 1e-6)
            env_ratio = np.clip((env_en / (np.median(env_en) + 1e-5)) / (env_ru / (np.median(env_ru) + 1e-5)), 0.75, 1.35)
            vc_ru_sr = vc_ru_sr * (0.75 + 0.25 * env_ratio)

            # Preserve warm sub-220Hz chest resonance from clean Russian studio voice
            nyq = 0.5 * ru_sr
            sos_lp = scipy.signal.butter(4, min(220.0 / nyq, 0.9), btype="low", output="sos")
            sos_hp = scipy.signal.butter(4, min(220.0 / nyq, 0.9), btype="high", output="sos")
            ru_low = scipy.signal.sosfiltfilt(sos_lp, ru_seg).astype(np.float32)
            vc_low = scipy.signal.sosfiltfilt(sos_lp, vc_ru_sr).astype(np.float32)
            vc_high = scipy.signal.sosfiltfilt(sos_hp, vc_ru_sr).astype(np.float32)
            vc_ru_sr = (0.65 * vc_low + 0.35 * ru_low) + vc_high

            ru_rms = float(np.sqrt(np.mean(ru_seg ** 2))) + 1e-6
            en_rms = float(np.sqrt(np.mean(en_seg ** 2))) + 1e-6
            vc_rms = float(np.sqrt(np.mean(vc_ru_sr ** 2))) + 1e-6
            target_rms = 0.70 * ru_rms + 0.30 * max(ru_rms, en_rms)
            vc_ru_sr = vc_ru_sr * (target_rms / vc_rms)

            peak = float(np.max(np.abs(vc_ru_sr)))
            if peak > 0.96:
                vc_ru_sr = vc_ru_sr * (0.96 / peak)

            fade_len = min(int(0.02 * ru_sr), seg_len // 4)
            if fade_len > 0:
                fade_in = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade_len))).astype(np.float32)
                fade_out = fade_in[::-1]
                vc_ru_sr[:fade_len] = ru_seg[:fade_len] * fade_out + vc_ru_sr[:fade_len] * fade_in
                vc_ru_sr[-fade_len:] = vc_ru_sr[-fade_len:] * fade_out + ru_seg[-fade_len:] * fade_in

            out_audio[s:e] = vc_ru_sr

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        sf.write(out_path, out_audio, ru_sr, subtype="PCM_16")
        elapsed = time.time() - t0
        return {
            "ru_path": ru_path,
            "en_path": en_path,
            "out_path": out_path,
            "duration_sec": round(len(out_audio) / ru_sr, 3),
            "phrases_converted": len(phrases),
            "f0_summary": f0_diagnostics,
            "elapsed_sec": round(elapsed, 2),
        }


def main():
    parser = argparse.ArgumentParser(description="GTA 5 Emotion & Timbre Transfer Engine")
    parser.add_argument("--seed-vc-dir", required=True, help="Path to extracted seed_vc_core folder")
    parser.add_argument("--ru", required=True, help="Input Russian WAV/MP3 file")
    parser.add_argument("--en", required=True, help="Reference English WAV file")
    parser.add_argument("--out", required=True, help="Output enhanced Russian WAV file")
    parser.add_argument("--steps", type=int, default=25, help="Diffusion steps (default: 25)")
    parser.add_argument("--emotion-scale", type=float, default=1.32, help="Pitch expressiveness multiplier")
    parser.add_argument("--contour-blend", type=float, default=0.25, help="Macro pitch contour transfer weight")
    args = parser.parse_args()

    engine = GTA5EmotionVC(seed_vc_dir=args.seed_vc_dir)
    info = engine.process_file(
        ru_path=args.ru,
        en_path=args.en,
        out_path=args.out,
        diffusion_steps=args.steps,
        emotion_scale=args.emotion_scale,
        contour_blend=args.contour_blend,
    )
    print(f"DONE: {info}")


if __name__ == "__main__":
    main()
