# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

import math
import random
from typing import List, Optional, Sequence, Tuple, Type, TypeVar

import pytest
import torch
from scipy.stats import binom_test
from torch.utils.checkpoint import checkpoint

import xformers.ops
from xformers.ops import fmha
from xformers.ops.fmha.common import AttentionOpBase

from .utils import assert_allclose

torch.backends.cuda.matmul.allow_tf32 = False
cuda_only = pytest.mark.skipif(not torch.cuda.is_available(), reason="requires CUDA")
compute_capability = (0, 0)
if torch.cuda.is_available():
    compute_capability = torch.cuda.get_device_capability("cuda")
sm75_or_better_only = pytest.mark.skipif(
    compute_capability < (7, 5), reason="requires sm75+"
)
_devices = ["cpu", "cuda"] if torch.cuda.is_available() else ["cpu"]

ALL_FW_OPS: Sequence[Type[fmha.common.AttentionFwOpBase]] = [
    fmha.cutlass.FwOp,
    fmha.flash.FwOp,
    fmha.triton.FwOp,
    fmha.small_k.FwOp,
]

ALL_BW_OPS: Sequence[Type[fmha.common.AttentionBwOpBase]] = [
    fmha.cutlass.BwOp,
    fmha.flash.BwOp,
    fmha.triton.BwOp,
    fmha.small_k.BwOp,
]

T = TypeVar(
    "T", Type[fmha.common.AttentionFwOpBase], Type[fmha.common.AttentionBwOpBase]
)


def _filter_unsupported_ops(ops: Sequence[T]) -> Sequence[T]:
    return [
        op
        for op in ops
        if "cpu" in op.SUPPORTED_DEVICES
        or op.CUDA_MINIMUM_COMPUTE_CAPABILITY <= compute_capability
    ]


ALL_FW_OPS = _filter_unsupported_ops(ALL_FW_OPS)
ALL_BW_OPS = _filter_unsupported_ops(ALL_BW_OPS)


def sample_random_supported_fw(
    inp: fmha.Inputs, seed: int
) -> Type[fmha.common.AttentionFwOpBase]:
    r = random.Random(seed)
    fw_ops = list(ALL_FW_OPS)
    r.shuffle(fw_ops)
    for op in fw_ops:
        if op.supports(inp):
            return op
    raise NotImplementedError(f"Could not find a FW operator for: {inp}")


def generate_test_shapes_B_Mq_Mkv_H_K_Kv(op):
    shapes = []
    for B in op._TEST_BATCH_SIZES:
        for Mq in [32, 256]:
            for Mkv in [32, 64, 256]:
                for K in op._TEST_K:
                    shapes.append((B, Mq, Mkv, 1, K, K))
        Mq = 256
        Mkv = 128
        K = 32
        H = 1
        # Weird values of parameters
        for M in [2, 3, 15, 31, 32, 34, 68, 72, 90, 132, 136]:
            shapes.append((B, M, Mkv, H, K, K))
            shapes.append((B, Mq, M, H, K, K))
        for _K in [1, 2, 3, 31, 34, 36, 38, 40, 64, 256 + 2, 256 + 8, 512]:
            if _K <= op.SUPPORTED_MAX_K:
                shapes.append((B, Mq, Mkv, H, _K, _K))
        # Different value for K / Kv
        if op.SUPPORTS_DIFFERENT_VALUE_EMBED:
            for _K in [32, 36, 64, 256 + 8]:
                shapes.append((B, Mq, Mkv, H, K, _K))
                shapes.append((B, Mq, Mkv, H, _K, K))
        # Exotic sizes
        for K in op._TEST_K:
            shapes.append((B, 16, 1024, H, K, K))
            shapes.append((B, 1024, 16, H, K, K))
        # Some number of heads
        for H in [3, 5, 12]:
            shapes.append((max(1, B // H), Mq, Mkv, H, K, K))
    # Some strides don't fit on an uint16
    shapes.append((1, 128, 128, 300, 128, 128))
    # TODO: Some strides don't fit on an uint32
    # Crashes on Flash, Errors on Cutlass
    # shapes.append((1, 1, 64000, 300, 128, 128))
    # Add some random shapes
    if op in [
        fmha.cutlass.FwOp,
        fmha.cutlass.BwOp,
        fmha.flash.BwOp,
    ]:
        K_CHOICES = [8 * i for i in range(1, 256 // 8)]
        r = random.Random(0)
        for _ in range(20):
            B = r.randint(1, 400)
            Mq = r.randint(1, 500)
            Mkv = r.randint(1, 500)
            H = r.randint(2, 11)
            B = max(B // H, 1)
            K = r.choice(K_CHOICES)
            Kv = r.choice(K_CHOICES)
            if not op.SUPPORTS_DIFFERENT_VALUE_EMBED:
                Kv = K
            shapes.append((B, Mq, Mkv, H, K, Kv))
    return shapes


def _generate_op_device_dtype_biasT_B_Mq_Mkv_H_K_Kv(
    ops_list: Sequence[Type[fmha.AttentionOpBase]], max_shapes_per_op: int = 65000
):
    r = random.Random(0)
    combination = []
    ids = []
    for op in ops_list:
        op_count = 0
        for shape in generate_test_shapes_B_Mq_Mkv_H_K_Kv(op):
            has_one = False
            for device in _devices:
                if device not in op.SUPPORTED_DEVICES:
                    continue
                for dtype in op.SUPPORTED_DTYPES:
                    bias_type = r.choice(list(op.SUPPORTED_ATTN_BIAS_TYPES))
                    # Avoid using too much memory
                    if bias_type not in [
                        type(None),
                        fmha.attn_bias.LowerTriangularMask,
                    ]:
                        B, Mq, Mkv, H, K, Kv = shape
                        B = min(B, 12)
                        shape = (B, Mq, Mkv, H, K, Kv)
                    combination.append((op, device, dtype, bias_type, *shape))
                    ids.append(
                        f"{op.NAME}-{device}-{str(dtype)}-{bias_type.__name__}-{'-'.join([str(s) for s in shape])}"
                    )
                    has_one = True
            if has_one:
                op_count += 1
            if op_count > max_shapes_per_op:
                break
    return {
        "argvalues": combination,
        "ids": ids,
    }


parametrize_opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv = pytest.mark.parametrize(
    "opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv",
    **_generate_op_device_dtype_biasT_B_Mq_Mkv_H_K_Kv(ALL_FW_OPS),
)
parametrize_opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv__xs = pytest.mark.parametrize(
    "opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv",
    **_generate_op_device_dtype_biasT_B_Mq_Mkv_H_K_Kv(ALL_FW_OPS, max_shapes_per_op=1),
)
parametrize_opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv = pytest.mark.parametrize(
    "opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv",
    **_generate_op_device_dtype_biasT_B_Mq_Mkv_H_K_Kv(ALL_BW_OPS),
)
parametrize_opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv__xs = pytest.mark.parametrize(
    "opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv",
    **_generate_op_device_dtype_biasT_B_Mq_Mkv_H_K_Kv(ALL_BW_OPS, max_shapes_per_op=1),
)


def ref_attention(q, k, v, attn_bias=None, drop_mask=None, p=0.0, scale=None):
    if q.ndim == 4:
        assert p == 0.0
        return ref_attention_bmhk(q, k, v, attn_bias=attn_bias)
    q = q.float()
    k = k.float()
    v = v.float()

    scale = scale if scale is not None else (1 / q.shape[-1] ** 0.5)
    q = q * scale

    attn = q @ k.transpose(-2, -1)
    if attn_bias is not None:
        if isinstance(attn_bias, xformers.ops.AttentionBias):
            # Always create in B,H,Mq,Mk format
            attn_bias_tensor = attn_bias.materialize(
                (q.shape[0], 1, q.shape[1], k.shape[1]),
                device=q.device,
                dtype=torch.float32,
            )
        else:
            attn_bias_tensor = attn_bias
        if attn_bias_tensor.ndim == 4:
            assert q.shape[0] == attn_bias_tensor.shape[0] * attn_bias_tensor.shape[1]
            attn_bias_tensor = attn_bias_tensor.reshape(
                [-1, *attn_bias_tensor.shape[2:]]
            )
        attn = attn + attn_bias_tensor.float()
    attn = attn.softmax(-1)
    if drop_mask is not None:
        attn = attn * (drop_mask / (1 - p))
    return attn @ v


def ref_attention_bmhk(q, k, v, attn_bias, scale=None) -> torch.Tensor:
    assert q.ndim == 4

    def T(t):
        return t.permute((0, 2, 1, 3)).reshape(
            [t.shape[0] * t.shape[2], t.shape[1], t.shape[3]]
        )

    if isinstance(attn_bias, xformers.ops.AttentionBias):
        attn_bias = attn_bias.materialize(
            (q.shape[0], q.shape[2], q.shape[1], k.shape[1]),
            device=q.device,
            dtype=torch.float32,
        ).reshape([q.shape[0] * q.shape[2], q.shape[1], k.shape[1]])
    out = ref_attention(T(q), T(k), T(v), attn_bias, scale=scale)
    out = out.reshape([q.shape[0], q.shape[2], q.shape[1], v.shape[3]])
    return out.permute((0, 2, 1, 3))


def _rand_seqlens(
    bs: int, q_len: int, kv_len: int
) -> Tuple[Sequence[int], Sequence[int]]:
    q_len *= bs
    kv_len *= bs

    seqlens_q: List[int] = []
    seqlens_k: List[int] = []

    step_q = [max(1, q_len // 10), max(2, q_len // 2)]
    step_k = [max(1, kv_len // 10), max(2, kv_len // 2)]
    while sum(seqlens_q) < q_len and sum(seqlens_k) < kv_len:
        seqlens_q.append(random.randrange(*step_q))
        seqlens_k.append(random.randrange(*step_k))
    seqlens_q[-1] = q_len - sum(seqlens_q[:-1])
    seqlens_k[-1] = kv_len - sum(seqlens_k[:-1])
    return seqlens_q, seqlens_k


def create_attn_bias(
    bias_type,
    batch_size: int,
    num_heads: int,
    q_len: int,
    kv_len: int,
    device,
    dtype,
    requires_grad: bool,
    fmt: str,
):
    if bias_type is None or isinstance(None, bias_type):
        return None
    if bias_type is torch.Tensor:
        if fmt == "BMK":
            batch_size *= num_heads
            num_heads = 1
        attn_bias = (
            torch.randn((batch_size, num_heads, 1, kv_len), device=device, dtype=dtype)
            * 3
        )
        attn_bias = attn_bias.expand(batch_size, num_heads, q_len, kv_len)
        if requires_grad:
            attn_bias.requires_grad_(True)
        return attn_bias
    if bias_type is fmha.attn_bias.LowerTriangularMask:
        return fmha.attn_bias.LowerTriangularMask()
    if bias_type is fmha.attn_bias.LowerTriangularMaskWithTensorBias:
        attn_bias = (
            torch.randn(
                (batch_size, num_heads, q_len, kv_len), device=device, dtype=dtype
            )
            * 3
        )
        if requires_grad:
            attn_bias.requires_grad_(True)
        return fmha.attn_bias.LowerTriangularMaskWithTensorBias(attn_bias)
    if bias_type in [
        fmha.attn_bias.BlockDiagonalMask,
        fmha.attn_bias.BlockDiagonalCausalMask,
    ]:
        # This bias is not supported in BMK format
        assert fmt == "BMHK"
        block_diag = fmha.attn_bias.BlockDiagonalMask.from_seqlens(
            *_rand_seqlens(batch_size, q_len, kv_len)
        )
        if bias_type is fmha.attn_bias.BlockDiagonalCausalMask:
            block_diag = block_diag.make_causal()
        return block_diag
    assert False, f"Unsupported bias type: {bias_type}"


def get_bias_grad(attn_bias, clear: bool = False) -> Optional[torch.Tensor]:
    tensor_with_grad: Optional[torch.Tensor] = None
    if isinstance(attn_bias, torch.Tensor):
        tensor_with_grad = attn_bias
    if isinstance(attn_bias, fmha.attn_bias.LowerTriangularMaskWithTensorBias):
        tensor_with_grad = attn_bias._bias
    if tensor_with_grad is not None:
        grad = tensor_with_grad.grad
        if clear:
            tensor_with_grad.grad = None
        return grad
    return None


def create_tensors(
    op: Type[AttentionOpBase],
    device,
    dtype,
    attn_bias_type,
    B,
    q_len,
    kv_len,
    h,
    k,
    kv,
    *,
    attn_bias_requires_grad: bool = False,
    fmt: str = "BMK",
):
    torch.manual_seed(B * q_len + kv_len * k + kv)
    scale = 3
    if fmt == "BMK":
        query = torch.randn((B * h, q_len, k), device=device, dtype=dtype).mul_(scale)
        key = torch.randn((B * h, kv_len, k), device=device, dtype=dtype).mul_(scale)
        value = torch.randn((B * h, kv_len, kv), device=device, dtype=dtype).mul_(scale)
    else:
        assert fmt == "BMHK"
        query = torch.randn((B, q_len, h, k), device=device, dtype=dtype).mul_(scale)
        key = torch.randn((B, kv_len, h, k), device=device, dtype=dtype).mul_(scale)
        value = torch.randn((B, kv_len, h, kv), device=device, dtype=dtype).mul_(scale)

    if fmt == "BMK" and not fmha.common._is_bias_type_supported_in_BMK(attn_bias_type):
        attn_bias_type = None
    attn_bias = None
    if attn_bias_type is not None:
        attn_bias = create_attn_bias(
            attn_bias_type,
            batch_size=B,
            num_heads=h,
            q_len=q_len,
            kv_len=kv_len,
            dtype=dtype,
            device=device,
            requires_grad=attn_bias_requires_grad,
            fmt=fmt,
        )
        if isinstance(attn_bias, fmha.attn_bias.BlockDiagonalMask):
            query, key, value = [
                x.reshape([1, -1, *x.shape[2:]]) for x in [query, key, value]
            ]

    inputs = fmha.Inputs(query=query, key=key, value=value, attn_bias=attn_bias)
    reasons = op.not_supported_reasons(inputs)
    if reasons:
        err_msg = f"{op.NAME}: unsupported ({'/'.join(reasons)})"
        # Ensure we free memory to avoid OOMs
        del query, key, value, attn_bias, inputs
        pytest.skip(err_msg)
    return query, key, value, attn_bias


def bmhk2bmk(tensor) -> torch.Tensor:
    return (
        tensor.permute((0, 2, 1, 3))
        .contiguous()
        .view([tensor.shape[0] * tensor.shape[2], tensor.shape[1], tensor.shape[3]])
    )


def bmk2bmhk(tensor, num_heads: int) -> torch.Tensor:
    return tensor.reshape([-1, num_heads, tensor.shape[1], tensor.shape[2]]).permute(
        (0, 2, 1, 3)
    )


@pytest.mark.parametrize("fmt", ["BMK", "BMHK"])
@pytest.mark.parametrize("packed", [False, True])
@parametrize_opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
def test_forward(
    opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv,
    packed,
    fmt,
):
    (
        op,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
    if packed and not (k == kv and q_len == kv_len):
        pytest.skip(
            f"packed incompatible with `k ({k}) != kv ({kv})` or `q_len ({q_len}) != kv_len ({kv_len})`"
        )
    if fmt == "BMK" and not fmha.common._is_bias_type_supported_in_BMK(bias_type):
        pytest.skip("BMK incompatible with this bias")
    query, key, value, attn_bias = create_tensors(
        *opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv, fmt="BMHK" if packed else fmt
    )

    if packed:
        c = torch.stack([query, key, value], 2)
        if fmt == "BMK":
            # bm3hk -> 3bhmk -> 3Bmk
            c = c.permute(2, 0, 3, 1, 4).view([3, -1, q_len, k])
            query, key, value = c[0], c[1], c[2]
            # Re-create bias in the right format
            attn_bias = create_attn_bias(
                bias_type=bias_type,
                batch_size=batch_size,
                num_heads=h,
                q_len=q_len,
                kv_len=kv_len,
                device=device,
                dtype=dtype,
                requires_grad=False,
                fmt=fmt,
            )
        else:
            # bm3hk -> 3 x bmhk
            query, key, value = xformers.ops.unbind(c, 2)
        assert not query.is_contiguous()

    out = xformers.ops.memory_efficient_attention_forward(
        query, key, value, attn_bias, op=op
    ).float()
    ref = ref_attention(query, key, value, attn_bias)
    assert out.shape == ref.shape, out.shape

    assert_allclose(
        out,
        ref,
        atol=op.ERROR_ATOL[dtype],
        rtol=op.ERROR_RTOL.get(dtype, 1e-5),
    )


@pytest.mark.parametrize("k_len", [5, 6, 32])
@pytest.mark.parametrize("batch_size", [1, 4])
@pytest.mark.parametrize("kv_len", [128, 512])
@pytest.mark.parametrize("q_len", [128, 512])
@pytest.mark.parametrize("device", _devices)
def test_key_query_all_ones(device, q_len, kv_len, batch_size, k_len):
    scale = 3
    query = torch.ones((batch_size, q_len, k_len), device=device)
    key = torch.ones((batch_size, kv_len, k_len), device=device)
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    out = xformers.ops.memory_efficient_attention(query, key, value)
    # this should be equivalent to the average over value
    ref = value.mean(1, keepdim=True).expand_as(query)

    assert_allclose(out, ref, atol=1e-5)


def _block_diag_reshape_lse(
    lse: torch.Tensor, q_seqinfo: fmha.attn_bias._SeqLenInfo
) -> torch.Tensor:
    """LSE can be padded, let's remove the padding"""
    parts = []
    for slice, start, end in zip(
        lse.unbind(0), q_seqinfo.cu_seqlen_py, q_seqinfo.cu_seqlen_py[1:]
    ):
        parts.append(slice[:, : end - start])
    return torch.cat(parts, dim=1).unsqueeze(1)


@parametrize_opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
def test_logsumexp(opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv):
    (
        op,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
    query, key, value, attn_bias = create_tensors(
        *opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv, fmt="BMK"
    )

    _out, lse = xformers.ops.memory_efficient_attention_forward_requires_grad(
        query,
        key,
        value,
        op=op,
        attn_bias=attn_bias,
    )
    attn = (query.float() / k**0.5) @ key.float().transpose(-2, -1)
    if attn_bias is not None:
        if isinstance(attn_bias, xformers.ops.AttentionBias):
            tensor_bias = attn_bias.materialize(
                (query.shape[0], 1, query.shape[1], key.shape[1]),
                device=query.device,
                dtype=torch.float32,
            )
        else:
            assert isinstance(attn_bias, torch.Tensor)
            tensor_bias = attn_bias
        if tensor_bias.ndim == 4:
            tensor_bias = tensor_bias.reshape([-1, *tensor_bias.shape[2:]])
        attn = attn + tensor_bias.float()
    ref_lse = attn.logsumexp(-1)
    if isinstance(attn_bias, fmha.attn_bias.BlockDiagonalMask):
        lse = _block_diag_reshape_lse(lse, attn_bias.q_seqinfo)
    assert_allclose(lse[:, 0, : ref_lse.shape[1]], ref_lse, atol=2e-4)


@pytest.mark.parametrize("fmt", ["BMK", "BMHK"])
@pytest.mark.parametrize("grad_out_contiguous", [False, True])
@parametrize_opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
def test_backward(
    opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv,
    grad_out_contiguous,
    fmt,
):
    (
        op_bw,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
    attn_bias_requires_grad = (
        random.Random(q_len + kv_len * batch_size).randint(0, 1) > 0
    )
    query, key, value, attn_bias = create_tensors(
        *opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv,
        attn_bias_requires_grad=attn_bias_requires_grad,
        fmt=fmt,
    )
    op_fw = (
        sample_random_supported_fw(
            fmha.Inputs(query=query, key=key, value=value, attn_bias=attn_bias),
            seed=q_len * kv + kv_len * k,
        )
        if op_bw != fmha.cutlass.BwOp
        else fmha.cutlass.FwOp
    )
    qkv = None

    if (
        fmt == "BMHK"
        and query.shape[3] == value.shape[3]
        and query.shape[1] == value.shape[1]
    ):
        qkv = torch.stack([query, key, value], 2)
        qkv.requires_grad_(True)
        # bm3hk -> 3 x bmhk
        query, key, value = xformers.ops.unbind(qkv, 2)
        assert not query.is_contiguous()

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    if not op_bw.supports(fmha.Inputs(query, key, value, attn_bias)):
        pytest.skip("inputs not supported")

    out = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias, op=(op_fw, op_bw)
    )

    grad_out = torch.ones_like(out)
    if grad_out_contiguous is False:
        grad_out = torch.tensor([1.0], dtype=query.dtype, device=device)[
            None, None, :
        ].expand_as(out)

    out.backward(grad_out)

    if qkv is None and op_bw == fmha.cutlass.BwOp:
        assert query.stride() == query.grad.stride()

    grads = []
    if qkv is None:
        grads = [query.grad, key.grad, value.grad]
        query.grad = None
        key.grad = None
        value.grad = None
    else:
        grads = [qkv.grad]
        qkv.grad = None
    if attn_bias_requires_grad:
        attn_bias_grad = get_bias_grad(attn_bias, clear=True)
        if attn_bias_grad is not None:
            grads.append(attn_bias_grad)

    ref = ref_attention(query, key, value, attn_bias)
    ref.backward(grad_out)

    assert_allclose(
        out.float(),
        ref.float(),
        "fw pass",
        atol=op_fw.ERROR_ATOL[dtype],
        rtol=op_fw.ERROR_RTOL.get(dtype, 1e-5),
    )

    del out
    del grad_out
    del ref

    atol = op_bw.ERROR_ATOL[dtype]
    rtol = op_bw.ERROR_RTOL[dtype]

    grads_ref = []
    grads_name = []
    if qkv is None:
        assert isinstance(query.grad, torch.Tensor)
        assert isinstance(key.grad, torch.Tensor)
        assert isinstance(value.grad, torch.Tensor)
        grads_ref = [query.grad, key.grad, value.grad]
        grads_name = ["query", "key", "value"]
    else:
        assert isinstance(qkv.grad, torch.Tensor)
        grads_ref = [qkv.grad]
        grads_name = ["qkv"]

    if attn_bias_requires_grad:
        attn_bias_grad = get_bias_grad(attn_bias)
        if attn_bias_grad is not None:
            grads_ref.append(attn_bias.grad)
            grads_name.append("bias")

    del query
    del key
    del value
    del qkv

    assert len(grads_ref) == len(
        grads
    ), "Wrong number of gradients (maybe bias grad didn't backprop?)"
    for name, calc_grad, ref_grad in zip(grads_name, grads, grads_ref):
        assert_allclose(
            calc_grad,
            ref_grad,
            msg=f"{op_fw.NAME}+{op_bw.NAME}:{name}",
            atol=atol,
            rtol=rtol,
        )


def _vec_binom_test(x, n, p):
    """
    vectorized implementation of scipy.stats.binom_test
    this makes our tests much faster
    reference: https://github.com/scipy/scipy/blob/v1.8.0/scipy/stats/_morestats.py#L2609-L2702
    """
    import numpy as np
    from scipy.stats import distributions

    x = np.atleast_1d(x)
    d = distributions.binom.pmf(x, n, p)[:, None]
    rerr = 1 + 1e-7
    # x < p * n case
    i = np.arange(np.ceil(p * n), n + 1)
    y = np.sum(distributions.binom.pmf(i, n, p) <= d * rerr, axis=1)
    pval1 = distributions.binom.cdf(x, n, p) + distributions.binom.sf(n - y, n, p)

    # other case
    i = np.arange(np.floor(p * n) + 1)
    y = np.sum(distributions.binom.pmf(i, n, p) <= d * rerr, axis=1)
    pval2 = distributions.binom.cdf(y - 1, n, p) + distributions.binom.sf(x - 1, n, p)

    pval = np.where(x < p * n, pval1, pval2)
    pval = np.minimum(1.0, pval)
    return pval


def _get_drop_mask(op, batch_size, q_len, kv_len, p, device):
    if op == fmha.cutlass.FwOp:
        mask = torch.empty((batch_size, 1, q_len, kv_len), device=device)
        rand_uniform = torch.ops.xformers._cutlass_rand_uniform(p, mask)
        mask = (rand_uniform > p).to(torch.float32)
        mask = mask.reshape(batch_size, q_len, kv_len)
    else:
        mask = torch.empty((batch_size, q_len, kv_len), device=device)
        mask = torch.ops.xformers._temp_dropout(mask, p)

    return mask


@cuda_only
@pytest.mark.parametrize("seed", [42, 124])
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
@pytest.mark.parametrize("op", ALL_FW_OPS, ids=list(map(lambda t: t.NAME, ALL_FW_OPS)))
def test_dropout(op, q_len, kv_len, batch_size, k_len, p, seed):
    device = "cuda"
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    attn_bias = None

    inputs_for_support_check = fmha.Inputs(query, key, value, attn_bias, p, None)
    if not op.supports(inputs_for_support_check):
        del query, key, value, attn_bias
        pytest.skip(f"{op.NAME}: unsupported input")

    torch.manual_seed(seed)
    out = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias, p, op=(op, None)
    )

    torch.manual_seed(seed)
    out2 = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias, p, op=(op, None)
    )

    assert_allclose(out, out2, "dropout reproducibility")

    torch.manual_seed(seed)
    mask = _get_drop_mask(op, batch_size, q_len, kv_len, p, device)
    ref = ref_attention(query, key, value, attn_bias, mask, p)
    assert_allclose(out, ref, atol=2e-4), f"{(out - ref).abs().max()}"

    num_trials = 1000
    p_val_tol = 1e-6
    keep_prob = 1 - p
    masks = []
    for i in range(num_trials):
        mask = _get_drop_mask(op, batch_size, q_len, kv_len, p, device)
        masks.append(mask.clone().cpu())
    masks = torch.stack(masks, dim=0)
    p_value = binom_test(masks.sum(), masks.numel(), p=keep_prob)
    assert p_value > p_val_tol, p_value
    masks = masks.sum(0).flatten()
    p_values = _vec_binom_test(masks, num_trials, p=keep_prob)
    assert all(p_values > p_val_tol)


def _test_dropout_backward(q_len, kv_len, batch_size, k_len, p, op, dtype):
    scale = 3
    device = "cuda"
    query = torch.randn((batch_size, q_len, k_len), device=device, dtype=dtype) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device, dtype=dtype) * scale

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)

    assert op.supports(fmha.Inputs(query=query, key=key, value=value, p=p))

    seed = 42
    torch.manual_seed(seed)
    out = xformers.ops.memory_efficient_attention(query, key, value, p=p, op=(op, None))

    out.backward(grad_out)

    # Only test correctness for small_k
    if op is not fmha.small_k.FwOp:
        return

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    torch.manual_seed(seed)
    mask = _get_drop_mask(op, batch_size, q_len, kv_len, p, device)

    ref = ref_attention(query, key, value, None, mask, p)
    ref.backward(grad_out)

    # there is some extra precision loss in the CPU implementation due to an
    # extra accumulation step in grad_q, which is not present in the CUDA
    # implementation
    atol = 5e-4 if device == "cuda" else 6e-4
    assert_allclose(grad_q, query.grad, "grad_q", atol=atol)
    assert_allclose(grad_k, key.grad, "grad_k", atol=atol)
    assert_allclose(grad_v, value.grad, "grad_v", atol=atol)


@cuda_only
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [5, 6, 32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
def test_dropout_backward_small_k(q_len, kv_len, batch_size, k_len, p):
    _test_dropout_backward(
        q_len, kv_len, batch_size, k_len, p, op=fmha.small_k.FwOp, dtype=torch.float32
    )


@sm75_or_better_only
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [16, 32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
def test_dropout_backward_flash(q_len, kv_len, batch_size, k_len, p):
    _test_dropout_backward(
        q_len, kv_len, batch_size, k_len, p, op=fmha.flash.FwOp, dtype=torch.float16
    )


@cuda_only
@pytest.mark.parametrize("p", [0.3, 0.7])
@pytest.mark.parametrize("k_len", [16, 32])
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("kv_len", [3, 15, 32, 33])
@pytest.mark.parametrize("q_len", [2, 33])
def test_dropout_backward_cutlass(q_len, kv_len, batch_size, k_len, p):
    _test_dropout_backward(
        q_len, kv_len, batch_size, k_len, p, op=fmha.cutlass.FwOp, dtype=torch.float16
    )


@pytest.mark.parametrize("k_len", [32])
@pytest.mark.parametrize("batch_size", [1])
@pytest.mark.parametrize("kv_len", [3 * 32])
@pytest.mark.parametrize("q_len", [3 * 32])
@pytest.mark.parametrize("device", _devices)
def test_memory_efficient_attention_full_block_masked(
    device, q_len, kv_len, batch_size, k_len
):
    scale = 3
    query = torch.randn((batch_size, q_len, k_len), device=device) * scale
    key = torch.randn((batch_size, kv_len, k_len), device=device) * scale
    value = torch.randn((batch_size, kv_len, k_len), device=device) * scale

    # in this case, most of the blocks in a row get masked
    attn_bias = torch.full((3, 32), float("-inf"), device=device)
    attn_bias[:2, :4] = 0
    attn_bias = attn_bias.flatten()[None, None, :].expand(1, q_len, -1)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias)
    ref = ref_attention(query, key, value, attn_bias)

    assert_allclose(out, ref, atol=2e-4)

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    grad_out = torch.ones_like(query)

    out = xformers.ops.memory_efficient_attention(query, key, value, attn_bias)
    out.backward(grad_out)

    grad_q = query.grad
    grad_k = key.grad
    grad_v = value.grad

    query.grad = None
    key.grad = None
    value.grad = None

    ref = ref_attention(query, key, value, attn_bias)
    ref.backward(grad_out)

    # there is some extra precision loss in the CPU implementation due to an
    # extra accumulation step in grad_q, which is not present in the CUDA
    # implementation
    atol = 5e-4 if device == "cuda" else 6e-4
    assert_allclose(grad_q, query.grad, "grad_q", atol=atol)
    assert_allclose(grad_k, key.grad, "grad_k", atol=atol)
    assert_allclose(grad_v, value.grad, "grad_v", atol=atol)


@pytest.mark.parametrize("fmt", ["BMK", "BMHK"])
@parametrize_opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv__xs
def test_lowlevel_api_shapes(opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv, fmt):
    query, key, value, attn_bias = create_tensors(
        *opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv, fmt=fmt
    )
    grad_out = torch.ones_like(query)
    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    out, lse = xformers.ops.memory_efficient_attention_forward_requires_grad(
        query, key, value, attn_bias
    )
    assert out.ndim == query.ndim
    dq, dk, dv = xformers.ops.memory_efficient_attention_backward(
        grad_out, out, lse, query, key, value, attn_bias
    )
    assert dq.shape == query.shape
    assert dk.shape == key.shape
    assert dv.shape == value.shape


@parametrize_opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv__xs
def test_cuda_streams(
    opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv,
):
    (
        op,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
    if device != "cuda":
        pytest.skip("Not CUDA")
    # Needs to be big enough so kernels take some time
    # as we are trying to do a race-condition here
    q_len = 1024
    kv_len = 1024
    bias_type = None
    opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv = [
        op,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ]
    s_hipri = torch.cuda.Stream(priority=-1)
    s_lopri = torch.cuda.Stream(priority=0)
    with torch.cuda.stream(s_lopri):
        query, key, value, attn_bias = create_tensors(
            *opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv, fmt="BMHK"
        )
        # Queue a lot of kernels
        for i in range(20):
            query = query.relu()
        query = query * 2
    s_hipri.wait_stream(s_lopri)
    with torch.cuda.stream(s_hipri):
        out = xformers.ops.memory_efficient_attention(query, key, value, op=(op, None))
        # This will run in hi-pri AFTER the kernel if it
        # runs on the correct stream
        out = out / 2
    torch.cuda.synchronize()
    ref = ref_attention(query, key, value) / 2
    assert out.shape == ref.shape, out.shape

    assert_allclose(
        out.float(),
        ref.float(),
        atol=op.ERROR_ATOL[dtype],
        rtol=op.ERROR_RTOL.get(dtype, 1e-5),
    )


@parametrize_opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv__xs
def test_custom_scale(opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv):
    p = 0.0
    scale = 1.0

    (
        op_bw,
        device,
        dtype,
        _,
        _,
        q_len,
        kv_len,
        _,
        k,
        _,
    ) = opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
    torch.manual_seed(q_len + kv_len + k)
    if device != "cuda":
        pytest.skip("Not CUDA")

    query, key, value, attn_bias = create_tensors(
        *opBW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv, fmt="BMK"
    )
    inputs = fmha.Inputs(
        query=query, key=key, value=value, attn_bias=attn_bias, scale=scale
    )
    op_fw = sample_random_supported_fw(inputs, seed=q_len * k + kv_len * k)
    grad_out = torch.ones_like(query)
    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    reasons = op_fw.not_supported_reasons(inputs)
    if reasons:
        pytest.skip(f"{op_fw.NAME}: unsupported ({'/'.join(reasons)})")
    reasons = op_bw.not_supported_reasons(inputs)
    if reasons:
        pytest.skip(f"{op_bw.NAME}: unsupported ({'/'.join(reasons)})")

    # NOTE: we still need to scale the inputs to not blowup
    # the pre-softmax values (numerical stability)
    s = k**-0.5
    out = xformers.ops.memory_efficient_attention(
        query * s, key, value, attn_bias, p, scale, op=(op_fw, op_bw)
    )
    out.backward(grad_out)
    grad_q, grad_k, grad_v = query.grad, key.grad, value.grad
    query.grad = key.grad = value.grad = None

    ref = ref_attention(query * s, key, value, attn_bias, None, p, scale)
    ref.backward(grad_out)
    ref_grad_q, ref_grad_k, ref_grad_v = query.grad, key.grad, value.grad
    query.grad = key.grad = value.grad = None

    atol = op_fw.ERROR_ATOL[dtype]
    rtol = op_fw.ERROR_RTOL[dtype]
    assert_allclose(out.float(), ref.float(), "out", atol=atol, rtol=rtol)
    atol = op_bw.ERROR_ATOL[dtype]
    rtol = op_bw.ERROR_RTOL[dtype]
    assert_allclose(grad_q, ref_grad_q, "grad_q", atol=atol, rtol=rtol)
    assert_allclose(grad_k, ref_grad_k, "grad_k", atol=atol, rtol=rtol)
    assert_allclose(grad_v, ref_grad_v, "grad_v", atol=atol, rtol=rtol)


def apply_attention(query, key, value, attn_bias, op_fw, proj):
    x = xformers.ops.memory_efficient_attention(
        query, key, value, attn_bias=attn_bias, op=(op_fw, None)
    )
    x = proj(x)
    return x


@pytest.mark.parametrize("use_reentrant", [False, True])
@parametrize_opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv__xs
def test_grad_checkpointing(
    opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv,
    use_reentrant,
):
    fmt = "BMHK"
    (
        op,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    ) = opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv
    bias_type = None
    opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv = (
        op,
        device,
        dtype,
        bias_type,
        batch_size,
        q_len,
        kv_len,
        h,
        k,
        kv,
    )
    query, key, value, attn_bias = create_tensors(
        *opFW_device_dtype_biasT_B_Mq_Mkv_H_K_Kv,
        fmt=fmt,
    )
    qkv = None

    if (
        fmt == "BMHK"
        and query.shape[3] == value.shape[3]
        and query.shape[1] == value.shape[1]
    ):
        qkv = torch.stack([query, key, value], 2)
        qkv.requires_grad_(True)
        # bm3hk -> 3 x bmhk
        query, key, value = xformers.ops.unbind(qkv, 2)
        assert not query.is_contiguous()

    query.requires_grad_(True)
    key.requires_grad_(True)
    value.requires_grad_(True)

    proj = torch.nn.Linear(kv, k, device=device, dtype=dtype)

    x = query
    for _ in range(5):
        x = checkpoint(
            apply_attention,
            x,
            key,
            value,
            attn_bias,
            op,
            proj,
            use_reentrant=use_reentrant,
        )
    x.mean().backward()


ALL_FW_OPS_NO_SMALLK = [op for op in ALL_FW_OPS if op is not fmha.small_k.FwOp]


@pytest.mark.parametrize(
    "op", ALL_FW_OPS_NO_SMALLK, ids=[op.NAME for op in ALL_FW_OPS_NO_SMALLK]
)
def test_unsupported_cpu(op: Type[fmha.AttentionFwOpBase]):
    q = torch.empty([1, 1, 1, 32])
    with pytest.raises(ValueError):
        fmha.memory_efficient_attention(q, q, q, op=(op, None))


@cuda_only
@pytest.mark.parametrize(
    "op", ALL_FW_OPS_NO_SMALLK, ids=[op.NAME for op in ALL_FW_OPS_NO_SMALLK]
)
def test_unsupported_stride_lastdim(op: Type[fmha.AttentionFwOpBase]):
    q = torch.empty([1, 1, 32, 4], device="cuda", dtype=torch.float16).permute(
        0, 1, 3, 2
    )
    try:
        fmha.memory_efficient_attention(q, q, q, op=(op, None))
    except ValueError:
        q = q.contiguous()
        fmha.memory_efficient_attention(q, q, q, op=(op, None))


@cuda_only
@pytest.mark.parametrize(
    "op", ALL_FW_OPS_NO_SMALLK, ids=[op.NAME for op in ALL_FW_OPS_NO_SMALLK]
)
def test_unsupported_stride_alignment(op: Type[fmha.AttentionFwOpBase]):
    q = torch.empty([1, 2, 2, 33], device="cuda", dtype=torch.float16)[:, :, :, :32]
    try:
        fmha.memory_efficient_attention(q, q, q, op=(op, None))
    except ValueError:
        q = q.contiguous()
        fmha.memory_efficient_attention(q, q, q, op=(op, None))


@sm75_or_better_only
def test_unsupported_dropout_combine_flash_cutlass() -> None:
    q = torch.empty(
        [1, 4, 1, 16], device="cuda", dtype=torch.float16, requires_grad=True
    )
    with pytest.raises(ValueError):
        out = fmha.memory_efficient_attention(
            q, q, q, p=0.1, op=(fmha.cutlass.FwOp, fmha.flash.BwOp)
        )
        out.backward(out)
    with pytest.raises(ValueError):
        out = fmha.memory_efficient_attention(
            q, q, q, p=0.1, op=(fmha.flash.FwOp, fmha.cutlass.BwOp)
        )
        out.backward(out)


def test_attn_bias_causal() -> None:
    m = -math.inf
    causal_mask = torch.tensor([[0, m], [0, 0], [0, 0]])
    tensor_bias = torch.tensor([[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]])

    attn_bias = fmha.attn_bias.LowerTriangularMask()
    assert_allclose(attn_bias.materialize(causal_mask.shape), causal_mask, "causal")
    attn_bias = attn_bias.add_bias(tensor_bias)
    assert_allclose(
        attn_bias.materialize(causal_mask.shape),
        tensor_bias + causal_mask,
        "causal+tensor_bias",
    )


def test_attn_bias_torch_tensor() -> None:
    tensor_bias = torch.tensor([[1.0, 2.0, 3.0], [3.0, 4.0, 5.0]])
    attn_bias = fmha.attn_bias.LowerTriangularMaskWithTensorBias(tensor_bias)
    m = -math.inf
    causal_bias = torch.tensor([[0, m, m], [0, 0, m]])
    assert_allclose(
        attn_bias.materialize((2, 3)), causal_bias + tensor_bias, "tensor_bias+causal"
    )


def test_attn_bias_blockdiag() -> None:
    queries = [
        torch.randn([1, 3, 1, 8]),
        torch.randn([1, 2, 1, 8]),
        torch.randn([1, 5, 1, 8]),
    ]
    attn_bias, q = fmha.BlockDiagonalMask.from_tensor_list(queries)

    # Verify mask
    as_tensor = attn_bias.materialize((10, 10))
    assert int((as_tensor != -math.inf).sum().item()) == 3 * 3 + 2 * 2 + 5 * 5
    assert_allclose(as_tensor[0:3, 0:3], torch.zeros([3, 3]), "batch0")
    assert_allclose(as_tensor[3:5, 3:5], torch.zeros([2, 2]), "batch1")
    assert_allclose(as_tensor[5:, 5:], torch.zeros([5, 5]), "batch2")

    # Verify we can split it back
    queries2 = attn_bias.split(q)
    assert len(queries) == len(queries2)
    for q1, q2 in zip(queries, queries2):
        assert_allclose(q1, q2)


def test_attn_bias_blockdiag_batched() -> None:
    queries = [
        torch.randn([1, 3, 1, 8]),
        torch.randn([3, 2, 1, 8]),
        torch.randn([1, 5, 1, 8]),
    ]
    attn_bias, q = fmha.BlockDiagonalMask.from_tensor_list(queries)

    # Verify mask
    as_tensor = attn_bias.materialize((14, 14))
    assert int((as_tensor != -math.inf).sum().item()) == 3 * 3 + 3 * 2 * 2 + 5 * 5
    assert_allclose(as_tensor[0:3, 0:3], torch.zeros([3, 3]), "batch0")
    assert_allclose(as_tensor[3:5, 3:5], torch.zeros([2, 2]), "batch1.0")
    assert_allclose(as_tensor[5:7, 5:7], torch.zeros([2, 2]), "batch1.1")
    assert_allclose(as_tensor[7:9, 7:9], torch.zeros([2, 2]), "batch1.2")
    assert_allclose(as_tensor[9:, 9:], torch.zeros([5, 5]), "batch2")

    # Verify we can split it back
    queries2 = attn_bias.split(q)
    assert len(queries) == len(queries2)
    for q1, q2 in zip(queries, queries2):
        assert_allclose(q1, q2)


def test_attn_bias_blockdiag_crossattn_causal() -> None:
    # Q / KV have different seqlen
    list_q = [
        torch.randn([1, 3, 1, 8]),
        torch.randn([2, 1, 1, 8]),
    ]
    list_k = [
        torch.randn([1, 2, 1, 8]),
        torch.randn([2, 3, 1, 8]),
    ]

    attn_bias, q, k, _ = fmha.attn_bias.BlockDiagonalMask.from_tensor_lists_qkv(
        list_q, list_k
    )

    # Verify mask
    as_tensor = attn_bias.materialize((q.shape[1], k.shape[1]))
    assert int((as_tensor != -math.inf).sum().item()) == 3 * 2 + 2 * 3 * 1
    assert_allclose(as_tensor[0:3, 0:2], torch.zeros([3, 2]), "batch0")
    assert_allclose(as_tensor[3:4, 2:5], torch.zeros([1, 3]), "batch1.0")
    assert_allclose(as_tensor[4:, 5:], torch.zeros([1, 3]), "batch1.1")

    # Also test causal version
    as_tensor = attn_bias.make_causal().materialize((q.shape[1], k.shape[1]))
    assert_allclose(
        as_tensor[3:4, 2:5],
        fmha.attn_bias.LowerTriangularMask().materialize((1, 3)),
        "batch1.0[causal]",
    )

    # Verify we can split it back
    list_q2 = attn_bias.split_queries(q)
    assert len(list_q) == len(list_q2)
    for q1, q2 in zip(list_q, list_q2):
        assert_allclose(q1, q2)
    with pytest.raises(ValueError):
        attn_bias.split_queries(k)
    list_k2 = attn_bias.split_kv(k)
    assert len(list_k) == len(list_k2)
    for k1, k2 in zip(list_k, list_k2):
        assert_allclose(k1, k2)


def test_attn_bias_from_seqlens() -> None:
    bias = fmha.attn_bias.BlockDiagonalMask.from_seqlens([3, 5, 1])
    out = bias.split(torch.randn([1, 3 + 5 + 1, 16]))
    assert len(out) == 3
    assert tuple(out[0].shape) == (1, 3, 16)
