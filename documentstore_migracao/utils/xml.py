""" module to methods xml file """

import logging
from lxml import etree
from xml.dom.minidom import parseString
from documentstore_migracao.utils import string, convert_html_body

logger = logging.getLogger(__name__)


def unescape_body_html(obj_body):
    _string = obj_body.findtext('.//p')
    if _string:
        _string = string.normalize(_string)

        try:
            return etree.fromstring(
                "<body>{}</body>".format(_string))
        except etree.XMLSyntaxError:
            return obj_body
    return obj_body


def find_medias(obj_xml):

    html = obj_xml.find("body")
    media = []
    # IMG
    imgs = html.findall(".//graphic")
    for img in imgs:
        logger.info("\t IMG %s", img.attrib["src"])
        media.append(img.attrib["src"])

    # FILES
    tags_a = html.findall("a[@href]")
    for a in tags_a:
        href = a.attrib["href"]
        if href.startswith("/img/"):
            logger.info("\t FILE %s", a.attrib)
            media.append(href)

    return media


def parser_body_xml(obj_body):
    str_body = etree.tostring(obj_body, encoding="unicode")
    convert = convert_html_body.HTML2SPSPipeline()
    html = convert.deploy(str_body)

    return html[1]


def old_parser_body_xml(obj_body):

    txt_body = getattr(obj_body.find("./p"), "text", "")
    convert = convert_html_body.HTML2SPSPipeline()
    html = convert.deploy(txt_body)

    return html[1]


def prettyPrint_format(xml_string):
    return parseString(xml_string).toprettyxml()


def get_languages(obj_xml):
    """The language of the main document plus all translations.
    """
    return obj_xml.xpath(
        '/article/@xml:lang | //sub-article[@article-type="translation"]/@xml:lang'
    )
