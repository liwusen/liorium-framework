"""真实 models.dev 目录（默认跳过；显式 ``-m live`` 才跑）。"""

from __future__ import annotations

import asyncio

import pytest

from liorium.provider import catalog


@pytest.mark.live
def test_models_dev_catalog_reachable(tmp_path):
    cache_file = tmp_path / "api.json"

    catalog_data = asyncio.run(catalog.refresh_catalog(cache_path=cache_file))

    assert isinstance(catalog_data, dict) and catalog_data
    for provider_id, entry in catalog_data.items():
        assert isinstance(entry, dict), provider_id
        assert isinstance(entry.get("models", {}), dict), provider_id

    status = catalog.catalog_status(cache_path=cache_file)
    assert status["loaded"] is True
    assert status["providers"] == len(catalog_data)
