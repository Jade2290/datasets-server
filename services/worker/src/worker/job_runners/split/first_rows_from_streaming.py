# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import functools
import itertools
import logging
import time
import warnings
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, List, Literal, Mapping, Optional, TypeVar, Union, cast

from datasets import (
    Dataset,
    DownloadConfig,
    Features,
    IterableDataset,
    get_dataset_config_info,
    load_dataset,
)
from libcommon.constants import (
    PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_PARQUET_VERSION,
    PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_STREAMING_VERSION,
)
from libcommon.processing_graph import ProcessingStep
from libcommon.queue import JobInfo
from libcommon.simple_cache import DoesNotExist, SplitFullName, get_response
from libcommon.storage import StrPath

from worker.config import AppConfig, FirstRowsConfig
from worker.features import get_cell_value
from worker.job_runner import (
    CompleteJobResult,
    ConfigNotFoundError,
    JobRunnerError,
    SplitNotFoundError,
)
from worker.job_runners._datasets_based_job_runner import DatasetsBasedJobRunner
from worker.utils import (
    SplitFirstRowsResponse,
    create_truncated_row_items,
    get_json_size,
    to_features_list,
)

SplitFirstRowsFromStreamingJobRunnerErrorCode = Literal[
    "SplitsNamesError",
    "EmptyDatasetError",
    "InfoError",
    "FeaturesError",
    "StreamingRowsError",
    "NormalRowsError",
    "RowsPostProcessingError",
    "TooManyColumnsError",
    "TooBigContentError",
    "PreviousStepStatusError",
    "PreviousStepFormatError",
    "ResponseAlreadyComputedError",
]


class SplitFirstRowsFromStreamingJobRunnerError(JobRunnerError):
    """Base class for exceptions in this module."""

    def __init__(
        self,
        message: str,
        status_code: HTTPStatus,
        code: SplitFirstRowsFromStreamingJobRunnerErrorCode,
        cause: Optional[BaseException] = None,
        disclose_cause: bool = False,
    ):
        super().__init__(
            message=message, status_code=status_code, code=code, cause=cause, disclose_cause=disclose_cause
        )


class SplitsNamesError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the split names could not be fetched."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "SplitsNamesError", cause, True)


class EmptyDatasetError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the dataset has no data."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "EmptyDatasetError", cause, True)


class InfoError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the info could not be fetched."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "InfoError", cause, True)


class FeaturesError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the features could not be fetched."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "FeaturesError", cause, True)


class StreamingRowsError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the rows could not be fetched in streaming mode."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "StreamingRowsError", cause, True)


class NormalRowsError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the rows could not be fetched in normal mode."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "NormalRowsError", cause, True)


class RowsPostProcessingError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the rows could not be post-processed successfully."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "RowsPostProcessingError", cause, False)


class TooManyColumnsError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the dataset exceeded the max number of columns."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "TooManyColumnsError", cause, True)


class TooBigContentError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the first rows content exceeded the max size of bytes."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "TooBigContentError", cause, False)


class PreviousStepStatusError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the previous step gave an error. The job should not have been created."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "PreviousStepStatusError", cause, False)


class PreviousStepFormatError(SplitFirstRowsFromStreamingJobRunnerError):
    """Raised when the content of the previous step has not the expected format."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "PreviousStepFormatError", cause, False)


FuncT = TypeVar("FuncT", bound=Callable[..., Any])


def retry(func: FuncT) -> FuncT:
    """retries with an increasing sleep before every attempt"""
    SLEEPS = [1, 7, 70, 7 * 60, 70 * 60]
    MAX_ATTEMPTS = len(SLEEPS)

    @functools.wraps(func)
    def decorator(*args: Any, **kwargs: Any) -> Any:
        attempt = 0
        last_err = None
        while attempt < MAX_ATTEMPTS:
            try:
                """always sleep before calling the function. It will prevent rate limiting in the first place"""
                duration = SLEEPS[attempt]
                logging.info(f"Sleep during {duration} seconds to preventively mitigate rate limiting.")
                time.sleep(duration)
                return func(*args, **kwargs)
            except ConnectionError as err:
                logging.info("Got a ConnectionError, possibly due to rate limiting. Let's retry.")
                last_err = err
                attempt += 1
        raise RuntimeError(f"Give up after {attempt} attempts with ConnectionError") from last_err

    return cast(FuncT, decorator)


Row = Mapping[str, Any]


@retry
def get_rows(
    dataset: str,
    config: str,
    split: str,
    streaming: bool,
    rows_max_number: int,
    use_auth_token: Union[bool, str, None] = False,
) -> List[Row]:
    download_config = DownloadConfig(delete_extracted=True)
    ds = load_dataset(
        dataset,
        name=config,
        split=split,
        streaming=streaming,
        use_auth_token=use_auth_token,
        download_config=download_config,
    )
    if streaming:
        if not isinstance(ds, IterableDataset):
            raise TypeError("load_dataset should return an IterableDataset in streaming mode")
    elif not isinstance(ds, Dataset):
        raise TypeError("load_dataset should return a Dataset in normal mode")
    rows_plus_one = list(itertools.islice(ds, rows_max_number + 1))
    # ^^ to be able to detect if a split has exactly ROWS_MAX_NUMBER rows
    if len(rows_plus_one) <= rows_max_number:
        logging.debug(f"all the rows in the split have been fetched ({len(rows_plus_one)})")
    else:
        logging.debug(f"the rows in the split have been truncated ({rows_max_number} rows)")
    return rows_plus_one[:rows_max_number]


def transform_rows(
    dataset: str,
    config: str,
    split: str,
    rows: List[Row],
    features: Features,
    assets_base_url: str,
    assets_directory: StrPath,
) -> List[Row]:
    return [
        {
            featureName: get_cell_value(
                dataset=dataset,
                config=config,
                split=split,
                row_idx=row_idx,
                cell=row[featureName] if featureName in row else None,
                featureName=featureName,
                fieldType=fieldType,
                assets_base_url=assets_base_url,
                assets_directory=assets_directory,
            )
            for (featureName, fieldType) in features.items()
        }
        for row_idx, row in enumerate(rows)
    ]


def compute_first_rows_response(
    dataset: str,
    config: str,
    split: str,
    assets_base_url: str,
    hf_token: Optional[str],
    min_cell_bytes: int,
    rows_max_bytes: int,
    rows_max_number: int,
    rows_min_number: int,
    columns_max_number: int,
    assets_directory: StrPath,
    max_size_fallback: Optional[int] = None,
) -> SplitFirstRowsResponse:
    """
    Get the response of /first-rows for one specific split of a dataset from huggingface.co.
    Dataset can be private or gated if you pass an acceptable token.

    It is assumed that the dataset exist and can be accessed using the token.

    Args:
        dataset (`str`):
            A namespace (user or an organization) and a repo name separated
            by a `/`.
        config (`str`):
            A configuration name.
        split (`str`):
            A split name.
        assets_base_url (`str`):
            The base url of the assets.
        hf_endpoint (`str`):
            The Hub endpoint (for example: "https://huggingface.co")
        hf_token (`str` or `None`):
            An authentication token (See https://huggingface.co/settings/token)
        max_size_fallback (`int` or `None`): **DEPRECATED**
            The maximum number of bytes of the split to fallback to normal mode if the streaming mode fails.
            This argument is now hard-coded to 100MB, and will be removed in a future version.
        rows_max_bytes (`int`):
            The maximum number of bytes of the response (else, the response is truncated).
        rows_max_number (`int`):
            The maximum number of rows of the response.
        rows_min_number (`int`):
            The minimum number of rows of the response.
        columns_max_number (`int`):
            The maximum number of columns supported.
        assets_directory (`str` or `pathlib.Path`):
            The directory where the assets are stored.
    Returns:
        [`SplitFirstRowsResponse`]: The list of first rows of the split.
    <Tip>
    Raises the following errors:
        - [`~job_runner.ConfigNotFoundError`]
          If the config does not exist in the dataset.
        - [`~job_runner.SplitNotFoundError`]
          If the split does not exist in the dataset.
        - [`~job_runners.first_rows.InfoError`]
          If the config info could not be obtained using the datasets library.
        - [`~job_runners.first_rows.FeaturesError`]
          If the split features could not be obtained using the datasets library.
        - [`~job_runners.first_rows.StreamingRowsError`]
          If the split rows could not be obtained using the datasets library in streaming mode.
        - [`~job_runners.first_rows.NormalRowsError`]
          If the split rows could not be obtained using the datasets library in normal mode.
        - [`~job_runners.first_rows.RowsPostProcessingError`]
          If the post-processing of the split rows failed, e.g. while saving the images or audio files to the assets.
        - [`~job_runners.first_rows.TooManyColumnsError`]
          If the number of columns (features) exceeds the maximum supported number of columns.
        - [`~job_runners.first_rows.TooBigContentError`]
          If the first rows content exceeds the maximum supported size of bytes.
    </Tip>
    """
    logging.info(f"get first-rows for dataset={dataset} config={config} split={split}")
    use_auth_token: Union[bool, str, None] = hf_token if hf_token is not None else False
    # first ensure the tuple (dataset, config, split) exists on the Hub
    try:
        upstream_response = get_response(kind="/split-names-from-streaming", dataset=dataset, config=config)
        splits_content = upstream_response["content"]["splits"]
    except Exception:
        try:
            upstream_response = get_response(kind="/split-names-from-dataset-info", dataset=dataset, config=config)
            splits_content = upstream_response["content"]["splits"]
        except DoesNotExist as e:
            raise ConfigNotFoundError(f"The config '{config}' does not exist for the dataset.'", e) from e
        except Exception as e:
            raise PreviousStepFormatError("Previous step did not return the expected content.", e) from e

    if upstream_response["http_status"] != HTTPStatus.OK:
        raise PreviousStepStatusError(
            f"Previous step gave an error: {upstream_response['http_status']}. This job should not have been created."
        )

    if split not in [split_item["split"] for split_item in splits_content]:
        raise SplitNotFoundError(f"The split '{split}' does not exist for the config '{config}' of the dataset.")
    # get the features
    try:
        info = get_dataset_config_info(
            path=dataset,
            config_name=config,
            use_auth_token=use_auth_token,
        )
    except Exception as err:
        raise InfoError(
            f"The info cannot be fetched for the config '{config}' of the dataset.",
            cause=err,
        ) from err
    if not info.features:
        try:
            # https://github.com/huggingface/datasets/blob/f5826eff9b06ab10dba1adfa52543341ef1e6009/src/datasets/iterable_dataset.py#L1255
            iterable_dataset = load_dataset(
                path=dataset,
                name=config,
                split=split,
                streaming=True,
                use_auth_token=use_auth_token,
            )
            if not isinstance(iterable_dataset, IterableDataset):
                raise TypeError("load_dataset should return an IterableDataset.")
            iterable_dataset = iterable_dataset._resolve_features()
            if not isinstance(iterable_dataset, IterableDataset):
                raise TypeError("load_dataset should return an IterableDataset.")
            features = iterable_dataset.features
        except Exception as err:
            raise FeaturesError(
                (
                    f"Cannot extract the features (columns) for the split '{split}' of the config '{config}' of the"
                    " dataset."
                ),
                cause=err,
            ) from err
    else:
        features = info.features

    if features and len(features) > columns_max_number:
        raise TooManyColumnsError(
            f"The number of columns ({len(features)}) exceeds the maximum supported number of columns"
            f" ({columns_max_number}). This is a current limitation of the datasets viewer. You can reduce the number"
            " of columns if you want the viewer to work."
        )

    # validate size of response without the rows
    features_list = to_features_list(features=features)
    response_features_only: SplitFirstRowsResponse = {
        "dataset": dataset,
        "config": config,
        "split": split,
        "features": features_list,
        "rows": [],
    }

    surrounding_json_size = get_json_size(response_features_only)
    if surrounding_json_size > rows_max_bytes:
        raise TooBigContentError(
            f"The size of the content of the first rows ({surrounding_json_size} B) exceeds the maximum"
            f" supported size ({rows_max_bytes} B) even after truncation. Please report the issue."
        )

    # get the rows
    try:
        rows = get_rows(
            dataset=dataset,
            config=config,
            split=split,
            streaming=True,
            rows_max_number=rows_max_number,
            use_auth_token=use_auth_token,
        )
    except Exception as err:
        MAX_SIZE_FALLBACK = 100_000_000
        if max_size_fallback:
            warnings.warn(
                (
                    f"The parameter 'max_size_fallback' is deprecated. The hard-coded value `{MAX_SIZE_FALLBACK}`"
                    " will be used instead."
                ),
                category=DeprecationWarning,
            )
        if info.size_in_bytes is None or info.size_in_bytes > MAX_SIZE_FALLBACK:
            raise StreamingRowsError(
                "Cannot load the dataset split (in streaming mode) to extract the first rows.",
                cause=err,
            ) from err
        try:
            rows = get_rows(
                dataset=dataset,
                config=config,
                split=split,
                streaming=False,
                rows_max_number=rows_max_number,
                use_auth_token=use_auth_token,
            )
        except Exception as err:
            raise NormalRowsError(
                "Cannot load the dataset split (in normal download mode) to extract the first rows.",
                cause=err,
            ) from err
    # transform the rows, if needed (e.g. save the images or audio to the assets, and return their URL)
    try:
        transformed_rows = transform_rows(
            dataset=dataset,
            config=config,
            split=split,
            rows=rows,
            features=features,
            assets_base_url=assets_base_url,
            assets_directory=assets_directory,
        )
    except Exception as err:
        raise RowsPostProcessingError(
            "Server error while post-processing the split rows. Please report the issue.",
            cause=err,
        ) from err

    # truncate the rows to fit within the restrictions, and prepare them as RowItems
    row_items = create_truncated_row_items(
        rows=transformed_rows,
        min_cell_bytes=min_cell_bytes,
        rows_max_bytes=rows_max_bytes - surrounding_json_size,
        rows_min_number=rows_min_number,
    )

    response = response_features_only
    response["rows"] = row_items

    # return the response
    return response


class SplitFirstRowsFromStreamingJobRunner(DatasetsBasedJobRunner):
    assets_directory: StrPath
    first_rows_config: FirstRowsConfig

    @staticmethod
    def get_job_type() -> str:
        return "split-first-rows-from-streaming"

    @staticmethod
    def get_job_runner_version() -> int:
        return PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_STREAMING_VERSION

    def __init__(
        self,
        job_info: JobInfo,
        app_config: AppConfig,
        processing_step: ProcessingStep,
        first_rows_config: FirstRowsConfig,
        hf_datasets_cache: Path,
        assets_directory: StrPath,
    ) -> None:
        super().__init__(
            job_info=job_info,
            app_config=app_config,
            processing_step=processing_step,
            hf_datasets_cache=hf_datasets_cache,
        )
        self.first_rows_config = first_rows_config
        self.assets_directory = assets_directory
        self.assets_base_url = app_config.assets.base_url

    def compute(self) -> CompleteJobResult:
        if self.config is None or self.split is None:
            raise ValueError("config and split are required")
        self.raise_if_parallel_response_exists(
            parallel_job_type="split-first-rows-from-parquet",
            parallel_job_version=PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_PARQUET_VERSION,
        )
        return CompleteJobResult(
            compute_first_rows_response(
                dataset=self.dataset,
                config=self.config,
                split=self.split,
                assets_base_url=self.assets_base_url,
                assets_directory=self.assets_directory,
                hf_token=self.common_config.hf_token,
                min_cell_bytes=self.first_rows_config.min_cell_bytes,
                rows_max_bytes=self.first_rows_config.max_bytes,
                rows_max_number=self.first_rows_config.max_number,
                rows_min_number=self.first_rows_config.min_number,
                columns_max_number=self.first_rows_config.columns_max_number,
            )
        )

    def get_new_splits(self, _: Mapping[str, Any]) -> set[SplitFullName]:
        """Get the set of new splits, from the content created by compute."""
        if self.config is None or self.split is None:
            raise ValueError("config and split are required")
        return {SplitFullName(dataset=self.dataset, config=self.config, split=self.split)}