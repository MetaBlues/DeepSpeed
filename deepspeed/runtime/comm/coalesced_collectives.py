# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
"""
batched collective operations for overhead amortization and better
bandwidth utilization
"""

import math
from typing import List
import torch
from torch import Tensor
from deepspeed import comm as dist
# NOTE: Use torch.distributed's ProcessGroup class until we have our own.
from torch.distributed import ProcessGroup, all_to_all_single
from deepspeed.accelerator import get_accelerator
from deepspeed.utils import instrument_w_nvtx
from deepspeed.ops import op_builder


def _torch_reduce_scatter_fn(input_tensor: Tensor, output_tensor: Tensor, group=None, async_op=False, prof=False):
    return instrument_w_nvtx(dist.reduce_scatter_fn)(output_tensor, input_tensor, group=group, async_op=False)


quantizer_module = None


@instrument_w_nvtx
@torch.no_grad()
def all_to_all_quant_reduce(tensors: List[Tensor], groups: {}) -> List[Tensor]:
    global quantizer_module
    if quantizer_module is None:
        quantizer_module = op_builder.QuantizerBuilder().load()
    local_world_size = get_accelerator().device_count()
    global_world_size = dist.get_world_size()
    num_nodes = global_world_size // local_world_size
    this_rank = dist.get_rank()
    intra_idx = int(this_rank / local_world_size)
    inter_idx = this_rank % local_world_size
    output_lst: List[Tensor] = [None] * len(tensors)
    intra_group_cache = dict()
    max_elems_per_intra_group = 40960  # 40k limit
    for idx, tensor in enumerate(tensors):
        if tensor.dim() == 1:
            output_lst[idx] = reduce_scatter_coalesced([tensor])[0]
            continue
        else:
            # max_elems_per_intra_in_group = 40960(40k limit) = (input_numel / intra_quant_group)
            # max_elems_per_inter_out_group = 49152(48k limit) = (input_numel / num_nodes / intra_quant_group)
            # (input_numel / intra_quant_group) <= 40k
            # (input_numel / intra_quant_group) / num_nodes <= 48k
            # final constraint: (input_numel / intra_quant_group) <= 40k
            padding_size = (global_world_size -
                            tensor.numel() % global_world_size) if tensor.numel() % global_world_size != 0 else 0
            aligned_size = tensor.numel() + padding_size
            flat_tensor = torch.empty(aligned_size, dtype=tensor.dtype, device=tensor.device)
            flat_tensor.narrow(0, 0, tensor.numel()).copy_(tensor.view(-1).data)
            if aligned_size in intra_group_cache.keys():
                intra_quant_group = intra_group_cache[aligned_size]
            else:
                intra_quant_group = global_world_size
                while intra_quant_group < aligned_size:
                    if aligned_size % intra_quant_group == 0 \
                        and aligned_size / intra_quant_group <= max_elems_per_intra_group:
                        break
                    intra_quant_group += global_world_size
                while True:
                    if aligned_size % (intra_quant_group *
                                       2) == 0 and aligned_size / intra_quant_group > max_elems_per_intra_group:
                        intra_quant_group *= 2
                    else:
                        break
                assert (
                    intra_quant_group % global_world_size == 0
                ), f"  Quantized grad requires intra_quant_group be a multiple of global_world_size. Yet group size {intra_quant_group} cannot be divided by {global_world_size}"
                assert (aligned_size / intra_quant_group <= max_elems_per_intra_group), \
                    f"  Elems_per_group {aligned_size} / {intra_quant_group} is larger than 40k."
                assert tensor.numel(
                ) > intra_quant_group, f"  Adaptive grouping algorithm cannot find a group size for grad tensor of size {tensor.numel()}"
                intra_group_cache[aligned_size] = intra_quant_group
            inter_quant_group = intra_quant_group // local_world_size
            intra_quant_int4, intra_q_scales = quantizer_module.swizzle_quant(flat_tensor, intra_quant_group, 4,
                                                                              quantizer_module.Symmetric, 1, num_nodes,
                                                                              local_world_size)
            local_output = torch.empty_like(intra_quant_int4)
            scale_output = torch.empty_like(intra_q_scales)
            all_to_all_single(local_output, intra_quant_int4, group=groups[f'local_{intra_idx}'])
            all_to_all_single(scale_output, intra_q_scales, group=groups[f'local_{intra_idx}'])
            global_input_tensor, global_scales = quantizer_module.quantized_reduction(
                local_output, scale_output, intra_quant_group, inter_quant_group, 4, quantizer_module.Symmetric,
                local_world_size)
            global_output = torch.empty_like(global_input_tensor)
            global_scale_output = torch.empty_like(global_scales)
            all_to_all_single(global_output, global_input_tensor, group=groups[f'global_{inter_idx}'])
            all_to_all_single(global_scale_output, global_scales, group=groups[f'global_{inter_idx}'])
            final_output = quantizer_module.dequantize(global_output, global_scale_output, global_scale_output.numel(),
                                                       4, quantizer_module.Symmetric)
            partition_size = aligned_size // global_world_size
            start = partition_size * this_rank
            end = start + partition_size
            if start < tensor.numel() and end <= tensor.numel():
                output_lst[idx] = (sum(list(final_output.chunk(num_nodes))) / num_nodes).view(-1)
                assert output_lst[idx].numel() == partition_size
            else:
                if start < tensor.numel():
                    elements_to_copy = tensor.numel() - start
                    output_lst[idx] = (sum(list(final_output.chunk(num_nodes))) / num_nodes).view(-1).narrow(
                        0, 0, elements_to_copy)
                    assert this_rank == global_world_size - 1
    return output_lst


@instrument_w_nvtx
@torch.no_grad()
def reduce_scatter_coalesced(
    tensors: List[Tensor],
    group: ProcessGroup = None,
) -> List[Tensor]:
    """simultaneously reduce-scatter a list of tensors - this can be done more
    efficiently than individual reduce scatter calls
    TODO. see if PyTorch team wants a c++ version of this for ProcessGroupNCCL
    """
    this_rank = dist.get_rank(group)
    world_sz = dist.get_world_size(group)

    partition_lst_for_each_tensor = [None] * len(tensors)
    for tensor_idx, tensor in enumerate(tensors):
        flattened_tensor = tensor.view(-1)
        chunk_sz = math.ceil(tensor.numel() / world_sz)
        partition_lst_for_each_tensor[tensor_idx] = [
            flattened_tensor[rank * chunk_sz:rank * chunk_sz + chunk_sz] for rank in range(0, world_sz)
        ]

    padded_partition_sz_for_each_tensor = tuple(math.ceil(t.numel() / world_sz) for t in tensors)

    if len(tensors) == 1 and tensors[0].numel() % world_sz == 0:
        # if there's only one tensor being reduced and we don't need to pad
        # we have an opportunity to avoid a memory allocation
        tensor_partition_flat_buffer = tensors[0].view(-1)
    else:
        # interleave tensor partitions such that the correct reduced partitions of each tensor
        # end up at each rank
        tensor_partitions_lst_with_padding = []
        for rank in range(world_sz):
            for tensor_idx in range(len(tensors)):
                # add tensor content
                tensor_chunk = partition_lst_for_each_tensor[tensor_idx][rank]
                tensor_partitions_lst_with_padding.append(tensor_chunk)

                # add padding if necessary
                padding_sz = padded_partition_sz_for_each_tensor[tensor_idx] - tensor_chunk.numel()
                if padding_sz > 0:
                    tensor_partitions_lst_with_padding.append(
                        torch.empty(padding_sz, dtype=tensor_chunk.dtype, device=tensor_chunk.device))

        tensor_partition_flat_buffer = instrument_w_nvtx(torch.cat)(tensor_partitions_lst_with_padding)

    tensor_partition_flat_buffer.div_(world_sz)  # pre-divide
    tensor_partition_buffer_for_each_rank: List[Tensor] = torch.chunk(tensor_partition_flat_buffer, world_sz)

    # batched reduce-scatter call
    _torch_reduce_scatter_fn(tensor_partition_flat_buffer,
                             tensor_partition_buffer_for_each_rank[this_rank],
                             group=group)

    # reverse procedure of the interleaving done previously, done on the
    # result of the batched reduce-scatter
    output_lst: List[Tensor] = [None] * len(tensors)
    offset = 0
    for tensor_idx in range(len(tensors)):
        output_lst[tensor_idx] = tensor_partition_buffer_for_each_rank[this_rank].narrow(
            0, offset, partition_lst_for_each_tensor[tensor_idx][this_rank].numel())

        offset += padded_partition_sz_for_each_tensor[tensor_idx]
    return output_lst
