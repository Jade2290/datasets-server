from dataclasses import dataclass
from http import HTTPStatus
from typing import Optional
from unittest.mock import Mock

import pytest
from libcommon.exceptions import CustomError
from libcommon.processing_graph import ProcessingGraph, ProcessingStep
from libcommon.queue import Queue
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import (
    CachedResponse,
    DoesNotExist,
    get_response,
    get_response_with_details,
    upsert_response,
)
from libcommon.utils import JobInfo, Priority, Status

from worker.common_exceptions import PreviousStepError
from worker.config import AppConfig
from worker.job_manager import JobManager
from worker.job_runners.dataset.dataset_job_runner import DatasetJobRunner
from worker.utils import CompleteJobResult

from .fixtures.hub import get_default_config_split


@pytest.fixture(autouse=True)
def prepare_and_clean_mongo(
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> None:
    # prepare the database before each test, and clean it afterwards
    pass


class DummyJobRunner(DatasetJobRunner):
    @staticmethod
    def _get_dataset_git_revision() -> Optional[str]:
        return "0.1.2"

    @staticmethod
    def get_job_runner_version() -> int:
        return 1

    @staticmethod
    def get_job_type() -> str:
        return "dummy"

    def compute(self) -> CompleteJobResult:
        return CompleteJobResult({"key": "value"})


@dataclass
class CacheEntry:
    error_code: Optional[str]
    job_runner_version: Optional[int]
    dataset_git_revision: Optional[str]
    progress: Optional[float] = None


def test_check_type(
    test_processing_graph: ProcessingGraph,
    another_processing_step: ProcessingStep,
    test_processing_step: ProcessingStep,
    app_config: AppConfig,
) -> None:
    job_id = "job_id"
    dataset = "dataset"
    config = "config"
    split = "split"

    job_type = f"not-{test_processing_step.job_type}"
    job_info = JobInfo(
        job_id=job_id,
        type=job_type,
        params={
            "dataset": dataset,
            "config": config,
            "split": split,
        },
        priority=Priority.NORMAL,
    )
    with pytest.raises(ValueError):
        job_runner = DummyJobRunner(
            job_info=job_info,
            processing_step=test_processing_step,
            app_config=app_config,
        )

        JobManager(
            job_info=job_info, app_config=app_config, job_runner=job_runner, processing_graph=test_processing_graph
        )

    job_info = JobInfo(
        job_id=job_id,
        type=test_processing_step.job_type,
        params={
            "dataset": dataset,
            "config": config,
            "split": split,
        },
        priority=Priority.NORMAL,
    )
    with pytest.raises(ValueError):
        job_runner = DummyJobRunner(
            job_info=job_info,
            processing_step=another_processing_step,
            app_config=app_config,
        )

        JobManager(
            job_info=job_info, app_config=app_config, job_runner=job_runner, processing_graph=test_processing_graph
        )


@pytest.mark.parametrize(
    "priority",
    [
        Priority.LOW,
        Priority.NORMAL,
    ],
)
def test_backfill(priority: Priority, app_config: AppConfig) -> None:
    graph = ProcessingGraph(
        {
            "dummy": {"input_type": "dataset"},
            "dataset-child": {"input_type": "dataset", "triggered_by": "dummy"},
            "config-child": {"input_type": "config", "triggered_by": "dummy"},
            "dataset-unrelated": {"input_type": "dataset"},
        }
    )
    root_step = graph.get_processing_step("dummy")
    job_info = JobInfo(
        job_id="job_id",
        type=root_step.job_type,
        params={
            "dataset": "dataset",
            "config": None,
            "split": None,
        },
        priority=priority,
    )

    job_runner = DummyJobRunner(
        job_info=job_info,
        processing_step=root_step,
        app_config=app_config,
    )

    job_manager = JobManager(job_info=job_info, app_config=app_config, job_runner=job_runner, processing_graph=graph)
    job_manager.get_dataset_git_revision = Mock(return_value="0.1.2")  # type: ignore

    # we add an entry to the cache
    job_manager.run()
    # check that the missing cache entries have been created
    queue = Queue()
    dataset_child_jobs = queue.get_dump_with_status(job_type="dataset-child", status=Status.WAITING)
    assert len(dataset_child_jobs) == 1
    assert dataset_child_jobs[0]["dataset"] == "dataset"
    assert dataset_child_jobs[0]["config"] is None
    assert dataset_child_jobs[0]["split"] is None
    assert dataset_child_jobs[0]["priority"] is priority.value
    dataset_unrelated_jobs = queue.get_dump_with_status(job_type="dataset-unrelated", status=Status.WAITING)
    assert len(dataset_unrelated_jobs) == 1
    assert dataset_unrelated_jobs[0]["dataset"] == "dataset"
    assert dataset_unrelated_jobs[0]["config"] is None
    assert dataset_unrelated_jobs[0]["split"] is None
    assert dataset_unrelated_jobs[0]["priority"] is priority.value
    # check that no config level jobs have been created, because the config names are not known
    config_child_jobs = queue.get_dump_with_status(job_type="config-child", status=Status.WAITING)
    assert len(config_child_jobs) == 0


def test_job_runner_set_crashed(
    test_processing_graph: ProcessingGraph,
    test_processing_step: ProcessingStep,
    app_config: AppConfig,
) -> None:
    job_id = "job_id"
    dataset = "dataset"
    config = "config"
    split = "split"
    message = "I'm crashed :("

    job_info = JobInfo(
        job_id=job_id,
        type=test_processing_step.job_type,
        params={
            "dataset": dataset,
            "config": config,
            "split": split,
        },
        priority=Priority.NORMAL,
    )
    job_runner = DummyJobRunner(
        job_info=job_info,
        processing_step=test_processing_step,
        app_config=app_config,
    )

    job_manager = JobManager(
        job_info=job_info, app_config=app_config, job_runner=job_runner, processing_graph=test_processing_graph
    )
    job_manager.get_dataset_git_revision = Mock(return_value="0.1.2")  # type: ignore

    job_manager.set_crashed(message=message)
    response = CachedResponse.objects()[0]
    expected_error = {"error": message}
    assert response.http_status == HTTPStatus.NOT_IMPLEMENTED
    assert response.error_code == "JobManagerCrashedError"
    assert response.dataset == dataset
    assert response.config == config
    assert response.split == split
    assert response.content == expected_error
    assert response.details == expected_error
    # TODO: check if it stores the correct dataset git sha and job version when it's implemented


def test_raise_if_parallel_response_exists(
    test_processing_graph: ProcessingGraph,
    test_processing_step: ProcessingStep,
    app_config: AppConfig,
) -> None:
    dataset = "dataset"
    config = "config"
    split = "split"
    current_dataset_git_revision = "CURRENT_GIT_REVISION"
    upsert_response(
        kind="dummy-parallel",
        dataset=dataset,
        config=config,
        split=split,
        content={},
        dataset_git_revision=current_dataset_git_revision,
        job_runner_version=1,
        progress=1.0,
        http_status=HTTPStatus.OK,
    )

    job_info = JobInfo(
        job_id="job_id",
        type="dummy",
        params={
            "dataset": dataset,
            "config": config,
            "split": split,
        },
        priority=Priority.NORMAL,
    )
    job_runner = DummyJobRunner(
        job_info=job_info,
        processing_step=test_processing_step,
        app_config=app_config,
    )

    job_manager = JobManager(
        job_info=job_info, app_config=app_config, job_runner=job_runner, processing_graph=test_processing_graph
    )
    job_manager.get_dataset_git_revision = Mock(return_value=current_dataset_git_revision)  # type: ignore
    with pytest.raises(CustomError) as exc_info:
        job_manager.raise_if_parallel_response_exists(parallel_cache_kind="dummy-parallel", parallel_job_version=1)
    assert exc_info.value.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert exc_info.value.code == "ResponseAlreadyComputedError"


@pytest.mark.parametrize("disclose_cause", [False, True])
def test_previous_step_error(disclose_cause: bool) -> None:
    dataset = "dataset"
    config = "config"
    split = "split"
    kind = "cache_kind"
    error_code = "ErrorCode"
    error_message = "error message"
    cause_exception = "CauseException"
    cause_message = "cause message"
    cause_traceback = ["traceback1", "traceback2"]
    details = {
        "error": error_message,
        "cause_exception": cause_exception,
        "cause_message": cause_message,
        "cause_traceback": cause_traceback,
    }
    content = details if disclose_cause else {"error": error_message}
    job_runner_version = 1
    dataset_git_revision = "dataset_git_revision"
    progress = 1.0
    upsert_response(
        kind=kind,
        dataset=dataset,
        config=config,
        split=split,
        content=content,
        http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
        error_code=error_code,
        details=details,
        job_runner_version=job_runner_version,
        dataset_git_revision=dataset_git_revision,
        progress=progress,
    )
    response = get_response_with_details(kind=kind, dataset=dataset, config=config, split=split)
    error = PreviousStepError.from_response(response=response, kind=kind, dataset=dataset, config=config, split=split)
    assert error.disclose_cause == disclose_cause
    assert error.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert error.code == error_code
    assert error.as_response_without_cause() == {
        "error": error_message,
    }
    assert error.as_response_with_cause() == {
        "error": error_message,
        "cause_exception": cause_exception,
        "cause_message": cause_message,
        "cause_traceback": [
            "The previous step failed, the error is copied to this step:",
            f"  {kind=} {dataset=} {config=} {split=}",
            "---",
            *cause_traceback,
        ],
    }
    if disclose_cause:
        assert error.as_response() == error.as_response_with_cause()
    else:
        assert error.as_response() == error.as_response_without_cause()


def test_doesnotexist(app_config: AppConfig) -> None:
    dataset = "doesnotexist"
    dataset, config, split = get_default_config_split(dataset)

    job_info = JobInfo(
        job_id="job_id",
        type="dummy",
        params={
            "dataset": dataset,
            "config": config,
            "split": split,
        },
        priority=Priority.NORMAL,
    )
    processing_step_name = "dummy"
    processing_graph = ProcessingGraph(
        {
            "dataset-level": {"input_type": "dataset"},
            processing_step_name: {
                "input_type": "dataset",
                "job_runner_version": DummyJobRunner.get_job_runner_version(),
                "triggered_by": "dataset-level",
            },
        }
    )
    processing_step = processing_graph.get_processing_step(processing_step_name)

    job_runner = DummyJobRunner(
        job_info=job_info,
        processing_step=processing_step,
        app_config=app_config,
    )

    job_manager = JobManager(
        job_info=job_info, app_config=app_config, job_runner=job_runner, processing_graph=processing_graph
    )

    assert not job_manager.process()
    with pytest.raises(DoesNotExist):
        get_response(kind=job_manager.processing_step.cache_kind, dataset=dataset, config=config, split=split)
