from typing import List, Optional, TypedDict

from datasets import get_dataset_config_names

from datasets_preview_backend.constants import DEFAULT_CONFIG_NAME, FORCE_REDOWNLOAD
from datasets_preview_backend.exceptions import Status400Error, Status404Error
from datasets_preview_backend.models.info import Info, get_info
from datasets_preview_backend.models.split import Split, get_splits


class Config(TypedDict):
    config_name: str
    splits: List[Split]
    info: Info


def filter_configs(configs: List[Config], config_name: Optional[str] = None) -> List[Config]:
    if config_name is not None:
        if not isinstance(config_name, str):
            raise TypeError("config argument should be a string")
        configs = [config for config in configs if config["config_name"] == config_name]
        if not configs:
            raise Status404Error("config not found in dataset")
    return configs


def get_config(dataset_name: str, config_name: str) -> Config:
    if not isinstance(config_name, str):
        raise TypeError("config_name argument should be a string")
    # Get all the data
    info = get_info(dataset_name, config_name)
    splits = get_splits(dataset_name, config_name, info)

    return {"config_name": config_name, "splits": splits, "info": info}


def get_config_names(dataset_name: str) -> List[str]:
    try:
        config_names: List[str] = get_dataset_config_names(
            dataset_name, download_mode=FORCE_REDOWNLOAD  # type: ignore
        )
        if not config_names:
            config_names = [DEFAULT_CONFIG_NAME]
    except FileNotFoundError as err:
        raise Status404Error("The dataset could not be found.", err)
    except Exception as err:
        raise Status400Error("The config names could not be parsed from the dataset.", err)
    return config_names


def get_configs(dataset_name: str) -> List[Config]:
    return [get_config(dataset_name, config_name) for config_name in get_config_names(dataset_name)]