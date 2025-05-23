# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

from typing import Union

import pytest
import torch
from composer import Trainer
from composer.utils import get_device
from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import \
    CheckpointWrapper

from llmfoundry.models.mpt.modeling_mpt import ComposerMPTCausalLM


@pytest.mark.world_size(2)
@pytest.mark.gpu
@pytest.mark.parametrize('activation_checkpointing', [True, False])
@pytest.mark.parametrize(
    'activation_checkpointing_target',
    [
        'grouped_query_attention',
        [],
        ['grouped_query_attention'],
        {
            'mptblock': [1],
            'grouped_query_attention': 'first-1, last-1',
        },
    ],
)
def test_fsdp_act_checkpoint(
    activation_checkpointing: bool,
    activation_checkpointing_target: Union[list, str, dict],
):
    device = get_device('gpu')
    model_cfg = {
        'name': 'mpt_causal_lm',
        'd_model': 128,
        'n_heads': 4,
        'n_layers': 3,
        'expansion_ratio': 1,
        'max_seq_len': 16,
        'vocab_size': 50368,
        'attn_config': {
            'attn_type': 'grouped_query_attention',
            'kv_n_heads': 2,
        },
        'activation_checkpointing_target': activation_checkpointing_target,
    }

    fsdp_config = {
        'activation_checkpointing': activation_checkpointing,
        'activation_checkpointing_reentrant': False,
        'activation_cpu_offload': False,
    }

    model = ComposerMPTCausalLM(**model_cfg)
    model = device.module_to_device(model)

    trainer = Trainer(
        model=model,
        device='gpu',
        parallelism_config={'fsdp': fsdp_config},
    )

    assert trainer.state.fsdp_enabled

    # Asserting that all of these are modules and not Tensors
    assert isinstance(trainer.state.model.model, torch.nn.Module)
    assert isinstance(
        trainer.state.model.model._fsdp_wrapped_module,
        torch.nn.Module,
    )
    assert isinstance(
        trainer.state.model.model._fsdp_wrapped_module.transformer,
        torch.nn.Module,
    )
    assert isinstance(
        trainer.state.model.model._fsdp_wrapped_module.transformer.blocks,
        torch.nn.ModuleList,
    )

    if not activation_checkpointing:
        assert not isinstance(
            trainer.state.model.model._fsdp_wrapped_module.transformer.
            blocks[0],
            CheckpointWrapper,
        )
    elif (not activation_checkpointing_target):
        module = trainer.state.model.model._fsdp_wrapped_module.transformer.blocks[
            0]._fsdp_wrapped_module
        assert isinstance(module, CheckpointWrapper)
    elif activation_checkpointing_target == [
        'grouped_query_attention',
    ] or activation_checkpointing_target == 'grouped_query_attention':
        assert isinstance(
            trainer.state.model.model._fsdp_wrapped_module.transformer.
            blocks[0]._fsdp_wrapped_module.attn, # type: ignore
            CheckpointWrapper,
        )
    elif activation_checkpointing_target == {
        'mptblock': [1],
        'grouped_query_attention': 'first-1, last-1',
    }:
        assert isinstance(
            trainer.state.model.model._fsdp_wrapped_module.transformer.
            blocks[0]._fsdp_wrapped_module.attn, # type: ignore
            CheckpointWrapper,
        )
        assert isinstance(
            trainer.state.model.model._fsdp_wrapped_module.transformer.
            blocks[1]._fsdp_wrapped_module, # type: ignore
            CheckpointWrapper,
        )
        assert isinstance(
            trainer.state.model.model._fsdp_wrapped_module.transformer.
            blocks[2]._fsdp_wrapped_module.attn, # type: ignore
            CheckpointWrapper,
        )
    else:
        raise ValueError(
            f'Unknown activation_checkpointing_target: {activation_checkpointing_target}',
        )
