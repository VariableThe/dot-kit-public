import unittest
from unittest.mock import patch

import test_beeper_http as helpers


class HeaderSafetyTests(unittest.TestCase):
    setUpClass = classmethod(helpers.HTTPTests.setUpClass.__func__)
    setUp = helpers.HTTPTests.setUp
    build = helpers.HTTPTests.build
    call = helpers.HTTPTests.call
    token = helpers.HTTPTests.token

    def test_valid_wsgi_latin1_and_horizontal_tab_remain_supported(self):
        for value in ('x\ty', 'x\x80y', 'x\xffy'):
            with self.subTest(value=ascii(value)):
                self.assertEqual(self.call(token=self.token(), HTTP_X_TEST=value)[0], 200)

    def test_unsafe_header_characters_rejected_before_body_or_dispatch(self):
        class NoRead:
            calls = 0
            def read(self, *args):
                self.calls += 1
                raise AssertionError('rejected headers must not read body')
        with patch('plugins.ecosystem_connectivity.beeper_mcp_plugin.BeeperMCPPlugin.handle') as handle:
            for value in ('x\x00y', 'x\x01y', 'x\x7fy', 'x\ud800y', 'x\u0100y'):
                with self.subTest(value=ascii(value)):
                    stream = NoRead()
                    status, _, _ = self.call(HTTP_X_TEST=value, **{'wsgi.input': stream})
                    self.assertEqual(status, 400)
                    self.assertEqual(stream.calls, 0)
            handle.assert_not_called()


if __name__ == '__main__':
    unittest.main()
