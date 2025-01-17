# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/5A. T2S dataset preparation.ipynb.

# %% auto 0
__all__ = []

# %% ../nbs/5A. T2S dataset preparation.ipynb 2
import sys
import os
import itertools
from pathlib import Path

import numpy as np
import torch
import torchaudio
import torch.nn.functional as F
from torch.profiler import profile, record_function, ProfilerActivity

from fastprogress import progress_bar
from fastcore.script import *

import whisper, whisperx
from . import vad, wh_transcribe, vq_stoks, extract_acoustic
import webdataset as wds

# %% ../nbs/5A. T2S dataset preparation.ipynb 4
def flac_to_t2s_name(input):
    return input.rsplit("/", 1)[1].replace('flac', 't2s') + ".gz"

# %% ../nbs/5A. T2S dataset preparation.ipynb 6
class Transcriber:
    """
    A helper class to transcribe a batch of 30 second audio chunks.
    """
    def __init__(self, model_size, lang=False):
        self.model = whisperx.asr.load_model(model_size, "cuda", compute_type="float16", language=lang)
        # without calling vad_model at least once the rest segfaults for some reason...
        self.model.vad_model({"waveform": torch.zeros(1, 16000), "sample_rate": 16000})
        
    def transcribe(self, batch):
        batch = whisper.log_mel_spectrogram(batch)
        embs = self.model.model.encode(batch.cpu().numpy())
        return self.model.tokenizer.tokenizer.decode_batch([x.sequences_ids[0] for x in 
            self.model.model.model.generate(
                embs,
                [self.model.model.get_prompt(self.model.tokenizer, [], without_timestamps=True)]*len(batch),
            )])

# %% ../nbs/5A. T2S dataset preparation.ipynb 7
@call_parse
def prepare_t2s(
    input:str,  # FLAC webdataset file path (or - to read the names from stdin)
    proc_dataset_path:Path, # processed VAD files path
    output:str=None, # output file name
    vq_model:str="collabora/spear-tts-pytorch:whisper-vq-stoks.model", # the model path (use repo_id:filename to download it from hugginface)
    n_samples:int=None, # process a limited amount of samples
    batch_size:int=1, # process several segments at once
    transcription_model:str="small.en",
):
    if ":" in vq_model:
        repo, fname = vq_model.split(":", 1)
        vq_model = vq_stoks.RQBottleneckTransformer.load_model(repo, fname).cuda()
    else:
        vq_model = vq_stoks.RQBottleneckTransformer.load_model(local_filename=vq_model).cuda()
    transcriber = Transcriber(transcription_model)
        
    if input == "-":
        input = [f.strip() for f in sys.stdin.readlines()]
        assert output, "please provide the output shard name"
    else:
        if output is None: output = flac_to_t2s_name(input)
        input = [input]
        
    total = n_samples//batch_size if n_samples else 'noinfer'
    if n_samples: print(f"Benchmarking run of {n_samples} samples ({total} batches)")

    ds = wds.WebDataset(input, shardshuffle=True, rename_files=vad.fix_dots_in_names).compose(
        wds.decode(wds.torch_audio),
        vq_stoks.merge_in(vq_stoks.derived_dataset(proc_dataset_path, 'vad')),
        wds.map_dict(**{"vad.npy": lambda s: wh_transcribe.chunk_merger(s, wh_transcribe.random_cutter)}),
        lambda x: wh_transcribe.split_to_chunks(x),
        # drop the first and last segment because they tend to be inaccurate
        # (the transcriptions don't have the "LibriVox" header and "end of chapter" suffix)
        wds.select(lambda x: x['i'] != 0 and x['i'] != x['imax']),
        wds.to_tuple('__key__', 'rpad', 'samples'),
        wds.batched(64),
    )

    dl = wds.WebLoader(ds, num_workers=4, batch_size=None).unbatched().shuffle(2000).batched(batch_size)

    speakers = set()
    tmp = output+".tmp"
    with wds.TarWriter(tmp) as sink:
        for keys, rpads, samples in progress_bar(dl, total=total):
            with record_function('to_cuda'):
                csamples = samples.cuda()
            with record_function('transcribe'):
                txts = transcriber.transcribe(csamples)
            with record_function('vq_stoks'):
                stoks = vq_model.encode_audio(csamples)
            with record_function('from_cuda'):
                stoks = stoks.cpu().numpy().astype(np.int16)
            for key, rpad, txt, _stoks in zip(keys, rpads, txts, stoks):
                speakers.add(key.split('/')[1])
                sink.write({
                    "__key__": key,
                    "txt": txt,
                    "stoks.npy": _stoks[:int(-rpad/16000 * 25)],
                })
    with open(output+".speakers.txt", "w") as f: f.write("\n".join(speakers))
    if not n_samples:
        os.rename(tmp, output)
