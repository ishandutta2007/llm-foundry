# Copyright 2022 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

import contextlib
import copy
import functools
import logging
import os
import re
import warnings
from collections import OrderedDict
from typing import Any, ContextManager, Iterable, Optional, Union

import torch
from composer.core import Algorithm, Callback, Evaluator
from composer.loggers import LoggerDestination
from composer.models import ComposerModel
from composer.optim.scheduler import ComposerScheduler
from composer.utils import dist
from omegaconf import DictConfig
from omegaconf import OmegaConf as om
from torch.distributed.checkpoint import LoadPlanner, SavePlanner
from torch.distributed.tensor.parallel.style import ParallelStyle
from torch.optim.optimizer import Optimizer
from torchmetrics import Metric
from transformers import AutoTokenizer, PreTrainedTokenizerBase

from llmfoundry import registry
from llmfoundry.callbacks import EvalGauntlet
from llmfoundry.data.dataloader import build_dataloader
from llmfoundry.eval.datasets.in_context_learning_evaluation import (
    get_icl_task_dataloader,
)
from llmfoundry.utils.config_utils import to_dict_container, to_list_container
from llmfoundry.utils.registry_utils import construct_from_registry
from llmfoundry.utils.warnings import experimental_function

log = logging.getLogger(__name__)

__all__ = [
    'build_algorithm',
    'build_callback',
    'build_evaluators',
    'build_icl_data_and_gauntlet',
    'build_icl_evaluators',
    'build_logger',
    'build_optimizer',
    'build_scheduler',
    'build_tokenizer',
    'build_composer_model',
    'build_metric',
    'build_tp_strategies',
]


def build_evaluators(
    eval_loader_config: Optional[Union[dict[str, Any], list[dict[str, Any]]]],
    icl_tasks_config: Optional[Union[str, list[dict[str, Any]]]],
    eval_gauntlet_config: Optional[Union[str, dict[str, Any]]],
    *,
    tokenizer: Optional[PreTrainedTokenizerBase],
    device_eval_batch_size: Union[int, float],
    icl_seq_len: int,
    icl_subset_num_batches: Optional[int],
) -> tuple[list[Evaluator], list[str], Optional[EvalGauntlet]]:

    evaluators = []
    if eval_loader_config is not None:
        evaluators = build_eval_loaders(
            eval_loader_config,
            tokenizer,
            device_eval_batch_size,
        )

    logger_keys = []
    eval_gauntlet_callback = None
    if icl_tasks_config is not None:
        if tokenizer is None:
            raise ValueError('Tokenizer is required for icl tasks')
        if not isinstance(device_eval_batch_size, int):
            raise ValueError(
                'device_eval_batch_size should be an int for icl tasks.',
            )

        icl_evaluators, logger_keys, eval_gauntlet_callback = build_icl_data_and_gauntlet(
            icl_tasks_config,
            eval_gauntlet_config,
            tokenizer,
            device_eval_batch_size,
            icl_seq_len,
            icl_subset_num_batches,
        )
        evaluators.extend(icl_evaluators)

    return evaluators, logger_keys, eval_gauntlet_callback


def build_eval_loaders(
    eval_loader_config: Union[dict[str, Any], list[dict[str, Any]]],
    tokenizer: Optional[PreTrainedTokenizerBase],
    device_eval_batch_size: Union[int, float],
) -> list[Evaluator]:
    evaluators: list[Evaluator] = []
    if isinstance(eval_loader_config, list):
        eval_configs = eval_loader_config
        is_multi_eval = True
    elif isinstance(eval_loader_config, dict):
        eval_configs = [eval_loader_config]
        is_multi_eval = False
    else:
        raise ValueError(
            f'Got invalid type for eval_loader_config: {type(eval_loader_config)}, {eval_loader_config=}',
        )

    for eval_config in eval_configs:
        label = eval_config.pop('label') if is_multi_eval else None
        eval_dataloader = build_dataloader(
            eval_config,
            tokenizer,
            device_eval_batch_size,
        )
        eval_loader: Evaluator = Evaluator(
            label=f'eval/{label}' if is_multi_eval else 'eval',
            dataloader=eval_dataloader,
            # Load the eval data to fail fast. metrics will get added
            # later in add_metrics_to_eval_loaders, after the model is loaded
            metric_names=[],
            device_eval_microbatch_size=device_eval_batch_size,
        )
        evaluators.append(eval_loader)
    return evaluators


def add_metrics_to_eval_loaders(
    evaluators: list[Evaluator],
    metric_names: list[str],
) -> list[Evaluator]:
    eval_loaders, other_evaluators = [], []
    for evaluator in evaluators:
        if evaluator.metric_names == []:
            evaluator.metric_names = metric_names
            eval_loaders.append(evaluator)
        else:
            other_evaluators.append(evaluator)

    # Put the base eval_loaders first
    return eval_loaders + other_evaluators


def build_icl_data_and_gauntlet(
    icl_tasks_config: Union[str, list[dict[str, Any]]],
    eval_gauntlet_config: Optional[Union[str, dict[str, Any]]],
    tokenizer: PreTrainedTokenizerBase,
    device_eval_batch_size: int,
    icl_seq_len: int,
    icl_subset_num_batches: Optional[int] = None,
) -> tuple[list[Evaluator], list[str], Optional[EvalGauntlet]]:
    icl_evaluators, logger_keys = build_icl_evaluators(
        icl_tasks_config,
        tokenizer,
        icl_seq_len,
        device_eval_batch_size,
        icl_subset_num_batches=icl_subset_num_batches,
    )
    eval_gauntlet_cb = None
    if eval_gauntlet_config is not None:
        if isinstance(eval_gauntlet_config, str):
            with open(eval_gauntlet_config, 'r') as icl_f:
                eval_gauntlet_cfg = om.load(icl_f)
                assert isinstance(eval_gauntlet_cfg, DictConfig)
            eval_gauntlet = to_dict_container(
                eval_gauntlet_cfg['eval_gauntlet'],
            )
        elif isinstance(eval_gauntlet_config, dict):  # pyright: ignore
            eval_gauntlet = eval_gauntlet_config
        else:
            raise ValueError(
                f'Got invalid type for eval_gauntlet_config: {type(eval_gauntlet_config)}',
            )
        eval_gauntlet['logger_keys'] = logger_keys
        eval_gauntlet['benchmark_sizes'] = {
            e.label: e.dataloader.num_samples for e in icl_evaluators
        }
        eval_gauntlet_cb = EvalGauntlet(**eval_gauntlet)
    return icl_evaluators, logger_keys, eval_gauntlet_cb


def build_load_planner(name: str, **kwargs: Any) -> LoadPlanner:
    """Builds a load planner from the registry.

    Args:
        name (str): Name of the load planner to build.
        kwargs (Any): Other relevant keyword arguments.

    Returns:
        LoadPlanner: The load planner.
    """
    return construct_from_registry(
        name=name,
        registry=registry.load_planners,
        partial_function=True,
        pre_validation_function=LoadPlanner,
        post_validation_function=None,
        kwargs=kwargs,
    )


def build_save_planner(name: str, **kwargs: Any) -> SavePlanner:
    """Builds a save planner from the registry.

    Args:
        name (str): Name of the save planner to build.
        kwargs (Any): Other relevant keyword arguments.

    Returns:
        savePlanner: The save planner.
    """
    return construct_from_registry(
        name=name,
        registry=registry.save_planners,
        partial_function=True,
        pre_validation_function=SavePlanner,
        post_validation_function=None,
        kwargs=kwargs,
    )


def build_composer_model(
    name: str,
    cfg: dict[str, Any],
    tokenizer: Optional[PreTrainedTokenizerBase],
    init_context: Optional[ContextManager] = None,
    master_weights_dtype: Optional[str] = None,
) -> ComposerModel:
    """Builds a ComposerModel from the registry.

    Args:
        name (str): Name of the model to build.
        cfg (DictConfig): Configuration for the model.
        tokenizer (Optional[PreTrainedTokenizerBase]): Tokenizer to use.
        init_context (Optional[ContextManager], optional): Context manager to use for initialization. Defaults to None.
        master_weights_dtype (Optional[str], optional): Master weights dtype. Defaults to None.

    Returns:
        ComposerModel: _description_
    """
    if init_context is None:
        init_context = contextlib.nullcontext()

    with init_context:
        model = construct_from_registry(
            name=name,
            registry=registry.models,
            pre_validation_function=ComposerModel,
            post_validation_function=None,
            kwargs={
                **cfg,
                'tokenizer': tokenizer,
            },
        )

    str_dtype_to_torch_dtype = {
        'f16': torch.float16,
        'float16': torch.float16,
        'bf16': torch.bfloat16,
        'bfloat16': torch.bfloat16,
    }

    if master_weights_dtype is not None:
        if master_weights_dtype not in str_dtype_to_torch_dtype:
            raise ValueError(
                f'Invalid master_weights_dtype: {master_weights_dtype}. ' +
                f'Valid options are: {list(str_dtype_to_torch_dtype.keys())}.',
            )
        dtype = str_dtype_to_torch_dtype[master_weights_dtype]
        model = model.to(dtype=dtype)

    return model


def build_callback(
    name: str,
    kwargs: Optional[dict[str, Any]] = None,
    train_config: Any = None,
) -> Callback:
    """Builds a callback from the registry."""
    registry_to_use = registry.callbacks
    if name in registry.callbacks_with_config:
        if kwargs is None:
            kwargs = {}
        if 'train_config' in kwargs:
            raise ValueError(
                f'`train_config` is a reserved keyword for callbacks with config. Please remove it from the kwargs.',
            )
        kwargs['train_config'] = copy.deepcopy(train_config)
        registry_to_use = registry.callbacks_with_config

    return construct_from_registry(
        name=name,
        registry=registry_to_use,
        partial_function=True,
        pre_validation_function=Callback,
        post_validation_function=None,
        kwargs=kwargs,
    )


def build_logger(
    name: str,
    kwargs: Optional[dict[str, Any]] = None,
) -> LoggerDestination:
    """Builds a logger from the registry."""
    return construct_from_registry(
        name=name,
        registry=registry.loggers,
        partial_function=True,
        pre_validation_function=LoggerDestination,
        post_validation_function=None,
        kwargs=kwargs,
    )


def build_algorithm(
    name: str,
    kwargs: Optional[dict[str, Any]] = None,
) -> Algorithm:
    """Builds an algorithm from the registry."""
    return construct_from_registry(
        name=name,
        registry=registry.algorithms,
        partial_function=True,
        pre_validation_function=Algorithm,
        post_validation_function=None,
        kwargs=kwargs,
    )


def build_metric(name: str, kwargs: Optional[dict[str, Any]] = None) -> Metric:
    """Builds a metric from the registry."""
    return construct_from_registry(
        name=name,
        registry=registry.metrics,
        partial_function=True,
        pre_validation_function=Metric,
        post_validation_function=None,
        kwargs=kwargs,
    )


def _extract_param_groups(
    model: torch.nn.Module,
    optimizer_config: Optional[dict[str, Any]] = None,
) -> Union[Iterable[torch.Tensor], Iterable[dict[str, Any]]]:
    """Extracts parameter groups defined in the optimizer config.

    The optimizer_config defines the optimizer args. It can additionally have key
    `disable_grad` which is a string or list of strings. If a string matches a
    parameter name, then that parameter will have `requires_grad=False`. This is
    useful for freezing parameters. It can additionally have a key
    `param_groups` which is a list of dicts. In this dict, key `param_str_match`
    defines a string; if a parameter name contains this string, then it will be
    in this parameter group. This is useful for grouping parameters together.
    The dict can also contain any other key that is a valid optimizer arg.
    Note: to handle name overlap conflicts, params are assigned to parameter
    groups and added to `param_groups` in the order that `param_str_match` appear
    in `param_groups`.

    Usage
    To disable gradient for all parameters that contain the string "norm" or "bias":
    ```
    optimizer_config: {
        "name": "decoupled_lionw",
        "lr": 1e-3,
        "weight_decay": 1e-2,
        "betas": [0.9, 0.999],
        "eps": 1e-8,
        "disable_grad": ["norm", "bias"]
    }
    ```

    To create and modify the optimizer parameters for all parameters that contain
    the string "norm" and "bias" separately:
    ```
    optimizer_config: {
        "name": "decoupled_lionw",
        "lr": 1e-3,
        "weight_decay": 1e-2,
        "betas": [0.9, 0.999],
        "eps": 1e-8,
        "param_groups": [
            {
                "param_str_match": "norm",
                "lr": 1e-4,
                "weight_decay": 0.0,
            },
            {
                "param_str_match": "bias",
                "lr": 5e-4,
                "weight_decay": 0.0,
            },
        ],
    }
    ```

    Args:
        model (torch.nn.Module): model to extract parameters from
        optimizer_config (Dict[str, Any]): optimizer config

    Returns:
        Union[Iterable[torch.Tensor], Iterable[Dict[str, Any]]]: an iterable of
            torch.Tensor's or dict's. Specifies what Tensors should be optimized
            and their param groupings.
    """
    if optimizer_config is None:
        return model.parameters()

    if 'disable_grad' in optimizer_config.keys():
        str_matches = optimizer_config.pop('disable_grad')
        if isinstance(str_matches, str):
            str_matches = [str_matches]
        for str_match in str_matches:
            for n, p in model.named_parameters():
                if re.search(str_match, n):
                    p.requires_grad = False
                    log.debug(f'Setting `{n}.requires_grad = False`.')

    param_groups_config = optimizer_config.pop('param_groups', None)
    if param_groups_config is not None:
        params = []
        param_dict = OrderedDict((n, p) for n, p in model.named_parameters())

        log.debug(f'Default optimizer settings: {optimizer_config}.')
        for param_group_config in param_groups_config:
            str_match = param_group_config.pop('param_str_match')
            filter_fn = functools.partial(re.search, str_match)
            param_names = [n for n in param_dict.keys() if filter_fn(n)]
            group_params = {'params': [param_dict.pop(n) for n in param_names]}
            group_params.update(param_group_config)

            log.debug(
                f'Creating optimizer param_group with parameters: {param_names} ' +\
                f'(extracted using {str_match=}). The param_group optimizer ' +\
                f'setting overrides are: {param_group_config}.')

            params.append(group_params)

        params.insert(0, {'params': param_dict.values()})
        return params

    return model.parameters()


def build_optimizer(
    model: torch.nn.Module,
    name: str,
    optimizer_config: dict[str, Any],
) -> Optimizer:

    params = _extract_param_groups(model, optimizer_config)
    kwargs = {**optimizer_config}

    if 'params' in kwargs:
        raise ValueError(
            'The `params` will be automatically extracted from the model and ' +
            'optimizer config. Please remove it from the optimizer config kwargs.',
        )

    kwargs['params'] = params
    return construct_from_registry(
        name=name,
        registry=registry.optimizers,
        partial_function=True,
        pre_validation_function=Optimizer,
        post_validation_function=None,
        kwargs=kwargs,
    )


def build_scheduler(
    name: str,
    scheduler_config: Optional[dict[str, Any]] = None,
) -> ComposerScheduler:
    return construct_from_registry(
        name=name,
        registry=registry.schedulers,
        partial_function=True,
        pre_validation_function=ComposerScheduler,
        post_validation_function=None,
        kwargs=scheduler_config,
    )


def build_tokenizer(
    tokenizer_name: str,
    tokenizer_kwargs: dict[str, Any],
) -> PreTrainedTokenizerBase:
    os.environ['TRANSFORMERS_NO_ADVISORY_WARNINGS'] = '1'
    os.environ['TOKENIZERS_PARALLELISM'] = 'false'

    signal_file_path = dist.get_node_signal_file_name()

    if dist.is_available() and dist.is_initialized() and dist.get_world_size(
    ) > 1:
        # Make sure the tokenizer files are downloaded and cached first by local rank 0
        with dist.local_rank_zero_download_and_wait(signal_file_path):
            pass

    if tokenizer_name in registry.tokenizers:
        tokenizer = construct_from_registry(
            name=tokenizer_name,
            registry=registry.tokenizers,
            partial_function=True,
            pre_validation_function=PreTrainedTokenizerBase,
            post_validation_function=None,
            kwargs=tokenizer_kwargs,
        )
    else:
        tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_name,
            **tokenizer_kwargs,
        )

        # HuggingFace does not respect the model_max_length kwarg, and overrides it with
        # min(kwargs['model_max_length'], original_config['model_max_length']), so we
        # explicitly set it here
        tokenizer.model_max_length = tokenizer_kwargs.get(
            'model_max_length',
            int(1e30),
        )

    if not hasattr(tokenizer, 'eos_token') or tokenizer.eos_token is None:
        raise ValueError(
            f'The tokenizer {tokenizer_name} must have an eos_token.',
        )

    if dist.is_available() and dist.is_initialized() and dist.get_world_size(
    ) > 1:
        if dist.get_local_rank() == 0:
            with open(signal_file_path, 'wb') as f:
                f.write(b'local_rank0_completed_tokenizer_setup')

        dist.barrier()

        if dist.get_local_rank() == 0:
            os.remove(signal_file_path)

    return tokenizer


def build_icl_evaluators(
    icl_tasks: Union[str, list[dict[str, Any]]],
    tokenizer: PreTrainedTokenizerBase,
    default_max_seq_len: int,
    default_batch_size: int,
    destination_dir: Optional[str] = None,
    icl_subset_num_batches: Optional[int] = None,
) -> tuple[list[Evaluator], list[str]]:
    if destination_dir is None:
        destination_dir = os.getcwd()

    evaluators = []
    logger_keys = []

    icl_tasks_list = None
    if isinstance(icl_tasks, str):
        log.info(f'Extracting ICL task config from path: {icl_tasks}')
        with open(icl_tasks, 'r') as icl_f:
            icl_task_cfg = om.load(icl_f)
        icl_tasks_list = to_list_container(icl_task_cfg.icl_tasks)
    else:
        icl_tasks_list = icl_tasks

    def _validate_cfg(icl_cfg: dict[str, Any]):
        assert 'label' in icl_cfg
        assert 'dataset_uri' in icl_cfg and icl_cfg['dataset_uri'] is not None
        assert 'icl_task_type' in icl_cfg
        assert 'num_fewshot' in icl_cfg

        if 'metric_names' not in icl_cfg:
            if icl_cfg['icl_task_type'] == 'language_modeling':
                icl_cfg['metric_names'] = ['InContextLearningLMAccuracy']
            elif icl_cfg['icl_task_type'] == 'multiple_choice':
                icl_cfg['metric_names'] = [
                    'InContextLearningMultipleChoiceAccuracy',
                ]
            elif icl_cfg['icl_task_type'] == 'schema':
                icl_cfg['metric_names'] = [
                    'InContextLearningMultipleChoiceAccuracy',
                ]
            elif icl_cfg['icl_task_type'] == 'generation_task_with_answers':
                icl_cfg['metric_names'] = [
                    'InContextLearningGenerationExactMatchAccuracy',
                ]
            else:
                icl_task_type = icl_cfg['icl_task_type']
                raise ValueError(
                    f'No metric_names defined, unable to build default metrics for icl_task_type={icl_task_type}.',
                )

        if 'max_seq_len' not in icl_cfg:
            icl_cfg['max_seq_len'] = default_max_seq_len
        if 'batch_size' not in icl_cfg:
            icl_cfg['batch_size'] = default_batch_size

        if 'num_beams' in icl_cfg:
            raise ValueError(
                'num_beams is no longer supported as a top level icl_task parameter.'  + \
                'Please use generation_kwargs.num_beams instead.')

    for icl_cfg in icl_tasks_list:
        assert isinstance(
            icl_cfg,
            dict,
        ), f'Expected dict, got {type(icl_cfg)}, {icl_cfg=}'
        _validate_cfg(icl_cfg)
        for num_fewshot in list(icl_cfg['num_fewshot']):
            if tokenizer.pad_token_id is None:
                # Current workaround to support GPT2 tokenizer with `pad_token_id = None`
                pad_tok_id = tokenizer.eos_token_id
            else:
                pad_tok_id = tokenizer.pad_token_id

            icl_cfg_label = icl_cfg['label']
            label = f'{icl_cfg_label}/{num_fewshot}-shot'
            metric_names = list(icl_cfg['metric_names'])
            # TODO: fix Composer bug when copying local paths and destination exists
            destination_path = f'{destination_dir}/{icl_cfg_label}-{num_fewshot}.jsonl'
            if dist.get_local_rank() == 0 and os.path.exists(destination_path):
                os.remove(destination_path)
            dist.barrier()

            hf_parsing_map = icl_cfg.get('hf_parsing_map', {})
            hf_loading_vars = icl_cfg.get('hf_loading_vars', {})
            early_stopping_criteria = icl_cfg.get(
                'early_stopping_criteria',
                [],
            )
            # TODO: fix manual removal of non-constructor fields
            icl_constructor_kwargs = copy.deepcopy(icl_cfg)
            icl_constructor_kwargs.pop('label', None)
            icl_constructor_kwargs.pop('metric_names', None)
            icl_constructor_kwargs.pop('icl_task_type', None)
            icl_constructor_kwargs.pop('batch_size', None)
            icl_constructor_kwargs.pop('has_categories', None)

            # Add custom constructor arguments
            icl_constructor_kwargs['pad_tok_id'] = pad_tok_id
            icl_constructor_kwargs['num_fewshot'] = num_fewshot

            # Support backwards compatibility for the naming of "prelimiter" as "question_prelimiter"
            if 'question_prelimiter' in icl_constructor_kwargs:
                if 'prelimiter' in icl_constructor_kwargs:
                    raise ValueError(
                        'Both "question_prelimiter" and "prelimiter" are specified in the ICL task config. '
                        +
                        'Please only specify one of them, as they map to the same argument.',
                    )
                else:
                    icl_constructor_kwargs['prelimiter'
                                          ] = icl_constructor_kwargs.pop(
                                              'question_prelimiter',
                                          )

            assert early_stopping_criteria is None or isinstance(
                early_stopping_criteria,
                list,
            )

            dataloaders = get_icl_task_dataloader(
                icl_task_type=icl_cfg['icl_task_type'],
                dataset_uri=icl_cfg['dataset_uri'],
                tokenizer=tokenizer,
                batch_size=icl_cfg['batch_size'],
                hf_loading_vars=hf_loading_vars,
                hf_parsing_map=hf_parsing_map,
                has_categories=icl_cfg.get('has_categories', False),
                destination_path=destination_path,
                kwargs=icl_constructor_kwargs,
            )
            if 'has_categories' in icl_cfg and icl_cfg[
                'has_categories'] and isinstance(dataloaders, dict):
                for category in dataloaders.keys():
                    logger_keys.extend([
                        f'metrics/{label}/{category}/{m}' for m in metric_names
                    ])
                    evaluators.append(
                        Evaluator(
                            label=f'{label}/{category}',
                            dataloader=dataloaders[category],
                            metric_names=metric_names,
                        ),
                    )
            else:
                logger_keys.extend([
                    f'metrics/{label}/{m}' for m in metric_names
                ])
                evaluators.append(
                    Evaluator(
                        label=label,
                        dataloader=dataloaders,
                        metric_names=metric_names,
                        subset_num_batches=icl_subset_num_batches,
                    ),
                )

    return evaluators, logger_keys


@experimental_function('Tensor Parallelism')
def build_tp_strategies(
    name: str,
    model: ComposerModel,
) -> dict[str, ParallelStyle]:

    warnings.warn(
        'Checkpointing is not currently supported for tensor parallelism due to this pytorch bug: https://github.com/pytorch/pytorch/issues/134095#issuecomment-2345018244',
    )
    return construct_from_registry(
        name=name,
        registry=registry.tp_strategies,
        partial_function=False,
        kwargs={'model': model},
    )
