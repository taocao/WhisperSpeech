# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb.

# %% auto 0
__all__ = ['languages', 'lang_to_id', 'load_dataset', 'CMLMVisual', 'DelSumEmbedding', 'DelSumHead', 'rand', 'Tunables',
           'SADelARTransformer']

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 1
import io
import time
import math
import random
import dataclasses

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 2
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.profiler import profile, record_function, ProfilerActivity, schedule
from fastcore.basics import store_attr
from huggingface_hub import hf_hub_download

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 3
from pathlib import Path
import json
from fastprogress import progress_bar, master_bar
import webdataset as wds

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 4
import whisper
from encodec.model import EncodecModel
from .train import *
from .modules import *
from . import vq_stoks, utils

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 8
def rand(start, end):
    return random.random() * (end - start) + start

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 9
def random_trunc(random_trunc_p, atoks_len = 2250, stoks_len = 750):
    atoks_per_second = atoks_len / 30
    def _trunc(samples):
        for s in samples:
            if random.random() < random_trunc_p:
                seconds = rand(0.3, 30)
                s['atoks.npy'] = s['atoks.npy'][:,:math.ceil(seconds * atoks_per_second)]
            s['stoks.npy'] = s['stoks.npy'][:math.ceil(s['atoks.npy'].shape[-1]/atoks_len*stoks_len)]
            yield s
    return _trunc

def pad_samples(atoks_len = 2250, stoks_len = 750, stoks_pad_token = 4096):
    def _pad(samples):
        for s in samples:
            s['stoks.npy'] = F.pad(torch.tensor(s['stoks.npy']), (1, stoks_len - s['stoks.npy'].shape[-1]-1), value=stoks_pad_token)
            s['out_stoks'] = F.pad(torch.tensor(s['stoks.npy']), (0, stoks_len - s['stoks.npy'].shape[-1]), value=stoks_pad_token)
            s['atoks.npy'] = F.pad(torch.tensor(s['atoks.npy']), (0, atoks_len - s['atoks.npy'].shape[-1]), value=-100)
            yield s
    return _pad

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 10
def make_speaker_map(shards):
    speakers = set()
    for shard in shards:
        with open(shard+'.speakers.txt') as f: speakers = speakers.union(set(x.strip() for x in f.readlines()))
    return {id:i for i,id in enumerate(sorted(speakers))}

def speaker_id_extractor(speaker_map):
    def _extractor(samples):
        for s in samples:
            s['speaker'] = torch.tensor(speaker_map[s['__key__'].split("/")[1]])
            yield s
    return _extractor

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 28
languages = tuple(whisper.tokenizer.LANGUAGES.keys())

def lang_to_id(lang):
    return languages.index(whisper.tokenizer.TO_LANGUAGE_CODE.get(lang, lang))

def load_dataset(
        atoks_shard_spec:str,             # webdataset folder
        stoks_shard_dir:str,   # stoks webdataset base dir
        samples:int,           # samples per epoch
        random_trunc_p:float=0,# probability of truncating the input to less than 30 seconds
        vq_codes:int=4096,
        language:str='en',
        weight:float=1,
        validation:bool=False,
        exclude_files:str=None,
        randomize_speakers:bool=False,
    ):
    shards = utils.shard_glob(atoks_shard_spec)
    excludes = {x for file in exclude_files.split() for x in utils.readlines(file)} if exclude_files else set()
    
    language = lang_to_id(language)

    def check_for_nan(s):
        if torch.tensor(s['spk_emb.npy']).isnan().any(): print("found NaN:", s['__key__'])
        return s
    
    def set_language(x):
        x['language'] = language
        return x
    
    same_on_all_nodes = lambda urls: urls # will only be used for validation
    ds = wds.WebDataset(shards, resampled=not validation, nodesplitter=same_on_all_nodes).compose(
        wds.decode(),
        utils.merge_in(utils.derived_dataset('maxvad-stoks', base='atoks-3kbps', suffix='', dir=stoks_shard_dir)),
        wds.map(check_for_nan),
        wds.select(lambda s: s['__key__'] not in excludes),
        wds.map_dict(**{'spk_emb.npy':np.nan_to_num}), # remove nans from the speaker embedding model
        random_trunc(random_trunc_p) if random_trunc_p > 0 else lambda x: x,
        pad_samples(stoks_pad_token=vq_codes-1),
        wds.map(set_language),
        wds.to_tuple('stoks.npy', 'atoks.npy', 'spk_emb.npy', 'language', 'out_stoks'),
        wds.shuffle(20000, initial=20000),
        wds.batched(64),
    )
    if randomize_speakers:
        rng = np.random.default_rng()
        ds = ds.compose(
            wds.map_tuple(None, None, lambda x: rng.permutation(x), None),
        )
    if validation:
        ds = ds.slice(samples // 64)
    ds.total_samples = samples
    ds.weight = weight
    
    return ds

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 31
import pylab as plt
import fastprogress
import IPython
import numpy as np

class CMLMVisual:
    """Visualize training progress"""
    def __init__ (self, model, masterbar, total_steps):
        self.model = model
        self.masterbar = masterbar
        self.total_steps = total_steps
        self.epochs = total_steps // masterbar.main_bar.total
        
        gs = plt.GridSpec(3, 1, height_ratios=[2,2,1])
        graph_fig = plt.figure(figsize=(10,6))
        self.graph_fig = graph_fig
        self.loss_p = graph_fig.add_subplot(gs[0])
        self.acc_p = graph_fig.add_subplot(gs[1], sharex=self.loss_p)
        self.acc_p.tick_params('x', labelbottom=False)
        self.lr_p = graph_fig.add_subplot(gs[2], sharex=self.loss_p)
        self.lr_p.tick_params('x', labelbottom=False)
        self.graph_out = None
        
        self.its = []
        self.train_losses = []
        self.val_losses = []
        self.lr_history = []
        self.acc = np.nan
        self.acc_history = []
        self.pacc_history = []
            
    def show(self):
        self.start_t = time.time()
        self.masterbar.write(["samples", "train", "val", "time"], table=True)
        self.graph_out = display(self.graph_fig, display_id=True)
        self.acc_out = display(IPython.display.HTML(''), display_id=True)
    
    def hide(self):
        if self.graph_out is not None:
            self.graph_out.update(IPython.display.HTML(''))
    
    def plot(self):
        loss_p, acc_p, lr_p = self.loss_p, self.acc_p, self.lr_p
        loss_p.clear()
        loss_p.plot(self.its, self.train_losses)
        loss_p.plot(self.its, self.val_losses)
        loss_p.set_xlim(0, self.total_steps)
        loss_p.set_yscale('log')
        acc_p.clear()
        for k in self.acc_history[-1].keys():
            acc_p.plot(self.its, [x[k] for x in self.acc_history], ':')
#         acc_p.plot(self.its, np.stack(self.pacc_history), label=range(len(self.pacc_history[0])))
        lr_p.clear()
        lrs = np.array(self.lr_history)
        lr_p.plot(self.its, lrs)
        self.graph_out.update(self.graph_fig)
    
    def add_data(self, it, lr, train_loss, val_los):
        self.its.append(it)
        self.train_losses.append(train_loss)
        self.val_losses.append(val_los)
        self.lr_history.append(lr)
        metrics = self.model.get_metrics()
        self.acc_history.append(metrics)
#         self.acc_out.update(f"Accuracy: {self.entropy_history[-1]:.2f}")
#         self.pacc_history.append((self.model.pval_true / self.model.pval_total).cpu().numpy())
#         if self.acc_history:
        html  = "<h5>Accuracies:</h5><table>"
        html += "<thead>"+(''.join([f"<td>{k}<td>" for k,x in metrics.items()]))+"</thead>"
        html += "<tr>"+(''.join([f"<td>{x*100:.1f}%<td>" for k,x in metrics.items()]))+"</tr>"
        html += "</table>"
        self.acc_out.update(IPython.display.HTML(html))
        self.plot()

    def add_table_row(self, it, avg_train_loss, val_loss):
        elapsed_t = time.time() - self.start_t
        self.masterbar.write([it, f"{avg_train_loss:.5f}", f"{val_loss:.5f}", fastprogress.core.format_time(elapsed_t)], table=True)
    
    def on_iter(self, bar, it, avg_train_loss, val_loss):
        epoch = math.ceil(it / self.total_steps * self.epochs)
        bar.comment = f"#{epoch}/{self.epochs} loss: {avg_train_loss:.3f} / {val_loss:.3f}"

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 38
class DelSumEmbedding(nn.Module):
    def __init__(self, n_head=6, head_width=64, atoks_width=None, length=2250, codes=1024, quantizers=8, pos_embs=None):
        super().__init__()
        self.length = length
        width = n_head * head_width
        if atoks_width is None: atoks_width = width
        self.width = width
        self.quantizers = quantizers

        emb = None
        embs = []
        for _ in range(quantizers):
            emb = FlexEmbeddings(codes, width, special_codes=2, frozen_width=atoks_width,
                                 special_embedding=emb and emb.special)
            embs.append(emb)
        self.embeddings = nn.ModuleList(embs)
        if pos_embs is not None:
            self.register_buffer("positional_embedding", pos_embs)

    def forward(self, toks, xenc):
        with record_function("embeddings"):
            b,_,n = toks.shape
            newn = min(n, self.length)

            embs = torch.zeros((b,newn,self.width), dtype=xenc.dtype, device=xenc.device)
            for i in range(self.quantizers):
                embs[:, :] += self.embeddings[i](toks[:,i,:])
            
            x = embs.to(xenc.dtype)
        return x

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 39
class DelSumHead(nn.Module):
    def __init__(self, quantizers=8, n_head=6, head_width=64):
        super().__init__()
        self.width = n_head * head_width
        self.quantizers = quantizers
        self.splitter = nn.Sequential(
            nn.Linear(self.width, self.width * quantizers),
            nn.GELU(),
        )

    def forward(self, x, embeddings=None):
        b, newn, _ = x.shape
        with record_function("splitter"):
            split = self.splitter(x).view(b,newn,self.quantizers,self.width)
        with record_function("unembed"):
            logits = torch.stack([embeddings[q].unembed(split[:,:,q]) for q in range(self.quantizers)], dim=1)
        return logits
        
def rand(start, end):
    return random.random() * (end - start) + start
    
@dataclasses.dataclass
class Tunables:
    init_std :float = 9
    embeddings_std :float = 0.2
    embeddings_lr_scale: float = 10
    output_mult :float = 5.6
    # FIXME: try separate mults for self and cross attention
    query_mult :float = .3
    encoder_depth_ratio :float = 0.25
    linear_heads :bool = False
    rope :bool = True
    
    lr0 :float = 3e-3
    clip_gradient_norm :float = 2
    weight_decay :float = 1e-3
    warmup_steps :float = 2000

    random :bool = False

    def __post_init__(self):
        # randomize the hyperparams if requested
        if self.random:
            self.init_std = 2*10**rand(0,1)
            self.embeddings_std = 10**rand(-1.7,-0.22)
            self.embeddings_lr_scale = 2**rand(2,4)
            self.output_mult = 2**rand(1.5,3)
            self.query_mult = 2**rand(-3,-1.3)
            self.encoder_depth_ratio = random.choice([0.25,0.5])
            self.linear_heads = False
            self.rope = True
            
            self.lr0 = 3e-3
            self.clip_gradient_norm = 10**rand(-1,1)
            self.warmup_steps = 100*(10**rand(1.18,1.3))
            
    @staticmethod
    def upgrade(args):
        args = {k:v for k,v in args.items()}
        def old_default(name, value):
            if name not in args: args[name] = value
        old_default('rope', False)
        old_default('linear_heads', True)
        return args
            
class SADelARTransformer(nn.Module):
    def __init__(self, depth=3, ctx_n=2250,
                 stoks_len=750, stoks_codes=4097, stoks_width=None,
                 spk_width=None,
                 atoks_width=None,
                 n_head=3, head_width=64, ffn_mult=4,
                 quantizers=8, speaker_map={"1":0}, tunables=Tunables(),
                 use_kv_cache=True):
        super().__init__()
        self.quantizers = quantizers
        self.codes = 1024
        width = n_head * head_width
        store_attr("depth,ctx_n,stoks_len,stoks_codes,stoks_width,spk_width,atoks_width,n_head,head_width,ffn_mult,quantizers,speaker_map")
        self.width = width
        self.base_width = 3 * head_width
        self.tunables = tunables
        
        if stoks_width is None: stoks_width = width
        if spk_width is None: spk_width = width
        self.emb_factor = width != stoks_width
        self.spk_factor = width != spk_width

        if tunables.rope:
            self.positional_embeddings = None
        else:
            self.register_buffer('positional_embeddings', sinusoids(ctx_n, width))
        
#         self.speaker_embedding = nn.Embedding(len(speaker_map), spk_width)
        self.semantic_embedding = nn.Embedding(stoks_codes, stoks_width)
        if self.emb_factor:
            self.emb_to_hidden = nn.Linear(stoks_width, width)
            self.hidden_to_emb = nn.Linear(width, stoks_width)
        
        if self.spk_factor:
            self.spk_to_hidden = nn.Linear(spk_width, width)

        qk_scale = self.tunables.query_mult * 8 / math.sqrt(head_width)
        
        encoder_depth = int(depth * 2 * tunables.encoder_depth_ratio)
        decoder_depth = depth * 2 - encoder_depth
        self.encoder = nn.Sequential(*[
            ResidualAttentionBlock(width, n_head, qk_scale=qk_scale, ffn_mult=ffn_mult, rope=tunables.rope) for _ in range(encoder_depth)
        ]) # FIXME: enclm requires causal attention here
        self.ln_post = LayerNorm(width)

        self.embds = DelSumEmbedding(
            pos_embs=self.positional_embeddings, length=ctx_n,
            n_head=n_head, head_width=head_width, atoks_width=atoks_width,
            quantizers=quantizers,
        )
        self.use_kv_cache = use_kv_cache
        self.decoder = BaseDecoder(qk_scale=qk_scale, length=ctx_n,
                                     n_head=n_head, width=n_head * head_width, 
                                     ffn_mult=ffn_mult, depth=decoder_depth,
                                     rope=tunables.rope,use_kv_cache=use_kv_cache)
        self.head = DelSumHead(n_head=n_head, head_width=head_width, quantizers=quantizers)
        for l in self.decoder.layers:
            l.cross_attn.key_subsampling = 3
#         for l in self.encoder:
#             l.attn.key_subsampling = 3
#             l.attn.query_subsampling = 3
        
        self.register_buffer('val_true', torch.zeros(self.quantizers).cuda())
        self.register_buffer('val_total', torch.zeros(self.quantizers).cuda())
        self.apply(self.init_transformer)

    def setup(self, device):
        pass
        
    def load_frozen_semantic_embeddings(self, vqmodel):
        with torch.no_grad():
            self.semantic_embedding.weight[:] = vqmodel.rq.layers[0]._codebook.embed[0]
            self.semantic_embedding.lr_scale = 0

    def load_frozen_acoustic_embeddings(self, amodel):
        for i in range(self.quantizers):
            self.decoder.embeddings[i].set_frozen_embeddings(amodel.quantizer.vq.layers[i].codebook)
            
    def init_transformer(self, m):
        if isinstance(m, LinearHead):
            m.no_weight_decay = True
            torch.nn.init.constant_(m.weight, 0)
        elif isinstance(m, QueryHead):
            m.lr_scale = 1/(m.weight.shape[1] / self.base_width)
            torch.nn.init.constant_(m.weight, 0)
        elif isinstance(m, nn.Embedding):
            m.no_weight_decay = True
            m.lr_scale = self.tunables.embeddings_lr_scale
            std = self.tunables.embeddings_std
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
#         elif isinstance(m, EmbeddingProjector):
#             m.lr_scale = self.tunables.embeddings_lr_scale #1/(m.weight.shape[1] / self.base_width)
#             m.lr_scale = 2/(m.weight.shape[1] / self.base_width)
#             std = self.tunables.init_std / m.weight.shape[1]
#             torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
        elif isinstance(m, nn.Linear):
            m.lr_scale = 1/(m.weight.shape[1] / self.base_width)
            std = self.tunables.init_std / m.weight.shape[1]
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
            if m.bias is not None:
                torch.nn.init.trunc_normal_(m.bias, std=std, a=-3*std, b=3*std)
        elif isinstance(m, nn.LayerNorm):
            m.no_weight_decay = True
            torch.nn.init.constant_(m.bias, 0)
            torch.nn.init.constant_(m.weight, 1)

    def embed_stoks(self, Stoks):
        b,n = Stoks.shape
        if self.stoks_len == 1500:
            # converts 50 toks/s to 75 toks/s by adding padding between every two tokens
            x = Stoks.reshape(b,n//2,2)
            x = x.repeat_interleave(2, -1)[:,:,:3]
            x[:,:,1] = 1024
            x = x.reshape(b,n//2*3)
        else:
            # it's a lot easier with 25 toks/s
#             x = Stoks.repeat_interleave(3, -1)
            x = Stoks
        # embed semantic tokens
        Sembs = self.semantic_embedding(x.to(torch.long))
        if self.emb_factor:
            Sembs = self.emb_to_hidden(Sembs)
        return Sembs

    def run_encoder(self, Stoks, speakers):
        semb = self.embed_stoks(Stoks)
        with record_function("encoder"):
            if self.positional_embeddings is not None: semb = semb + self.positional_embeddings
            xenc = self.ln_post(self.encoder(semb))
        if self.training:
            enc_logits = (self.hidden_to_emb(xenc) @ self.semantic_embedding.weight.to(xenc.dtype).T).float()
            enc_logits = enc_logits * self.tunables.output_mult / (self.width / self.base_width)
        else:
            enc_logits = None
#         print(xenc.shape, speakers.shape)
        spk_embs = F.normalize(speakers, dim=-1) # use extracted embeddings
        if self.spk_factor: spk_embs = self.spk_to_hidden(spk_embs)
        return xenc + spk_embs.unsqueeze(1), enc_logits

    def forward(self, Stoks, Atoks, speakers, langs=None, out_stoks=None, noloss=False, xenc=None, offset=0):
        if xenc is None:
            Atoks = Atoks.to(torch.long)
            out_stoks = out_stoks.to(torch.long)
            Atoks_gt = Atoks.clone()
            Atoks_gt[Atoks == -100] = 1024
            xenc, enc_logits = self.run_encoder(Stoks, speakers)
        else:
            Atoks_gt = Atoks
        with record_function("decoder"):
            embs = self.embds(Atoks, xenc)
            x = self.decoder(embs, xenc, offset=offset)
            logits = self.head(x, embeddings=self.embds.embeddings)
            logits *= self.tunables.output_mult / (self.width / self.base_width)
            
        if noloss:
            return logits

        with record_function("loss"):
            N = Atoks.shape[-1]
            loss = 0
            for i in range(self.quantizers):
                loss += F.cross_entropy(logits[:,i,i:].reshape(-1,logits.shape[-1]), Atoks[:,i,:N-i].reshape(-1))
                if self.training and i == 0:
                    loss *= 5
            loss /= self.quantizers
            if self.training:
                loss += 0.1 * F.cross_entropy(enc_logits.transpose(-1,-2), out_stoks)

        if not self.training:
            for i in range(self.quantizers):
                Atoks_i = Atoks[:,i,:N-i]
                valid_Atoks = Atoks_i != -100
                self.val_true[i] += (logits[:,i,i:].argmax(-1)[valid_Atoks] == Atoks_i[valid_Atoks]).float().sum()
                self.val_total[i] += valid_Atoks.float().sum()

        return logits, loss

    def get_metrics(self):
        metrics = {
            f'acc_{i}':x.item() for i,x in enumerate(self.val_true / self.val_total)
        }
        self.val_true[:] = 0
        self.val_total[:] = 0
        return metrics

    #
    # inference
    #
    @classmethod
    def load_model(cls, ref="collabora/whisperspeech:s2a-q4-small-en+pl.model",
                   repo_id=None, filename=None, local_filename=None, use_kv_cache=True):
        if repo_id is None and filename is None and local_filename is None:
            if ":" in ref:
                repo_id, filename = ref.split(":", 1)
            else:
                local_filename = ref
        if not local_filename:
            local_filename = hf_hub_download(repo_id=repo_id, filename=filename)
        spec = torch.load(local_filename)
        if '_extra_state' not in spec['state_dict']: spec['state_dict']['_extra_state'] = { 'speaker_map': spec['config']['speaker_map'] }
        model = cls(**spec['config'], tunables=Tunables(**Tunables.upgrade(spec['tunables'])), use_kv_cache=use_kv_cache)
        model.load_state_dict(spec['state_dict'])
        model.eval()
        return model
    
    def get_extra_state(self):
        return { 'speaker_map': self.speaker_map }
    
    def set_extra_state(self, st):
        self.speaker_map = st['speaker_map']

    def load_checkpoint(self, local_filename):
        spec = torch.load(local_filename, map_location='cpu')
        assert 'pytorch-lightning_version' in spec, 'not a valid PyTorch Lightning checkpoint'
        state_dict = {k.replace('model.', ''):v
                      for k,v in spec['state_dict'].items()}
        self.load_state_dict(state_dict)
        return self
    
    def save_model(self, fname):
        torch.save(dict(config = self.__stored_args__,
                        tunables = dataclasses.asdict(self.tunables),
                        state_dict = self.state_dict()), fname)

    @property
    def device(self):
        return next(self.parameters()).device
    
    @torch.no_grad()
    def generate(self, stoks, speakers, langs=None, N=None, T=0.7, top_k=None, show_progress_bar=True, step=None, subsample_enc=False):
        dev = self.device
        if self.stoks_len == 1500:
            N = N or len(stoks) * 3 // 2
        else:
            N = N or len(stoks) * 3
        stoks = F.pad(stoks.to(dev), (1, self.stoks_len - len(stoks)-1), value=self.stoks_codes-1).unsqueeze(0)
#         speakers = torch.tensor([self.speaker_map[spk] for spk in speakers], device=dev)
        speakers = speakers.to(device=dev)
        # if self.decoder.lang_embeddings:
        #     langs = torch.tensor([lang_to_id(lang) for lang in langs], device=dev)
        toks = torch.full((1,self.quantizers,N+1), self.codes+1, dtype=torch.long, device=dev)
        it = range(0,N)
        if show_progress_bar: it = progress_bar(it)
        if self.decoder.kv_cache is not None: self.decoder.kv_cache.clear()
        xenc, _ = self.run_encoder(stoks, speakers)
        vN = 128
        for i in it:
            if i >= vN: vN *= 2
            toks_ = toks[:,:,:vN+1]
            if self.use_kv_cache:
                toks_ = toks_[:,:,i:i+1]
            p = self(None, toks_, None, langs, noloss=True, xenc=xenc, offset=i)
            if not self.use_kv_cache:
                last_p = p[0, :, i]
            else:
                last_p = p[0, :, -1]
            if top_k:
                last_p[last_p < torch.topk(last_p, top_k).values[:,-1,None]] = -torch.inf
            
            for j,tok in enumerate(torch.multinomial((last_p / float(T)).softmax(-1), 1)):
                if i-j>=0:
                    toks[0,j,i+1] = tok
            
            if toks[0,0,i+1] == 1024:
                toks = toks[:,:,:i+1]
                break

            if step is not None: step()

        # shift tokens
        toks = toks[:,:,1:]
        for j in range(self.quantizers):
            toks[0, j] = torch.roll(toks[0, j], -j)
        return toks[0]

# %% ../nbs/4B. Multi-language semantic to acoustic token modeling.ipynb 40
def _make_model(size:str, quantizers:int=4, tunables:Tunables=Tunables(), **kwargs):
    kwargs = dict(quantizers=quantizers, tunables=tunables, **kwargs)
    if size == 'micro':
        return SADelARTransformer(depth=4, n_head=3, ffn_mult=2, **kwargs)
    if size == 'tiny-narrow':
        return SADelARTransformer(depth=4, n_head=6, ffn_mult=1, **kwargs)
    if size == 'tiny':
        return SADelARTransformer(depth=4, n_head=6, **kwargs)
    if size == 'base':
        return SADelARTransformer(depth=6, n_head=8, **kwargs)
    if size == 'base-deep':
        return SADelARTransformer(depth=9, n_head=8, **kwargs)
    if size == 'base-wide':
        return SADelARTransformer(depth=6, n_head=12, **kwargs)
    if size == 'small/2':
        return SADelARTransformer(depth=9, n_head=12, **kwargs)
    if size == 'small':
        return SADelARTransformer(depth=12, n_head=12, **kwargs)
    if size == 'medium':
        return SADelARTransformer(depth=24, n_head=16, **kwargs)

def make_model(size:str, quantizers:int=4, frozen_embeddings_model:str=None, frozen_acoustic_embeddings:bool=False, spk_width:int=None, tunables:Tunables=Tunables(), dataset=None):
    amodel = EncodecModel.encodec_model_24khz() if frozen_acoustic_embeddings else None
    vqmodel = vq_stoks.RQBottleneckTransformer.load_model(frozen_embeddings_model) if frozen_embeddings_model else None
    model = _make_model(size, quantizers, tunables,
                        spk_width=spk_width,
                        atoks_width=amodel and amodel.quantizer.vq.layers[0]._codebook.embed.shape[-1],
                        stoks_codes=vqmodel.vq_codes+1, stoks_width=vqmodel.rq.layers[0]._codebook.embed[0].shape[-1])
    if vqmodel: model.load_frozen_semantic_embeddings(vqmodel)
    if amodel: model.load_frozen_acoustic_embeddings(amodel)
    return model