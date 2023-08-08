import math
from functools import partial
from random import random

import torch
from torch import nn, Tensor, einsum
from torch.nn import Module
import torch.nn.functional as F

import torchode as to

from torchdiffeq import odeint

from beartype import beartype
from beartype.typing import Tuple

from einops import rearrange, repeat, reduce, pack, unpack

from voicebox_pytorch.attend import Attend

# helper functions

def exists(val):
    return val is not None

def identity(t):
    return t

def default(val, d):
    return val if exists(val) else d

def divisible_by(num, den):
    return (num % den) == 0

def is_odd(n):
    return not divisible_by(n, 2)

def coin_flip():
    return random() < 0.5

def pack_one(t, pattern):
    return pack([t], pattern)

def unpack_one(t, ps, pattern):
    return unpack(t, ps, pattern)[0]

# tensor helpers

def prob_mask_like(shape, prob, device):
    if prob == 1:
        return torch.ones(shape, device = device, dtype = torch.bool)
    elif prob == 0:
        return torch.zeros(shape, device = device, dtype = torch.bool)
    else:
        return torch.zeros(shape, device = device).float().uniform_(0, 1) < prob

# mask construction helpers

def mask_from_start_end_indices(
    seq_len: int,
    start: Tensor,
    end: Tensor
):
    assert start.shape == end.shape
    device = start.device

    seq = torch.arange(seq_len, device = device, dtype = torch.long)
    seq = seq.reshape(*((-1,) * start.ndim), seq_len)
    seq = seq.expand(*start.shape, seq_len)

    mask = seq >= start[..., None].long()
    mask &= seq < end[..., None].long()
    return mask

def mask_from_frac_lengths(
    seq_len: int,
    frac_lengths: Tensor
):
    device = frac_lengths

    lengths = (frac_lengths * seq_len).long()
    max_start = seq_len - lengths

    rand = torch.zeros_like(frac_lengths).float().uniform_(0, 1)
    start = (max_start * rand).clamp(min = 0)
    end = start + lengths

    return mask_from_start_end_indices(seq_len, start, end)

# sinusoidal positions

class LearnedSinusoidalPosEmb(Module):
    """ used by @crowsonkb """

    def __init__(self, dim):
        super().__init__()
        assert divisible_by(dim, 2)
        half_dim = dim // 2
        self.weights = nn.Parameter(torch.randn(half_dim))

    def forward(self, x):
        x = rearrange(x, 'b -> b 1')
        freqs = x * rearrange(self.weights, 'd -> 1 d') * 2 * math.pi
        fouriered = torch.cat((freqs.sin(), freqs.cos()), dim = -1)
        return fouriered

# rotary positional embeddings
# https://arxiv.org/abs/2104.09864

class RotaryEmbedding(Module):
    def __init__(self, dim, theta = 10000):
        super().__init__()
        inv_freq = 1.0 / (theta ** (torch.arange(0, dim, 2).float() / dim))
        self.register_buffer("inv_freq", inv_freq)

    @property
    def device(self):
        return self.inv_freq.device

    def forward(self, seq_len):
        t = torch.arange(seq_len, device = self.device).type_as(self.inv_freq)
        freqs = torch.einsum('i , j -> i j', t, self.inv_freq)
        freqs = torch.cat((freqs, freqs), dim = -1)
        return freqs

def rotate_half(x):
    x1, x2 = x.chunk(2, dim = -1)
    return torch.cat((-x2, x1), dim = -1)

def apply_rotary_pos_emb(pos, t):
    return t * pos.cos() + rotate_half(t) * pos.sin()

# convolutional positional generating module

class ConvPositionEmbed(Module):
    def __init__(
        self,
        dim,
        *,
        kernel_size,
        groups = None
    ):
        super().__init__()
        assert is_odd(kernel_size)
        groups = default(groups, dim) # full depthwise conv by default

        self.dw_conv1d = nn.Sequential(
            nn.Conv1d(dim, dim, kernel_size, groups = groups, padding = kernel_size // 2),
            nn.GELU()
        )

    def forward(self, x):
        x = rearrange(x, 'b n c -> b c n')
        x = self.dw_conv1d(x)
        return rearrange(x, 'b c n -> b n c')

# norms

class RMSNorm(Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return F.normalize(x, dim = -1) * self.scale * self.gamma

# attention

class Attention(Module):
    def __init__(
        self,
        dim,
        dim_head = 64,
        heads = 8,
        flash = False
    ):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        dim_inner = dim_head * heads

        self.attend = Attend(flash = flash)

        self.norm = RMSNorm(dim)
        self.to_qkv = nn.Linear(dim, dim_inner * 3, bias = False)
        self.to_out = nn.Linear(dim_inner, dim, bias = False)

    def forward(self, x, mask = None, rotary_emb = None):
        h = self.heads

        x = self.norm(x)

        q, k, v = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, k, v))

        if exists(rotary_emb):
            q, k = map(lambda t: apply_rotary_pos_emb(t, rotary_emb), (q, k))

        out = self.attend(q, k, v, mask = mask)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# feedforward

def FeedForward(dim, mult = 4):
    return nn.Sequential(
        RMSNorm(dim),
        nn.Linear(dim, dim * mult),
        nn.GELU(),
        nn.Linear(dim * mult, dim)
    )

# transformer

class Transformer(Module):
    def __init__(
        self,
        dim,
        *,
        depth,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        attn_flash = False
    ):
        super().__init__()
        assert divisible_by(depth, 2)

        self.layers = nn.ModuleList([])

        self.rotary_emb = RotaryEmbedding(dim = dim_head)

        for ind in range(depth):
            layer = ind + 1
            has_skip = layer > (depth // 2)

            self.layers.append(nn.ModuleList([
                nn.Linear(dim * 2, dim) if has_skip else None,
                Attention(dim = dim, dim_head = dim_head, heads = heads, flash = attn_flash),
                FeedForward(dim = dim, mult = ff_mult)
            ]))

        self.final_norm = RMSNorm(dim)

    def forward(self, x):
        skip_connects = []

        rotary_emb = self.rotary_emb(x.shape[-2])

        for skip_combiner, attn, ff in self.layers:

            # in the paper, they use a u-net like skip connection
            # unclear how much this helps, as no ablations or further numbers given besides a brief one-two sentence mention

            if not exists(skip_combiner):
                skip_connects.append(x)
            else:
                x = torch.cat((x, skip_connects.pop()), dim = -1)
                x = skip_combiner(x)

            x = attn(x, rotary_emb = rotary_emb) + x
            x = ff(x) + x

        return self.final_norm(x)

# both duration and main denoising model are transformers

class DurationPredictor(Module):
    def __init__(
        self,
        *,
        num_phoneme_tokens,
        dim_phoneme_emb = 512,
        dim = 512,
        depth = 10,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        conv_pos_embed_kernel_size = 31,
        conv_pos_embed_groups = None,
        attn_flash = False,
        p_drop_prob = 0.2, # p_drop in paper
        frac_lengths_mask: Tuple[float, float] = (0.1, 1.)
    ):
        super().__init__()

        self.null_phoneme_id = num_phoneme_tokens # use last phoneme token as null token for CFG
        self.to_phoneme_emb = nn.Embedding(num_phoneme_tokens + 1, dim_phoneme_emb)

        self.p_drop_prob = p_drop_prob
        self.frac_lengths_mask = frac_lengths_mask

        self.to_embed = nn.Linear(dim * 2 + dim_phoneme_emb, dim)

        self.null_cond = nn.Parameter(torch.zeros(dim))

        self.conv_embed = ConvPositionEmbed(
            dim = dim,
            kernel_size = conv_pos_embed_kernel_size,
            groups = conv_pos_embed_groups
        )

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            dim_head = dim_head,
            heads = heads,
            ff_mult = ff_mult,
            attn_flash = attn_flash
        )

        self.to_pred = nn.Sequential(
            nn.Linear(dim, 1),
            Rearrange('... 1 -> ...')
        )

    @property
    def device(self):
        return next(self.parameters()).device

    @torch.inference_mode()
    def forward_with_cond_scale(
        self,
        *args,
        cond_scale = 1.,
        **kwargs
    ):
        logits = self.forward(*args, cond_drop_prob = 0., **kwargs)

        if cond_scale == 1.:
            return logits

        null_logits = self.forward(*args, cond_drop_prob = 1., **kwargs)
        return null_logits + (logits - null_logits) * cond_scale

    def forward(
        self,
        x,
        *,
        phoneme_ids,
        cond,
        cond_drop_prob = 0.,
        target = None,
        mask = None
    ):
        batch, seq_len, cond_dim = cond.shape
        assert cond_dim == x.shape[-1]

        # construct mask if not given

        if not exists(mask):
            if coin_flip():
                frac_lengths = torch.zeros((batch,), device = self.device).float().uniform_(*self.frac_lengths_mask)
                mask = mask_from_frac_lengths(seq_len, frac_lengths)
            else:
                mask = prob_mask_like((batch, seq_len), self.p_drop_prob, self.device)

        # classifier free guidance

        if cond_drop_prob > 0.:
            cond_drop_mask = prob_mask_like(cond.shape[:1], cond_drop_prob, cond.device)

            cond = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1 1'),
                self.null_cond,
                cond
            )

            phoneme_ids = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1'),
                self.null_phoneme_id,
                phoneme_ids
            )

        phoneme_emb = self.to_phoneme_emb(phoneme_ids)

        # combine audio, phoneme, conditioning

        embed = torch.cat((x, phoneme_emb, cond), dim = -1)
        x = self.to_embed(embed)

        x = self.conv_embed(x) + x
        x = self.transformer(x)

        if not exists(mask):
            return F.l1_loss(x, target)

        loss = F.l1_loss(x, target, reduction = 'none')
        loss = loss.masked_fill(mask, 0.)

        # masked mean

        num = reduce(loss, 'b n -> b', 'sum')
        den = mask.sum(dim = -1).clamp(min = 1e-5)
        loss = num / den

        return loss.mean()


class VoiceBox(Module):
    def __init__(
        self,
        *,
        num_phoneme_tokens,
        dim_phoneme_emb = 1024,
        dim = 1024,
        depth = 24,
        dim_head = 64,
        heads = 16,
        ff_mult = 4,
        conv_pos_embed_kernel_size = 31,
        conv_pos_embed_groups = None,
        attn_flash = False,
        p_drop_prob = 0.3, # p_drop in paper
        frac_lengths_mask: Tuple[float, float] = (0.7, 1.)
    ):
        super().__init__()
        self.sinu_pos_emb = LearnedSinusoidalPosEmb(dim)

        self.null_phoneme_id = num_phoneme_tokens # use last phoneme token as null token for CFG
        self.to_phoneme_emb = nn.Embedding(num_phoneme_tokens + 1, dim_phoneme_emb)

        self.p_drop_prob = p_drop_prob
        self.frac_lengths_mask = frac_lengths_mask

        self.to_embed = nn.Linear(dim * 2 + dim_phoneme_emb, dim)

        self.null_cond = nn.Parameter(torch.zeros(dim))

        self.conv_embed = ConvPositionEmbed(
            dim = dim,
            kernel_size = conv_pos_embed_kernel_size,
            groups = conv_pos_embed_groups
        )

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            dim_head = dim_head,
            heads = heads,
            ff_mult = ff_mult,
            attn_flash = attn_flash
        )

        self.to_pred = nn.Linear(dim, dim, bias = False)

    @property
    def device(self):
        return next(self.parameters()).device

    @torch.inference_mode()
    def forward_with_cond_scale(
        self,
        *args,
        cond_scale = 1.,
        **kwargs
    ):
        logits = self.forward(*args, cond_drop_prob = 0., **kwargs)

        if cond_scale == 1.:
            return logits

        null_logits = self.forward(*args, cond_drop_prob = 1., **kwargs)
        return null_logits + (logits - null_logits) * cond_scale

    def forward(
        self,
        x,
        *,
        phoneme_ids,
        cond,
        times,
        cond_drop_prob = 0.1,
        target = None,
        mask = None,
    ):
        batch, seq_len, cond_dim = cond.shape
        assert cond_dim == x.shape[-1]

        # auto manage shape of times, for odeint times

        if times.ndim == 0:
            times = repeat(times, '-> b', b = cond.shape[0])

        if times.ndim == 1 and times.shape[0] == 1:
            times = repeat(times, '1 -> b', b = cond.shape[0])

        # construct mask if not given

        if not exists(mask):
            if coin_flip():
                frac_lengths = torch.zeros((batch,), device = self.device).float().uniform_(*self.frac_lengths_mask)
                mask = mask_from_frac_lengths(seq_len, frac_lengths)
            else:
                mask = prob_mask_like((batch, seq_len), self.p_drop_prob, self.device)

        # classifier free guidance

        if cond_drop_prob > 0.:
            cond_drop_mask = prob_mask_like(cond.shape[:1], cond_drop_prob, self.device)

            cond = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1 1'),
                self.null_cond,
                cond
            )

            phoneme_ids = torch.where(
                rearrange(cond_drop_mask, '... -> ... 1'),
                self.null_phoneme_id,
                phoneme_ids
            )

        phoneme_emb = self.to_phoneme_emb(phoneme_ids)
        embed = torch.cat((x, phoneme_emb, cond), dim = -1)
        x = self.to_embed(embed)

        x = self.conv_embed(x) + x

        # add sinusoidal time embedding along time axis

        time_emb = self.sinu_pos_emb(times)
        x, ps = pack((time_emb, x), 'b * d')

        # attend

        x = self.transformer(x)

        x = self.to_pred(x)

        # split out time embedding

        _, x = unpack(x, ps, 'b * d')

        # if no target passed in, just return logits

        if not exists(target):
            return x

        if not exists(mask):
            return F.mse_loss(x, target)

        loss = F.mse_loss(x, target, reduction = 'none')

        loss = reduce(loss, 'b n d -> b n', 'mean')
        loss = loss.masked_fill(mask, 0.)

        # masked mean

        num = reduce(loss, 'b n -> b', 'sum')
        den = mask.sum(dim = -1).clamp(min = 1e-5)
        loss = num / den

        return loss.mean()

# wrapper for the CNF

class ConditionalFlowMatcherWrapper(Module):
    @beartype
    def __init__(
        self,
        voicebox: VoiceBox,
        sigma = 0.,
        ode_atol = 1e-5,
        ode_rtol = 1e-5,
        ode_step_size = 0.0625,
        use_torchode = False,
        torchdiffeq_ode_method = 'midpoint',                      # use midpoint for torchdiffeq, as in paper
        torchode_method_klass = to.Tsit5,      # use tsit5 for torchode, as torchode does not have midpoint (recommended by Bryan @b-chiang)
        cond_drop_prob = 0.
    ):
        super().__init__()
        self.sigma = sigma

        self.voicebox = voicebox

        self.cond_drop_prob = cond_drop_prob

        self.use_torchode = use_torchode
        self.torchode_method_klass = torchode_method_klass

        self.odeint_kwargs = dict(
            atol = ode_atol,
            rtol = ode_rtol,
            method = torchdiffeq_ode_method,
            options = dict(step_size = ode_step_size)
        )

    @property
    def device(self):
        return next(self.parameters()).device

    @torch.inference_mode()
    def sample(
        self,
        *,
        phoneme_ids,
        cond,
        mask = None,
        steps = 3,
        cond_scale = 1.
    ):
        shape = cond.shape
        batch = shape[0]

        self.voicebox.eval()

        def fn(t, x, *, packed_shape = None):
            if exists(packed_shape):
                x = unpack_one(x, packed_shape, 'b *')

            out = self.voicebox.forward_with_cond_scale(
                x,
                times = t,
                phoneme_ids = phoneme_ids,
                cond = cond,
                cond_scale = cond_scale
            )

            if exists(packed_shape):
                out = rearrange(out, 'b ... -> b (...)')

            return out

        y0 = torch.randn_like(cond)
        t = torch.linspace(0, 1, steps, device = self.device)

        if not self.use_torchode:
            print('sampling with torchdiffeq')

            trajectory = odeint(fn, y0, t, **self.odeint_kwargs)
            sampled = trajectory[-1]
        else:
            print('sampling with torchode')

            t = repeat(t, 'n -> b n', b = batch)
            y0, packed_shape = pack_one(y0, 'b *')

            fn = partial(fn, packed_shape = packed_shape)

            term = to.ODETerm(fn)
            step_method = self.torchode_method_klass(term = term)

            step_size_controller = to.IntegralController(
                atol = self.odeint_kwargs['atol'],
                rtol = self.odeint_kwargs['rtol'],
                term = term
            )

            solver = to.AutoDiffAdjoint(step_method, step_size_controller)
            jit_solver = torch.compile(solver)

            init_value = to.InitialValueProblem(y0 = y0, t_eval = t)

            sol = jit_solver.solve(init_value)

            sampled = sol.ys[:, -1]
            sampled = unpack_one(sampled, packed_shape, 'b *')

        return sampled

    def forward(
        self,
        x1,
        *,
        phoneme_ids,
        cond,
        mask = None
    ):
        """
        following eq (5) (6) in https://arxiv.org/pdf/2306.15687.pdf
        """

        batch, seq_len, dtype, σ = *x1.shape[:2], x1.dtype, self.sigma

        x0 = torch.randn_like(x1)

        # random times

        times = torch.rand((batch,), dtype = dtype, device = self.device)
        t = rearrange(times, 'b -> b 1 1')

        # sample xt (w in the paper)

        w = (1 - (1 - σ) * t) * x0 + t * x1

        flow = x1 - (1 - σ) * x0

        # predict

        self.voicebox.train()

        loss = self.voicebox(
            w,
            phoneme_ids = phoneme_ids,
            cond = cond,
            mask = mask,
            times = times,
            target = flow,
            cond_drop_prob = self.cond_drop_prob
        )

        return loss
