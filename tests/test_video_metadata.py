import unittest

from wxbot_framework.video_metadata import VideoMetadataError, parse_video_metadata


class VideoMetadataTests(unittest.TestCase):
    def test_parses_video_without_exposing_credentials(self):
        xml = ('<msg><videomsg length="123" rawlength="456" playlength="2" '
               'md5="' + 'a' * 32 + '" rawmd5="' + 'b' * 32 + '" '
               'cdnvideourl="fixture-cdn-value" aeskey="' + 'c' * 32 + '" '
               'cdnrawvideourl="fixture-original" cdnrawvideoaeskey="' + 'd' * 32 + '"/></msg>')
        video = parse_video_metadata(xml)
        self.assertEqual((video.length, video.raw_length, video.duration), (123, 456, 2))
        self.assertEqual(video.raw_md5, 'b' * 32)
        self.assertNotIn('fixture-cdn-value', repr(video))
        self.assertNotIn('c' * 32, repr(video))

    def test_missing_optional_values_are_unknown(self):
        video = parse_video_metadata('<msg><videomsg/></msg>')
        self.assertIsNone(video.length)
        self.assertEqual(video.cdn_url, '')

    def test_rejects_untrusted_xml_and_malformed_fields(self):
        for xml in ('<!DOCTYPE msg><msg><videomsg/></msg>',
                    '<msg><videomsg/><videomsg/></msg>',
                    '<msg><x><videomsg/></x></msg>',
                    '<msg><videomsg length="-1"/></msg>',
                    '<msg><videomsg md5="bad"/></msg>',
                    '<msg><videomsg aeskey="bad"/></msg>',
                    '<msg><videomsg cdnvideourl="' + 'x' * 1024 + '"/></msg>',
                    '<msg><videomsg>\x00</videomsg></msg>'):
            with self.subTest(xml=xml[:40]), self.assertRaises(VideoMetadataError):
                parse_video_metadata(xml)

    def test_error_does_not_expose_credentials(self):
        secret = 'sensitive-example'
        with self.assertRaises(VideoMetadataError) as caught:
            parse_video_metadata('<msg><videomsg aeskey="' + secret + '"/></msg>')
        self.assertNotIn(secret, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
