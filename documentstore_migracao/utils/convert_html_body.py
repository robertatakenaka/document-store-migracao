import logging
import plumber
import html
import re
import os
from urllib import request, error
from copy import deepcopy
from lxml import etree
from documentstore_migracao.utils import files
from documentstore_migracao.utils import xml as utils_xml
from documentstore_migracao import config
from documentstore_migracao.utils.convert_html_body_inferer import Inferer

import faulthandler

faulthandler.enable()
logger = logging.getLogger(__name__)
TIMEOUT = config.get("TIMEOUT") or 5


def _remove_element_or_comment(node, remove_inner=False):
    parent = node.getparent()
    if parent is None:
        return

    removed = node.tag
    try:
        node.tag = "REMOVE_NODE"
    except AttributeError:
        is_comment = True
        node_text = ""
    else:
        is_comment = False
        node_text = node.text or ""
        text = get_node_text(node)

    if is_comment or remove_inner or not text.strip():
        _preserve_node_tail_before_remove_node(node, node_text)
        parent.remove(node)
        return removed
    etree.strip_tags(parent, "REMOVE_NODE")
    return removed


def _process(xml, tag, func):
    logger.debug("\tbuscando tag '%s'", tag)
    nodes = xml.findall(".//%s" % tag)
    for node in nodes:
        func(node)
    logger.info("Total de %s tags '%s' processadas", len(nodes), tag)


def wrap_node(node, elem_wrap="p"):

    _node = deepcopy(node)
    p = etree.Element(elem_wrap)
    p.append(_node)
    node.getparent().replace(node, p)

    return p


def wrap_content_node(_node, elem_wrap="p"):

    p = etree.Element(elem_wrap)
    if _node.text:
        p.text = _node.text
    if _node.tail:
        p.tail = _node.tail

    _node.text = None
    _node.tail = None
    _node.insert(0, p)


def find_or_create_asset_node(root, elem_name, elem_id, node=None):
    if elem_name is None or elem_id is None:
        return
    xpath = './/{}[@id="{}"]'.format(elem_name, elem_id)
    asset = root.find(xpath)
    if asset is None and node is not None:
        asset = search_asset_node_backwards(node)
    if asset is None and node is not None:
        parent = node.getparent()
        if parent is not None:
            previous = parent.getprevious()
            if previous is not None:
                asset = previous.find(".//*[@id]")
                if asset is not None and len(asset.getchildren()) > 0:
                    asset = None

    if asset is None:
        asset = etree.Element(elem_name)
        asset.set("id", elem_id)
    return asset


def get_node_text(node):
    if node is None:
        return ""
    return join_texts([item.strip() for item in node.itertext() if item.strip()])


class CustomPipe(plumber.Pipe):
    def __init__(self, super_obj=None, *args, **kwargs):

        self.super_obj = super_obj
        super(CustomPipe, self).__init__(*args, **kwargs)


class HTML2SPSPipeline(object):
    def __init__(self, pid, index_body=1):
        self.pid = pid
        self.index_body = index_body
        self.document = Document(None)
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.SaveRawBodyPipe(super_obj=self),
            self.ConvertRemote2LocalPipe(),
            self.DeprecatedHTMLTagsPipe(),
            self.RemoveImgSetaPipe(),
            self.RemoveDuplicatedIdPipe(),
            self.RemoveExcedingStyleTagsPipe(),
            self.RemoveEmptyPipe(),
            self.RemoveStyleAttributesPipe(),
            self.RemoveCommentPipe(),
            self.BRPipe(),
            self.PPipe(),
            self.DivPipe(),
            self.LiPipe(),
            self.OlPipe(),
            self.UlPipe(),
            self.DefListPipe(),
            self.DefItemPipe(),
            self.IPipe(),
            self.EmPipe(),
            self.UPipe(),
            self.BPipe(),
            self.StrongPipe(),
            self.ConvertElementsWhichHaveIdPipe(),
            self.TdCleanPipe(),
            self.TableCleanPipe(),
            self.BlockquotePipe(),
            self.HrPipe(),
            self.TagsHPipe(),
            self.DispQuotePipe(),
            self.GraphicChildrenPipe(),
            self.FixBodyChildrenPipe(),
            self.RemovePWhichIsParentOfPPipe(),
            self.RemoveRefIdPipe(),
            self.FixIdAndRidPipe(super_obj=self),
            self.SanitizationPipe(),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(plumber.Pipe):
        def transform(self, data):
            try:
                text = etree.tostring(data)
            except TypeError:
                xml = utils_xml.str2objXML(data)
                text = data
            else:
                xml = data
            return text, xml

    class SaveRawBodyPipe(CustomPipe):
        def transform(self, data):
            raw, xml = data
            root = xml.getroottree()
            root.write(
                os.path.join(
                    "/tmp/", "%s.%s.xml" % (
                        self.super_obj.pid, self.super_obj.index_body)),
                encoding="utf-8",
                doctype=config.DOC_TYPE_XML,
                xml_declaration=True,
                pretty_print=True,
            )
            return data, xml

    class ConvertRemote2LocalPipe(plumber.Pipe):
        def transform(self, data):
            logger.info("ConvertRemote2LocalPipe")
            raw, xml = data
            html_page = Remote2LocalConversion(xml)
            html_page.remote_to_local()
            logger.info("ConvertRemote2LocalPipe - fim")
            return data

    class DeprecatedHTMLTagsPipe(plumber.Pipe):
        TAGS = ["font", "small", "big", "dir", "span", "s", "lixo", "center"]

        def transform(self, data):
            raw, xml = data
            for tag in self.TAGS:
                nodes = xml.findall(".//" + tag)
                if len(nodes) > 0:
                    etree.strip_tags(xml, tag)
            return data

    class RemoveDuplicatedIdPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data

            nodes = xml.findall(".//*[@id]")
            root = xml.getroottree()
            for node in nodes:
                attr = node.attrib
                d_ids = root.findall(".//*[@id='%s']" % attr["id"])
                if len(d_ids) > 1:
                    for index, d_n in enumerate(d_ids[1:]):
                        d_n.set("id", "%s-duplicate-%s" % (attr["id"], index))

            return data

    class RemoveExcedingStyleTagsPipe(plumber.Pipe):
        TAGS = ("b", "i", "em", "strong", "u")

        def transform(self, data):
            raw, xml = data
            for tag in self.TAGS:
                for node in xml.findall(".//" + tag):
                    text = get_node_text(node)
                    if not text:
                        node.tag = "STRIPTAG"
            etree.strip_tags(xml, "STRIPTAG")
            return data

    class RemoveEmptyPipe(plumber.Pipe):
        EXCEPTIONS = ["a", "br", "img", "hr"]

        def _is_empty_element(self, node):
            return node.findall("*") == [] and not (node.text or "").strip()

        def _remove_empty_tags(self, xml):
            removed_tags = []
            for node in xml.xpath("//*"):
                if node.tag not in self.EXCEPTIONS:
                    if self._is_empty_element(node):
                        removed = _remove_element_or_comment(node)
                        if removed:
                            removed_tags.append(removed)
            return removed_tags

        def transform(self, data):
            raw, xml = data
            total_removed_tags = []
            remove = True
            while remove:
                removed_tags = self._remove_empty_tags(xml)
                total_removed_tags.extend(removed_tags)
                remove = len(removed_tags) > 0
            if len(total_removed_tags) > 0:
                logger.info(
                    "Total de %s tags vazias removidas", len(total_removed_tags)
                )
                logger.info(
                    "Tags removidas:%s ",
                    ", ".join(sorted(list(set(total_removed_tags)))),
                )
            return data

    class RemoveStyleAttributesPipe(plumber.Pipe):
        EXCEPT_FOR = [
            "caption",
            "col",
            "colgroup",
            "style-content",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
        ]

        def transform(self, data):
            raw, xml = data
            count = 0
            for node in xml.xpath(".//*"):
                if node.tag in self.EXCEPT_FOR:
                    continue
                _attrib = deepcopy(node.attrib)
                style = _attrib.pop("style", None)
                if style:
                    count += 1
                    logger.debug("removendo style da tag '%s'", node.tag)
                node.attrib.clear()
                node.attrib.update(_attrib)
            logger.info("Total de %s tags com style", count)
            return data

    class BRPipe(plumber.Pipe):
        ALLOWED_IN = [
            "aff",
            "alt-title",
            "article-title",
            "chem-struct",
            "disp-formula",
            "product",
            "sig",
            "sig-block",
            "subtitle",
            "td",
            "th",
            "title",
            "trans-subtitle",
            "trans-title",
        ]

        def transform(self, data):
            raw, xml = data
            changed = False
            nodes = xml.findall("*[br]")
            for node in nodes:
                if node.tag in self.ALLOWED_IN:
                    for br in node.findall("br"):
                        br.tag = "break"
                elif node.tag == "p":
                    if node.text:
                        p = etree.Element("p")
                        p.set("content-type", "break")
                        p.text = node.text
                        node.insert(0, p)
                        node.text = ""
                    for br in node.findall("br"):
                        br.tag = "p"
                        br.set("content-type", "break")
                        if br.tail:
                            br.text = br.tail
                            br.tail = ""
                    _remove_element_or_comment(node)

            etree.strip_tags(xml, "br")
            return data

    class PPipe(plumber.Pipe):
        TAGS = [
            "abstract",
            "ack",
            "annotation",
            "app",
            "app-group",
            "author-comment",
            "author-notes",
            "bio",
            "body",
            "boxed-text",
            "caption",
            "def",
            "disp-quote",
            "fig",
            "fn",
            "glossary",
            "list-item",
            "note",
            "notes",
            "open-access",
            "ref-list",
            "sec",
            "speech",
            "statement",
            "supplementary-material",
            "support-description",
            "table-wrap-foot",
            "td",
            "th",
            "trans-abstract",
        ]

        def parser_node(self, node):
            _id = node.attrib.pop("id", None)
            node.attrib.clear()
            if _id:
                node.set("id", _id)

            etree.strip_tags(node, "big")

            parent = node.getparent()
            if not parent.tag in self.TAGS:
                logger.warning("Tag `p` in `%s`", parent.tag)

        def transform(self, data):
            raw, xml = data
            _process(xml, "p", self.parser_node)
            return data

    class DivPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "p"
            _id = node.attrib.pop("id", None)
            node.attrib.clear()
            if _id:
                node.set("id", _id)

        def transform(self, data):
            raw, xml = data

            _process(xml, "div", self.parser_node)
            return data

    class LiPipe(plumber.Pipe):
        ALLOWED_CHILDREN = ("label", "title", "p", "def-list", "list")

        def parser_node(self, node):
            node.tag = "list-item"
            node.attrib.clear()

            c_not_allowed = [
                c_node
                for c_node in node.getchildren()
                if c_node.tag not in self.ALLOWED_CHILDREN
            ]
            for c_node in c_not_allowed:
                wrap_node(c_node, "p")

            if node.text:
                p = etree.Element("p")
                p.text = node.text

                node.insert(0, p)
                node.text = ""

        def transform(self, data):
            raw, xml = data

            _process(xml, "li", self.parser_node)

            return data

    class OlPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "list"
            node.set("list-type", "order")

        def transform(self, data):
            raw, xml = data

            _process(xml, "ol", self.parser_node)
            return data

    class UlPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "list"
            node.set("list-type", "bullet")
            node.attrib.pop("list", None)

        def transform(self, data):
            raw, xml = data

            _process(xml, "ul", self.parser_node)
            return data

    class DefListPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "def-list"
            node.attrib.clear()

        def transform(self, data):
            raw, xml = data

            _process(xml, "dl", self.parser_node)
            return data

    class DefItemPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "def-item"
            node.attrib.clear()

        def transform(self, data):
            raw, xml = data

            _process(xml, "dd", self.parser_node)
            return data

    class IPipe(plumber.Pipe):
        def parser_node(self, node):
            etree.strip_tags(node, "break")
            node.tag = "italic"
            node.attrib.clear()

        def transform(self, data):
            raw, xml = data

            _process(xml, "i", self.parser_node)
            return data

    class BPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "bold"
            etree.strip_tags(node, "break")
            etree.strip_tags(node, "span")
            etree.strip_tags(node, "p")
            node.attrib.clear()

        def transform(self, data):
            raw, xml = data

            _process(xml, "b", self.parser_node)
            return data

    class StrongPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "bold"
            node.attrib.clear()
            etree.strip_tags(node, "span")
            etree.strip_tags(node, "p")

        def transform(self, data):
            raw, xml = data

            _process(xml, "strong", self.parser_node)
            return data

    class TdCleanPipe(plumber.Pipe):
        EXPECTED_INNER_TAGS = [
            "email",
            "ext-link",
            "uri",
            "hr",
            "inline-supplementary-material",
            "related-article",
            "related-object",
            "disp-formula",
            "disp-formula-group",
            "break",
            "citation-alternatives",
            "element-citation",
            "mixed-citation",
            "nlm-citation",
            "bold",
            "fixed-case",
            "italic",
            "monospace",
            "overline",
            "roman",
            "sans-serif",
            "sc",
            "strike",
            "underline",
            "ruby",
            "chem-struct",
            "inline-formula",
            "def-list",
            "list",
            "tex-math",
            "mml:math",
            "p",
            "abbrev",
            "index-term",
            "index-term-range-end",
            "milestone-end",
            "milestone-start",
            "named-content",
            "styled-content",
            "alternatives",
            "array",
            "code",
            "graphic",
            "media",
            "preformat",
            "inline-graphic",
            "inline-media",
            "private-char",
            "fn",
            "target",
            "xref",
            "sub",
            "sup",
        ]
        EXPECTED_ATTRIBUTES = [
            "abbr",
            "align",
            "axis",
            "char",
            "charoff",
            "colspan",
            "content-type",
            "headers",
            "id",
            "rowspan",
            "scope",
            "style",
            "valign",
            "xml:base",
        ]

        def parser_node(self, node):
            for c_node in node.getchildren():
                if c_node.tag not in self.EXPECTED_INNER_TAGS:
                    _remove_element_or_comment(c_node)

            _attrib = {}
            for key in node.attrib.keys():
                if key in self.EXPECTED_ATTRIBUTES:
                    _attrib[key] = node.attrib[key].lower()
            node.attrib.clear()
            node.attrib.update(_attrib)

        def transform(self, data):
            raw, xml = data

            _process(xml, "td", self.parser_node)
            return data

    class TableCleanPipe(TdCleanPipe):
        EXPECTED_INNER_TAGS = ["col", "colgroup", "thead", "tfoot", "tbody", "tr"]

        EXPECTED_ATTRIBUTES = [
            "border",
            "cellpadding",
            "cellspacing",
            "content-type",
            "frame",
            "id",
            "rules",
            "specific-use",
            "style",
            "summary",
            "width",
            "xml:base",
        ]

        def transform(self, data):
            raw, xml = data

            _process(xml, "table", self.parser_node)
            return data

    class EmPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "italic"
            node.attrib.clear()
            etree.strip_tags(node, "break")

        def transform(self, data):
            raw, xml = data

            _process(xml, "em", self.parser_node)
            return data

    class UPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "underline"

        def transform(self, data):
            raw, xml = data

            _process(xml, "u", self.parser_node)
            return data

    class BlockquotePipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "disp-quote"

        def transform(self, data):
            raw, xml = data

            _process(xml, "blockquote", self.parser_node)
            return data

    class HrPipe(plumber.Pipe):
        def parser_node(self, node):
            node.attrib.clear()
            node.tag = "p"
            node.set("content-type", "hr")

        def transform(self, data):
            raw, xml = data

            _process(xml, "hr", self.parser_node)
            return data

    class TagsHPipe(plumber.Pipe):
        def parser_node(self, node):
            node.attrib.clear()
            org_tag = node.tag
            node.tag = "p"
            node.set("content-type", org_tag)

        def transform(self, data):
            raw, xml = data

            tags = ["h1", "h2", "h3", "h4", "h5", "h6"]
            for tag in tags:
                _process(xml, tag, self.parser_node)
            return data

    class DispQuotePipe(plumber.Pipe):
        TAGS = [
            "label",
            "title",
            "address",
            "alternatives",
            "array",
            "boxed-text",
            "chem-struct-wrap",
            "code",
            "fig",
            "fig-group",
            "graphic",
            "media",
            "preformat",
            "supplementary-material",
            "table-wrap",
            "table-wrap-group",
            "disp-formula",
            "disp-formula-group",
            "def-list",
            "list",
            "tex-math",
            "mml:math",
            "p",
            "related-article",
            "related-object",
            "disp-quote",
            "speech",
            "statement",
            "verse-group",
            "attrib",
            "permissions",
        ]

        def parser_node(self, node):
            node.attrib.clear()
            if node.text and node.text.strip():
                new_p = etree.Element("p")
                new_p.text = node.text
                node.text = None
                node.insert(0, new_p)

            for c_node in node.getchildren():
                if c_node.tail and c_node.tail.strip():
                    new_p = etree.Element("p")
                    new_p.text = c_node.tail
                    c_node.tail = None
                    c_node.addnext(new_p)

                if c_node.tag not in self.TAGS:
                    wrap_node(c_node, "p")

        def transform(self, data):
            raw, xml = data

            _process(xml, "disp-quote", self.parser_node)
            return data

    class GraphicChildrenPipe(plumber.Pipe):
        TAGS = (
            "addr-line",
            "alternatives",
            "alt-title",
            "article-title",
            "attrib",
            "award-id",
            "bold",
            "chapter-title",
            "code",
            "collab",
            "comment",
            "compound-kwd-part",
            "compound-subject-part",
            "conf-theme",
            "data-title",
            "def-head",
            "disp-formula",
            "element-citation",
            "fixed-case",
            "funding-source",
            "inline-formula",
            "italic",
            "label",
            "license-p",
            "meta-value",
            "mixed-citation",
            "monospace",
            "named-content",
            "overline",
            "p",
            "part-title",
            "private-char",
            "product",
            "roman",
            "sans-serif",
            "sc",
            "see",
            "see-also",
            "sig",
            "sig-block",
            "source",
            "std",
            "strike",
            "styled-content",
            "sub",
            "subject",
            "subtitle",
            "sup",
            "supplement",
            "support-source",
            "td",
            "term",
            "term-head",
            "textual-form",
            "th",
            "title",
            "trans-source",
            "trans-subtitle",
            "trans-title",
            "underline",
            "verse-line",
        )

        def parser_node(self, node):
            parent = node.getparent()
            if parent.tag in self.TAGS:
                node.tag = "inline-graphic"

        def transform(self, data):
            raw, xml = data

            _process(xml, "graphic", self.parser_node)
            return data

    class RemoveCommentPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data

            comments = xml.xpath("//comment()")
            for comment in comments:
                _remove_element_or_comment(comment)
            logger.info("Total de %s 'comentarios' processadas", len(comments))
            return data

    class HTMLEscapingPipe(plumber.Pipe):
        def parser_node(self, node):
            text = node.text
            if text:
                node.text = html.escape(text)

        def transform(self, data):
            raw, xml = data
            _process(xml, "*", self.parser_node)
            return data

    class RemovePWhichIsParentOfPPipe(plumber.Pipe):
        def _tag_texts(self, xml):
            for node in xml.xpath(".//p[p]"):
                if node.text and node.text.strip():
                    new_p = etree.Element("p")
                    new_p.text = node.text
                    node.text = ""
                    node.insert(0, new_p)

                for child in node.getchildren():
                    if child.tail and child.tail.strip():
                        new_p = etree.Element("p")
                        new_p.text = child.tail
                        child.tail = ""
                        child.addnext(new_p)

        def _identify_extra_p_tags(self, xml):
            for node in xml.xpath(".//p[p]"):
                node.tag = "REMOVE_P"

        def _tag_text_in_body(self, xml):
            for body in xml.xpath(".//body"):
                for node in body.findall("*"):
                    if node.tail and node.tail.strip():
                        new_p = etree.Element("p")
                        new_p.text = node.tail
                        node.tail = ""
                        node.addnext(new_p)

        def _solve_open_p(self, xml):
            node = xml.find(".//p[p]")
            if node is not None:
                new_p = etree.Element("p")
                if node.text and node.text.strip():
                    new_p.text = node.text
                    node.text = ""
                for child in node.getchildren():
                    if child.tag != "p":
                        new_p.append(deepcopy(child))
                        node.remove(child)
                    else:
                        break
                if new_p.text or new_p.getchildren():
                    node.addprevious(new_p)
                node.tag = "REMOVE_P"
                etree.strip_tags(xml, "REMOVE_P")

        def _solve_open_p_items(self, xml):
            node = xml.find(".//p[p]")
            while node is not None:
                self._solve_open_p(xml)
                node = xml.find(".//p[p]")

        def transform(self, data):
            raw, xml = data
            self._solve_open_p_items(xml)
            # self._tag_texts(xml)
            # self._identify_extra_p_tags(xml)
            # self._tag_text_in_body(xml)
            etree.strip_tags(xml, "REMOVE_P")
            return data

    class RemoveRefIdPipe(plumber.Pipe):
        def parser_node(self, node):
            node.attrib.pop("xref_id", None)

        def transform(self, data):
            raw, xml = data

            _process(xml, "*[@xref_id]", self.parser_node)
            return data

    class FixIdAndRidPipe(CustomPipe):
        def transform(self, data):
            raw, xml = data
            for node in xml.findall(".//*[@rid]"):
                self._update(node, "rid")
            for node in xml.findall(".//*[@id]"):
                self._update(node, "id")
            return data, xml

        def _update(self, node, attr_name):
            value = node.get(attr_name)
            value = self._fix(node.get("ref-type") or node.tag, value)
            node.attrib[attr_name] = value

        def _fix(self, tag, value):
            if not value:
                value = tag
            if not value.isalnum():
                value = "".join([c if c.isalnum() else "_" for c in value])
            if not value[0].isalpha():
                if tag[0] in value:
                    value = value[value.find(tag[0]):]
                else:
                    value = tag[:3] + value
            if self.super_obj.index_body > 1:
                value = value + "-body{}".format(self.super_obj.index_body)
            return value.lower()

    class SanitizationPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data

            convert = DataSanitizationPipeline()
            _, obj = convert.deploy(xml)
            return raw, obj

    class RemoveImgSetaPipe(plumber.Pipe):
        def parser_node(self, node):
            if "/seta." in node.find("img").attrib.get("src"):
                _remove_element_or_comment(node.find("img"))

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[img]", self.parser_node)
            return data

    class ConvertElementsWhichHaveIdPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data

            convert = ConvertElementsWhichHaveIdPipeline()
            _, obj = convert.deploy(xml)
            return raw, obj

    class FixBodyChildrenPipe(plumber.Pipe):
        ALLOWED_CHILDREN = [
            "address",
            "alternatives",
            "array",
            "boxed-text",
            "chem-struct-wrap",
            "code",
            "fig",
            "fig-group",
            "graphic",
            "media",
            "preformat",
            "supplementary-material",
            "table-wrap",
            "table-wrap-group",
            "disp-formula",
            "disp-formula-group",
            "def-list",
            "list",
            "tex-math",
            "mml:math",
            "p",
            "related-article",
            "related-object",
            "disp-quote",
            "speech",
            "statement",
            "verse-group",
            "sec",
            "sig-block",
        ]

        def transform(self, data):
            raw, xml = data
            body = xml.find(".//body")
            if body is not None and body.tag == "body":
                for child in body.getchildren():
                    if child.tag not in self.ALLOWED_CHILDREN:
                        new_child = etree.Element("p")
                        new_child.append(deepcopy(child))
                        child.addprevious(new_child)
                        body.remove(child)
                    elif child.tail:
                        new_child = etree.Element("p")
                        new_child.text = child.tail.strip()
                        child.tail = child.tail.replace(new_child.text, "")
                        child.addnext(new_child)
            return data


class DataSanitizationPipeline(object):
    def __init__(self):
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.GraphicInExtLink(),
            self.TableinBody(),
            self.TableinP(),
            self.AddPinFN(),
            self.WrapNodeInDefItem(),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(plumber.Pipe):
        def transform(self, data):

            new_obj = deepcopy(data)
            return data, new_obj

    class GraphicInExtLink(plumber.Pipe):
        def parser_node(self, node):

            graphic = node.find("graphic")
            graphic.tag = "inline-graphic"
            wrap_node(graphic, "p")

        def transform(self, data):
            raw, xml = data

            _process(xml, "ext-link[graphic]", self.parser_node)
            return data

    class TableinBody(plumber.Pipe):
        def parser_node(self, node):

            table = node.find("table")
            wrap_node(table, "table-wrap")

        def transform(self, data):
            raw, xml = data

            _process(xml, "body[table]", self.parser_node)
            return data

    class TableinP(TableinBody):
        def transform(self, data):
            raw, xml = data

            _process(xml, "p[table]", self.parser_node)
            return data

    class AddPinFN(plumber.Pipe):
        def parser_node(self, node):
            if node.text:
                wrap_content_node(node, "p")

        def transform(self, data):
            raw, xml = data

            _process(xml, "fn", self.parser_node)
            return data

    class WrapNodeInDefItem(plumber.Pipe):
        def parser_node(self, node):
            text = node.text or ""
            tail = node.tail or ""
            if text.strip() or tail.strip():
                wrap_content_node(node, "term")

            for c_node in node.getchildren():
                if c_node.tag not in ["term", "def"]:
                    wrap_node(c_node, "def")

        def transform(self, data):
            raw, xml = data

            _process(xml, "def-item", self.parser_node)
            return data


class ConvertElementsWhichHaveIdPipeline(object):
    def __init__(self):
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.RemoveThumbImgPipe(),
            self.AddNameAndIdToElementAPipe(),
            self.RemoveAnchorAndLinksToTextPipe(),
            self.MoveElementAName(),
            self.DeduceAndSuggestConversionPipe(),
            self.ApplySuggestedConversionPipe(),
            self.AssetElementAddContentPipe(),
            self.AssetElementIdentifyLabelAndCaptionPipe(),
            self.AssetElementFixContentPipe(),
            self.APipe(),
            self.ImgPipe(),
            self.CompleteFnConversionPipe(),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(plumber.Pipe):
        def transform(self, data):
            new_obj = deepcopy(data)
            return data, new_obj

    class RemoveThumbImgPipe(plumber.Pipe):
        def parser_node(self, node):
            path = node.attrib.get("src") or ""
            if "thumb" in path:
                parent = node.getparent()
                _remove_element_or_comment(node, True)
                if parent.tag == "a" and parent.attrib.get("href"):
                    for child in parent.getchildren():
                        _remove_element_or_comment(child, True)
                    parent.tag = "img"
                    parent.set("src", parent.attrib.pop("href"))
                    parent.text = ""

        def transform(self, data):
            raw, xml = data
            _process(xml, "img", self.parser_node)
            return data

    class AddNameAndIdToElementAPipe(plumber.Pipe):
        """Garante que todos os elemento a[@name] e a[@id] tenham @name e @id"""
        def parser_node(self, node):
            _id = node.attrib.get("id")
            _name = node.attrib.get("name")
            if _id is None or (_name and _id != _name):
                node.set("id", _name)
            if _name is None:
                node.set("name", _id)
            href = node.attrib.get("href")
            if href:
                if href[0] == "#":
                    a = etree.Element("a")
                    a.set("name", node.attrib.get("name"))
                    a.set("id", node.attrib.get("id"))
                    node.addprevious(a)
                    node.attrib.pop("id")
                    node.attrib.pop("name")

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@id]", self.parser_node)
            _process(xml, "a[@name]", self.parser_node)
            return data

    class MoveElementAName(plumber.Pipe):
        """
        Os elementos `a[@name]` servem para identificar os ativos digitais
        e/ou notas de rodapé
        Para a identificação ocorrer corretamente, é necessário que a[@name]:
        - contenha os dados de ativos digitais ou notas de rodapé;
        - ou que seu "nós irmãos" sejam dados de ativos digitais ou notas de
        rodapé
        Então, este pipe tem que garantir que `a[@name]` esteja nestas
        situações
        """
        def _select(self, xml):
            for a in xml.findall(".//a[@name]"):
                if not a.getchildren() and a.getnext() is None and not a.tail:
                    a.set("move", "true")

        def _move_nodes(self, xml):
            while True:
                found = xml.find(".//a[@move]")
                if found is None:
                    break
                self._move_a_name(found)

        def _move_a_name(self, a_name):
            parent = a_name.getparent()
            new = deepcopy(a_name)
            parent.addnext(new)
            parent.remove(a_name)
            if new.getnext() is not None or (new.tail or "").strip():
                new.attrib.pop("move")

        def transform(self, data):
            raw, xml = data
            self._select(xml)
            self._move_nodes(xml)
            return data

    class DeduceAndSuggestConversionPipe(plumber.Pipe):
        """Este pipe analisa os dados doss elementos a[@href] e a[@name],
        deduz e sugere tag, id, ref-type para a conversão de elementos,
        adicionando aos elementos a, os atributos: @xml_tag, @xml_id,
        @xml_reftype, @xml_label.
        Por exemplo:
        - a[@href] pode ser convertido para link para um
        ativo digital, pode ser link para uma nota de rodapé, ...
        - a[@name] pode ser convertido para a fig, table-wrap,
        disp-formula, fn, app etc
        Nota: este pipe não executa a conversão.
        """
        inferer = Inferer()

        def _update(self, node, elem_name, ref_type, new_id, text=None):
            node.set("xml_tag", elem_name)
            node.set("xml_reftype", ref_type)
            node.set("xml_id", new_id)
            if text:
                node.set("xml_label", text)

        def _add_xml_attribs_to_a_href_from_text(self, texts):
            for text, data in texts.items():
                nodes_with_id, nodes_without_id = data
                tag_reftype = self.inferer.tag_and_reftype_from_a_href_text(text)
                if not tag_reftype:
                    continue

                tag, reftype = tag_reftype
                node_id = None
                for node in nodes_with_id:
                    node_id = node.attrib.get("href")[1:]
                    new_id = node_id
                    self._update(node, tag, reftype, new_id, text)

                for node in nodes_without_id:
                    alt_id = None
                    if not node_id:
                        href = node.attrib.get("href")
                        tag_reftype_id = self.inferer.tag_and_reftype_and_id_from_filepath(
                            href, tag
                        )
                        if tag_reftype_id:
                            alt_tag, alt_reftype, alt_id = tag_reftype_id
                    if node_id or alt_id:
                        new_id = node_id or alt_id
                        self._update(node, tag, reftype, new_id, text)

        def _classify_nodes(self, nodes):
            incomplete = []
            complete = None
            for node in nodes:
                data = [
                    node.attrib.get("xml_label"),
                    node.attrib.get("xml_tag"),
                    node.attrib.get("xml_reftype"),
                    node.attrib.get("xml_id"),
                ]
                if all(data):
                    complete = data
                else:
                    incomplete.append(node)
            return complete, incomplete

        def _add_xml_attribs_to_a_href_from_file_paths(self, file_paths):
            for path, nodes in file_paths.items():
                new_id = None
                complete, incomplete = self._classify_nodes(nodes)
                if complete:
                    text, tag, reftype, new_id = complete
                else:
                    tag_reftype_id = self.inferer.tag_and_reftype_and_id_from_filepath(
                        path
                    )
                    if tag_reftype_id:
                        tag, reftype, _id = tag_reftype_id
                        new_id = _id
                        text = ""
                if new_id:
                    for node in incomplete:
                        self._update(node, tag, reftype, new_id, text)

        def _add_xml_attribs_to_a_name(self, a_names):
            for name, a_name_and_hrefs in a_names.items():
                new_id = None
                a_name, a_hrefs = a_name_and_hrefs
                complete, incomplete = self._classify_nodes(a_hrefs)
                if complete:
                    text, tag, reftype, new_id = complete
                else:
                    tag_reftype = self.inferer.tag_and_reftype_from_a_href_text(
                        a_name.tail
                    )
                    if not tag_reftype:
                        tag_reftype = self.inferer.tag_and_reftype_from_name(name)
                    if tag_reftype:
                        tag, reftype = tag_reftype
                        new_id = name
                        text = ""
                if new_id:
                    self._update(a_name, tag, reftype, new_id, text)
                    for node in incomplete:
                        self._update(node, tag, reftype, new_id, text)

        def _search_asset_node_related_to_img(self, new_id, img):
            if new_id:
                asset_node = img.getroottree().find(".//*[@xml_id='{}']".format(new_id))
                if asset_node is not None:
                    return asset_node
            found = search_asset_node_backwards(img, "xml_tag")
            if found is not None and found.attrib.get("name"):
                if found.attrib.get("xml_tag") in ["app", "fig", "table-wrap", "disp-formula"]:
                    return found

        def _add_xml_attribs_to_img(self, images):
            for path, images in images.items():
                text, new_id, tag, reftype = None, None, None, None
                tag_reftype_id = self.inferer.tag_and_reftype_and_id_from_filepath(path)
                if tag_reftype_id:
                    tag, reftype, _id = tag_reftype_id
                    new_id = _id
                for img in images:
                    found = self._search_asset_node_related_to_img(new_id, img)
                    if found is not None:
                        text = found.attrib.get("xml_label")
                        new_id = found.attrib.get("xml_id")
                        tag = found.attrib.get("xml_tag")
                        reftype = found.attrib.get("xml_reftype")
                    if all([tag, reftype, new_id]):
                        self._update(img, tag, reftype, new_id, text)

        def transform(self, data):
            raw, xml = data
            document = Document(xml)
            texts, file_paths = document.a_href_items
            names = document.a_names
            images = document.images
            self._add_xml_attribs_to_a_href_from_text(texts)
            self._add_xml_attribs_to_a_name(names)
            self._add_xml_attribs_to_a_href_from_file_paths(file_paths)
            self._add_xml_attribs_to_img(images)
            return data

    class RemoveAnchorAndLinksToTextPipe(plumber.Pipe):
        """
        No texto há ancoras e referencias cruzada do texto para as notas e
        também das notas para o texto. Este pipe é para remover as
        âncoras e referências cruzadas das notas para o texto.
        """
        def _identify_order(self, xml):
            items_by_id = {}
            for a in xml.findall(".//a[@xml_tag]"):
                if a.attrib.get("xml_tag") == "fn":
                    _id = a.attrib.get("xml_id")
                    items_by_id[_id] = items_by_id.get(_id, [])
                    items_by_id[_id].append(a)
            return items_by_id

        def _exclude(self, items_by_id):
            for _id, nodes in items_by_id.items():
                if len(nodes) >= 2 and nodes[0].attrib.get("name"):
                    for n in nodes:
                        _remove_element_or_comment(n)

        def transform(self, data):
            raw, xml = data
            items_by_id = self._identify_order(xml)
            self._exclude(items_by_id)
            return data

    class ApplySuggestedConversionPipe(plumber.Pipe):
        """
        Converte os elementos a, para as tags correspondentes, considerando
        os valores dos atributos: @xml_tag, @xml_id, @xml_reftype, @xml_label,
        inseridos por DeduceAndSuggestConversionPipe()
        """
        def _remove_a(self, a_name, a_href_items):
            _remove_element_or_comment(a_name, True)
            for a_href in a_href_items:
                _remove_element_or_comment(a_href, True)

        def _update_a_name(self, node, new_id, new_tag):
            _name = node.attrib.get("name")
            #node.attrib.clear()
            node.set("id", new_id)
            if new_tag == "symbol":
                node.set("symbol", _name)
                new_tag = "fn"
            elif new_tag == "corresp":
                node.set("fn-type", "corresp")
                new_tag = "fn"
            node.tag = new_tag

        def _update_a_href_items(self, a_href_items, new_id, reftype):
            for ahref in a_href_items:
                ahref.attrib.clear()
                ahref.set("ref-type", reftype)
                ahref.set("rid", new_id)
                ahref.tag = "xref"

        def transform(self, data):
            raw, xml = data
            document = Document(xml)
            for name, a_name_and_hrefs in document.a_names.items():
                a_name, a_hrefs = a_name_and_hrefs
                if a_name.attrib.get("xml_id"):
                    new_id = a_name.attrib.get("xml_id")
                    new_tag = a_name.attrib.get("xml_tag")
                    reftype = a_name.attrib.get("xml_reftype")
                    self._update_a_name(a_name, new_id, new_tag)
                    self._update_a_href_items(a_hrefs, new_id, reftype)
                else:
                    self._remove_a(a_name, a_hrefs)
            return data

    class AssetElementAddContentPipe(plumber.Pipe):
        def _find_xml_text_in_node(self, xml_text, node_text):
            if "." in xml_text:
                xml_text_parts = xml_text.replace(".", "").split()
                if len(xml_text_parts) > 1:
                    start, number = xml_text_parts[:2]
                    parts = node_text.split()
                    if parts[0].startswith(start.capitalize()) and parts[1].startswith(number):
                        return True
            elif xml_text.lower().startswith(node_text.lower()):
                return True

        def _find_label_and_content(self, asset_node):
            children = []
            _next = asset_node
            label = asset_node.find(".//label")
            img = asset_node.find(".//img")
            table = asset_node.find(".//table")
            max_times = 5
            for item in [label, img, table]:
                if item is not None:
                    max_times -= 1
            if asset_node.tail:
                asset_node.text = asset_node.tail
                asset_node.tail = ""
            i = 0
            while True:
                _next = _next.getnext()
                if label is not None and (img is not None or table is not None):
                    break
                if i > max_times:
                    break
                if _next is None:
                    break
                if (_next.find(".//fig") is not None or
                    _next.find(".//table-wrap") is not None or
                    _next.find(".//app") is not None):
                    break
                if _next.tag in ["fig", "table-wrap", "app"]:
                    break
                if _next.tag == "img" or _next.findall(".//img"):
                    img = _next
                    img.set("content-type", "img")
                elif _next.tag == "table" or _next.findall(".//table"):
                    table = _next
                    table.set("content-type", "table")
                elif _next.tag == "bold" and _next.get("label-of") or _next.findall(".//bold[@label-of]"):
                    label = _next
                    label.set("content-type", "label")
                else:
                    xml_text = asset_node.get("xml_text") or asset_node.get("xml_label")
                    text = get_node_text(_next)
                    if xml_text and text:
                        if self._find_xml_text_in_node(xml_text, text):
                            label = _next
                            label.set("content-type", "label")
                i += 1
                children.append(_next)
            return children, label, img, table

        def add_content_to_asset_node(self, asset_node):
            asset_node.set("status", "identify-content")
            children, label, img, table = self._find_label_and_content(asset_node)
            found = [item for item in [label, img, table] if item is not None]
            p = asset_node.getparent()

            if label is not None and (img is not None or table is not None):
                for child in children:
                    asset_node.append(deepcopy(child))
                    p.remove(child)
            elif img is not None:
                for child in children:
                    if asset_node.find(".//img") is not None:
                        break
                    asset_node.append(deepcopy(child))
                    p.remove(child)
            elif table is not None:
                for child in children:
                    if asset_node.find(".//table") is not None:
                        break
                    asset_node.append(deepcopy(child))
                    p.remove(child)

        def transform(self, data):
            raw, xml = data
            for tag in ("disp-formula", "fig", "table-wrap", "app"):
                logger.info("AssetElementAddContentPipe - {}".format(tag))
                for asset_node in xml.findall(".//{}".format(tag)):
                    self.add_content_to_asset_node(asset_node)
            return data

    class AssetElementIdentifyLabelAndCaptionPipe(plumber.Pipe):

        def transform(self, data):
            raw, xml = data
            logger.info("AssetElementIdentifyLabelAndCaptionPipe")
            for asset_node in xml.findall(".//*[@status='identify-content']"):
                self.identify_label_and_caption(asset_node)
                id = asset_node.get("id")
                asset_node.attrib.clear()
                asset_node.set("id", id)
            return data

        def identify_label_and_caption(self, asset_node):
            search_expr = asset_node.get("xml_text") or asset_node.get("xml_label")
            if search_expr is None or not search_expr[0].isalpha():
                return
            if asset_node.get("content-type") == "label":
                label_parent = asset_node
            else:
                label_parent = asset_node.find(".//*[@content-type='label']")

            if label_parent is None:
                for child in asset_node.findall("*"):
                    label_text = get_node_text(child).lower()
                    if label_text.startswith(search_expr):
                        child.set("content-type", "label")
                        label_parent = child
                        break

            if label_parent is not None:
                label_of = label_parent.find(".//*[@label-of]")
                if label_of is None:
                    for child in [label_parent] + label_parent.findall(".//*"):
                        child_text = (child.text or "").lower()
                        if child_text.startswith(search_expr):
                            label_of = child
                            break

                if label_of is not None:
                    node_text = (label_of.text or "").strip()

                    label = etree.Element("label")
                    caption = etree.Element("caption")
                    title = etree.Element("title")
                    caption.append(title)
                    label.text = node_text[:len(search_expr)]
                    title.text = node_text[len(search_expr):]

                    n = label_of
                    while True:
                        n = n.getnext()
                        if n is None or n.tag in ["table", "img"]:
                            break
                        if n.find(".//table") is not None:
                            break
                        if n.find(".//img") is not None:
                            break
                        title.append(n)

                    parent = label_of.getparent()
                    label_of.addprevious(label)
                    if get_node_text(caption):
                        label_of.addprevious(caption)
                    parent.remove(label_of)

    class AssetElementFixContentPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            logger.info("AssetElementFixContentPipe")
            for tag in ("disp-formula", "fig", "table-wrap", "app"):
                for node in xml.findall(".//{}".format(tag)):
                    while True:
                        for child in node.getchildren():
                            if child.tag in ("label", "caption"):
                                child.attrib.clear()
                            elif child.tag in ("img"):
                                src = child.get("src")
                                child.attrib.clear()
                                child.set("src", src)
                            else:
                                child.tag = "REMOVEPFIXASSETCONTENT"
                        if node.find("REMOVEPFIXASSETCONTENT") is None:
                            break
                        etree.strip_tags(xml, "REMOVEPFIXASSETCONTENT")
            return data

    class APipe(plumber.Pipe):
        def _parser_node_external_link(self, node, extlinktype="uri"):
            node.tag = "ext-link"

            href = node.attrib.get("href")
            node.attrib.clear()
            _attrib = {
                "ext-link-type": extlinktype,
                "{http://www.w3.org/1999/xlink}href": href,
            }
            node.attrib.update(_attrib)

        def _create_email(self, node):
            a_node_copy = deepcopy(node)
            href = a_node_copy.attrib.get("href")
            if "mailto:" in href:
                href = href.split("mailto:")[1]

            node.attrib.clear()
            node.tag = "email"

            if not href:
                # devido ao caso do href estar mal
                # formado devemos so trocar a tag
                # e retorna para continuar o Pipe
                return

            img = node.find("img")
            if img is not None:
                graphic = etree.Element("graphic")
                graphic.attrib["{http://www.w3.org/1999/xlink}href"] = img.attrib["src"]
                _remove_element_or_comment(img)
                parent = node.getprevious() or node.getparent()
                graphic.append(node)
                parent.append(graphic)

            if not href:
                return

            if node.text and node.text.strip():
                if href == node.text:
                    pass
                elif href in node.text:
                    node.tag = "REMOVE_TAG"
                    texts = node.text.split(href)
                    node.text = texts[0]
                    email = etree.Element("email")
                    email.text = href
                    email.tail = texts[1]
                    node.append(email)
                    etree.strip_tags(node.getparent(), "REMOVE_TAG")
                else:
                    node.attrib["{http://www.w3.org/1999/xlink}href"] = href
            if not node.text:
                node.text = href

        def _parser_node_anchor(self, node):
            root = node.getroottree()

            href = node.attrib.pop("href")

            node.tag = "xref"
            node.attrib.clear()

            xref_name = href.replace("#", "")
            if xref_name == "ref":
                rid = node.text or ""
                if not rid.isdigit():
                    rid = (
                        rid.replace("(", "")
                        .replace(")", "")
                        .replace("-", ",")
                        .split(",")
                    )
                    rid = rid[0]
                node.set("rid", "B%s" % rid)
                node.set("ref-type", "bibr")
            else:
                rid = xref_name
                ref_node = root.find("//*[@xref_id='%s']" % rid)

                node.set("rid", rid)
                if ref_node is not None:
                    ref_type = ref_node.tag
                    if ref_type == "table-wrap":
                        ref_type = "table"
                    node.set("ref-type", ref_type)
                    ref_node.attrib.pop("xref_id")
                else:
                    # nao existe a[@name=rid]
                    _remove_element_or_comment(node, xref_name == "top")

        def parser_node(self, node):
            try:
                href = node.attrib["href"].strip()
            except KeyError:
                if "id" not in node.attrib.keys():
                    logger.debug("\tTag 'a' sem href removendo node do xml")
                    _remove_element_or_comment(node)
            else:
                if href.startswith("#"):
                    self._parser_node_anchor(node)
                elif "mailto" in href or "@" in href:
                    self._create_email(node)
                elif "/" in href or href.startswith("www") or "http" in href:
                    self._parser_node_external_link(node)

        def transform(self, data):
            raw, xml = data

            _process(xml, "a", self.parser_node)
            return data

    class ImgPipe(plumber.Pipe):
        def parser_node(self, node):
            node.tag = "graphic"
            src = node.attrib.pop("src")
            node.attrib.clear()
            node.set("{http://www.w3.org/1999/xlink}href", src)

        def transform(self, data):
            raw, xml = data
            _process(xml, "img", self.parser_node)
            return data

    class CompleteFnConversionPipe(plumber.Pipe):
        """
        """
        def _remove_invalid_node(self, node, parent, _next):
            if _next is not None and _next.tag == "xref" and get_node_text(node) == "":
                _id = node.attrib.get("id")
                if _id.startswith("fn") or _id.startswith("replace_by_reftype"):
                    if _id.endswith("a"):
                        _remove_element_or_comment(node)
                    else:
                        _remove_element_or_comment(_next)
                    return True

        def _move_fn_tail_into_fn(self, node):
            _next = node.getnext()
            parent = node.getparent()
            items = []
            while _next is not None:
                if (_next.tag == "fn" or
                    _next.tag == "p" and _next.attrib.get("content-type") != "break"
                    ):
                    break
                else:
                    items.append(_next)
                    _next = _next.getnext()
            if len(items) > 0 or node.tail:
                node.text = node.tail
                for item in items:
                    node.append(deepcopy(item))
                    parent.remove(item)
                node.tail = ""

        def _identify_label_and_p(self, node):
            """Para fn que contém text, mas nao contém filhos,
            identificar label (se houver) e p.
            """
            children = node.getchildren()
            self._create_label(node)
            if len(children) == 0:
                self._create_p_for_simple_content(node)
            else:
                self._create_p_for_complex_content(node)

        def _create_label(self, node):
            parent = node.getparent()
            node_text = get_node_text(node)
            children = node.getchildren()
            if (node.text or "").strip():
                texts = node.text.split()
                if not texts[0].isalpha():
                    label = etree.Element("label")
                    label.text = texts[0]
                    node.insert(0, label)
                    label.tail = node.text.replace(texts[0], "")
                    node.text = ""
            elif children:
                if children[0].tag == "p":
                    elem = children[0].find("*")
                    if elem is not None and elem.tag in ["sup", "bold"]:
                        children[0].tag = "label"
                elif children[0].tag in ["sup", "bold"]:
                    children_text = get_node_text(children[0])
                    if len(children_text.split()) <= 3 and \
                            children_text != get_node_text(node):
                        label = etree.Element("label")
                        label_content = deepcopy(children[0])
                        label_content.tail = ""
                        label.append(label_content)
                        label.tail = children[0].tail
                        node.insert(0, label)
                        node.remove(children[0])

        def _create_p_for_simple_content(self, node):
            p = etree.Element("p")
            label = node.find("label")
            if label is None:
                p.text = node.text
                node.text = ""
            else:
                p.text = label.tail.strip()
                label.tail = ""
            node.append(p)

        def _create_p_for_complex_content(self, node):
            parent = node.getparent()
            node_text = get_node_text(node)
            children = node.getchildren()
            for child in children:
                if child.tag in ["label", "p"]:
                    if (child.tail or "").strip():
                        new_p = etree.Element("p")
                        new_p.text = child.tail
                        child.tail = ""
                        child.addnext(new_p)
                else:
                    new_p = etree.Element("p")
                    new_p.append(deepcopy(child))
                    child.addprevious(new_p)
                    node.remove(child)

        def update(self, node):
            parent = node.getparent()
            _next = node.getnext()
            fn_text = get_node_text(node)
            fn_children = node.getchildren()
            invalid_node = self._remove_invalid_node(node, parent, _next)
            if not invalid_node:
                if not fn_text:
                    self._move_fn_tail_into_fn(node)
                self._identify_label_and_p(node)

        def transform(self, data):
            raw, xml = data
            items = []
            for fn in xml.findall(".//fn"):
                self.update(fn)
                items.append(etree.tostring(fn))
            return data


def join_texts(texts):
    return " ".join([item for item in texts if item])


def _preserve_node_tail_before_remove_node(node, node_text):
    parent = node.getparent()
    if node.tail:
        text = join_texts([node_text.rstrip(), node.tail.lstrip()])
        previous = node.getprevious()
        if previous is not None:
            previous.tail = join_texts([(previous.tail or "").rstrip(), text])
        else:
            parent.text = join_texts([(parent.text or "").rstrip(), text])



def search_asset_node_backwards(node, attr="id"):

    previous = node.getprevious()
    if previous is not None:
        if previous.attrib.get(attr):
            if len(previous.getchildren()) == 0:
                return previous

    parent = node.getparent()
    if parent is not None:
        previous = parent.getprevious()
        if previous is not None:
            asset = previous.find(".//*[@{}]".format(attr))
            if asset is not None:
                if len(asset.getchildren()) == 0:
                    return asset


def search_backwards_for_elem_p_or_body(node):
    up = node.getparent()
    while up is not None and up.tag not in ["p", "body"]:
        last = up
        up = up.getparent()
    if up.tag == "p":
        return up
    if up.tag == "body":
        return last


def create_p_for_asset(a_href, asset):

    new_p = etree.Element("p")
    new_p.set("content-type", "asset")
    new_p.append(asset)

    up = search_backwards_for_elem_p_or_body(a_href)

    _next = up
    while _next is not None and _next.attrib.get("content-type") == "asset":
        up = _next
        _next = up.getnext()

    up.addnext(new_p)


class Document:
    def __init__(self, xmltree):
        self.xmltree = xmltree

    @property
    def a_href_items(self):
        texts = {}
        file_paths = {}
        for a_href in self.xmltree.findall(".//a[@href]"):
            href = a_href.attrib.get("href").strip()
            text = get_node_text(a_href).lower().strip()

            if text:
                if text not in texts.keys():
                    # tem id, nao tem id
                    texts[text] = ([], [])
                i = 0 if href and href[0] == "#" else 1
                texts[text][i].append(a_href)

            if href:
                if href[0] != "#" and ":" not in href and "@" not in href:
                    filename, __ = files.extract_filename_ext_by_path(href)
                    if filename not in file_paths.keys():
                        file_paths[filename] = []
                    file_paths[filename].append(a_href)
        return texts, file_paths

    @property
    def a_names(self):
        names = {}
        for a in self.xmltree.findall(".//a[@name]"):
            name = a.attrib.get("name").strip()
            if name:
                if name not in names.keys():
                    names[name] = (
                        a,
                        self.xmltree.findall('.//a[@href="#{}"]'.format(name)),
                    )
        return names

    @property
    def images(self):
        items = {}
        for img in self.xmltree.findall(".//img[@src]"):
            value = img.attrib.get("src").lower().strip()
            if value:
                filename, __ = files.extract_filename_ext_by_path(value)
                if filename not in items.keys():
                    items[filename] = []
                items[filename].append(img)
        return items


class FileLocation:
    def __init__(self, href):
        self.href = href
        self.basename = os.path.basename(href)
        self.new_href, self.ext = os.path.splitext(self.basename)

    @property
    def remote(self):
        file_path = self.href
        if file_path.startswith("/"):
            file_path = file_path[1:]
        return os.path.join(config.get("STATIC_URL_FILE"), file_path)

    @property
    def local(self):
        parts = self.remote.split("/")
        _local = "/".join(parts[-4:])
        _local = os.path.join(config.get("SITE_SPS_PKG_PATH"), _local)
        return _local.replace("//", "/")

    def ent2char(self, data):
        return html.unescape(data.decode("utf-8")).encode("utf-8").strip()

    def get_remote_content(self, timeout=TIMEOUT):
        with request.urlopen(self.remote, timeout=timeout) as fp:
            return fp.read()

    @property
    def content(self):
        _content = self.local_content
        if not _content:
            logger.info("Baixar %s" % self.remote)
            self.download()
            _content = self.local_content
        logger.info("%s %s" % (len(_content or ""), self.local))
        return _content

    @property
    def local_content(self):
        logger.info("Existe %s: %s" % (self.local, os.path.isfile(self.local)))
        if self.local and os.path.isfile(self.local):
            with open(self.local, "rb") as fp:
                return fp.read()

    def download(self):
        try:
            _content = self.get_remote_content()
        except (error.HTTPError, error.URLError) as e:
            logger.exception("Falha ao acessar %s: %s" % (self.remote, e))
        else:
            dirname = os.path.dirname(self.local)
            if not dirname.startswith(config.get("SITE_SPS_PKG_PATH")):
                logger.info(
                    "%s: valor inválido de caminho local para ativo digital"
                    % self.local
                )
                return
            if not os.path.isdir(dirname):
                os.makedirs(dirname)
            with open(self.local, "wb") as fp:
                if self.ext in [".html", ".htm"]:
                    fp.write(self.ent2char(_content))
                else:
                    fp.write(_content)
            return _content


def fix_img_revistas_path(node):
    attr = "src" if node.get("src") else "href"
    location = node.get(attr)
    old_location = location
    if location.startswith("img/"):
        location = "/" + location
    if "/img" in location:
        location = location[location.find("/img") :]
    if " " in location:
        location = "".join(location.split())
    location = location.replace("/img/fbpe", "/img/revistas")
    if old_location != location:
        logger.info(
            "fix_img_revistas_path: de {} para {}".format(old_location, location)
        )
        node.set(attr, location)


class Remote2LocalConversion:
    """
    - Identifica os a[@href] e os classifica
    - Se o arquivo é um HTML, baixa-o do site config.get("STATIC_URL_FILE")
    - Armazena-o em config.get("SITE_SPS_PKG_PATH"),
      mantendo a estrutura de acron/volnum
    - Insere seu conteúdo dentro de self.body
    - Se o arquivo é um link para uma imagem externa, transforma em img
    """

    IMG_EXTENSIONS = (".gif", ".jpg", ".jpeg", ".svg", ".png", ".tif", ".bmp")

    def __init__(self, xml):
        self.xml = xml
        self.body = self.xml.find(".//body")
        self._digital_assets_path = self.find_digital_assets_path()
        self.names = []

    def find_digital_assets_path(self):
        for node in self.xml.xpath(".//*[@src]|.//*[@href]"):
            location = node.get("src", node.get("href"))
            if location.startswith("/img/"):
                dirnames = os.path.dirname(location).split("/")
                return "/".join(dirnames[:5])

    @property
    def digital_assets_path(self):
        if self._digital_assets_path:
            return self._digital_assets_path
        self._digital_assets_path = self.find_digital_assets_path()

    @property
    def body_children(self):
        if self.body is not None:
            return self.body.findall("*")
        return []

    def remote_to_local(self):
        self._import_all_href_html_files()
        self._convert_a_href_into_images_or_media()

    def _classify_a_href(self):
        for node in self.xml.findall(".//*[@src]"):
            src = node.get("src")
            if ":" in src:
                node.set("link-type", "external")
                logger.info("Classificou: %s" % etree.tostring(node))
                continue

            value = src.split("/")[0]
            if "." in value:
                if src.startswith("./") or src.startswith("../"):
                    node.set(
                        "src",
                        os.path.join(
                            self.digital_assets_path, src[src.find("/")+1:]))
                else:
                    # pode ser URL
                    node.set("link-type", "external")
                    logger.info("Classificou: %s" % etree.tostring(node))
                    continue
                fix_img_revistas_path(node)

        for a_href in self.xml.findall(".//*[@href]"):
            if not a_href.get("link-type"):

                href = a_href.get("href")
                if ":" in href:
                    a_href.set("link-type", "external")
                    logger.info("Classificou: %s" % etree.tostring(a_href))
                    continue

                if href and href[0] == "#":
                    a_href.set("link-type", "internal")
                    logger.info("Classificou: %s" % etree.tostring(a_href))
                    continue

                value = href.split("/")[0]
                if "." in value:
                    if href.startswith("./") or href.startswith("../"):
                        a_href.set("href", os.path.join(self.digital_assets_path, href[href.find("/")+1:]))
                    else:
                        # pode ser URL
                        a_href.set("link-type", "external")
                        logger.info("Classificou a[@href]: %s" % etree.tostring(a_href))
                        continue

                fix_img_revistas_path(a_href)

                basename = os.path.basename(href)
                f, ext = os.path.splitext(basename)
                if ".htm" in ext:
                    a_href.set("link-type", "html")
                elif href.startswith("/pdf/"):
                    a_href.set("link-type", "pdf")
                elif href.startswith("/img/revistas"):
                    a_href.set("link-type", "asset")
                else:
                    logger.info("link-type=???")
                logger.info("Classificou a[@href]: %s" % etree.tostring(a_href))

    def _import_all_href_html_files(self):
        self._classify_a_href()
        while True:
            self._import_html_files_content()
            self._classify_a_href()
            if self.body.find(".//a[@link-type='html']") is None:
                break

    def _import_html_files_content(self):
        new_p_items = []
        for bodychild in self.body_children:
            for a_link_type in bodychild.findall(".//a[@link-type='html']"):
                new_p = self._import_html_file_content(a_link_type)
                if new_p is not None:
                    new_p_items.append((bodychild, new_p))

        for bodychild, new_p in new_p_items[::-1]:
            logger.info(
                "Insere novo p com conteudo do HTML: %s"
                % etree.tostring(new_p)
            )
            bodychild.addnext(new_p)
        return len(new_p_items)

    def _import_html_file_content(self, node_a):
        logger.info("Importar HTML de %s" % etree.tostring(node_a))
        href = node_a.get("href")
        if "#" in href:
            href, anchor = href.split("#")
        f, ext = os.path.splitext(href)
        new_href = os.path.basename(f)
        file_location = FileLocation(href)
        if file_location.content:
            html_tree = etree.fromstring(
                file_location.content, parser=etree.HTMLParser()
            )
            if html_tree is not None:
                html_body = html_tree.find(".//body")
                if html_body is not None:
                    return self._convert_a_href(node_a, new_href, html_body)
        else:
            node_a.set("href", new_href)
            node_a.attrib.pop("link-type")
            logger.info("FALHA: AUSENCIA DE HTML: %s " % href)

    def _convert_a_href_into_images_or_media(self):
        new_p_items = []
        for child in self.body_children:
            for node_a in child.findall(".//a[@link-type='asset']"):
                logger.info("Converter %s" % etree.tostring(node_a))
                href = node_a.get("href")
                f, ext = os.path.splitext(href)
                new_href = os.path.basename(f)
                if ext:
                    new_p = self._convert_a_href(node_a, new_href)
                    if new_p is not None:
                        new_p_items.append((child, new_p))
        for bodychild, new_p in new_p_items[::-1]:
            logger.info("Insere novo p: %s" % etree.tostring(new_p))
            bodychild.addnext(new_p)
        return len(new_p_items)

    def _convert_a_href(self, node_a, new_href, html_body=None):
        location = node_a.get("href")

        self._update_a_href(node_a, new_href)
        content_type = "asset"
        if html_body is not None:
            content_type = "html"
        delete_tag = "REMOVE_" + content_type

        found_a_name = self.find_a_name(node_a, new_href, delete_tag)
        if not found_a_name:
            if html_body is not None:
                node_content = self._imported_html_body(
                    new_href, html_body, delete_tag)
            else:
                node_content = self._asset_data(node_a, location, new_href)
            if node_content is not None:
                new_p = self._create_new_p(
                    new_href, node_content, content_type, delete_tag)
                return new_p

    def _update_a_href(self, a_href, new_href):
        a_href.set("href", "#" + new_href)
        a_href.set("link-type", "internal")
        logger.info("Atualiza a[@href]: %s" % etree.tostring(a_href))

    def find_a_name(self, a_href, new_href, delete_tag="REMOVETAG"):
        if new_href in self.names:
            logger.info("Será criado")
            return True
        a_name = a_href.getroottree().find(".//a[@name='{}']".format(new_href))
        if a_name is not None:
            if a_name.getchildren():
                logger.info("Já importado")
                return True
            else:
                # existe um a[@name] mas é inválido porque está sem conteúdo
                a_name.tag = delete_tag
                return True

    def _create_new_p(self, new_href, node_content, content_type, delete_tag):
        self.names.append(new_href)
        a_name = etree.Element("a")
        a_name.set("id", new_href)
        a_name.set("name", new_href)
        a_name.append(node_content)

        new_p = etree.Element("p")
        new_p.set("content-type", content_type)
        new_p.append(a_name)
        etree.strip_tags(new_p, delete_tag)
        logger.info("Cria novo p: %s" % etree.tostring(new_p))

        return new_p

    def _imported_html_body(self, new_href, html_body, delete_tag="REMOVETAG"):
        # Criar o a[@name] com o conteúdo do body
        body = deepcopy(html_body)
        body.tag = delete_tag
        for a in body.findall(".//a"):
            logger.info(
                "Encontrado elem a no body importado: %s" % etree.tostring(a))
            href = a.get("href")
            if href and href[0] == "#":
                a.set("href", "#" + new_href + href[1:].replace("#", "X"))
            elif a.get("name"):
                a.set("name", new_href + "X" + a.get("name"))
            logger.info("Atualiza elem a importado: %s" % etree.tostring(a))

        a_name = body.find(".//a[@name='{}']".format(new_href))
        if a_name is not None:
            a_name.tag = delete_tag
        return body

    def _asset_data(self, node_a, location, new_href):
        asset = node_a.getroottree().find(".//*[@src='{}']".format(location))
        if asset is None:
            # Criar o a[@name] com o <img src=""/>
            tag = "img"
            ign, ext = os.path.splitext(location)
            if ext.lower() not in self.IMG_EXTENSIONS:
                tag = "media"
            asset = etree.Element(tag)
            asset.set("src", location)
            return asset
        elif asset.getparent().get("name") != new_href:
            a = etree.Element("a")
            a.set("name", new_href)
            a.set("id", new_href)
            asset.addprevious(a)
            a.append(deepcopy(asset))
            parent = asset.getparent()
            parent.remove(asset)
            self.names.append(new_href)
