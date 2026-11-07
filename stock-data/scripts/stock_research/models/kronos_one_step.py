"""Shared causal one-step inference, copied from the audited full-hourly test.

Native sampling/time helpers are injected to avoid importing a global ``model``
package. All normalization and numerical operations retain that test's policy.
The native equivalence profile covered eight configurations, not every input.
"""
from __future__ import annotations

import numpy as np
import torch
import torch.nn.functional as F


def attention_projection(attn, normalized):
    b, n, _ = normalized.shape
    q = attn.q_proj(normalized).view(b, n, attn.n_heads, attn.head_dim).transpose(1, 2)
    k = attn.k_proj(normalized).view(b, n, attn.n_heads, attn.head_dim).transpose(1, 2)
    v = attn.v_proj(normalized).view(b, n, attn.n_heads, attn.head_dim).transpose(1, 2)
    return q, k, v


def output_attention(attn, tensor):
    b, _, n, _ = tensor.shape
    value = tensor.transpose(1, 2).contiguous().view(b, n, attn.d_model)
    return attn.resid_dropout(attn.out_proj(value))


def rotate_at_position(rotary, q, k, position):
    cos, sin = rotary._update_cos_sin_cache(q, position + 1)
    cos = cos[:, :, position:position + 1, :]
    sin = sin[:, :, position:position + 1, :]
    return q * cos + rotary._rotate_half(q) * sin, k * cos + rotary._rotate_half(k) * sin


def tokenizer_last(tokenizer, history_tokens, future_tokens):
    history = tokenizer.post_quant_embed(tokenizer.indices_to_bits(history_tokens, half=True))
    future = tokenizer.post_quant_embed(tokenizer.indices_to_bits(future_tokens, half=True))
    n, paths = history.shape[1], future.shape[0]
    for block in tokenizer.decoder:
        if block.training:
            raise ValueError('optimization_requires_eval')
        attn = block.self_attn
        hq, hk, hv = attention_projection(attn, block.norm1(history))
        hq, hk = attn.rotary(hq, hk)
        ha = F.scaled_dot_product_attention(hq, hk, hv, dropout_p=0.0, is_causal=True)
        next_history = history + output_attention(attn, ha)
        next_history = next_history + block.ffn(block.norm2(next_history))
        fq, fk, fv = attention_projection(attn, block.norm1(future))
        fq, fk = rotate_at_position(attn.rotary, fq, fk, n)
        all_k = torch.cat([hk.expand(paths, -1, -1, -1), fk], dim=2)
        all_v = torch.cat([hv.expand(paths, -1, -1, -1), fv], dim=2)
        fa = F.scaled_dot_product_attention(fq, all_k, all_v, dropout_p=0.0, is_causal=False)
        future = future + output_attention(attn, fa)
        future = future + block.ffn(block.norm2(future))
        history = next_history
    return tokenizer.head(future)


def second_logits(network, context, sample_pre):
    paths = sample_pre.shape[0]
    sibling = network.embedding.emb_s1(sample_pre)
    attn = network.dep_layer.cross_attn
    b, n, _ = context.shape
    q = attn.q_proj(sibling).view(paths, 1, attn.n_heads, attn.head_dim).transpose(1, 2)
    k = attn.k_proj(context).view(b, n, attn.n_heads, attn.head_dim).transpose(1, 2)
    v = attn.v_proj(context).view(b, n, attn.n_heads, attn.head_dim).transpose(1, 2)
    # Preserve native cross-rotary's query-length-one cache convention.
    q, k = attn.rotary(q, k.expand(paths, -1, -1, -1))
    attention = F.scaled_dot_product_attention(q, k, v.expand(paths, -1, -1, -1),
        dropout_p=0.0, is_causal=False)
    value = output_attention(attn, attention)
    last = network.dep_layer.norm(context[:, -1:, :].expand(paths, -1, -1) + value)
    return network.head.cond_forward(last)[:, -1, :]


class SharedOneStep:
    def __init__(self, network, tokenizer, paths=16, clip=5.0, *, sample_from_logits, calc_time_stamps):
        self.network = network.eval()
        self.tokenizer = tokenizer.eval()
        self.paths, self.clip = paths, clip
        self.sample_from_logits = sample_from_logits
        self.calc_time_stamps = calc_time_stamps

    def forecast(self, values, timestamps, seed):
        x = np.asarray(values, dtype=np.float32)
        mean, std = np.mean(x, axis=0), np.std(x, axis=0)
        normalized = np.clip((x - mean) / (std + 1e-5), -self.clip, self.clip)
        stamp = self.calc_time_stamps(timestamps).values.astype(np.float32)
        torch.manual_seed(seed)
        with torch.inference_mode():
            encoded = self.tokenizer.encode(torch.from_numpy(normalized[None, :, :]), half=True)
            logits, context = self.network.decode_s1(encoded[0], encoded[1], torch.from_numpy(stamp[None, :, :]))
            pre = self.sample_from_logits(logits[:, -1, :].expand(self.paths, -1), temperature=1.0,
                top_k=0, top_p=0.9, sample_logits=True)
            post_logits = second_logits(self.network, context, pre)
            post = self.sample_from_logits(post_logits, temperature=1.0, top_k=0, top_p=0.9, sample_logits=True)
            decoded = tokenizer_last(self.tokenizer, encoded, [pre, post])[:, 0, :].cpu().numpy()
        return decoded * (std + 1e-5) + mean, {'pre': pre[:, 0].tolist(), 'post': post[:, 0].tolist()}
