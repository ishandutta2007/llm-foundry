# Copyright 2024 MosaicML LLM Foundry authors
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import patch

import pytest
from omegaconf import DictConfig, ListConfig
from transformers import PreTrainedTokenizerBase

from llmfoundry.models.inference_api_wrapper import (
    FMAPICasualLMEvalWrapper,
    FMAPIChatAPIEvalWrapper,
)
from llmfoundry.models.inference_api_wrapper.fmapi import FMAPIEvalInterface
from llmfoundry.utils.builders import build_icl_evaluators
from llmfoundry.utils.config_utils import to_list_container


def load_icl_config():
    return DictConfig({
        'icl_tasks':
            ListConfig([
                DictConfig({
                    'label':
                        'jeopardy',
                    'dataset_uri':
                        'scripts/eval/local_data/world_knowledge/jeopardy_all.jsonl',
                    'num_fewshot': [0, 1],
                    'icl_task_type':
                        'language_modeling',
                    'continuation_delimiter':
                        '\nAnswer: ',
                    'has_categories':
                        True,
                }),
            ]),
    })


class MockTopLogProb:

    def __init__(self, expected_token: str) -> None:
        self.top_logprobs = [{expected_token: 0}]


class MockLogprob:

    def __init__(self, expected_token: str) -> None:
        self.logprobs = MockTopLogProb(expected_token)


class MockCompletion:

    def __init__(self, expected_token: str) -> None:
        self.choices = [MockLogprob(expected_token)]


class MockContent:

    def __init__(self, expected_token: str) -> None:
        setattr(self, 'content', expected_token)


class MockMessage:

    def __init__(self, expected_token: str) -> None:
        setattr(self, 'message', MockContent(expected_token))


class MockChatCompletion:

    def __init__(self, expected_token: str) -> None:
        setattr(self, 'choices', [MockMessage(expected_token)])


def mock_create(**kwargs: dict[str, str]):
    prompt = kwargs['prompt']
    if prompt == 'AMERICAN HISTORY: On May 29, 1765 Patrick Henrys Stamp Act protest was interrupted with this one word\nAnswer:':  # pyright: ignore[reportUnnecessaryComparison]
        return MockCompletion(' Tre')

    elif prompt == 'AMERICAN HISTORY: On May 29, 1765 Patrick Henrys Stamp Act protest was interrupted with this one word\nAnswer: Tre':  # pyright: ignore[reportUnnecessaryComparison]
        return MockCompletion('ason')

    elif prompt == 'AMERICAN HISTORY: On May 29, 1765 Patrick Henrys Stamp Act protest was interrupted with this one word\nAnswer: Treason':  # pyright: ignore[reportUnnecessaryComparison]
        return MockCompletion('!')

    else:
        # dummy token to make sure the model is incorrect on any other prompt
        return MockCompletion(' ')


@pytest.mark.filterwarnings('ignore::DeprecationWarning')
def test_causal_fmapi_wrapper(
    tmp_path: str,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    # patch block_until_ready
    with patch.object(FMAPIEvalInterface, 'block_until_ready') as mock:

        _ = pytest.importorskip('openai')

        model = FMAPICasualLMEvalWrapper(
            om_model_config=DictConfig({
                'local': True,
                'name': 'mosaicml/mpt-7b-8k-instruct',
            }),
            tokenizer=tiny_gpt2_tokenizer,
        )
        with patch.object(model, 'client') as mock:
            mock.completions.create = mock_create

            task_cfg = load_icl_config()
            evaluators, _ = build_icl_evaluators(
                to_list_container(task_cfg.icl_tasks),
                tiny_gpt2_tokenizer,
                1024,
                2,
                destination_dir=str(tmp_path),
            )

            batch = next(evaluators[0].dataloader.dataloader.__iter__())
            result = model.eval_forward(batch)
            model.update_metric(
                batch,
                result,
                metric=model.get_metrics()['InContextLearningLMAccuracy'],
            )  # pyright: ignore
            acc = model.get_metrics(
            )['InContextLearningLMAccuracy'
             ].compute(  # pyright: ignore
             )  # pyright: ignore
            assert acc == 0.5


@pytest.mark.filterwarnings('ignore::DeprecationWarning')
def test_chat_fmapi_wrapper(
    tmp_path: str,
    tiny_gpt2_tokenizer: PreTrainedTokenizerBase,
):
    with patch.object(FMAPIEvalInterface, 'block_until_ready') as mock:
        _ = pytest.importorskip('openai')

        chatmodel = FMAPIChatAPIEvalWrapper(
            om_model_config=DictConfig({
                'local': True,
                'name': 'mosaicml/mpt-7b-8k-instruct',
            }),
            tokenizer=tiny_gpt2_tokenizer,
        )

        with patch.object(chatmodel, 'client') as mock:
            mock.chat.completions.create.return_value = MockChatCompletion(
                'Treason!',
            )

            task_cfg = load_icl_config()
            evaluators, _ = build_icl_evaluators(
                to_list_container(task_cfg.icl_tasks),
                tiny_gpt2_tokenizer,
                1024,
                2,
                destination_dir=str(tmp_path),
            )

            batch = next(evaluators[0].dataloader.dataloader.__iter__())
            result = chatmodel.eval_forward(batch)
            chatmodel.update_metric(
                batch,
                result,
                metric=chatmodel.get_metrics()['InContextLearningLMAccuracy'],
            )  # pyright: ignore
            acc = chatmodel.get_metrics(
            )['InContextLearningLMAccuracy'].compute(  # pyright: ignore
            )
            assert acc == 0.5
