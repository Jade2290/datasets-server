# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging
from typing import List, Optional, Set

from libcommon.processing_graph import ProcessingGraph
from libcommon.simple_cache import get_valid_datasets
from starlette.requests import Request
from starlette.responses import Response

from api.prometheus import StepProfiler
from api.utils import (
    Endpoint,
    UnexpectedError,
    get_json_api_error_response,
    get_json_ok_response,
)


def get_valid(processing_graph: ProcessingGraph) -> List[str]:
    # a dataset is considered valid if at least one response for PROCESSING_STEPS_FOR_VALID
    # is valid.
    datasets: Optional[Set[str]] = None
    for processing_step in processing_graph.get_processing_steps_required_by_dataset_viewer():
        kind_datasets = get_valid_datasets(kind=processing_step.cache_kind)
        if datasets is None:
            # first iteration fills the set of datasets
            datasets = kind_datasets
        else:
            # next iterations remove the datasets that miss a required processing step
            datasets.intersection_update(kind_datasets)
    # note that the list is sorted alphabetically for consistency
    return [] if datasets is None else sorted(datasets)


def create_valid_endpoint(
    processing_graph: ProcessingGraph,
    max_age_long: int = 0,
    max_age_short: int = 0,
) -> Endpoint:
    # this endpoint is used by the frontend to know which datasets support the dataset viewer
    async def valid_endpoint(_: Request) -> Response:
        with StepProfiler(method="valid_endpoint", step="all"):
            try:
                logging.info("/valid")
                with StepProfiler(method="valid_endpoint", step="prepare content"):
                    content = {"valid": get_valid(processing_graph=processing_graph)}
                with StepProfiler(method="valid_endpoint", step="generate OK response"):
                    return get_json_ok_response(content, max_age=max_age_long)
            except Exception as e:
                with StepProfiler(method="valid_endpoint", step="generate API error response"):
                    return get_json_api_error_response(UnexpectedError("Unexpected error.", e), max_age=max_age_short)

    return valid_endpoint
