import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clinseekagent"))

from image_tool_pool import ImageToolPool


ROW = {
    "qid": "medmod_radiology_test_1",
    "image_paths": [
        "MedModOriginalLinked_v1/mimic-cxr/2.0.0/files/p14/p14479847/"
        "s58313165/33e72c08.jpg"
    ],
    "report_paths": [],
}
LINKED = ROW["image_paths"][0]
OTHER_STUDY = (
    "MedModOriginalLinked_v1/mimic-cxr/2.0.0/files/p14/p14479847/"
    "s59999999/deadbeef.jpg"
)


class ImageAssetScopeTests(unittest.TestCase):
    def setUp(self):
        self.pool = ImageToolPool()
        self.calls = []

        async def fake_call(qid, tool_name, tool_args):
            self.calls.append((tool_name, dict(tool_args)))
            return {"output": {"labels": ["Cardiomegaly"]}}

        self.pool._call_mcp_tool = fake_call
        self.pool._get_or_create_session("q1")

    def call(self, args, tool="image.chest_xray_classifier"):
        return asyncio.run(self.pool.call_tool("q1", tool, args))

    def test_linked_image_is_forwarded(self):
        self.pool.configure_session("q1", ROW)
        result = self.call({"image_path": LINKED})
        self.assertIn("Cardiomegaly", result)
        self.assertEqual(1, len(self.calls))

    def test_absolute_resolved_spelling_is_accepted(self):
        absolute = "/bench/" + LINKED
        self.pool.configure_session("q1", ROW, resolved_assets=[absolute])
        self.call({"image_path": absolute})
        self.assertEqual(1, len(self.calls))

    def test_other_study_is_refused_without_reaching_the_server(self):
        self.pool.configure_session("q1", ROW)
        result = self.call({"image_path": OTHER_STUDY})
        self.assertIn("not one of the images linked", result)
        self.assertEqual([], self.calls)

    def test_absolute_path_outside_bench_is_refused(self):
        self.pool.configure_session("q1", ROW)
        result = self.call({"image_path": "/etc/hostname"})
        self.assertIn("not one of the images linked", result)
        self.assertEqual([], self.calls)

    def test_dicom_path_argument_is_also_scoped(self):
        self.pool.configure_session("q1", ROW)
        result = self.call(
            {"dicom_path": OTHER_STUDY}, tool="image.dicom_processor"
        )
        self.assertIn("not one of the images linked", result)
        self.assertEqual([], self.calls)

    def test_package_prefix_stripped_spelling_is_accepted(self):
        # The release loader strips the upstream package segment before
        # resolving; same asset, different spelling.
        self.pool.configure_session("q1", ROW)
        stripped = LINKED.split("/", 1)[1]
        self.call({"image_path": stripped})
        self.assertEqual(1, len(self.calls))

    def test_prefix_stripping_does_not_admit_another_study(self):
        self.pool.configure_session("q1", ROW)
        result = self.call({"image_path": OTHER_STUDY.split("/", 1)[1]})
        self.assertIn("not one of the images linked", result)
        self.assertEqual([], self.calls)

    def test_unconfigured_session_fails_closed(self):
        result = self.call({"image_path": LINKED})
        self.assertIn("no trusted asset scope", result)
        self.assertEqual([], self.calls)

    def test_generated_artifact_can_be_chained(self):
        self.pool.configure_session("q1", ROW)
        artifact = "/tmp/mm_artifacts/artifact_abc123.png"

        async def render(qid, tool_name, tool_args):
            self.calls.append((tool_name, dict(tool_args)))
            return {"output": {"image_path": artifact}}

        self.pool._call_mcp_tool = render
        self.call({"image_path": LINKED}, tool="image.image_visualizer")

        async def classify(qid, tool_name, tool_args):
            self.calls.append((tool_name, dict(tool_args)))
            return {"output": {"labels": ["Effusion"]}}

        self.pool._call_mcp_tool = classify
        result = self.call({"image_path": artifact})
        self.assertIn("Effusion", result)
        self.assertEqual(2, len(self.calls))

    def test_scope_is_per_qid(self):
        self.pool.configure_session("q1", ROW)
        self.pool._get_or_create_session("q2")
        self.pool.configure_session(
            "q2", {"image_paths": [OTHER_STUDY], "report_paths": []}
        )
        self.assertIn(
            "not one of the images linked",
            asyncio.run(
                self.pool.call_tool(
                    "q1", "image.chest_xray_classifier",
                    {"image_path": OTHER_STUDY},
                )
            ),
        )
        self.assertIn(
            "not one of the images linked",
            asyncio.run(
                self.pool.call_tool(
                    "q2", "image.chest_xray_classifier",
                    {"image_path": LINKED},
                )
            ),
        )


if __name__ == "__main__":
    unittest.main()
