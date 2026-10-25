#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GTA 5 Emotion & Timbre Transfer Engine (Seed-VC 44.1kHz F0 + Expressive Prosody Transfer)
Transfers the original English actor's vocal timbre, pitch register, pitch dynamic range,
macro emotional intonation arc, and dynamic energy envelope onto Nerual Dreming Russian audio,
while preserving 100% of Russian word/syllable timings and leaving cutscene SFX/silence untouched.
"""

import os
import sys

# Store all HuggingFace and Torch caches on D:\gta5-voice\checkpoints (never clutter C: drive)
os.environ.setdefault("HF_HOME", r"D:\gta5-voice\checkpoints")
os.environ.setdefault("TORCH_HOME", r"D:\gta5-voice\checkpoints")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import time
import argparse
import yaml
import numpy as np
import soundfile as sf
import scipy.signal
import torch
import torchaudio

# Ensure weights_only=False by default for PyTorch >= 2.6 checkpoint compatibility
_orig_torch_load = torch.load
def _safe_torch_load(*args, **kwargs):
    if "weights_only" not in kwargs:
        kwargs["weights_only"] = False
    return _orig_torch_load(*args, **kwargs)
torch.load = _safe_torch_load


def find_speech_phrases(audio: np.ndarray, sr: int, min_phrase_sec: float = 0.28, max_phrase_sec: float = 6.0):
    """
    Detect active spoken phrases [start_sample, end_sample] in audio, ignoring silence and low-level ambient SFX.
    """
    if len(audio) < int(1.8 * sr):
        return [(0, len(audio))]

    win = int(0.02 * sr)  # 20ms frame
    n_frames = len(audio) // win
    if n_frames == 0:
        return [(0, len(audio))]

    frames = audio[:n_frames * win].reshape(n_frames, win)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    rms_smooth = np.convolve(rms, np.ones(3) / 3.0, mode="same")

    peak_rms = float(np.percentile(rms_smooth, 95)) if len(rms_smooth) > 0 else 0.1
    thr = max(0.030, peak_rms * 0.14)
    min_peak_thr = max(0.055, peak_rms * 0.24)
    active = (rms_smooth > thr).copy()

    # Bridge short pauses <= 280ms (14 frames) within a single sentence
    bridge = 14
    true_idx = np.where(active)[0]
    for k in range(len(true_idx) - 1):
        if 1 < (true_idx[k + 1] - true_idx[k]) <= bridge + 1:
            active[true_idx[k]:true_idx[k + 1]] = True

    raw_segs = []
    in_seg = False
    start = 0
    for i, a in enumerate(active):
        if a and not in_seg:
            in_seg = True
            start = max(0, i - 3)  # 60ms pre-roll
        elif not a and in_seg:
            in_seg = False
            end = min(n_frames, i + 4)  # 80ms post-roll
            if (end - start) * 0.02 >= min_phrase_sec and float(np.max(rms_smooth[start:end])) >= min_peak_thr:
                raw_segs.append((start, end))
    if in_seg and float(np.max(rms_smooth[start:n_frames])) >= min_peak_thr:
        raw_segs.append((start, n_frames))

    final_segs = []
    for s, e in raw_segs:
        cur_s = s
        while (e - cur_s) * 0.02 > max_phrase_sec:
            search_s = cur_s + int(2.0 / 0.02)
            search_e = min(e - int(0.8 / 0.02), cur_s + int(max_phrase_sec / 0.02))
            if search_e <= search_s:
                break
            valley_idx = search_s + int(np.argmin(rms_smooth[search_s:search_e]))
            if float(np.max(rms_smooth[cur_s:valley_idx])) >= min_peak_thr:
                final_segs.append((cur_s * win, valley_idx * win))
            cur_s = valley_idx
        if (e - cur_s) * 0.02 >= min_phrase_sec and float(np.max(rms_smooth[cur_s:e])) >= min_peak_thr:
            final_segs.append((cur_s * win, min(len(audio), e * win)))

    return final_segs if final_segs else [(0, len(audio))]


class GTA5EmotionVC:
    def __init__(self, seed_vc_dir: str, device: str = "cuda", fp16: bool = True):
        if seed_vc_dir not in sys.path:
            sys.path.insert(0, seed_vc_dir)

        from modules.commons import recursive_munch, build_model, load_checkpoint
        from modules.audio import mel_spectrogram
        from modules.rmvpe import RMVPE
        from modules.campplus.DTDNN import CAMPPlus
        from modules.bigvgan import bigvgan
        from transformers import AutoFeatureExtractor, WhisperModel
        from hf_utils import load_custom_model_from_hf

        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.fp16 = fp16 and (self.device.type == "cuda")

        print(f"[GTA5EmotionVC] Loading 44.1kHz F0-Conditioned Seed-VC models on {self.device}...")
        dit_ckpt_path, dit_cfg_path = load_custom_model_from_hf(
            "Plachta/Seed-VC",
            "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
            "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
        )

        rmvpe_path = load_custom_model_from_hf("lj1995/VoiceConversionWebUI", "rmvpe.pt", None)
        self.f0_extractor = RMVPE(rmvpe_path, is_half=False, device=self.device)

        config = yaml.safe_load(open(dit_cfg_path, "r", encoding="utf-8"))
        model_params = recursive_munch(config["model_params"])
        model_params.dit_type = "DiT"
        model = build_model(model_params, stage="DiT")

        self.sr = config["preprocess_params"]["sr"]  # 44100
        self.hop_length = config["preprocess_params"]["spect_params"]["hop_length"]  # 512

        model, _, _, _ = load_checkpoint(
            model, None, dit_ckpt_path,
            load_only_params=True, ignore_modules=[], is_distributed=False
        )
        for k in model:
            model[k].eval()
            model[k].to(self.device)
        model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)
        self.model = model

        campplus_ckpt = load_custom_model_from_hf("funasr/campplus", "campplus_cn_common.bin", config_filename=None)
        self.campplus = CAMPPlus(feat_dim=80, embedding_size=192)
        self.campplus.load_state_dict(torch.load(campplus_ckpt, map_location="cpu", weights_only=False))
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
        )
        ori_input_features = ori_inputs.input_features.to(
            self.device, dtype=self.whisper_model.encoder.dtype
        )
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
        emotion_scale: float = 1.35,
        contour_blend: float = 0.28,
    ) -> np.ndarray:
        """
        Convert a single Russian speech phrase using the matching English phrase as timbre & emotion reference.
        """
        if len(en_ref_44k) < int(1.5 * self.sr):
            repeats = int(np.ceil(int(1.5 * self.sr) / max(1, len(en_ref_44k))))
            en_ref_44k = np.tile(en_ref_44k, repeats)[: int(2.0 * self.sr)]
        elif len(en_ref_44k) > int(12.0 * self.sr):
            en_ref_44k = en_ref_44k[: int(12.0 * self.sr)]

        src_t = torch.from_numpy(ru_seg_44k).unsqueeze(0).float().to(self.device)
        ref_t = torch.from_numpy(en_ref_44k).unsqueeze(0).float().to(self.device)

        src_16k = torchaudio.functional.resample(src_t, self.sr, 16000)
        ref_16k = torchaudio.functional.resample(ref_t, self.sr, 16000)

        S_alt = self._semantic_fn(src_16k)
        S_ori = self._semantic_fn(ref_16k)

        mel_src = self.to_mel(src_t)
        mel_ref = self.to_mel(ref_t)

        target_lengths = torch.LongTensor([mel_src.size(2)]).to(self.device)
        target2_lengths = torch.LongTensor([mel_ref.size(2)]).to(self.device)

        feat2 = torchaudio.compliance.kaldi.fbank(
            ref_16k, num_mel_bins=80, dither=0, sample_frequency=16000
        )
        feat2 = feat2 - feat2.mean(dim=0, keepdim=True)
        style2 = self.campplus(feat2.unsqueeze(0))

        # Extract pitch contours (RMVPE @ 16kHz, 10ms hop)
        F0_ori_np = self.f0_extractor.infer_from_audio(ref_16k[0], thred=0.03)
        F0_alt_np = self.f0_extractor.infer_from_audio(src_16k[0], thred=0.03)

        voiced_ori = F0_ori_np > 1.0
        voiced_alt = F0_alt_np > 1.0

        log_f0_alt = np.log(np.maximum(F0_alt_np, 1e-5))
        log_f0_ori = np.log(np.maximum(F0_ori_np, 1e-5))

        if np.any(voiced_ori) and np.any(voiced_alt):
            med_ori = float(np.median(log_f0_ori[voiced_ori]))
            med_alt = float(np.median(log_f0_alt[voiced_alt]))
            std_ori = float(np.std(log_f0_ori[voiced_ori])) + 1e-4
            std_alt = float(np.std(log_f0_alt[voiced_alt])) + 1e-4

            # Dynamic pitch variance expansion: transfer English pitch expressiveness
            dyn_ratio = np.clip((std_ori / std_alt) * emotion_scale, 1.10, 1.85)
            dev_alt = (log_f0_alt - med_alt) * dyn_ratio

            # Smooth macro pitch contour from English phrase aligned to Russian phrase duration
            if np.sum(voiced_ori) >= 8 and contour_blend > 0.0:
                t_ori = np.arange(len(log_f0_ori))
                interp_ori = np.interp(t_ori, t_ori[voiced_ori], log_f0_ori[voiced_ori] - med_ori)
                kernel = np.ones(15) / 15.0
                macro_ori = np.convolve(interp_ori, kernel, mode="same")
                macro_resampled = np.interp(
                    np.linspace(0, len(macro_ori) - 1, len(log_f0_alt)),
                    np.arange(len(macro_ori)),
                    macro_ori,
                )
                shifted_log = med_ori + (1.0 - contour_blend) * dev_alt + contour_blend * (dev_alt + macro_resampled)
            else:
                shifted_log = med_ori + dev_alt

            shifted_log = np.clip(shifted_log, med_ori - 0.58, med_ori + 0.65)
            F0_shifted_np = np.where(voiced_alt, np.exp(shifted_log), 0.0).astype(np.float32)
        else:
            F0_shifted_np = F0_alt_np.astype(np.float32)

        F0_ori = torch.from_numpy(F0_ori_np).to(self.device)[None]
        shifted_f0_alt = torch.from_numpy(F0_shifted_np).to(self.device)[None]

        cond, _, _, _, _ = self.model.length_regulator(
            S_alt, ylens=target_lengths, n_quantizers=3, f0=shifted_f0_alt
        )
        prompt_condition, _, _, _, _ = self.model.length_regulator(
            S_ori, ylens=target2_lengths, n_quantizers=3, f0=F0_ori
        )

        cat_condition = torch.cat([prompt_condition, cond], dim=1)
        with torch.autocast(device_type=self.device.type, dtype=torch.float16 if self.fp16 else torch.float32):
            vc_target = self.model.cfm.inference(
                cat_condition,
                torch.LongTensor([cat_condition.size(1)]).to(self.device),
                mel_ref,
                style2,
                None,
                diffusion_steps,
                inference_cfg_rate=inference_cfg_rate,
            )
            vc_target = vc_target[:, :, mel_ref.size(-1) :]

        vc_wave = self.vocoder(vc_target.float()).squeeze().cpu().numpy()
        return vc_wave

    def process_file(
        self,
        ru_path: str,
        en_path: str,
        out_path: str,
        diffusion_steps: int = 25,
        emotion_scale: float = 1.35,
        contour_blend: float = 0.28,
    ) -> dict:
        t0 = time.time()
        ru_audio, ru_sr = sf.read(ru_path, dtype="float32")
        en_audio, en_sr = sf.read(en_path, dtype="float32")
        if ru_audio.ndim > 1:
            ru_audio = ru_audio[:, 0]
        if en_audio.ndim > 1:
            en_audio = en_audio[:, 0]

        phrases = find_speech_phrases(ru_audio, ru_sr)
        out_audio = ru_audio.copy()

        en_voiced_indices = np.where(np.abs(en_audio) > 0.025)[0]
        if len(en_voiced_indices) > int(0.3 * en_sr):
            en_active_full = en_audio[en_voiced_indices[0] : en_voiced_indices[-1] + 1]
        else:
            en_active_full = en_audio

        for idx, (s, e) in enumerate(phrases):
            ru_seg = ru_audio[s:e]
            seg_len = e - s

            if len(phrases) == 1 and len(ru_audio) < 5.0 * ru_sr:
                en_seg = en_active_full
            else:
                s_sec = s / ru_sr
                e_sec = e / ru_sr
                pad_sec = max(0.0, (1.8 - (e_sec - s_sec)) * 0.5)
                s_en = max(0, int((s_sec - pad_sec) * en_sr))
                e_en = min(len(en_audio), int((e_sec + pad_sec) * en_sr))
                en_seg = en_audio[s_en:e_en]
                if len(en_seg) < int(0.4 * en_sr) or np.sqrt(np.mean(en_seg ** 2)) < 0.015:
                    en_seg = en_active_full

            ru_44k = torchaudio.functional.resample(
                torch.from_numpy(ru_seg).unsqueeze(0), ru_sr, self.sr
            ).squeeze(0).numpy()
            en_44k = torchaudio.functional.resample(
                torch.from_numpy(en_seg).unsqueeze(0), en_sr, self.sr
            ).squeeze(0).numpy()

            vc_44k = self.convert_phrase(
                ru_44k,
                en_44k,
                diffusion_steps=diffusion_steps,
                emotion_scale=emotion_scale,
                contour_blend=contour_blend,
            )

            vc_ru_sr = torchaudio.functional.resample(
                torch.from_numpy(vc_44k).unsqueeze(0), self.sr, ru_sr
            ).squeeze(0).numpy()

            if len(vc_ru_sr) != seg_len:
                vc_ru_sr = scipy.signal.resample(vc_ru_sr, seg_len).astype(np.float32)

            ru_rms = float(np.sqrt(np.mean(ru_seg ** 2))) + 1e-6
            en_rms = float(np.sqrt(np.mean(en_seg ** 2))) + 1e-6
            vc_rms = float(np.sqrt(np.mean(vc_ru_sr ** 2))) + 1e-6
            target_rms = 0.65 * ru_rms + 0.35 * en_rms
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
            "elapsed_sec": round(elapsed, 2),
        }


def main():
    parser = argparse.ArgumentParser(description="GTA 5 Emotion & Timbre Transfer Engine")
    parser.add_argument("--seed-vc-dir", required=True, help="Path to extracted seed_vc_core folder")
    parser.add_argument("--ru", required=True, help="Input Russian WAV/MP3 file")
    parser.add_argument("--en", required=True, help="Reference English WAV file")
    parser.add_argument("--out", required=True, help="Output enhanced Russian WAV file")
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--emotion-scale", type=float, default=1.35)
    parser.add_argument("--contour-blend", type=float, default=0.28)
    args = parser.parse_args()

    engine = GTA5EmotionVC(args.seed_vc_dir)
    info = engine.process_file(
        args.ru,
        args.en,
        args.out,
        diffusion_steps=args.steps,
        emotion_scale=args.emotion_scale,
        contour_blend=args.contour_blend,
    )
    print(f"DONE: {info}")


if __name__ == "__main__":
    main()
