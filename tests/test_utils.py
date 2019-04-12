import os
import unittest
from unittest.mock import patch
from lxml import etree
from documentstore_migracao.utils import files, xml, request, dicts

from . import SAMPLES_PATH, COUNT_SAMPLES_FILES


class TestUtilsFiles(unittest.TestCase):
    def test_xml_files_list(self):
        self.assertEqual(len(files.xml_files_list(SAMPLES_PATH)), COUNT_SAMPLES_FILES)

    def test_read_file(self):
        data = files.read_file(
            os.path.join(SAMPLES_PATH, "S0036-36341997000100001.xml")
        )
        self.assertIn("0036-3634", data)

    def test_write_file(self):
        expected_text = "<a><b>bar</b></a>"
        filename = "foo_test.txt"

        try:
            files.write_file(filename, expected_text)

            with open(filename, "r") as f:
                text = f.read()
        finally:
            os.remove(filename)

        self.assertEqual(expected_text, text)


class TestUtilsXML(unittest.TestCase):

    def test_unescape_body_html(self):
        text = "<body><p>&lt;b&gt;bar&lt;/b&gt;</p></body>"
        expected_text = "<body><b>bar</b></body>"
        node = etree.fromstring(text)
        obj = xml.unescape_body_html(node)
        self.assertIn(expected_text, etree.tostring(obj, encoding="unicode"))

    def test_unescape_body_html_no_changes(self):
        body_html = "<body><p>&lt;/b&gt;</p></body>"
        body_tree = etree.fromstring(body_html)
        obj = xml.unescape_body_html(body_tree)
        self.assertEqual(
            "<body><p>&lt;/b&gt;</p></body>",
            etree.tostring(obj, encoding="unicode"))

    def test_find_medias(self):

        with open(os.path.join(SAMPLES_PATH, "S0036-36341997000100003.xml"), "r") as f:
            text = f.read()
        obj = etree.fromstring(text)
        medias = xml.find_medias(obj)

        self.assertFalse(len(medias))

    def test_pipe_body_xml(self):
        tags = ("div", "li", "ol", "ul", "i", "b", "a")
        body = etree.Element('body')
        for tag in tags:
            e = etree.Element(tag)
            body.append(e)
        html = xml.parser_body_xml(body)
        for tag in tags:
            with self.subTest(tag=tag):
                expected = html.findall(".//%s" % tag)
                self.assertFalse(expected)


class TestUtilsRequest(unittest.TestCase):
    @patch("documentstore_migracao.utils.request.requests")
    def test_get(self, mk_requests):

        expected = {"params": {"collection": "spa"}}
        request.get("http://api.test.com", **expected)
        mk_requests.get.assert_called_once_with("http://api.test.com", **expected)


class TestUtilsDicts(unittest.TestCase):
    def test_merge(self):
        result = {"1": {"count": 2, "files": ("a", "b")}}

        data = {"count": 1, "files": ("c", "d")}
        dicts.merge(result, "1", data)
        self.assertEqual(result["1"]["count"], 3)

    def test_group(self):
        groups = dicts.group(range(10), 3)
        self.assertEqual(list(groups)[0], (0, 1, 2))

    def test_grouper(self):
        result = dicts.grouper(3, "abcdefg", "x")
        self.assertEqual(list(result)[0], ("a", "b", "c"))
