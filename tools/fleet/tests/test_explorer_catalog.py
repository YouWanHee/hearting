"""The explorer reads bounded runtime catalogs without invoking their providers."""
import json
import os
import tempfile
import unittest
from unittest import mock

from tools.fleet.explorer import model_catalog


class ModelCatalogTests(unittest.TestCase):
    def test_supported_runtime_shapes_keep_catalog_order_and_deduplicate(self):
        self.assertEqual(model_catalog._ids({"models": [{"slug": "model-a"}, {"id": "model-b"},
                                                          {"slug": "model-a"}]}),
                         ("model-a", "model-b"))
        self.assertEqual(model_catalog._ids({"providers": {"provider": {"models": {
            "model-c": {"name": "display name"}, "model-d": {}}}}}), ("model-c", "model-d"))

    def test_catalog_is_read_from_runtime_cache_and_missing_source_stays_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "catalog.json")
            with open(path, "w", encoding="utf-8") as stream:
                json.dump({"models": [{"slug": "runtime-model"}]}, stream)
            with mock.patch.object(model_catalog, "_candidates", return_value=(path,)):
                catalog = model_catalog.read_catalog("codex")
            self.assertEqual((catalog.state, catalog.models), ("available", ("runtime-model",)))
            self.assertEqual(catalog.source, path)
        with mock.patch.object(model_catalog, "_candidates", return_value=()):
            missing = model_catalog.read_catalog("claude")
        self.assertEqual((missing.state, missing.models, missing.source), ("unknown", (), None))
