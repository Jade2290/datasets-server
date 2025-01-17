# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging

from libcommon.constants import CACHE_COLLECTION_RESPONSES, CACHE_MONGOENGINE_ALIAS
from libcommon.simple_cache import CachedResponse
from mongoengine.connection import get_db

from mongodb_migration.check import check_documents
from mongodb_migration.migration import Migration

dataset_info = "/dataset-info"
dataset_info_updated = "dataset-info"


class MigrationCacheUpdateDatasetInfo(Migration):
    def up(self) -> None:
        logging.info(f"Rename cache_kind field from {dataset_info} to {dataset_info_updated}")
        db = get_db(CACHE_MONGOENGINE_ALIAS)

        # update existing documents with the old kind
        db[CACHE_COLLECTION_RESPONSES].update_many({"kind": dataset_info}, {"$set": {"kind": dataset_info_updated}})

    def down(self) -> None:
        logging.info(f"Rollback cache_kind field from {dataset_info_updated} to {dataset_info}")
        db = get_db(CACHE_MONGOENGINE_ALIAS)
        db[CACHE_COLLECTION_RESPONSES].update_many({"kind": dataset_info_updated}, {"$set": {"kind": dataset_info}})

    def validate(self) -> None:
        logging.info("Validate modified documents")

        check_documents(DocCls=CachedResponse, sample_size=10)
