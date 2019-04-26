import os
import unittest
from unittest.mock import patch
from lxml import etree
from uuid import UUID
from documentstore_migracao.utils import files, xml, request, dicts, string

from . import SAMPLES_PATH, COUNT_SAMPLES_FILES


class TestUtilsFiles(unittest.TestCase):
    def test_extract_filename_ext_by_path(self):

        filename, extension = files.extract_filename_ext_by_path(
            "xml/conversion/S0044-59672014000400003/S0044-59672014000400003.pt.xml"
        )
        self.assertEqual(filename, "S0044-59672014000400003")
        self.assertEqual(extension, ".xml")

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

    def test_write_binary_file(self):
        expected_text = b"<a><b>bar</b></a>"
        filename = "foo_test_binary.txt"

        try:
            files.write_file_binary(filename, expected_text)

            with open(filename, "rb") as f:
                text = f.read()
        finally:
            os.remove(filename)

        self.assertEqual(expected_text, text)

    @patch("documentstore_migracao.utils.files.shutil.move")
    def test_move_xml_to(self, mk_move):

        files.move_xml_to("test.xml", "/tmp/xml/source", "/tmp/xml/destiny")
        mk_move.assert_called_once_with(
            "/tmp/xml/source/test.xml", "/tmp/xml/destiny/test.xml"
        )

    def test_create_dir_exist(self):
        self.assertFalse(files.create_dir("/tmp"))

    def test_create_dir_not_exist(self):
        try:
            files.create_dir("/tmp/create_dir")
            self.assertTrue(os.path.exists("/tmp/create_dir"))

        finally:
            os.rmdir("/tmp/create_dir")

    def test_create_path_by_file(self):
        try:
            path = files.create_path_by_file(
                "/tmp/",
                "/xml/conversion/S0044-59672014000400003/S0044-59672014000400003.pt.xml",
            )

            self.assertEqual("/tmp/S0044-59672014000400003", path)
            self.assertTrue(os.path.exists("/tmp/S0044-59672014000400003"))

        finally:
            os.rmdir("/tmp/S0044-59672014000400003")


class TestUtilsXML(unittest.TestCase):
    def test_str2objXML(self):

        expected_text = "<a><b>bar</b></a>"
        obj = xml.str2objXML(expected_text)

        self.assertIn(expected_text, str(etree.tostring(obj)))

    @patch("documentstore_migracao.utils.xml.etree.fromstring")
    def test_str2objXML_except(self, mk_fromstring):
        def _side_effect(arg):
            if arg == "<body></body>":
                return b"<body></body>"
            raise etree.XMLSyntaxError("Test Error - READING XML", 1, 1, 1)

        mk_fromstring.side_effect = _side_effect
        obj = xml.str2objXML("<a><b>bar</b></a>")

        self.assertIn(b"<body></body>", obj)

    def test_find_medias(self):

        with open(
            os.path.join(SAMPLES_PATH, "S0044-59672003000300001.pt.xml"), "r"
        ) as f:
            text = f.read()
        obj = etree.fromstring(text)
        medias = xml.find_medias(obj)

        self.assertEqual(len(medias), 3)

    def test_pipe_body_xml(self):
        with open(os.path.join(SAMPLES_PATH, "S0036-36341997000100003.xml"), "r") as f:
            text = f.read()

        obj = etree.fromstring(text)
        html = xml.parser_body_xml(obj)
        tags = ("div", "img", "li", "ol", "ul", "i", "b", "a")
        for tag in tags:
            with self.subTest(tag=tag):
                expected = html.findall(".//%s" % tag)
                self.assertFalse(expected)

    def test_file2objXML(self):
        file_path = os.path.join(SAMPLES_PATH, "any.xml")
        expected_text = "<root><a><b>bar</b></a></root>"
        obj = xml.file2objXML(file_path)
        self.assertIn(expected_text, str(etree.tostring(obj)))

    def test_file2objXML_raise_OSError_for_filenotfound(self):
        file_path = os.path.join(SAMPLES_PATH, "none.xml")
        with self.assertRaises(OSError):
            xml.file2objXML(file_path)

    def test_file2objXML_raise_XMLSyntaxError_for_filenotfound(self):
        file_path = os.path.join(SAMPLES_PATH, "file.txt")
        with self.assertRaises(etree.XMLSyntaxError):
            xml.file2objXML(file_path)


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


class TestUtilsStrings(unittest.TestCase):

    def test_uuid2str(self):
        uuid = "585b0b68-aa1d-41ab-8f19-aaa37c516337"
        self.assertEqual(string.uuid2str(UUID(uuid)), "FX6F3cbyYmmwvtGmMB7WCgr")

    def test_str2uuid(self):
        self.assertEqual(
            string.str2uuid("FX6F3cbyYmmwvtGmMB7WCgr"),
            UUID("585b0b68-aa1d-41ab-8f19-aaa37c516337"),
        )

    @patch("documentstore_migracao.utils.string.uuid4")
    def test_generate_scielo_pid(self, mk_uuid4):
        mk_uuid4.return_value = UUID("585b0b68-aa1d-41ab-8f19-aaa37c516337")

        self.assertEqual(string.generate_scielo_pid(), "FX6F3cbyYmmwvtGmMB7WCgr")


class TestGetPublicationDate(unittest.TestCase):

    def test_pubdate_pubtype_epub(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="collection">
                    <year>2011</year>
                </pub-date>
                <pub-date pub-type="epub">
                    <year>2010</year>
                    <month>1</month>
                    <day>9</day>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2010-01-09')

    def test_pubdate_datetype_pub(self):
        _xml = """<root>
            <article-meta>
                <pub-date date-type="collection">
                    <year>2013</year>
                </pub-date>
                <pub-date date-type="pub">
                    <year>2012</year>
                    <month>9</month>
                    <day>3</day>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2012-09-03')

    def test_pubdate_pubtype_collection_year_month(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="collection">
                    <year>2013</year>
                    <month>2</month>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013-02')

    def test_pubdate_pubtype_collection_year(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="collection">
                    <year>2013</year>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013')

    def test_pubdate_pubtype_collection_year_month_day(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="collection">
                    <year>2013</year>
                    <month>2</month>
                    <day>4</day>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013-02-04')

    def test_pubdate_datetype_collection_year_month(self):
        _xml = """<root>
            <article-meta>
                <pub-date date-type="collection">
                    <year>2013</year>
                    <month>2</month>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013-02')

    def test_pubdate_datetype_collection_year(self):
        _xml = """<root>
            <article-meta>
                <pub-date date-type="collection">
                    <year>2013</year>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013')

    def test_pubdate_datetype_collection_year_month_day(self):
        _xml = """<root>
            <article-meta>
                <pub-date date-type="collection">
                    <year>2013</year>
                    <month>2</month>
                    <day>4</day>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013-02-04')

    def test_pubdate_pubtype_epubppub_year_month(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="epub-pub">
                    <year>2013</year>
                    <month>2</month>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013-02')

    def test_pubdate_pubtype_epubppub_year(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="epub-pub">
                    <year>2013</year>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013')

    def test_pubdate_pubtype_epubppub_year_month_day(self):
        _xml = """<root>
            <article-meta>
                <pub-date pub-type="epub-pub">
                    <year>2013</year>
                    <month>2</month>
                    <day>4</day>
                </pub-date>
            </article-meta>
        </root>
        """
        article_xml = etree.fromstring(_xml)
        self.assertEqual(
            xml.get_document_publication_date_for_migration(article_xml),
            '2013-02-04')
