# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging
from http import HTTPStatus
from typing import List, Literal, Optional, Union

from datasets import get_dataset_split_names
from datasets.data_files import EmptyDatasetError as _EmptyDatasetError
from libcommon.constants import (
    PROCESSING_STEP_SPLIT_NAMES_FROM_DATASET_INFO_VERSION,
    PROCESSING_STEP_SPLIT_NAMES_FROM_STREAMING_VERSION,
)

from worker.common_exceptions import JobRunnerError
from worker.job_runners.config.config_job_runner import ConfigCachedJobRunner
from worker.utils import CompleteJobResult, JobRunnerInfo, SplitItem, SplitsList

SplitNamesFromStreamingJobRunnerErrorCode = Literal[
    "EmptyDatasetError",
    "SplitNamesFromStreamingError",
]


class SplitNamesFromStreamingJobRunnerError(JobRunnerError):
    """Base class for split names job runner exceptions."""

    def __init__(
        self,
        message: str,
        status_code: HTTPStatus,
        code: SplitNamesFromStreamingJobRunnerErrorCode,
        cause: Optional[BaseException] = None,
        disclose_cause: bool = False,
    ):
        super().__init__(
            message=message, status_code=status_code, code=code, cause=cause, disclose_cause=disclose_cause
        )


class SplitNamesFromStreamingError(SplitNamesFromStreamingJobRunnerError):
    """Raised when the split names could not be fetched."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "SplitNamesFromStreamingError", cause, True)


class EmptyDatasetError(SplitNamesFromStreamingJobRunnerError):
    """Raised when the dataset has no data."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "EmptyDatasetError", cause, True)


def compute_split_names_from_streaming_response(
    dataset: str,
    config: str,
    hf_token: Optional[str] = None,
) -> SplitsList:
    """
    Get the response of /split-names-from-streaming for one specific dataset and config on huggingface.co.
    Dataset can be private or gated if you pass an acceptable token.

    It is assumed that the dataset exists and can be accessed using the token, and that the config exists in
    the dataset.

    This function relies on the streaming mode if the splits are not directly defined in the dataset config. See
    https://github.dev/huggingface/datasets/blob/e183a269067575db8765ee979bd8523d14a1adae/src/datasets/inspect.py#L389-L390

    The /split-names-from-streaming response generated by this function does not include stats about the split,
    like the size or number of samples. See dataset-info or dataset-size for that.

    Args:
        dataset (`str`):
            A namespace (user or an organization) and a repo name separated
            by a `/`.
        config (`str`):
            A configuration name.
        hf_token (`str`, *optional*):
            An authentication token (See https://huggingface.co/settings/token)
    Returns:
        `SplitsList`: An object with the list of split names for the dataset and config.
    <Tip>
    Raises the following errors:
        - [`~job_runners.config.split_names_from_streaming.EmptyDatasetError`]
          The dataset is empty.
        - [`~job_runners.config.split_names_from_streaming.SplitsNamesError`]
          If the list of splits could not be obtained using the datasets library.
    </Tip>
    """
    logging.info(f"get split names for dataset={dataset}, config={config}")
    use_auth_token: Union[bool, str, None] = hf_token if hf_token is not None else False

    try:
        split_name_items: List[SplitItem] = [
            {"dataset": dataset, "config": config, "split": str(split)}
            for split in get_dataset_split_names(path=dataset, config_name=config, use_auth_token=use_auth_token)
        ]
    except _EmptyDatasetError as err:
        raise EmptyDatasetError("The dataset is empty.", cause=err) from err
    except Exception as err:
        raise SplitNamesFromStreamingError(
            f"Cannot get the split names for the config '{config}' of the dataset.",
            cause=err,
        ) from err
    return SplitsList({"splits": split_name_items})


class SplitNamesFromStreamingJobRunner(ConfigCachedJobRunner):
    @staticmethod
    def get_job_type() -> str:
        return "/split-names-from-streaming"

    @staticmethod
    def get_job_runner_version() -> int:
        return PROCESSING_STEP_SPLIT_NAMES_FROM_STREAMING_VERSION

    @staticmethod
    def get_parallel_job_runner() -> JobRunnerInfo:
        return JobRunnerInfo(
            job_runner_version=PROCESSING_STEP_SPLIT_NAMES_FROM_DATASET_INFO_VERSION,
            job_type="/split-names-from-dataset-info",
        )

    def compute(self) -> CompleteJobResult:
        return CompleteJobResult(
            compute_split_names_from_streaming_response(
                dataset=self.dataset,
                config=self.config,
                hf_token=self.app_config.common.hf_token,
            )
        )
