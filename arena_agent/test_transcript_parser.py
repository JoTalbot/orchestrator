"""Offline regression tests for Arena RSC transcript parsing."""
import json
import unittest
from arena_api import parse_transcript, light_message

class TranscriptParserTests(unittest.TestCase):
    def test_field_order_and_nested_metadata(self):
        obj = {"taskId": "chat", "metadata": {"x": 1},
               "messages": [{"id": "one"}], "pagination": {}}
        self.assertEqual(parse_transcript("a:" + json.dumps(obj))["messages"], obj["messages"])

    def test_text_row(self):
        blob = 'a:T99,prefix {"taskId":"chat","messages":[{"id":"one"}]} suffix'
        self.assertEqual(len(parse_transcript(blob)["messages"]), 1)

    def test_translation_dictionary_is_not_transcript(self):
        self.assertIsNone(parse_transcript('a:{"messages":{"title":"Hello"}}'))

    def test_numeric_reasoning(self):
        result = light_message({"parts": [{"type": "reasoning", "text": 42}, {"type": "text", "text": 0}]})
        self.assertEqual(result["reasoning"], ["42"])
        self.assertEqual(result["text"], "0")

    def test_empty_transcript(self):
        self.assertEqual(parse_transcript('a:{"messages":[]}')["messages"], [])

    def test_rsc_reference(self):
        blob = 'a:{"messages":["$b"]}\nb:{"id":"one"}'
        self.assertEqual(parse_transcript(blob)["messages"], [{"id": "one"}])

if __name__ == "__main__":
    unittest.main()
