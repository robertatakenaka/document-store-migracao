import logging
import plumber
import html
import re
import os
from copy import deepcopy

import requests
from lxml import etree
from documentstore_migracao.utils import files
from documentstore_migracao.utils import xml as utils_xml
from documentstore_migracao import config
from documentstore_migracao.utils.convert_html_body_inferer import Inferer


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
            self.RemoveExcedingStyleTagsPipe(),
            self.RemoveEmptyPipe(),
            self.RemoveStyleAttributesPipe(),
            self.RemoveCommentPipe(),
            self.AHrefPipe(),
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
            self.RemoveInvalidBRPipe(),
            self.ConvertElementsWhichHaveIdPipe(),
            self.RemoveInvalidBRPipe(),
            self.BRPipe(),
            self.BR2PPipe(),
            self.TdCleanPipe(),
            self.TableCleanPipe(),
            self.BlockquotePipe(),
            self.HrPipe(),
            self.TagsHPipe(),
            self.DispQuotePipe(),
            self.GraphicChildrenPipe(),
            self.FixBodyChildrenPipe(),
            self.RemovePWhichIsParentOfPPipe(),
            self.PPipe(),
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
            logger.info("BRPipe.transform - inicio")
            raw, xml = data
            for node in xml.findall(".//*[br]"):
                if node.tag in self.ALLOWED_IN:
                    for br in node.findall("br"):
                        br.tag = "break"
            logger.info("BRPipe.transform - inicio")
            return data

    class RemoveInvalidBRPipe(plumber.Pipe):
        def _remove_first_or_last_br(self, xml):
            """
            b'<bold><br/>Luis Huicho</bold>
            b'<bold>Luis Huicho</bold>
            """
            logger.info("RemoveInvalidBRPipe._remove_br - inicio")
            while True:
                change = False
                for node in xml.findall(".//*[br]"):
                    first = node.getchildren()[0]
                    last = node.getchildren()[-1]
                    if (node.text or "").strip() == "" and first.tag == "br":
                        first.tag = "REMOVEINVALIDBRPIPEREMOVETAG"
                        change = True
                    if (last.tail or "").strip() == "" and last.tag == "br":
                        last.tag = "REMOVEINVALIDBRPIPEREMOVETAG"
                        change = True
                if not change:
                    break
            etree.strip_tags(xml, "REMOVEINVALIDBRPIPEREMOVETAG")
            logger.info("RemoveInvalidBRPipe._remove_br - fim")

        def transform(self, data):
            logger.info("RemoveInvalidBRPipe - inicio")
            text, xml = data
            self._remove_first_or_last_br(xml)
            logger.info("RemoveInvalidBRPipe - fim")
            return data

    class BR2PPipe(plumber.Pipe):
        def _create_p(self, node, nodes, text):
            logger.info("BR2PPipe._create_p - inicio")
            if nodes or (text or "").strip():
                logger.info("BR2PPipe._create_p - element p")
                p = etree.Element("p")
                if node.tag not in ["REMOVE_P", "p"]:
                    p.set("content-type", "break")
                p.text = text
                logger.info("BR2PPipe._create_p - append nodes")
                for n in nodes:
                    p.append(deepcopy(n))
                logger.info("BR2PPipe._create_p - node.append(p)")
                node.append(p)
            logger.info("BR2PPipe._create_p - fim")

        def _create_new_node(self, node):
            """
            <root><p>texto <br/> texto 1</p></root>
            <root><p><p content-type= "break">texto </p><p content-type= "break"> texto 1</p></p></root>
            """
            logger.info("BR2PPipe._create_new_node - inicio")
            new = etree.Element(node.tag)
            for attr, value in node.attrib.items():
                new.set(attr, value)
            text = node.text
            nodes = []
            for i, child in enumerate(node.getchildren()):
                if child.tag == "br":
                    self._create_p(new, nodes, text)
                    nodes = []
                    text = child.tail
                else:
                    nodes.append(child)
            self._create_p(new, nodes, text)
            logger.info("BR2PPipe._create_new_node - fim")
            return new

        def _executa(self, xml):
            logger.info("BR2PPipe._executa - inicio")
            while True:
                node = xml.find(".//*[br]")
                if node is None:
                    break
                new = self._create_new_node(node)

                node.addprevious(new)
                if node.tag == "p":
                    new.tag = "BRTOPPIPEREMOVETAG"
                p = node.getparent()
                if p is not None:
                    p.remove(node)
            etree.strip_tags(xml, "BRTOPPIPEREMOVETAG")
            logger.info("BR2PPipe._executa - fim")

        def transform(self, data):
            logger.info("BR2PPipe - inicio")
            text, xml = data
            self._executa(xml)
            logger.info("BR2PPipe - fim")
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
        ATTRIBUTES = (
            "content-type",
            "id",
            "specific-use",
            "xml:base",
            "xml:lang",
        )

        def parser_node(self, node):
            if "class" in node.attrib.keys() and not node.get("content-type"):
                node.set("content-type", node.get("class"))
            for attr in node.attrib.keys():
                if attr not in self.ATTRIBUTES:
                    node.attrib.pop(attr)
            parent = node.getparent()
            if parent.tag not in self.TAGS:
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

    class AHrefPipe(plumber.Pipe):
        def _create_ext_link(self, node, extlinktype="uri"):
            node.tag = "ext-link"
            href = node.attrib.get("href")
            node.attrib.clear()
            node.set("ext-link-type", extlinktype)
            node.set("{http://www.w3.org/1999/xlink}href", href)

        def _create_email(self, node):
            href = node.get("href").strip()
            if "mailto:" in href:
                href = href.split("mailto:")[1]

            node_text = (node.text or "").strip()
            node.tag = "email"
            node.attrib.clear()
            if not href and node_text and "@" in node_text:
                texts = node_text.split()
                for text in texts:
                    if "@" in text:
                        href = text
                        break
            if not href:
                # devido ao caso do href estar mal
                # formado devemos so trocar a tag
                # e retorna para continuar o Pipe
                return

            node.set("{http://www.w3.org/1999/xlink}href", href)

            if href == node_text:
                return
            if href in node_text:
                root = node.getroottree()
                temp = etree.Element("AHREFPIPEREMOVETAG")
                texts = node_text.split(href)
                temp.text = texts[0]
                email = etree.Element("email")
                email.text = href
                temp.append(email)
                temp.tail = texts[1]
                node.addprevious(temp)
                etree.strip_tags(root, "AHREFPIPEREMOVETAG")
            else:
                # https://jats.nlm.nih.gov/publishing/tag-library/1.2/element/email.html
                node.tag = "ext-link"
                node.set("ext-link-type", "email")
                node.set(
                    "{http://www.w3.org/1999/xlink}href", "mailto:" + href)

        def parser_node(self, node):
            href = node.get("href")
            if href.startswith("#") or node.get("link-type") == "internal":
                return
            if "mailto" in href or "@" in href:
                return self._create_email(node)
            if ":" in href or node.get("link-type") == "external":
                return self._create_ext_link(node)
            if href.startswith("//"):
                return self._create_ext_link(node)

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@href]", self.parser_node)
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
            self.CompleteElementAWithNameAndIdPipe(),
            self.CompleteElementAWithXMLTextPipe(),
            self.EvaluateElementAToDeleteOrMarkAsFnLabelPipe(),
            self.DeduceAndSuggestConversionPipe(),
            self.ApplySuggestedConversionPipe(),
            self.AddAssetInfoToTablePipe(),
            self.CreateAssetElementsFromExternalLinkElementsPipe(),
            self.CreateAssetElementsFromImgOrTableElementsPipe(),
            self.ImgPipe(),
            self.FnAddContentPipe(),
            self.FnIdentifyLabelAndPPipe(),
            self.FnFixContentPipe(),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(plumber.Pipe):
        def transform(self, data):
            new_obj = deepcopy(data)
            return data, new_obj

    class AddAssetInfoToTablePipe(plumber.Pipe):
        def parser_node(self, node):
            _id = node.attrib.get("id")
            if _id:
                new_id = _id
                node.set("id", new_id)
                node.set("xml_id", new_id)
                node.set("xml_tag", "table-wrap")
                node.set("xml_label", "Tab")

        def transform(self, data):
            raw, xml = data
            _process(xml, "table[@id]", self.parser_node)
            return data

    class CreateAssetElementsFromExternalLinkElementsPipe(plumber.Pipe):
        def _create_asset_content_as_graphic(self, node_a):
            href = node_a.attrib.get("href")
            new_graphic = etree.Element("graphic")
            new_graphic.set("{http://www.w3.org/1999/xlink}href", href)
            return new_graphic

        def _create_asset_group(self, a_href):
            root = a_href.getroottree()
            new_id = a_href.attrib.get("xml_id")
            new_tag = a_href.attrib.get("xml_tag")
            if not new_tag or not new_id:
                return

            asset = find_or_create_asset_node(root, new_tag, new_id)
            if asset is not None:
                href = a_href.attrib.get("href")
                fname, ext = os.path.splitext(href.lower())
                asset_content = self._create_asset_content_as_graphic(a_href)

                if asset_content is not None:
                    asset.append(asset_content)
                    if asset_content.tag == "REMOVE_TAG":
                        etree.strip_tags(asset, "REMOVE_TAG")

                if asset.getparent() is None:
                    create_p_for_asset(a_href, asset)

                self._create_xref(a_href)

        def _create_xref(self, a_href):
            reftype = a_href.attrib.pop("xml_reftype")
            new_id = a_href.attrib.pop("xml_id")

            if new_id is None or reftype is None:
                return

            a_href.tag = "xref"
            a_href.attrib.clear()
            a_href.set("rid", new_id)
            a_href.set("ref-type", reftype)

        def transform(self, data):
            raw, xml = data
            document = Document(xml)
            a_href_texts, file_paths = document.a_href_items
            for path, nodes in file_paths.items():
                if nodes[0].attrib.get("xml_tag"):
                    self._create_asset_group(nodes[0])
                    for node in nodes[1:]:
                        self._create_xref(node)
            return data

    class CreateAssetElementsFromImgOrTableElementsPipe(plumber.Pipe):
        def _find_label_and_caption_in_node(self, node, previous_or_next):
            node_text = node.attrib.get("xml_label")
            if node_text is None:
                return
            text = get_node_text(previous_or_next)
            if text.lower().startswith(node_text.lower()):
                _node = previous_or_next
                parts = text.split()
                if len(parts) > 0:
                    if len(parts) == 1:
                        text = parts[0], ""
                    elif parts[1].isalnum():
                        text = parts[:2], parts[2:]
                    elif parts[1][:-1].isalnum():
                        text = (parts[0], parts[1][:-1]), parts[2:]
                    else:
                        text = parts[:1], parts[1:]
                    if len(text) == 2:
                        label = etree.Element("label")
                        label.text = join_texts(text[0])
                        title_text = join_texts(text[1])
                        caption = None

                        if title_text:
                            caption = etree.Element("caption")
                            title = etree.Element("title")
                            title.text = join_texts(text[1])
                            caption.append(title)
                        return _node, label, caption

        def _find_label_and_caption_around_node(self, node):
            parent = node.getparent()
            _node = None
            label = None
            caption = None
            node_label_caption = None

            previous = parent.getprevious()
            _next = parent.getnext()

            if previous is not None:
                node_label_caption = self._find_label_and_caption_in_node(
                    node, previous
                )

            if node_label_caption is None and _next is not None:
                node_label_caption = self._find_label_and_caption_in_node(node, _next)

            if node_label_caption is not None:
                _node, label, caption = node_label_caption
                parent = _node.getparent()
                if parent is not None:
                    parent.remove(_node)
                return label, caption

        def _get_asset_node(self, img_or_table, xml_new_tag, xml_id):
            asset = find_or_create_asset_node(
                img_or_table.getroottree(), xml_new_tag, xml_id, img_or_table
            )
            if asset is not None:
                parent = asset.getparent()
                if parent is None:
                    parent = img_or_table.getparent()
                    if parent.tag != "body":
                        parent.addprevious(asset)
                    else:
                        parent.append(asset)
            return asset

        def parser_node(self, img_or_table):
            xml_id = img_or_table.attrib.get("xml_id")
            xml_reftype = img_or_table.attrib.get("xml_reftype")
            xml_new_tag = img_or_table.attrib.get("xml_tag")
            xml_label = img_or_table.attrib.get("xml_label")
            if not xml_new_tag or not xml_id:
                return

            label_and_caption = self._find_label_and_caption_around_node(
                img_or_table)
            asset = self._get_asset_node(img_or_table, xml_new_tag, xml_id)
            if label_and_caption:
                if label_and_caption[1] is not None:
                    asset.insert(0, label_and_caption[1])
                asset.insert(0, label_and_caption[0])

            img_or_table_parent = img_or_table.getparent()
            new_img_or_table = deepcopy(img_or_table)
            for attr in ["xml_id", "xml_reftype", "xml_label", "xml_tag"]:
                if attr in new_img_or_table.attrib.keys():
                    new_img_or_table.attrib.pop(attr)
            asset.append(new_img_or_table)
            img_or_table_parent.remove(img_or_table)

        def transform(self, data):
            raw, xml = data
            _process(xml, "img[@xml_id]", self.parser_node)
            _process(xml, "table[@xml_id]", self.parser_node)
            return data

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

    class CompleteElementAWithNameAndIdPipe(plumber.Pipe):
        """Garante que todos os elemento a[@name] e a[@id] tenham @name e @id.
        Corrige id e name caso contenha caracteres nao alphanum.
        """
        def _fix_a_href(self, xml):
            for a in xml.findall(".//a[@name]"):
                name = a.attrib.get("name")
                for a_href in xml.findall(".//a[@href='{}']".format(name)):
                    a_href.set("href", "#" + name)

        def parser_node(self, node):
            _id = node.attrib.get("id")
            _name = node.attrib.get("name")
            node.set("id", _name or _id)
            node.set("name", _name or _id)
            href = node.attrib.get("href")
            if href and href[0] == "#":
                a = etree.Element("a")
                a.set("name", node.attrib.get("name"))
                a.set("id", node.attrib.get("id"))
                node.addprevious(a)
                node.set("href", "#" + href[1:])
                node.attrib.pop("id")
                node.attrib.pop("name")

        def transform(self, data):
            raw, xml = data
            self._fix_a_href(xml)
            _process(xml, "a[@id]", self.parser_node)
            _process(xml, "a[@name]", self.parser_node)
            return data

    class CompleteElementAWithXMLTextPipe(plumber.Pipe):
        """
        Adiciona o atributo @xml_text ao elemento a, com o valor completo 
        de seu rótulo. Por exemplo, explicitar se <a href="#2">2</a> é
        nota de rodapé <a href="#2" xml_text="2">2</a> ou 
        Fig 2 <a href="#2" xml_text="figure 2">2</a>.
        """
        def add_xml_text_to_a_href(self, xml):
            previous = etree.Element("none")
            for node in xml.findall(".//a[@href]"):
                text = get_node_text(node)
                if text:
                    text = text.lower()
                    node.set("xml_text", text)
                    xml_text = previous.get("xml_text") or ""
                    splitted = xml_text.split()
                    if text[0].isdigit() and len(splitted) >= 2:
                        label, number = splitted[:2]
                        if number[0] <= text[0]:
                            node.set("xml_text", label + " " + text)
                            logger.info(
                                "add_xml_text_to_a_href: %s " % etree.tostring(
                                    previous)
                            )
                            logger.info(
                                "add_xml_text_to_a_href: %s " % etree.tostring(
                                    node)
                            )
                previous = node

        def add_xml_text_to_other_a(self, xml):
            for node in xml.findall(".//a[@xml_text]"):
                href = node.get("href")
                if href:
                    xml_text = node.get("xml_text")
                    for n in xml.findall(".//a[@href='{}']".format(href)):
                        if not n.get("xml_text"):
                            n.set("xml_text", xml_text)
                    for n in xml.findall(".//a[@name='{}']".format(href[1:])):
                        if not n.get("xml_text"):
                            n.set("xml_text", xml_text)

        def transform(self, data):
            raw, xml = data
            logger.info("CompleteElementAWithXMLTextPipe")
            self.add_xml_text_to_a_href(xml)
            self.add_xml_text_to_other_a(xml)
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

    class EvaluateElementAToDeleteOrMarkAsFnLabelPipe(plumber.Pipe):
        """
        No texto há âncoras (a[@name]) e referencias cruzada (a[@href]):
        TEXTO->NOTAS e NOTAS->TEXTO.
        Remove as âncoras e referências cruzadas relacionadas com NOTAS->TEXTO.
        Também remover duplicidade de a[@name]
        Algumas NOTAS->TEXTO podem ser convertidas a "fn/label"
        """

        def _classify_elem_a_by_id(self, xml):
            items_by_id = {}
            for a in xml.findall(".//a"):
                _id = a.attrib.get("name")
                if not _id:
                    href = a.attrib.get("href")
                    if href and href.startswith("#"):
                        _id = href[1:]
                if _id:
                    items_by_id[_id] = items_by_id.get(_id, [])
                    items_by_id[_id].append(a)
            return items_by_id

        def _keep_only_one_a_name(self, items):
            # remove os a[@name] repetidos, se aplicável
            a_names = [n for n in items if n.attrib.get("name")]
            for n in a_names[1:]:
                items.remove(n)
                _remove_element_or_comment(n)

        def _exclude_invalid_a_name_and_identify_fn_label(self, items):
            if items[0].get("name"):
                if len(items) > 1:
                    items[0].tag = "_EXCLUDE_REMOVETAG"
                root = items[0].getroottree()
                for a_href in items[1:]:
                    found = None
                    if self._might_be_fn_label(a_href):
                        found = self._find_a_name_which_same_xml_text(
                            root, a_href.get("xml_text")
                        )
                    if found is None:
                        logger.info("remove: %s" % etree.tostring(a_href))
                        _remove_element_or_comment(a_href)
                    else:
                        logger.info("Identifica como fn/label")
                        logger.info(etree.tostring(a_href))
                        a_href.tag = "label"
                        a_href.set("label-of", found.get("name"))
                        logger.info(etree.tostring(a_href))

        def _exclude_invalid_unique_a_href(self, nodes):
            if len(nodes) == 1 and nodes[0].attrib.get("href"):
                _remove_element_or_comment(nodes[0])

        def _might_be_fn_label(self, a_href):
            xml_text = a_href.get("xml_text")
            if xml_text and get_node_text(a_href):
                return any(
                    [xml_text[0].isdigit(),
                    not xml_text[0].isalnum(),
                    xml_text[0].isalpha() and len(xml_text) == 1,
                    ]
                )

        def _find_a_name_which_same_xml_text(self, root, xml_text):
            for item in root.findall(".//a[@xml_text='{}']".format(xml_text)):
                if item.get("name"):
                    return item

        def transform(self, data):
            raw, xml = data
            logger.info("EvaluateElementAToDeleteOrCreateFnLabelPipe")
            items_by_id = self._classify_elem_a_by_id(xml)
            for _id, items in items_by_id.items():
                self._keep_only_one_a_name(items)
                self._exclude_invalid_a_name_and_identify_fn_label(items)
                self._exclude_invalid_unique_a_href(items)
            etree.strip_tags(xml, "_EXCLUDE_REMOVETAG")
            logger.info("EvaluateElementAToDeleteOrCreateFnLabelPipe - fim")
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
            node.attrib.clear()
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

    class FnAddContentPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            logger.info("FnAddContentPipe")
            for fn in xml.findall(".//fn"):
                fn.set("status", "add-content")
            while True:
                fn = xml.find(".//fn[@status='add-content']")
                if fn is None:
                    break
                fn.set("status", "identify-content")
                self._add_label_into_fn(fn)
                self._add_fn_tail_into_fn(fn)
            return data

        def _add_label_into_fn(self, node):
            logger.info("FnAddContentPipe._add_label_into_fn")
            text = None
            previous = node.getprevious()
            if previous is not None:
                if previous.tag == "label":
                    node.insert(0, deepcopy(previous))
                    _remove_element_or_comment(previous)
                    return
                else:
                    n = previous
                    text = get_node_text(previous)
            else:
                text = (node.getparent().text or "").strip()
                n = node.getparent()
            # print(etree.tostring(node.getparent()))
            # print(text)
            if text and text[-1] == "*":
                splitted = text.split()
                node.tail = splitted[-1] + " " + (node.tail or "")
                if n.tail:
                    n.tail = n.tail.replace(splitted[-1], "")
                elif n.text:
                    n.text = n.text.replace(splitted[-1], "")

        def _add_fn_tail_into_fn(self, node):
            logger.info("FnAddContentPipe._add_fn_tail_into_fn")
            if (node.tail or "").strip():
                node.text = node.tail
                node.tail = ""
            while True:
                _next = node.getnext()
                if _next is None:
                    break
                if _next.tag in ["fn"]:
                    break
                if _next.tag in ["p"]:
                    if get_node_text(node):
                        break
                node.append(deepcopy(_next))
                parent = _next.getparent()
                parent.remove(_next)

    class FnIdentifyLabelAndPPipe(plumber.Pipe):

        def _create_label(self, new_fn, node):
            if node.find(".//label") is not None:
                return

            children = node.getchildren()
            node_text = (node.text or "").strip()
            if node_text:
                # print("FnIdentifyLabelAndPPipe - _create_label_from_node_text")
                logger.info("FnIdentifyLabelAndPPipe - _create_label_from_node_text")
                label = self._create_label_from_node_text(new_fn, node)
            elif children:
                # print("FnIdentifyLabelAndPPipe - _create_label_from_style_tags")
                logger.info("FnIdentifyLabelAndPPipe - _create_label_from_style_tags")
                self._create_label_from_style_tags(new_fn, node)
                if new_fn.find(".//label") is None:
                    # print("FnIdentifyLabelAndPPipe - _create_label_from_children")
                    logger.info("FnIdentifyLabelAndPPipe - _create_label_from_children")
                    self._create_label_from_children(new_fn, node)
            logger.info(etree.tostring(new_fn))

        def _create_label_from_node_text(self, new_fn, node):
            # print(etree.tostring(node))
            label_text = self._get_label_text(node)
            if label_text:
                label = etree.Element("label")
                label.text = label_text
                new_fn.insert(0, label)
                node.text = node.text.replace(label_text, "").lstrip()
            # print(etree.tostring(node))

        def _get_label_text(self, node):
            node_text = get_node_text(node)
            if not node_text:
                return
            splitted = [item.strip() for item in node_text.split()]
            logger.info("_get_label_text")
            logger.info(splitted)
            label_text = None
            if splitted[0][0].isalpha():
                if len(splitted[0]) == 1 and node_text[0].lower() == node_text[0]:
                    label_text = splitted[0]
            else:
                label_text = self._get_not_alpha_characteres(splitted[0])
            return label_text

        def _get_not_alpha_characteres(self, text):
            label_text = []
            for c in text:
                if not c.isalpha():
                    label_text.append(c)
                else:
                    break
            return "".join(label_text)

        def _create_label_from_children(self, new_fn, node):
            """
            Melhorar
            b'<fn id="back2"><italic>** Address: Rua Itapeva 366 conj 132 - 01332-000 S&#227;o Paulo SP - Brasil.</italic></fn>'
            """
            # print(etree.tostring(node))
            label_text = self._get_label_text(node)
            if label_text:
                label = etree.Element("label")
                label.text = label_text
                new_fn.insert(0, label)
                for n in node.findall(".//*"):
                    if n.text and n.text.startswith(label_text):
                        n.text = n.text.replace(label_text, "")
                        break

        def _create_label_from_style_tags(self, new_fn, node):
            STYLE_TAGS = ("sup", "bold", "italic")
            children = node.getchildren()
            node_style = None
            if children[0].tag in STYLE_TAGS:
                node_style = children[0]
            else:
                for tag in ["sup", "bold", "italic"]:
                    n = children[0].find(".//{}".format(tag))
                    if n is None:
                        continue
                    if not n.getchildren():
                        node_style = n
                        break
            if node_style is not None:
                node_text = get_node_text(node)
                node_style_text = get_node_text(node_style)
                if node_style_text == node_text:
                    node_style = None
            if node_style is not None:
                label = etree.Element("label")
                cp = deepcopy(node_style)
                cp.tail = ""
                label.append(cp)
                new_fn.insert(0, label)
                node.text = node_style.tail
                parent = node_style.getparent()
                parent.remove(node_style)

        def _create_p(self, new_fn, node):
            new_p = None
            if (node.text or "").strip():
                new_p = etree.Element("p")
                new_p.text = node.text
            for child in node.getchildren():
                if child.tag in ["label", "p"]:
                    new_p = self._create_new_p(new_fn, new_p, child)
                else:
                    if new_p is None:
                        new_p = etree.Element("p")
                    new_p.append(deepcopy(child))
            if new_p is not None:
                new_fn.append(new_p)
            node.tag = "DELETE"
            node.addprevious(new_fn)

        def _create_new_p(self, new_fn, new_p, child):
            if new_p is not None:
                new_fn.append(new_p)

            p = deepcopy(child)
            p.tail = ""
            new_fn.append(p)

            new_p = None
            if child.tail:
                new_p = etree.Element("p")
                new_p.text = child.tail
            return new_p

        def _identify_label_and_p(self, fn):
            new_fn = etree.Element("fn")
            for k, v in fn.attrib.items():
                if k in ["id", "label", "fn-type"]:
                    new_fn.set(k, v)
            self._create_label(new_fn, fn)
            self._create_p(new_fn, fn)
            fn.addprevious(new_fn)
            for delete in fn.getroottree().findall(".//DELETE"):
                parent = delete.getparent()
                parent.remove(delete)

        def transform(self, data):
            raw, xml = data
            for fn in xml.findall(".//fn"):
                logger.info("FnIdentifyLabelAndPPipe")
                self._identify_label_and_p(fn)
            return data

    class FnFixContentPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            logger.info("FnFixContentPipe")
            for fn in xml.findall(".//fn"):
                children = fn.getchildren()
                label = fn.find(".//label")
                if label is not None:
                    label.attrib.clear()
                    bold = label.find("bold[@label-of]")
                    if bold is not None:
                        bold.attrib.clear()
                    if children[0].tag == "p" and children[0].text in ["(", "["]:
                        label.text = children[0].text + label.text + children[2].text[:1]
                        children[2].text = children[2].text[1:]
                        fn.remove(children[0])
                    elif children[0] is not label:
                        logger.info("FnFixContentPipe: %s" % etree.tostring(children[0]))
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

    @property
    def content(self):
        _content = self.local_content
        if not _content:
            _content = self.download()
            if _content:
                self.save(_content)
        logger.info("%s %s" % (len(_content or ""), self.local))
        return _content

    @property
    def local_content(self):
        logger.info("Get local content from: %s" % self.local)
        if self.local and os.path.isfile(self.local):
            logger.info("Found")
            with open(self.local, "rb") as fp:
                return fp.read()

    def download(self):
        logger.info("Download %s" % self.remote)
        r = requests.get(self.remote, timeout=TIMEOUT)
        if r.status_code == 404:
            logger.error(
                "FAILURE. REQUIRES MANUAL INTERVENTION. Not found %s. " % self.remote)
            return
        if not r.status_code == 200:
            logger.error(
                "%s: %s" % (self.remote, r.status_code))
            return
        return r.content

    def save(self, content):
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
            fp.write(content)


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
        self._import_all_html_files_found_in_body()
        self._convert_a_href_into_images_or_media()

    def _add_link_type_attribute_to_element_a(self):
        if self.digital_assets_path is None:
            return
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
                logger.info(
                    "Classificou a[@href]: %s" % etree.tostring(a_href))

    def _import_all_html_files_found_in_body(self):
        self._add_link_type_attribute_to_element_a()
        while True:
            if self.body.find(".//a[@link-type='html']") is None:
                break
            self._import_files_marked_as_link_type_html()
            self._add_link_type_attribute_to_element_a()

    def _import_files_marked_as_link_type_html(self):
        new_p_items = []
        for bodychild in self.body_children:
            for a_link_type in bodychild.findall(".//a[@link-type='html']"):
                new_p = self._import_html_file_content(a_link_type)
                if new_p is None:
                    a_link_type.set("link-type", "external")
                else:
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

