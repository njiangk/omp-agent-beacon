import json
from pathlib import Path
import tempfile
import unittest

from beacon import OmpSource, State


def message(role, **fields):
    return {'type': 'message', 'message': {'role': role, **fields}}


class OmpSourceTests(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.source = OmpSource('/unused')

    def test_parallel_tool_lifecycle_is_deduplicated(self):
        self.source.consume(self.state, message('assistant', model='main-model', content=[
            {'type': 'toolCall', 'id': 'a', 'name': 'read', 'arguments': {}},
            {'type': 'toolCall', 'id': 'b', 'name': 'bash', 'arguments': {}},
        ]))
        self.source.consume(self.state, {'type': 'custom', 'customType': 'tool_execution_start', 'data': {
            'toolCallId': 'a', 'toolName': 'read', 'intent': 'reading file',
        }})
        self.assertEqual(self.state.main.tools, 2)
        self.source.consume(self.state, message('toolResult', toolCallId='a', toolName='read', isError=False))
        self.assertEqual(self.state.main.state, 'running')
        self.source.consume(self.state, message('toolResult', toolCallId='b', toolName='bash', isError=False))
        self.assertEqual(self.state.main.state, 'waiting')
        self.source.consume(self.state, message('assistant', stopReason='stop', content=[]))
        self.assertEqual(self.state.main.state, 'done')

    def test_advisor_context_and_tools_do_not_change_main_or_inflate_calls(self):
        self.source.consume(self.state, {'type': 'model_change', 'model': 'main-model'})
        for _ in range(3):
            self.source.consume(self.state, message('user', content=[]), '__advisor')
        self.source.consume(self.state, message('assistant', model='advisor-model', content=[
            {'type': 'toolCall', 'id': 'advice', 'name': 'advise', 'arguments': {}},
        ]), '__advisor')
        self.source.consume(self.state, message('toolResult', toolCallId='advice', toolName='advise', isError=False), '__advisor')
        self.assertEqual(self.state.advisor.calls, 1)
        self.assertEqual(self.state.advisor.tools, 1)
        self.assertEqual(self.state.advisor.state, 'done')
        self.assertEqual(self.state.main.model, 'main-model')
        self.assertEqual(self.state.main.tools, 0)
        self.assertEqual(self.state.agents, {})

    def test_failure_is_isolated_to_child(self):
        self.source.consume(self.state, message('assistant', model='worker', stopReason='aborted', content=[]), 'Search')
        self.assertEqual(self.state.agents['Search'].state, 'error')
        self.assertEqual(self.state.main.state, 'waiting')

    def test_follow_append_partial_line_and_new_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'workspace' / 'session.jsonl'
            session.parent.mkdir()
            session.write_text(json.dumps({'type': 'session', 'cwd': '/project'}) + '\n')
            source = OmpSource(root)
            source.poll(self.state)
            with session.open('a') as stream:
                stream.write('{"type":"custom","customType":"tool_execution_start","data":{"toolCallId":"one","toolName":"read"}')
            source.poll(self.state)
            self.assertEqual(self.state.main.tools, 0)
            with session.open('a') as stream:
                stream.write('}\n')
            source.poll(self.state)
            self.assertEqual(self.state.main.tools, 1)
            child = session.with_suffix('') / 'Search.jsonl'
            child.parent.mkdir()
            child.write_text(json.dumps(message('assistant', model='worker', stopReason='stop', content=[])) + '\n')
            source.scan_at = 0
            source.poll(self.state)
            self.assertEqual(self.state.agents['Search'].model, 'worker')
            self.assertEqual(self.state.agents['Search'].state, 'done')

    def test_advisor_note_becomes_the_review_and_waits_main(self):
        self.source.consume(self.state, message('user', content=[]), '__advisor')
        self.source.consume(self.state, message('assistant', model='advisor-model', content=[
            {'type': 'toolCall', 'id': 'advice', 'name': 'advise', 'arguments': {'note': 'split the payment step first'}},
        ]), '__advisor')
        self.source.consume(self.state, message('toolResult', toolCallId='advice', toolName='advise', isError=False), '__advisor')
        self.assertEqual(len(self.state.reviews), 1)
        self.assertEqual(self.state.reviews[0].detail, 'split the payment step first')
        self.assertFalse(self.state.reviews[0].active)
        self.assertEqual(self.state.advisor.calls, 1)
        self.assertEqual(self.state.advisor.detail, 'split the payment step first')
        self.source.consume(self.state, message('assistant', stopReason='stop', content=[]), 'search')
        self.state.agents['search'].state = 'running'
        self.state.main.state = 'idle'
        self.assertEqual(self.state.waiting_on(), 'waiting on search')
        self.state.advisor.state = 'consulting'
        self.assertEqual(self.state.waiting_on(), 'waiting on advisor')

    def test_repeated_review_replaces_its_open_entry(self):
        self.source.consume(self.state, message('user', content=[]), '__advisor')
        self.source.consume(self.state, message('assistant', model='advisor-model', content=[
            {'type': 'toolCall', 'id': 'a1', 'name': 'advise', 'arguments': {'note': 'first'}},
        ]), '__advisor')
        self.source.consume(self.state, message('toolResult', toolCallId='a1', toolName='advise', isError=False), '__advisor')
        self.source.consume(self.state, message('user', content=[]), '__advisor')
        self.assertEqual(self.state.advisor.calls, 2)
        self.assertTrue(self.state.reviews[-1].active)
        self.source.consume(self.state, message('assistant', model='advisor-model', content=[
            {'type': 'toolCall', 'id': 'a2', 'name': 'advise', 'arguments': {'note': 'second'}},
        ]), '__advisor')
        self.source.consume(self.state, message('toolResult', toolCallId='a2', toolName='advise', isError=False), '__advisor')
        self.assertEqual([r.detail for r in self.state.reviews], ['first', 'second'])
        self.assertEqual(self.state.advisor.calls, 2)

    def test_usage_accumulates_per_agent_and_ignores_invalid_numbers(self):
        self.source.consume(self.state, message('assistant', model='main-model', content=[], usage={
            'input': 1400, 'output': 100, 'totalTokens': 1500, 'cost': {'total': 0.02},
        }))
        self.source.consume(self.state, message('assistant', model='main-model', content=[], usage={
            'input': 100, 'output': 50, 'cost': {'total': -5},
        }))
        self.source.consume(self.state, message('assistant', model='worker', content=[], usage={
            'totalTokens': 500, 'cost': {'total': 0.001},
        }), 'search')
        self.source.consume(self.state, message('assistant', model='advisor-model', content=[],
                                                usage={'totalTokens': 700, 'cost': {'total': 0.05}}), '__advisor')
        self.assertEqual(self.state.main.tokens, 1650)
        self.assertAlmostEqual(self.state.main.cost, 0.02)
        self.assertEqual(self.state.agents['search'].tokens, 500)
        self.assertEqual(self.state.advisor.tokens, 700)
        self.assertAlmostEqual(self.state.advisor.cost, 0.05)

    def test_usage_event_requires_finite_values(self):
        self.state.event({'type': 'usage', 'agent_id': 'search', 'tokens': 120, 'cost': 0.004})
        self.assertEqual(self.state.agents['search'].tokens, 120)
        self.assertAlmostEqual(self.state.agents['search'].cost, 0.004)
        with self.assertRaises(ValueError):
            self.state.event({'type': 'usage', 'tokens': -1})
        self.assertEqual(self.state.main.tokens, 0)


class ClaudeSourceTests(unittest.TestCase):
    def test_duplicate_message_ids_count_usage_once(self):
        from beacon import ClaudeSource
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'project' / 'session.jsonl'
            session.parent.mkdir()
            rows = [
                {'uuid': 'u1', 'type': 'assistant', 'message': {
                    'role': 'assistant', 'model': 'claude', 'id': 'msg_1', 'content': [],
                    'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 5},
                }},
                {'uuid': 'u2', 'type': 'assistant', 'message': {
                    'role': 'assistant', 'model': 'claude', 'id': 'msg_1', 'content': [],
                    'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 5},
                }},
                {'uuid': 'u3', 'type': 'assistant', 'message': {
                    'role': 'assistant', 'model': 'claude', 'id': 'msg_2', 'content': [],
                    'usage': {'input_tokens': 10, 'output_tokens': 2, 'cache_read_input_tokens': 0},
                }},
            ]
            session.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            state = State()
            source = ClaudeSource(root)
            source.poll(state)
            self.assertEqual(state.main.tokens, 137)

            source.poll(state)
            self.assertEqual(state.main.tokens, 137)


if __name__ == '__main__':
    unittest.main()
