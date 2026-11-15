"""Validate the comparison's scoring and pairing, without paid model calls."""

from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path

from company_agent.evals.comparison import ARMS, OPTIMIZED_ARMS, aggregate, manifest, schedule
from company_agent.evals.comparison_cases import BY_ID, CASES, Case, check_case, prepare_case
from company_agent.services.office.documents import create_word, edit
from company_agent.evals.comparison_report import build
from company_agent.tests.test_agent import Fixture


class ComparisonTests(unittest.TestCase):
    def test_schedule_pairs_same_inputs_and_excludes_chat_from_file_tasks(self):
        self.assertEqual(len(CASES), len(BY_ID))
        planned = schedule(CASES, ARMS, 2, 42)
        self.assertEqual(planned, schedule(CASES, ARMS, 2, 42))
        self.assertNotEqual(planned, schedule(CASES, ARMS, 2, 43))
        for case in CASES:
            for repeat in (1, 2):
                arms = {arm for case_id, arm, r in planned if case_id == case.id and r == repeat}
                self.assertEqual(arms, set(ARMS) - ({"argon_chat"} if case.fixture else set()))

    def test_offline_manifest_has_no_claimed_results(self):
        data = manifest(CASES, ARMS, 1, 42)
        self.assertFalse(data["live"])
        self.assertEqual(data["paid_calls"], 0)
        self.assertNotIn("summary", data)

    def test_specialist_candidates_are_separate_and_paired_only_on_matching_cases(self):
        self.assertIn('company_specialist_lite', OPTIMIZED_ARMS)
        self.assertIn('company_specialist_data_argon', OPTIMIZED_ARMS)
        arms = ['company_hybrid_lite', 'company_specialist_lite', 'company_specialist_data_argon']
        planned = schedule(CASES, arms, 1, 42)
        rows = [dict(case='chat_math', arm=arm, repeat=1, passed=True, seconds=1,
                     accounted_usd=0.01, category='reasoning', calls=[]) for arm in arms]
        result = aggregate(rows, planned)
        self.assertEqual(set(result['arms']), set(arms))
        self.assertEqual({(p['left'], p['right']) for p in result['paired']}, {
            ('company_hybrid_lite', 'company_specialist_lite'),
            ('company_specialist_lite', 'company_specialist_data_argon'),
        })

    def test_case_schema_rejects_a_string_instead_of_pattern_tuple(self):
        with self.assertRaises(TypeError):
            Case("bad", "uncertainty", "prompt", expected="pattern")

    def test_missing_source_accepts_an_honest_blocked_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["missing_source"]
            before = prepare_case(folder, case)
            checks = check_case(
                case, {"answer": "來源不存在，請上傳檔案。", "status": "blocked"}, {}, folder, before, []
            )
            self.assertTrue(all(checks.values()))

    def test_latex_percent_is_not_a_numeric_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["chat_weighted"]
            before = prepare_case(folder, case)
            checks = check_case(case, {"answer": r"900與100加權得到26\%。", "status": "done"}, {}, folder, before, [])
            self.assertTrue(all(checks.values()))

    def test_pairing_never_compares_different_cases_or_repeats(self):
        def row(case, arm, repeat, passed):
            return dict(
                case=case,
                arm=arm,
                repeat=repeat,
                passed=passed,
                seconds=1,
                accounted_usd=0.01,
                category="reasoning",
                calls=[],
            )

        rows = [
            row("a", "company_argon", 1, True),
            row("a", "argon_tools", 1, False),
            row("b", "company_argon", 2, False),
            row("b", "argon_tools", 1, True),
        ]
        result = aggregate(rows, [(r["case"], r["arm"], r["repeat"]) for r in rows])
        paired = next(p for p in result["paired"] if p["left"] == "company_argon")
        self.assertEqual(paired["pairs"], 1)
        self.assertEqual(paired["left_only_pass"], 1)
        self.assertEqual(paired["right_only_pass"], 0)

    def test_report_merge_does_not_pair_across_separate_experiments(self):
        def report(arm):
            return {
                "runs": [
                    dict(
                        case="chat_math",
                        arm=arm,
                        repeat=1,
                        passed=True,
                        seconds=1,
                        accounted_usd=0.01,
                        category="reasoning",
                        calls=[],
                    )
                ],
                "planned": [("chat_math", arm, 1)],
            }

        _, _, summary = build([report("company_argon"), report("argon_tools")])
        self.assertEqual(summary["paired"], [])

    def test_false_file_claim_does_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["two_deliverables"]
            before = prepare_case(folder, case)
            result = {"answer": "已完成Word與PPT，基礎施工、鋼筋檢查。", "status": "done"}
            checks = check_case(case, result, {}, folder, before, [])
            self.assertFalse(checks["file_set"])
            self.assertFalse(checks["two_slides"])

    def test_wrong_second_paragraph_cannot_pass_by_containing_both_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["repeated_edit"]
            before = prepare_case(folder, case)
            wrong = create_word(
                "錯誤修訂",
                [{"text": text, "heading": False} for text in ("進度：已確認", "進度：待確認", "保留此段內容")],
            )
            checks = check_case(case, {"answer": "完成", "status": "done"}, {"revised.docx": wrong}, folder, before, [])
            self.assertFalse(checks["correct_edit"])

    def test_valid_narrow_edit_preserves_formatting_and_passes_independent_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["repeated_edit"]
            before = prepare_case(folder, case)
            original = (folder / "repeated.docx").read_bytes()
            revised = edit(original, ".docx", "word/document.xml#t:2", "進度：待確認", "進度：已確認")
            checks = check_case(
                case, {"answer": "完成", "status": "done"}, {"revised.docx": revised}, folder, before, ["edit_text_run"]
            )
            self.assertTrue(all(checks.values()), checks)

    def test_exact_json_rejects_fences_and_extra_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["chat_json"]
            before = prepare_case(folder, case)
            for answer in (
                '```json\n{"project":"A","progress_percent":40,"completion_date":null}\n```',
                '{"project":"A","progress_percent":40,"completion_date":null,"extra":1}',
            ):
                checks = check_case(case, {"answer": answer, "status": "done"}, {}, folder, before, [])
                self.assertFalse(checks["exact_json"])

    def test_mutating_source_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "sources"
            case = BY_ID["chat_math"]
            before = prepare_case(folder, case)
            (folder / "source.txt").write_text("changed")
            checks = check_case(case, {"answer": "200、178、89%、乙", "status": "done"}, {}, folder, before, [])
            self.assertTrue(checks["fact_1"])
            self.assertFalse(checks["sources_unchanged"])


class SingleLoopTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_single_loop_uses_real_tool_result_without_a_paid_request(self):
        import httpx
        from company_agent.evals.comparison import run_single
        from company_agent.runtime.models import GatewayModel, close_model

        self.config.roots[0].name = "demo"
        self.file("source.txt", "本週完成基礎施工。".encode())
        requests = []

        def respond(request):
            data = json.loads(request.content)
            requests.append(data)
            if len(requests) == 1:
                name, args = "read_file", {"root": "demo", "path": "source.txt"}
            else:
                self.assertTrue(any(m["role"] == "tool" and "基礎施工" in m["content"] for m in data["messages"]))
                name, args = (
                    "complete_job",
                    {"outcome": "completed", "summary": "本週完成基礎施工，來源source.txt。", "artifact_ids": []},
                )
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "model": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": f"tool-{len(requests)}",
                                        "type": "function",
                                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                },
            )

        model = GatewayModel("http://localhost/v1", "synthetic-key", "test", httpx.MockTransport(respond))
        try:
            result, files, tools = await run_single(self.config, model, "alice", BY_ID["source_summary"])
        finally:
            await close_model(model)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["status"], "done")
        self.assertIn("基礎施工", result["answer"])
        self.assertEqual(tools, ["read_file", "complete_job"])
        self.assertEqual(files, {})


if __name__ == "__main__":
    unittest.main()
