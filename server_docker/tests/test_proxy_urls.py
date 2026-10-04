import unittest

import server_docker.app as appmod


class ProxyUrlTests(unittest.TestCase):
    def setUp(self):
        self.old_public = getattr(appmod, "PUBLIC_BASE_URL", "")
        self.old_trust = getattr(appmod, "TRUST_PROXY_HEADERS", False)

    def tearDown(self):
        appmod.PUBLIC_BASE_URL = self.old_public
        appmod.TRUST_PROXY_HEADERS = self.old_trust

    def test_direct_host_uses_request_origin(self):
        appmod.PUBLIC_BASE_URL = ""
        appmod.TRUST_PROXY_HEADERS = False
        with appmod.app.test_request_context("/", base_url="http://10.0.0.2:18770"):
            links = appmod.build_file_links("wxv_000001.mp4")
        self.assertEqual(links["download_path"], "/files/wxv_000001.mp4")
        self.assertEqual(links["download_url"], "http://10.0.0.2:18770/files/wxv_000001.mp4")

    def test_untrusted_forwarded_headers_are_ignored(self):
        appmod.PUBLIC_BASE_URL = ""
        appmod.TRUST_PROXY_HEADERS = False
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}
        with appmod.app.test_request_context("/", base_url="http://10.0.0.2:18770", headers=headers):
            links = appmod.build_file_links("wxv_000001.txt")
        self.assertEqual(links["download_url"], "http://10.0.0.2:18770/files/wxv_000001.txt")

    def test_trusted_forwarded_scheme_host_and_prefix(self):
        appmod.PUBLIC_BASE_URL = ""
        appmod.TRUST_PROXY_HEADERS = True
        headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "video.example.com",
            "X-Forwarded-Prefix": "/wechat",
        }
        with appmod.app.test_request_context("/", base_url="http://127.0.0.1:8080", headers=headers):
            links = appmod.build_file_links("wxv_000001.md")
        self.assertEqual(links["download_path"], "/wechat/files/wxv_000001.md")
        self.assertEqual(links["download_url"], "https://video.example.com/wechat/files/wxv_000001.md")

    def test_public_base_url_overrides_request_and_forwarded_headers(self):
        appmod.PUBLIC_BASE_URL = "https://downloads.example.com/tools/wx"
        appmod.TRUST_PROXY_HEADERS = True
        headers = {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "ignored.example"}
        with appmod.app.test_request_context("/", base_url="http://127.0.0.1:8080", headers=headers):
            links = appmod.build_file_links("wxv_000001.mp4")
        self.assertEqual(links["download_path"], "/files/wxv_000001.mp4")
        self.assertEqual(links["download_url"], "https://downloads.example.com/tools/wx/files/wxv_000001.mp4")


if __name__ == "__main__":
    unittest.main()
