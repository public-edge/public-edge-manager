import os
import ssl
import unittest

os.environ.setdefault("NODE_NAME", "test-node")

from public_edge_manager.redirector import client_hello_sni


def client_hello(hostname):
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    context = ssl.create_default_context()
    connection = context.wrap_bio(incoming, outgoing, server_side=False,
                                  server_hostname=hostname)
    with unittest.TestCase().assertRaises(ssl.SSLWantReadError):
        connection.do_handshake()
    return outgoing.read()


class RedirectorTests(unittest.TestCase):
    def test_extracts_exact_sni_from_client_hello(self):
        data = client_hello("kube-api.service.re8ch.com")
        self.assertEqual(client_hello_sni(data), "kube-api.service.re8ch.com")

    def test_waits_for_complete_tls_record(self):
        data = client_hello("kube-api.service.re8ch.com")
        self.assertIsNone(client_hello_sni(data[:20]))

    def test_returns_none_for_non_tls_input(self):
        self.assertIsNone(client_hello_sni(b"GET / HTTP/1.1\r\n\r\n"))


if __name__ == "__main__":
    unittest.main()
