"""Offline contract tests for the security gate; no AWS calls or sleep."""
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('gate', 'scripts/ecr_scan_gate.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def response(status='COMPLETE', counts=None):
    return {'imageScanStatus': {'status': status},
            'imageScanFindings': {'findingSeverityCounts': {} if counts is None else counts}}


def result(data=None, error=''):
    return subprocess.CompletedProcess([], 1 if error else 0,
                                       json.dumps(data), error)


class ScanGateTests(unittest.TestCase):
    def test_complete_empty_counts_pass(self):
        gate.check_findings(response())

    def test_vulnerabilities_fail_including_unfixed(self):
        for severity in ('HIGH', 'CRITICAL'):
            with self.subTest(severity=severity), self.assertRaises(ValueError):
                gate.check_findings(response(counts={severity: 1}))

    def test_invalid_or_incomplete_findings_fail(self):
        for data in ({}, response('IN_PROGRESS'),
                     {'imageScanStatus': {'status': 'COMPLETE'}},
                     response(counts={'HIGH': '0'}), response(counts={'CRITICAL': -1})):
            with self.subTest(data=data), self.assertRaises(ValueError):
                gate.check_findings(data)

    def run_gate(self, replies, clock=None):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {'ECR_REPOSITORY': 'ledger', 'IMAGE_DIGEST': 'sha256:test'}), \
                patch.object(gate, 'ecr', side_effect=replies) as aws, \
                patch.object(gate.time, 'sleep'), \
                patch.object(gate.time, 'monotonic', side_effect=clock or [0, 1, 2, 3]), \
                patch('pathlib.Path.write_text'):
            previous = os.getcwd()
            try:
                os.chdir(folder)
                gate.main()
            finally:
                os.chdir(previous)
            return aws

    def test_scan_on_push_race_and_polling(self):
        aws = self.run_gate([
            result(error='LimitExceededException'),
            result(error='ScanNotFoundException'),
            result(response('IN_PROGRESS')),
            result(response()),
        ])
        self.assertEqual(aws.call_count, 4)
        self.assertIn('imageDigest=sha256:test', aws.call_args.args)

    def test_api_denial_is_not_zero_vulnerabilities(self):
        with self.assertRaises(RuntimeError):
            self.run_gate([result({}), result(error='AccessDeniedException')])

    def test_start_scan_failure_blocks(self):
        with self.assertRaises(RuntimeError):
            self.run_gate([result(error='UnsupportedImageError')])

    def test_failed_or_enhanced_status_blocks(self):
        for status in ('FAILED', 'UNSUPPORTED_IMAGE', 'ACTIVE', 'SCAN_ELIGIBILITY_EXPIRED'):
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.run_gate([result({}), result(response(status))])

    def test_poll_timeout_blocks(self):
        with self.assertRaises(TimeoutError):
            self.run_gate([result({}), result(response('IN_PROGRESS'))], clock=[0, 1, 601])


if __name__ == '__main__':
    unittest.main()
