#!/usr/bin/env python3
"""
gta5_awc_tool.py — Чистый бинарный распаковщик/упаковщик контейнеров .awc (GTA V Legacy & Enhanced).

Устраняет 5 критических багов стандартного CodeWalker (AwcFile.cs):
1. Баг щелчка на конце реплик в DecodeADPCM (CodeWalker выделял буфер data.Length * 4 вместо
   sampleCount * 2, оставляя нулевой/мусорный хвост из-за 4-байтных заголовков ADPCM-блоков и паддинга).
2. Баг переполнения в ParseWavFile (sampleCount = datalen * 2 вместо datalen // 2 для 16-bit PCM).
3. Баг обрезки конца реплики в EncodeADPCM (буфер data.Length / 4 не учитывал 4-байтные заголовки
   [stepIndex, predictor] в начале каждого 2048-байтного блока на 4088 сэмплов).
4. Закомментированное обновление количества сэмплов (FormatChunk.Samples / StreamFormat.Samples)
   при изменении длительности реплики.
5. Сохранение лицевой анимации (animation / 0x5C), жестов (gesture / 0x2B) и тайм-маркеров
   (markers / 0xBD) строго побайтово (in-place binary) без разрушающей конвертации в XML.

Команды:
  python gta5_awc_tool.py info     <file.awc>
  python gta5_awc_tool.py unpack   <file.awc> <out_dir>
  python gta5_awc_tool.py repack   <orig.awc> <wav_dir> <out.awc>
  python gta5_awc_tool.py selftest
"""

import json
import math
import os
import struct
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Константы заголовка и чанков AWC
AWC_MAGIC_LE = 0x54414441  # "ADAT"

CHUNK_DATA = 0x55
CHUNK_FORMAT = 0xFA
CHUNK_ANIMATION = 0x5C
CHUNK_PEAK = 0x36
CHUNK_MID = 0x68
CHUNK_GESTURE = 0x2B
CHUNK_GRANULAR_GRAINS = 0x5A
CHUNK_GRANULAR_LOOPS = 0xD9
CHUNK_MARKERS = 0xBD
CHUNK_STREAM_FORMAT = 0x48
CHUNK_SEEK_TABLE = 0xA3

CHUNK_NAMES = {
    CHUNK_DATA: "data",
    CHUNK_FORMAT: "format",
    CHUNK_ANIMATION: "animation",
    CHUNK_PEAK: "peak",
    CHUNK_MID: "mid",
    CHUNK_GESTURE: "gesture",
    CHUNK_GRANULAR_GRAINS: "granulargrains",
    CHUNK_GRANULAR_LOOPS: "granularloops",
    CHUNK_MARKERS: "markers",
    CHUNK_STREAM_FORMAT: "streamformat",
    CHUNK_SEEK_TABLE: "seektable",
}

CODEC_PCM = 0
CODEC_ADPCM = 4

# Таблицы IMA ADPCM (стандарт Rockstar / IMA)
IMA_INDEX_TABLE = [
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
]

IMA_STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
    19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
    50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
    130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
    2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
    5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]


def _clip(val: int, lo: int, hi: int) -> int:
    return lo if val < lo else (hi if val > hi else val)


def decode_adpcm(data: bytes, sample_count: int) -> bytes:
    """
    Декодирует 2048-байтные блоки Rockstar IMA ADPCM в 16-bit signed PCM.
    Исправляет баг CodeWalker: возвращает РОВНО sample_count * 2 байт без мусорного хвоста в конце.
    """
    out = bytearray(sample_count * 2)
    predictor = 0
    step_index = 0
    read_off = 0
    write_off = 0
    bytes_in_block = 0
    remaining = sample_count
    data_len = len(data)

    while read_off < data_len and remaining > 0:
        if bytes_in_block == 0:
            if read_off + 4 > data_len:
                break
            step_index = _clip(data[read_off], 0, 88)
            predictor = struct.unpack_from("<h", data, read_off + 2)[0]
            bytes_in_block = 2044
            read_off += 4
        else:
            b = data[read_off]
            read_off += 1
            bytes_in_block -= 1

            for nibble in (b & 0x0F, (b >> 4) & 0x0F):
                if remaining <= 0:
                    break
                step = IMA_STEP_TABLE[step_index]
                diff = (((nibble & 7) << 1) + 1) * step >> 3
                if nibble & 8:
                    diff = -diff
                predictor = _clip(predictor + diff, -32768, 32767)
                step_index = _clip(step_index + IMA_INDEX_TABLE[nibble], 0, 88)
                struct.pack_into("<h", out, write_off, predictor)
                write_off += 2
                remaining -= 1

    return bytes(out[:write_off])


def encode_adpcm(pcm_data: bytes, pad_to_2048: bool = False) -> Tuple[bytes, int]:
    """
    Кодирует 16-bit signed mono PCM в Rockstar IMA ADPCM (блоки по 2048 байт = 4 байта заголовок + 2044 байт данных = 4088 сэмплов).
    Исправляет баги CodeWalker:
      1. Точный расчёт числа сэмплов: len(pcm_data) // 2.
      2. Корректное выделение памяти с учётом 4-байтных заголовков каждого блока по 4088 сэмплов.
      3. Выравнивание размера до кратности 4 байтам (для SingleChannel) или 2048 байтам (для MultiChannel).
    Возвращает (adpcm_bytes, exact_sample_count).
    """
    if len(pcm_data) % 2 != 0:
        pcm_data = pcm_data[:-1]

    total_samples = len(pcm_data) // 2
    if total_samples == 0:
        return (b"\x00" * (2048 if pad_to_2048 else 4), 0)

    # Для корректного кодирования попарных нибблов дополним нечётный сэмпл тишиной/повтором
    samples = list(struct.unpack(f"<{total_samples}h", pcm_data))
    if len(samples) % 2 != 0:
        samples.append(samples[-1])

    out = bytearray()
    predictor = 0
    step_index = 0
    idx = 0
    n_samples = len(samples)

    while idx < n_samples:
        # Начало 2048-байтного ADPCM фрейма (вмещает до 4088 сэмплов = 2044 байт нибблов)
        out.extend(struct.pack("<hh", step_index, predictor))
        frame_pairs = min(2044, (n_samples - idx) // 2)
        for _ in range(frame_pairs):
            s0 = samples[idx]
            s1 = samples[idx + 1]
            idx += 2

            nibbles = []
            for s in (s0, s1):
                delta = s - predictor
                val = 8 if delta < 0 else 0
                if delta < 0:
                    delta = -delta
                step = IMA_STEP_TABLE[step_index]
                diff = step >> 3
                if delta > step:
                    val |= 4
                    delta -= step
                    diff += step
                step >>= 1
                if delta > step:
                    val |= 2
                    delta -= step
                    diff += step
                step >>= 1
                if delta > step:
                    val |= 1
                    diff += step

                predictor += -diff if (val & 8) else diff
                predictor = _clip(predictor, -32768, 32767)
                step_index = _clip(step_index + IMA_INDEX_TABLE[val & 7], 0, 88)
                nibbles.append(val & 0x0F)

            out.append(nibbles[0] | (nibbles[1] << 4))

    if pad_to_2048:
        rem = len(out) % 2048
        if rem != 0:
            out.extend(b"\x00" * (2048 - rem))
    else:
        rem = len(out) % 4
        if rem != 0:
            out.extend(b"\x00" * (4 - rem))

    return bytes(out), total_samples


def compute_peaks(pcm_data: bytes, sample_count: int) -> Tuple[int, List[int]]:
    """
    Вычисляет PeakVal (первые 4096 сэмплов) и массив PeakChunk (каждые следующие 4096 сэмплов),
    как в AwcFile.BuildPeakChunks().
    """
    peak_size = 4096
    n_avail = min(sample_count, len(pcm_data) // 2)
    if n_avail == 0:
        return 0, []

    samples = struct.unpack_from(f"<{n_avail}h", pcm_data, 0)

    def window_peak(start_idx: int) -> int:
        end_idx = min(start_idx + peak_size, n_avail)
        if start_idx >= end_idx:
            return 0
        max_abs = max(abs(s) for s in samples[start_idx:end_idx])
        return min(max_abs * 2, 65535)

    peak0 = window_peak(0)
    peak_count = max(0, (sample_count - peak_size) // peak_size)
    extra_peaks = [window_peak((n + 1) * peak_size) for n in range(peak_count)]
    return peak0, extra_peaks


def write_wav_mono16(path: Path, pcm_data: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


def read_wav_mono16(path: Path) -> Tuple[bytes, int, int]:
    """
    Читает .wav файл (безопасно пропуская любые LIST/fact/JUNK чанки через стандартный модуль wave).
    Возвращает (pcm_bytes, sample_rate, sample_count).
    """
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)

    if channels != 1:
        raise ValueError(f"{path}: ожидается моно WAV (1 канал), получено каналов: {channels}")
    if sampwidth != 2:
        raise ValueError(f"{path}: ожидается 16-bit PCM WAV (sampwidth=2), получено: {sampwidth}")

    return raw, sample_rate, len(raw) // 2


@dataclass
class RawChunk:
    chunk_type: int
    offset: int
    size: int
    data: bytes
    orig_offset: int = 0

    @property
    def sort_order(self) -> int:
        if self.chunk_type in (CHUNK_DATA, CHUNK_MID):
            return 3
        if self.chunk_type in (
            CHUNK_MARKERS,
            CHUNK_GRANULAR_GRAINS,
            CHUNK_GRANULAR_LOOPS,
            CHUNK_ANIMATION,
            CHUNK_GESTURE,
        ):
            return 2
        if self.chunk_type == CHUNK_SEEK_TABLE:
            return 1
        return 0

    @property
    def align(self) -> int:
        if self.chunk_type in (CHUNK_DATA, CHUNK_MID):
            return 16
        if self.chunk_type == CHUNK_ANIMATION:
            # В оригинальных AWC чанки RSC7 (animation) выравниваются по 16 или 4 байтам
            if self.orig_offset > 0 and (self.orig_offset % 16 == 0):
                return 16
            return 4
        if self.chunk_type in (
            CHUNK_MARKERS,
            CHUNK_GRANULAR_GRAINS,
            CHUNK_GRANULAR_LOOPS,
        ):
            return 4
        return 0


@dataclass
class StreamEntry:
    stream_id: int
    chunks: List[RawChunk] = field(default_factory=list)

    def get_chunk(self, ctype: int) -> Optional[RawChunk]:
        for c in self.chunks:
            if c.chunk_type == ctype:
                return c
        return None


class AwcContainer:
    """Представление бинарного .awc контейнера с поддержкой Single-Channel и Multi-Channel."""

    def __init__(self, raw_bytes: bytes):
        if len(raw_bytes) < 16:
            raise ValueError("Файл слишком мал для .awc")

        magic, version, flags, stream_count, data_offset = struct.unpack_from("<IHHii", raw_bytes, 0)
        if magic != AWC_MAGIC_LE:
            raise ValueError(
                f"Неверный Magic 0x{magic:08X} (ожидался 0x{AWC_MAGIC_LE:08X} 'ADAT'). "
                "Если файл зашифрован на уровне .rpf, извлеките его через CodeWalker / gta-patch."
            )

        self.magic = magic
        self.version = version
        self.flags = flags
        self.stream_count = stream_count
        self.data_offset = data_offset

        pos = 16
        self.chunk_indices: List[int] = []
        if self.chunk_indices_flag:
            self.chunk_indices = list(struct.unpack_from(f"<{stream_count}H", raw_bytes, pos))
            pos += stream_count * 2

        stream_raw_vals = struct.unpack_from(f"<{stream_count}I", raw_bytes, pos)
        pos += stream_count * 4

        self.streams: List[StreamEntry] = []
        chunk_counts: List[int] = []
        for rv in stream_raw_vals:
            c_count = (rv >> 29) & 0x7
            s_id = rv & 0x1FFFFFFF
            self.streams.append(StreamEntry(stream_id=s_id))
            chunk_counts.append(c_count)

        for s_idx, c_count in enumerate(chunk_counts):
            for _ in range(c_count):
                raw_val = struct.unpack_from("<Q", raw_bytes, pos)[0]
                pos += 8
                ctype = (raw_val >> 56) & 0xFF
                csize = (raw_val >> 28) & 0x0FFFFFFF
                coffset = raw_val & 0x0FFFFFFF
                cdata = raw_bytes[coffset : coffset + csize]
                self.streams[s_idx].chunks.append(
                    RawChunk(
                        chunk_type=ctype,
                        offset=coffset,
                        size=csize,
                        data=cdata,
                        orig_offset=coffset,
                    )
                )

    @property
    def chunk_indices_flag(self) -> bool:
        return (self.flags & 1) == 1

    @property
    def single_channel_encrypt_flag(self) -> bool:
        return (self.flags & 2) == 2

    @property
    def multi_channel_flag(self) -> bool:
        return (self.flags & 4) == 4

    @property
    def multi_channel_encrypt_flag(self) -> bool:
        return (self.flags & 8) == 8

    def summary(self) -> dict:
        info = {
            "version": self.version,
            "flags": f"0x{self.flags:04X}",
            "multi_channel": self.multi_channel_flag,
            "single_channel_encrypted": self.single_channel_encrypt_flag,
            "multi_channel_encrypted": self.multi_channel_encrypt_flag,
            "stream_count": len(self.streams),
            "streams": [],
        }

        if not self.multi_channel_flag:
            for s in self.streams:
                fmt = s.get_chunk(CHUNK_FORMAT)
                s_info = {
                    "id_hex": f"0x{s.stream_id:08X}",
                    "chunks": [CHUNK_NAMES.get(c.chunk_type, f"0x{c.chunk_type:02X}") for c in s.chunks],
                }
                if fmt and len(fmt.data) >= 20:
                    samples, loop_pt, sr, headroom, lb, le, pe, pb, codec = struct.unpack_from(
                        "<IiHhHHHBB", fmt.data, 0
                    )
                    s_info.update(
                        {
                            "samples": samples,
                            "sample_rate": sr,
                            "duration_sec": round(samples / sr, 3) if sr else 0,
                            "codec": "ADPCM" if codec == CODEC_ADPCM else ("PCM" if codec == CODEC_PCM else codec),
                            "headroom": headroom,
                        }
                    )
                info["streams"].append(s_info)
        else:
            mc_src = next((s for s in self.streams if s.stream_id == 0), None)
            chan_map = {}
            if mc_src:
                sf = mc_src.get_chunk(CHUNK_STREAM_FORMAT)
                if sf and len(sf.data) >= 12:
                    block_count, block_size, chan_count = struct.unpack_from("<III", sf.data, 0)
                    info["mc_block_count"] = block_count
                    info["mc_block_size"] = block_size
                    info["mc_channel_count"] = chan_count
                    for i in range(chan_count):
                        cid, samples, headroom, sr, codec, u1, u2 = struct.unpack_from(
                            "<IIhHBBH", sf.data, 12 + i * 16
                        )
                        chan_map[cid] = {
                            "channel_index": i,
                            "samples": samples,
                            "sample_rate": sr,
                            "duration_sec": round(samples / sr, 3) if sr else 0,
                            "codec": "ADPCM" if codec == CODEC_ADPCM else ("PCM" if codec == CODEC_PCM else codec),
                            "headroom": headroom,
                        }
            for s in self.streams:
                s_info = {
                    "id_hex": f"0x{s.stream_id:08X}",
                    "chunks": [CHUNK_NAMES.get(c.chunk_type, f"0x{c.chunk_type:02X}") for c in s.chunks],
                }
                if s.stream_id in chan_map:
                    s_info.update(chan_map[s.stream_id])
                info["streams"].append(s_info)

        return info

    def unpack_to_dir(self, out_dir: Path) -> dict:
        out_dir.mkdir(parents=True, exist_ok=True)
        manifest = self.summary()
        manifest["exported_wavs"] = {}

        if not self.multi_channel_flag:
            for s in self.streams:
                fmt = s.get_chunk(CHUNK_FORMAT)
                dat = s.get_chunk(CHUNK_DATA)
                if not fmt or not dat:
                    continue
                samples, loop_pt, sr, headroom, lb, le, pe, pb, codec = struct.unpack_from(
                    "<IiHhHHHBB", fmt.data, 0
                )
                if codec == CODEC_ADPCM:
                    pcm = decode_adpcm(dat.data, samples)
                elif codec == CODEC_PCM:
                    pcm = dat.data[: samples * 2]
                else:
                    raise ValueError(f"Неподдерживаемый кодек {codec} в потоке 0x{s.stream_id:08X}")

                wav_name = f"0x{s.stream_id:08X}.wav"
                write_wav_mono16(out_dir / wav_name, pcm, sr)
                manifest["exported_wavs"][f"0x{s.stream_id:08X}"] = wav_name
        else:
            mc_src = next((s for s in self.streams if s.stream_id == 0), None)
            if not mc_src:
                raise ValueError("MultiChannel AWC не содержит источника (stream_id == 0)")
            sf = mc_src.get_chunk(CHUNK_STREAM_FORMAT)
            dat = mc_src.get_chunk(CHUNK_DATA)
            if not sf or not dat:
                raise ValueError("MultiChannel источник не содержит streamformat или data")

            block_count, block_size, chan_count = struct.unpack_from("<III", sf.data, 0)
            channels_meta = []
            for i in range(chan_count):
                cid, samples, headroom, sr, codec, u1, u2 = struct.unpack_from(
                    "<IIhHBBH", sf.data, 12 + i * 16
                )
                channels_meta.append((cid, samples, headroom, sr, codec, u1, u2))

            chan_raw_Streams: List[bytearray] = [bytearray() for _ in range(chan_count)]
            for b in range(block_count):
                b_start = b * block_size
                b_data = dat.data[b_start : b_start + block_size]
                if not b_data:
                    break
                r_pos = 0
                chan_headers = []
                for _ in range(chan_count):
                    start_blk, blk_cnt, u1, smp_cnt, u2, u3 = struct.unpack_from("<iiiiii", b_data, r_pos)
                    r_pos += 24
                    chan_headers.append((start_blk, blk_cnt, u1, smp_cnt, u2, u3))
                for c_idx in range(chan_count):
                    blk_cnt = chan_headers[c_idx][1]
                    r_pos += blk_cnt * 4  # SampleOffsets
                padc = (0x800 - (r_pos % 0x800)) % 0x800
                r_pos += padc
                for c_idx in range(chan_count):
                    blk_cnt = chan_headers[c_idx][1]
                    byte_cnt = blk_cnt * 2048
                    chan_raw_Streams[c_idx].extend(b_data[r_pos : r_pos + byte_cnt])
                    r_pos += byte_cnt

            for c_idx, (cid, samples, headroom, sr, codec, u1, u2) in enumerate(channels_meta):
                raw_chan = bytes(chan_raw_Streams[c_idx])
                if codec == CODEC_ADPCM:
                    pcm = decode_adpcm(raw_chan, samples)
                elif codec == CODEC_PCM:
                    pcm = raw_chan[: samples * 2]
                else:
                    raise ValueError(f"Неподдерживаемый кодек {codec} в канале 0x{cid:08X}")

                wav_name = f"0x{cid:08X}.wav"
                write_wav_mono16(out_dir / wav_name, pcm, sr)
                manifest["exported_wavs"][f"0x{cid:08X}"] = wav_name

        with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        return manifest

    def repack_from_dir(self, wav_dir: Path) -> bytes:
        """
        Заменяет аудиоданные из .wav файлов в wav_dir, сохраняя все остальные чанки
        (animation, gesture, markers) строго побайтово.
        """
        if not self.multi_channel_flag:
            for s in self.streams:
                wav_path = wav_dir / f"0x{s.stream_id:08X}.wav"
                if not wav_path.exists():
                    continue
                fmt = s.get_chunk(CHUNK_FORMAT)
                dat = s.get_chunk(CHUNK_DATA)
                if not fmt or not dat:
                    continue

                pcm, new_sr, new_samples = read_wav_mono16(wav_path)
                samples, loop_pt, sr, headroom, lb, le, pe, pb, codec = struct.unpack_from(
                    "<IiHhHHHBB", fmt.data, 0
                )

                if codec == CODEC_ADPCM:
                    encoded, exact_samples = encode_adpcm(pcm, pad_to_2048=False)
                elif codec == CODEC_PCM:
                    if len(pcm) % 4 != 0:
                        pcm += b"\x00" * (4 - (len(pcm) % 4))
                    encoded = pcm
                    exact_samples = new_samples
                else:
                    raise ValueError(f"Неподдерживаемый кодек {codec}")

                dat.data = encoded
                dat.size = len(encoded)

                peak0, extra_peaks = compute_peaks(pcm, exact_samples)
                if len(fmt.data) == 24:
                    old_peak_u32 = struct.unpack_from("<I", fmt.data, 20)[0]
                    peak_unk = (old_peak_u32 >> 16) & 0xFFFF
                    new_peak_u32 = (peak_unk << 16) | (peak0 & 0xFFFF)
                    fmt.data = struct.pack(
                        "<IiHhHHHBB",
                        exact_samples,
                        loop_pt,
                        new_sr,
                        headroom,
                        lb,
                        le,
                        pe,
                        pb,
                        codec,
                    ) + struct.pack("<I", new_peak_u32)
                else:
                    fmt.data = struct.pack(
                        "<IiHhHHHBB",
                        exact_samples,
                        loop_pt,
                        new_sr,
                        headroom,
                        lb,
                        le,
                        pe,
                        pb,
                        codec,
                    )

                peak_chunk = s.get_chunk(CHUNK_PEAK)
                if peak_chunk is not None:
                    if extra_peaks:
                        peak_chunk.data = struct.pack(f"<{len(extra_peaks)}H", *extra_peaks)
                        peak_chunk.size = len(peak_chunk.data)
                    else:
                        peak_chunk.data = b""
                        peak_chunk.size = 0
        else:
            mc_src = next((s for s in self.streams if s.stream_id == 0), None)
            if not mc_src:
                raise ValueError("MultiChannel AWC не содержит источника (stream_id == 0)")
            sf = mc_src.get_chunk(CHUNK_STREAM_FORMAT)
            dat = mc_src.get_chunk(CHUNK_DATA)
            block_count, block_size, chan_count = struct.unpack_from("<III", sf.data, 0)

            # Сначала распакуем существующие каналы, чтобы те, для которых нет нового WAV, остались прежними
            channels_meta = []
            for i in range(chan_count):
                cid, samples, headroom, sr, codec, u1, u2 = struct.unpack_from(
                    "<IIhHBBH", sf.data, 12 + i * 16
                )
                channels_meta.append([cid, samples, headroom, sr, codec, u1, u2])

            chan_raw_streams: List[bytearray] = [bytearray() for _ in range(chan_count)]
            for b in range(block_count):
                b_start = b * block_size
                b_data = dat.data[b_start : b_start + block_size]
                if not b_data:
                    break
                r_pos = 0
                chan_headers = []
                for _ in range(chan_count):
                    chan_headers.append(struct.unpack_from("<iiiiii", b_data, r_pos))
                    r_pos += 24
                for c_idx in range(chan_count):
                    r_pos += chan_headers[c_idx][1] * 4
                r_pos += (0x800 - (r_pos % 0x800)) % 0x800
                for c_idx in range(chan_count):
                    byte_cnt = chan_headers[c_idx][1] * 2048
                    chan_raw_streams[c_idx].extend(b_data[r_pos : r_pos + byte_cnt])
                    r_pos += byte_cnt

            encoded_channels: List[bytes] = []
            for c_idx, meta in enumerate(channels_meta):
                cid, samples, headroom, sr, codec, u1, u2 = meta
                wav_path = wav_dir / f"0x{cid:08X}.wav"
                if wav_path.exists():
                    pcm, new_sr, new_samples = read_wav_mono16(wav_path)
                    if codec == CODEC_ADPCM:
                        enc, exact_samples = encode_adpcm(pcm, pad_to_2048=True)
                    elif codec == CODEC_PCM:
                        rem = len(pcm) % 2048
                        enc = pcm + (b"\x00" * ((2048 - rem) % 2048))
                        exact_samples = new_samples
                    else:
                        raise ValueError(f"Неподдерживаемый кодек {codec}")
                    meta[1] = exact_samples
                    meta[3] = new_sr
                    encoded_channels.append(enc)
                else:
                    raw_c = bytes(chan_raw_streams[c_idx])
                    rem = len(raw_c) % 2048
                    if rem != 0:
                        raw_c += b"\x00" * (2048 - rem)
                    encoded_channels.append(raw_c)

            # Пересборка MultiChannel блоков по алгоритму CompactMultiChannelSources
            hdr_size = 96 * chan_count + (block_size // 512) + 1024
            hdr_size += (0x800 - (hdr_size % 0x800)) % 0x800
            sm_block_space = (block_size - hdr_size) // 2048
            sm_block_count = sm_block_space // chan_count

            tot_lg_blocks = 0
            for c_idx in range(chan_count):
                cdlen = len(encoded_channels[c_idx])
                tot_sm = (cdlen // 2048) + (1 if (cdlen % 2048) != 0 else 0)
                lg = (tot_sm // sm_block_count) + (1 if (tot_sm % sm_block_count) != 0 else 0)
                tot_lg_blocks = max(tot_lg_blocks, lg)

            tot_lg_blocks = max(1, tot_lg_blocks)
            seek_table = [i * sm_block_count * 4088 for i in range(tot_lg_blocks)]

            # Матрица блоков [lg_block][chan]
            blocks_matrix = [
                [
                    {
                        "start_block": c * sm_block_count,
                        "block_count": 0,
                        "sample_count": 0,
                        "offsets": [],
                        "data": b"\x00" * 0,
                    }
                    for c in range(chan_count)
                ]
                for _ in range(tot_lg_blocks)
            ]

            for c_idx in range(chan_count):
                chandata = encoded_channels[c_idx]
                cdlen = len(chandata)
                tot_sm = (cdlen // 2048) + (1 if (cdlen % 2048) != 0 else 0)
                samples_rem = channels_meta[c_idx][1] + 1
                chansmpoffs: List[int] = []

                for i in range(tot_sm):
                    chansmpoffs.append(i * 4088)
                    blkcnt = len(chansmpoffs)
                    if blkcnt == sm_block_count or i == tot_sm - 1:
                        lg_ind = i // sm_block_count
                        blk_start = lg_ind * sm_block_count
                        smpcnt = blkcnt * 4088
                        bytcnt = blkcnt * 2048
                        srcoff = blk_start * 2048
                        srccnt = max(0, min(bytcnt, cdlen - srcoff))
                        cdata = bytearray(bytcnt)
                        cdata[:srccnt] = chandata[srcoff : srcoff + srccnt]

                        blocks_matrix[lg_ind][c_idx] = {
                            "start_block": c_idx * sm_block_count,
                            "block_count": blkcnt,
                            "sample_count": max(0, min(smpcnt, samples_rem)),
                            "offsets": list(chansmpoffs),
                            "data": bytes(cdata),
                        }
                        chansmpoffs.clear()
                        samples_rem -= smpcnt

            out_blocks = bytearray()
            for lg_ind in range(tot_lg_blocks):
                blk_buf = bytearray()
                for c_idx in range(chan_count):
                    ch = blocks_matrix[lg_ind][c_idx]
                    blk_buf.extend(
                        struct.pack(
                            "<iiiiii",
                            ch["start_block"],
                            ch["block_count"],
                            0,
                            ch["sample_count"],
                            0,
                            0,
                        )
                    )
                for c_idx in range(chan_count):
                    ch = blocks_matrix[lg_ind][c_idx]
                    for off_val in ch["offsets"]:
                        blk_buf.extend(struct.pack("<i", off_val))
                padc = (0x800 - (len(blk_buf) % 0x800)) % 0x800
                if padc > 0:
                    blk_buf.extend(b"\x00" * padc)
                for c_idx in range(chan_count):
                    blk_buf.extend(blocks_matrix[lg_ind][c_idx]["data"])

                if len(blk_buf) < block_size:
                    blk_buf.extend(b"\x00" * (block_size - len(blk_buf)))
                out_blocks.extend(blk_buf)

            dat.data = bytes(out_blocks)
            dat.size = len(dat.data)

            # Обновляем streamformat и seektable во всех потоках, где они присутствуют
            sf_buf = bytearray(struct.pack("<III", tot_lg_blocks, block_size, chan_count))
            for meta in channels_meta:
                sf_buf.extend(struct.pack("<IIhHBBH", *meta))
            new_sf_bytes = bytes(sf_buf)
            new_seek_bytes = struct.pack(f"<{len(seek_table)}I", *seek_table)

            for s in self.streams:
                s_sf = s.get_chunk(CHUNK_STREAM_FORMAT)
                if s_sf is not None:
                    s_sf.data = new_sf_bytes
                    s_sf.size = len(new_sf_bytes)
                s_st = s.get_chunk(CHUNK_SEEK_TABLE)
                if s_st is not None:
                    s_st.data = new_seek_bytes
                    s_st.size = len(new_seek_bytes)

        return self.to_bytes()

    def to_bytes(self) -> bytes:
        stream_count = len(self.streams)
        info_start = 16 + (stream_count * 2 if self.chunk_indices_flag else 0)
        total_chunks = sum(len(s.chunks) for s in self.streams)
        data_offset = info_start + stream_count * 4 + total_chunks * 8

        # Собираем все чанки в порядке записи
        all_chunks: List[RawChunk] = []
        for s in self.streams:
            for c in s.chunks:
                c.size = len(c.data)
                all_chunks.append(c)

        is_sorted = self.multi_channel_flag or (not self.single_channel_encrypt_flag)
        if is_sorted:
            # Если у всех чанков есть исходный физический порядок из оригинального AWC, сохраняем его
            if all(c.orig_offset > 0 for c in all_chunks):
                all_chunks.sort(key=lambda c: c.orig_offset)
            else:
                all_chunks.sort(key=lambda c: c.sort_order)

        cur_offset = data_offset
        for c in all_chunks:
            if c.align > 0:
                padc = (c.align - (cur_offset % c.align)) % c.align
                cur_offset += padc
            c.offset = cur_offset
            cur_offset += c.size

        out = bytearray()
        out.extend(
            struct.pack(
                "<IHHii",
                self.magic,
                self.version,
                self.flags,
                stream_count,
                data_offset,
            )
        )

        if self.chunk_indices_flag:
            idx_val = 0
            for s in self.streams:
                out.extend(struct.pack("<H", idx_val))
                idx_val += len(s.chunks)

        for s in self.streams:
            raw_val = (s.stream_id & 0x1FFFFFFF) | ((len(s.chunks) & 0x7) << 29)
            out.extend(struct.pack("<I", raw_val))

        for s in self.streams:
            for c in s.chunks:
                raw_val = (
                    (c.offset & 0x0FFFFFFF)
                    | ((c.size & 0x0FFFFFFF) << 28)
                    | ((c.chunk_type & 0xFF) << 56)
                )
                out.extend(struct.pack("<Q", raw_val))

        for c in all_chunks:
            if c.align > 0:
                padc = (c.align - (len(out) % c.align)) % c.align
                if padc > 0:
                    out.extend(b"\x00" * padc)
            assert len(out) == c.offset, f"Offset mismatch: {len(out)} != {c.offset}"
            out.extend(c.data)

        return bytes(out)


def _generate_test_sine_pcm(freq: float, duration_sec: float, sr: int = 32000) -> bytes:
    n_samples = int(sr * duration_sec)
    samples = [
        int(18000 * math.sin(2.0 * math.pi * freq * i / sr))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def run_selftest() -> None:
    print("=== Запуск самопроверки gta5_awc_tool.py ===")

    # 1. Проверка кодека IMA ADPCM (отсутствие щелчка на хвосте + точность длины)
    sr = 32000
    orig_pcm = _generate_test_sine_pcm(440.0, 1.37, sr=sr)  # 43840 сэмплов (не кратно 4088!)
    orig_samples = len(orig_pcm) // 2
    adpcm_bytes, encoded_samples = encode_adpcm(orig_pcm, pad_to_2048=False)
    decoded_pcm = decode_adpcm(adpcm_bytes, encoded_samples)

    assert encoded_samples == orig_samples, f"{encoded_samples} != {orig_samples}"
    assert len(decoded_pcm) == len(orig_pcm), (
        f"Длина PCM после декодирования {len(decoded_pcm)} != {len(orig_pcm)} (щелчок на конце устранён!)"
    )

    s_orig = struct.unpack(f"<{orig_samples}h", orig_pcm)
    s_dec = struct.unpack(f"<{orig_samples}h", decoded_pcm)
    mse = sum((a - b) ** 2 for a, b in zip(s_orig, s_dec)) / orig_samples
    sig_pow = sum(a ** 2 for a in s_orig) / orig_samples
    snr_db = 10.0 * math.log10(sig_pow / max(mse, 1.0))
    print(f"[OK] Кодек IMA ADPCM: {orig_samples} сэмплов, блок-выравнивание 4 байта, SNR = {snr_db:.2f} dB")

    # 2. Проверка Single-Channel AWC с липсинком (animation 0x5C) и жестами (gesture 0x2B)
    mock_anim = b"RSC7\x2e\x00\x00\x00MOCK_LIPSYNC_YCD_BINARY_PAYLOAD_12345678"
    mock_gest = b"MOCK_GESTURE_CHUNK_36BYTES_123456789"
    peak0, extra_peaks = compute_peaks(orig_pcm, orig_samples)
    fmt_bytes = struct.pack(
        "<IiHhHHHBB", orig_samples, -1, sr, -187, 0, 0, 0, 0, CODEC_ADPCM
    ) + struct.pack("<I", peak0)
    peak_bytes = struct.pack(f"<{len(extra_peaks)}H", *extra_peaks)

    # Создаём тестовый контейнер с двумя репликами
    s1 = StreamEntry(
        stream_id=0x0A1B2C3D,
        chunks=[
            RawChunk(CHUNK_FORMAT, 0, len(fmt_bytes), fmt_bytes),
            RawChunk(CHUNK_PEAK, 0, len(peak_bytes), peak_bytes),
            RawChunk(CHUNK_ANIMATION, 0, len(mock_anim), mock_anim),
            RawChunk(CHUNK_GESTURE, 0, len(mock_gest), mock_gest),
            RawChunk(CHUNK_DATA, 0, len(adpcm_bytes), adpcm_bytes),
        ],
    )
    # Искусственно соберём контейнер
    dummy_header = struct.pack("<IHHii", AWC_MAGIC_LE, 1, 0xFF01, 0, 16)
    awc = AwcContainer(dummy_header)
    awc.streams = [s1]
    raw_single_awc = awc.to_bytes()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        awc_loaded = AwcContainer(raw_single_awc)
        awc_loaded.unpack_to_dir(tmp / "unpacked")

        # Заменим WAV репликой ДРУГОЙ длительности (например, 1.85 сек вместо 1.37 сек)
        new_pcm = _generate_test_sine_pcm(523.25, 1.85, sr=sr)
        write_wav_mono16(tmp / "unpacked" / "0x0A1B2C3D.wav", new_pcm, sr)

        repacked_bytes = awc_loaded.repack_from_dir(tmp / "unpacked")
        awc_repacked = AwcContainer(repacked_bytes)
        info = awc_repacked.summary()
        repacked_stream = awc_repacked.streams[0]

        assert info["streams"][0]["samples"] == len(new_pcm) // 2
        assert repacked_stream.get_chunk(CHUNK_ANIMATION).data == mock_anim, "Липсинк (animation) повреждён!"
        assert repacked_stream.get_chunk(CHUNK_GESTURE).data == mock_gest, "Жесты (gesture) повреждены!"
        print(
            f"[OK] Single-Channel AWC round-trip: сэмплы обновлены ({orig_samples} -> {info['streams'][0]['samples']}), "
            "бинарные чанки animation (липсинк) и gesture сохранены байт-в-байт!"
        )

        # 3. Проверка Multi-Channel AWC (диалоги миссий / катсцены с общим потоком id=0 и 2 каналами)
        mc_header = struct.pack("<IHHii", AWC_MAGIC_LE, 1, 0xFF05, 0, 16)  # Flags: ChunkIndices(1) | MultiChannel(4)
        mc_awc = AwcContainer(mc_header)
        block_size = 524288
        sf_init = struct.pack("<III", 1, block_size, 2) + (
            struct.pack("<IIhHBBH", 0x11111111, orig_samples, -187, sr, CODEC_ADPCM, 0, 0)
            + struct.pack("<IIhHBBH", 0x22222222, orig_samples, -187, sr, CODEC_ADPCM, 0, 0)
        )
        mc_src = StreamEntry(
            stream_id=0,
            chunks=[
                RawChunk(CHUNK_STREAM_FORMAT, 0, len(sf_init), sf_init),
                RawChunk(CHUNK_SEEK_TABLE, 0, 4, struct.pack("<I", 0)),
                RawChunk(CHUNK_DATA, 0, block_size, b"\x00" * block_size),
            ],
        )
        ch1_stream = StreamEntry(
            stream_id=0x11111111,
            chunks=[RawChunk(CHUNK_ANIMATION, 0, len(mock_anim), mock_anim)],
        )
        ch2_stream = StreamEntry(
            stream_id=0x22222222,
            chunks=[RawChunk(CHUNK_GESTURE, 0, len(mock_gest), mock_gest)],
        )
        mc_awc.streams = [mc_src, ch1_stream, ch2_stream]

        mc_wav_dir = tmp / "mc_wavs"
        write_wav_mono16(mc_wav_dir / "0x11111111.wav", orig_pcm, sr)
        write_wav_mono16(mc_wav_dir / "0x22222222.wav", new_pcm, sr)

        mc_packed_bytes = mc_awc.repack_from_dir(mc_wav_dir)
        mc_reloaded = AwcContainer(mc_packed_bytes)
        mc_unpacked_dir = tmp / "mc_unpacked"
        mc_manifest = mc_reloaded.unpack_to_dir(mc_unpacked_dir)

        u1_pcm, u1_sr, u1_smp = read_wav_mono16(mc_unpacked_dir / "0x11111111.wav")
        u2_pcm, u2_sr, u2_smp = read_wav_mono16(mc_unpacked_dir / "0x22222222.wav")
        assert u1_smp == orig_samples, f"{u1_smp} != {orig_samples}"
        assert u2_smp == len(new_pcm) // 2, f"{u2_smp} != {len(new_pcm) // 2}"
        assert mc_reloaded.streams[1].get_chunk(CHUNK_ANIMATION).data == mock_anim
        assert mc_reloaded.streams[2].get_chunk(CHUNK_GESTURE).data == mock_gest
        print(
            f"[OK] Multi-Channel AWC round-trip: 2 канала ({u1_smp} и {u2_smp} сэмплов) упакованы и распакованы "
            "с сохранением чанков animation/gesture на дочерних потоках!"
        )

    print("=== Все тесты пройдены успешно! ===")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "selftest":
        run_selftest()
    elif cmd == "info" and len(sys.argv) >= 3:
        awc = AwcContainer(Path(sys.argv[2]).read_bytes())
        print(json.dumps(awc.summary(), ensure_ascii=False, indent=2))
    elif cmd == "unpack" and len(sys.argv) >= 4:
        awc = AwcContainer(Path(sys.argv[2]).read_bytes())
        manifest = awc.unpack_to_dir(Path(sys.argv[3]))
        print(f"Распаковано потоков: {len(manifest['exported_wavs'])} -> {sys.argv[3]}")
    elif cmd == "repack" and len(sys.argv) >= 5:
        awc = AwcContainer(Path(sys.argv[2]).read_bytes())
        out_bytes = awc.repack_from_dir(Path(sys.argv[3]))
        Path(sys.argv[4]).write_bytes(out_bytes)
        print(f"Упакован новый AWC ({len(out_bytes)} байт) -> {sys.argv[4]}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
