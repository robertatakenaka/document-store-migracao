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


logger = logging.getLogger(__name__)

import faulthandler

faulthandler.enable()

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
        text = get_node_inner_text(node)

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


def get_node_inner_text(node):
    try:
        n = deepcopy(node)
        n.tail = ""
        texts = node.itertext()
    except (AttributeError, ValueError):
        # node is Comment or node is None
        return ""
    else:

        return join_texts([item.strip() for item in texts if item.strip()])


class CustomPipe(plumber.Pipe):
    def __init__(self, super_obj=None, *args, **kwargs):

        self.super_obj = super_obj
        super(CustomPipe, self).__init__(*args, **kwargs)


class HTML2SPSPipeline(object):
    def __init__(self, pid, index_body=1):
        self.pid = pid
        self.index_body = index_body
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.ConvertRemoteLocation2LocalLocation(),
            self.SaveRawBodyPipe(super_obj=self),
            self.DeprecatedHTMLTagsPipe(),
            self.ReplaceImgSetaPipe(),
            ##self.RemoveDuplicatedIdPipe(),
            self.RemoveOrMoveStyleTagsPipe(),
            self.RemoveEmptyPipe(),
            self.RemoveStyleAttributesPipe(),
            self.RemoveCommentPipe(),
            self.AHrefPipe(),
            # self.PPipe(),
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
                    "/tmp/",
                    "%s_%s.xml" % (
                        self.super_obj.pid, self.super_obj.index_body)),
                encoding="utf-8",
                doctype=config.DOC_TYPE_XML,
                xml_declaration=True,
                pretty_print=True,
            )
            return data, xml

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


    class ConvertRemoteLocation2LocalLocation(plumber.Pipe):

        def transform(self, data):
            logger.info("ConvertRemoteLocation2LocalLocation")
            raw, xml = data
            html_page = InsertExternalHTMLBodyIntoXMLBody(xml)
            html_page.remote_to_local()
            logger.info("ConvertRemoteLocation2LocalLocation - fim")
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
            node.tag = "email"
            node.attrib.clear()
            if not href:
                # devido ao caso do href estar mal
                # formado devemos so trocar a tag
                # e retorna para continuar o Pipe
                return
            node.set("{http://www.w3.org/1999/xlink}href", href)
            node_text = (node.text or "").strip()
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
            elif node_text:
                node.tag = "ext-link"
                node.set("ext-link-type", "email")
                node.set("{http://www.w3.org/1999/xlink}href", href)

        def parser_node(self, node):
            href = node.get("href")
            if href.startswith("#") or node.get("link-type") == "internal":
                return
            if "mailto" in href or "@" in href:
                return self._create_email(node)
            if ":" in href or node.get("link-type") == "external":
                return self._create_ext_link(node)

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@href]", self.parser_node)
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

    class RemoveOrMoveStyleTagsPipe(plumber.Pipe):
        STYLE_TAGS = ("i", "b", "em", "strong", "u", "sup", "sub")

        def _wrap_node_content_with_new_tag(self, node, new_tag):
            # envolve o conteúdo de node com new_tag
            if node.tag == new_tag:
                return
            text = get_node_inner_text(node)
            if not text:
                return
            node_copy = etree.Element(node.tag)
            for k, v in node.attrib.items():
                node_copy.set(k, v)
            new_elem = etree.Element(new_tag)

            new_elem.text = node.text
            for child in node.getchildren():
                new_elem.append(deepcopy(child))
            node_copy.append(new_elem)
            node.addprevious(node_copy)
            parent = node.getparent()
            parent.remove(node)

        def _move_style_tag_into_children(self, node):
            """
            Move tags de estilo para dentro de seus filhos
            """
            if (node.text or "").strip():
                e = etree.Element(node.tag)
                e.text = node.text
                node.text = ""
                node.addprevious(e)
            for node_child in node.getchildren():
                if (node_child.tail or "").strip():
                    e = etree.Element(node.tag)
                    e.text = node_child.tail
                    node_child.tail = ""
                    node_child.addnext(e)
                # envolve o conteúdo de node_child com a tag de estilo
                self._wrap_node_content_with_new_tag(node_child, node.tag)

        def _remove_or_move_style_tags(self, xml):
            for style_tag in self.STYLE_TAGS:
                for node in xml.findall(".//" + style_tag):
                    text = get_node_inner_text(node)
                    children = node.getchildren()
                    if not text:
                        node.tag = "STRIPTAG"
                    elif node.find(".//{}".format(style_tag)) is not None:
                        node.tag = "STRIPTAG"
                    else:
                        move = any((child.tag
                                    for child in node.getchildren()
                                    if child.tag not in self.STYLE_TAGS))
                        if move:
                            self._move_style_tag_into_children(node)
                            node.tag = "STRIPTAG"
            etree.strip_tags(xml, "STRIPTAG")

        def transform(self, data):
            raw, xml = data
            self._remove_or_move_style_tags(xml)
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
            items = self._executa(xml)
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

    class SanitizationPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data

            convert = DataSanitizationPipeline()
            _, obj = convert.deploy(xml)
            return raw, obj

    class ReplaceImgSetaPipe(plumber.Pipe):
        def parser_node(self, node):
            img = node.find("img")
            src = img.attrib.get("src")
            if "seta" in src or "flecha" in src:
                img.tag = "REPLACEIMGSETAREMOVETAG"
                #node.text = "&#8679;"

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[img]", self.parser_node)
            etree.strip_tags(xml, "REPLACEIMGSETAREMOVETAG")
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
            return data


class DataSanitizationPipeline(object):
    def __init__(self):
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.GraphicInExtLink(),
            self.TableinBody(),
            self.TableinP(),
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
            self.AddAttributeXMLTextToElementAPipe(),
            self.MoveANameWhichAreAtTheEndOfAnyElement(),
            self.IdentifyAssetLabelCandidatesPipe(),
            self.IdentifyFnLabelCandidatesPipe(),
            self.DeduceAndSuggestConversionPipe(),
            self.ApplySuggestedConversionPipe(),
            self.CompleteAssetPipe(),
            self.IdentifyAssetLabelAndCaptionPipe(),
            self.FixAssetContent(),
            self.ImgPipe(),
            self.MoveFnPipe(),
            self.AddContentToFnPipe(),
            self.IdentifyFnLabelAndPPipe(),
            self.FixFnContent(),
            self.TargetPipe(),
            self.GraphicInXrefPipe(),
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

    class MoveANameWhichAreAtTheEndOfAnyElement(plumber.Pipe):

        def _select(self, xml):
            for a in xml.findall(".//a[@name]"):
                if a.getchildren() == []:
                    a.set("move", "true")

        def _find(self, xml):
            for a in xml.findall(".//a[@move]"):
                p = a.getparent()
                if a.getnext() is None and not (a.tail or "").strip() and p is not None and p.getparent() is not None:
                    logger.info("mover %s" % etree.tostring(a))
                    logger.info(etree.tostring(a.getparent()))
                    return a
                else:
                    a.attrib.pop("move")

        def _move_a_name(self, a_name):
            p = a_name.getparent()
            new_a_name = deepcopy(a_name)
            p.addnext(new_a_name)
            p.remove(a_name)
            a_name = new_a_name

        def transform(self, data):
            raw, xml = data
            self._select(xml)
            while True:
                found = self._find(xml)
                if found is None:
                    break
                self._move_a_name(found)
            return data

    class AddAttributeXMLTextToElementAPipe(plumber.Pipe):

        def add_xml_text_to_a_href(self, xml):
            previous_xml_text = None
            previous = None
            for i, node in enumerate(xml.findall(".//a[@href]")):
                href = node.get("href")
                if ":" in href:
                    continue
                text = get_node_inner_text(node).lower()
                if not text:
                    continue
                node.set("xml_text", text)
                previous_href = None
                if previous is not None:
                    previous_href = previous.get("href")
                if (text[0].isdigit() and previous_xml_text and
                        previous_xml_text[0].isalpha() and previous_href):
                    pos = - len(text)

                    if (previous_href == node.get("href") or
                            previous_href[:pos] == node.get("href")[:pos]):
                        label = previous_xml_text.split()[0]
                        node.set("xml_text", label + " " + text)
                        logger.info(etree.tostring(previous))
                        logger.info(etree.tostring(node))
                previous_xml_text = text
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
            logger.info("AddAttributeXMLTextToElementAPipe")
            self.add_xml_text_to_a_href(xml)
            self.add_xml_text_to_other_a(xml)
            return data

    class IdentifyAssetLabelCandidatesPipe(plumber.Pipe):
        def add_href(self, xml):
            root = xml.getroottree()
            bold_items = {}
            for style_tag in ["bold"]:
                for style_node in xml.findall(".//{}".format(style_tag)):
                    if not style_node.getparent().get("href"):
                        text = (style_node.text or "").replace(":", "").strip().lower()
                        if text and text[0].isalpha():
                            bold_items[(text[0], text[-1])] = bold_items.get((text[0], text[-1]), [])
                            bold_items[(text[0], text[-1])].append(style_node)
            for node in xml.findall(".//a[@href]"):
                xml_text = node.get("xml_text")
                href = node.get("href")
                if href[0] == "#" and xml_text and xml_text[0].isalpha():
                    first, last = xml_text[0], xml_text[-1]
                    parts = xml_text.replace(".", "").split(" ")
                    for bold in bold_items.get((first, last), []):
                        bold_text = get_node_inner_text((bold)).replace(":", "").lower()
                        if len(parts) == 2 and bold_text.startswith(parts[0]) and bold_text.endswith(parts[1]):
                            if bold not in node.findall(".//*"):
                                bold.set("label-of", href[1:])
                                logger.info("bold: %s" % (etree.tostring(bold)))
                                logger.info("node: %s" % (etree.tostring(node)))
                        elif len(parts) == 1 and bold_text.startswith(parts[0]):
                            if bold not in node.findall(".//*"):
                                bold.set("label-of", href[1:])
                                logger.info("bold: %s" % (etree.tostring(bold)))
                                logger.info("node: %s" % (etree.tostring(node)))

        def transform(self, data):
            logger.info("IdentifyAssetLabelCandidatesPipe")
            raw, xml = data
            self.add_href(xml)
            logger.info("IdentifyAssetLabelCandidatesPipe - fim")
            return data

    class DeduceAndSuggestConversionPipe(plumber.Pipe):
        """Este pipe analisa os dados dos elementos a[@href] e a[@name],
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
            """
            De acordo com o a[@xml_text] e/ou a.text,
            adiciona no elemento a, atributos:
            xml_tag
            xml_id
            xml_reftype
            xml_label
            """
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
            """
            De acordo com o a[@href] cujo conteúdo é nome de arquivo,
            adiciona no elemento a, atributos:
            xml_tag
            xml_id
            xml_reftype
            xml_label
            """
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
            """
            De acordo com o a[@name],
            adiciona no elemento a, atributos:
            xml_tag
            xml_id
            xml_reftype
            xml_label
            """
            for name, a_name_and_hrefs in a_names.items():
                new_id = None
                a_name, a_hrefs = a_name_and_hrefs
                complete, incomplete = self._classify_nodes(a_hrefs)
                if complete:
                    text, tag, reftype, new_id = complete
                else:
                    tag_reftype = None
                    next_texts = [a_name.tail]
                    _next = a_name
                    img = None
                    for i in range(0, 2):
                        _next = _next.getnext()
                        if _next is None:
                            break
                        if _next.tag == "img" or _next.find(".//img") is not None:
                            img = True
                        next_texts.append(get_node_inner_text(_next))
                    for next_text in next_texts:
                        if next_text:
                            break
                    if next_text:
                        tag_reftype = self.inferer.tag_and_reftype_from_a_href_text(
                            next_text
                        )
                    if not tag_reftype:
                        tag_reftype = self.inferer.tag_and_reftype_from_name(name)
                    if not tag_reftype and img:
                        tag_reftype = "fig", "fig"
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
                if found.attrib.get("xml_tag") in [
                    "app",
                    "fig",
                    "table-wrap",
                    "disp-formula",
                ]:
                    return found

        def _add_xml_attribs_to_img(self, images):
            """
            De acordo com o img[@src],
            adiciona no elemento a, atributos:
            xml_tag
            xml_id
            xml_reftype
            xml_label
            """
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

    class IdentifyFnLabelCandidatesPipe(plumber.Pipe):
        """
        No texto há âncoras (a[@name]) e referencias cruzada (a[@href]):
        TEXTO->NOTAS e NOTAS->TEXTO.
        Remove as âncoras e referências cruzadas relacionadas com NOTAS->TEXTO.
        Também remover duplicidade de a[@name]
        Algumas NOTAS->TEXTO podem ser convertidas a "fn/label"
        """
        def _remove_p(self, xml):
            fn_items = xml.findall(".//a")
            for node in xml.findall(".//p[a]"):
                if len(node.getchildren()) == 1 and not get_node_inner_text(node):
                    node.tag = "IDENTIFYFNLABELREMOVETAG"
            etree.strip_tags(xml, "IDENTIFYFNLABELREMOVETAG")

        def _identify_order(self, xml):
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

        def _exclude_repeated_and_invalid_nodes_or_identify_as_label(self, items_by_id):
            for _id, nodes in items_by_id.items():
                repeated = [n for n in nodes if n.attrib.get("name")]
                # remove os a[@name] repetidos
                if len(repeated) > 1:
                    for n in repeated[1:]:
                        nodes.remove(n)
                        parent = n.getparent()
                        parent.remove(n)
                if nodes[0].get("name"):
                    nodes[0].tag = "_EXCLUDE_REMOVETAG"
                    root = None
                    for a_href in nodes[1:]:
                        found = None
                        if a_href.get("href"):
                            found = self._find_a_name_with_corresponding_xml_text(a_href, root)
                        if found is None:
                            logger.info("remove: %s" % etree.tostring(a_href))
                            _remove_element_or_comment(a_href)
                        else:
                            logger.info("Identifica candidato a fn/label")
                            logger.info(etree.tostring(a_href))
                            a_href.tag = "label"
                            a_href.set("label-of", found.get("name"))
                            logger.info(etree.tostring(a_href))
                    parent = nodes[0].getparent()
                    etree.strip_tags(parent, "_EXCLUDE_REMOVETAG")
                if len(nodes) == 1 and nodes[0].attrib.get("href"):
                    _remove_element_or_comment(nodes[0])

        def _find_a_name_with_corresponding_xml_text(self, a_href, root):
            xml_text = a_href.get("xml_text")
            if xml_text and not xml_text[0].isalpha() and get_node_inner_text(a_href):
                root = root or a_href.getroottree()
                for item in root.findall(".//a[@xml_text='{}']".format(xml_text)):
                    if item.get("name"):
                        return item

        def transform(self, data):
            raw, xml = data
            logger.info("IdentifyFnLabelCandidatesPipe")
            self._remove_p(xml)
            items_by_id = self._identify_order(xml)
            self._exclude_repeated_and_invalid_nodes_or_identify_as_label(items_by_id)
            etree.strip_tags(xml, "_EXCLUDE_REMOVETAG")
            logger.info("IdentifyFnLabelCandidatesPipe - fim")
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
            # node.attrib.clear()
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

    class GraphicInXrefPipe(plumber.Pipe):
        def parser_node(self, node):
            graphic = node.find("graphic")
            new = etree.Element("styled-content")
            new.append(deepcopy(graphic))
            node.remove(graphic)
            node.append(new)

        def transform(self, data):
            raw, xml = data
            _process(xml, "xref[graphic]", self.parser_node)
            return data

    class MoveFnPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            self._move_fn_out_of_style_tags(xml)
            self._remove_p_if_fn_is_only_child(xml)
            return data

        def _move_fn_out_of_style_tags(self, xml):
            changed = True
            while changed:
                changed = False
                for tag in ["sup", "bold", "italic"]:
                    self._identify_fn_to_move_out(xml, tag)
                    ret = self._move_fn_out(xml)
                    if ret:
                        changed = True

        def _remove_p_if_fn_is_only_child(self, xml):
            for p in xml.findall(".//p[fn]"):
                if len(p.findall(".//*")) == 1 and not get_node_inner_text(p):
                    p.tag = "REMOVEPIFFNISONLYCHLDREMOVETAG"
            etree.strip_tags(xml, "REMOVEPIFFNISONLYCHLDREMOVETAG")

        def _identify_fn_to_move_out(self, xml, style_tag):
            for node in xml.findall(".//{}[fn]".format(style_tag)):
                text = (node.text or "").strip()
                children = node.getchildren()
                if children[0].tag == "fn" and not text:
                    node.set("move", "backward")
                elif children[-1].tag == "fn" and not (children[-1].tail or "").strip():
                    node.set("move", "forward")

        def _move_fn_out(self, xml):
            changed = False
            for node in xml.findall(".//*[@move]"):
                move = node.attrib.pop("move")
                if move == "backward":
                    self._move_fn_out_and_backward(node)
                elif move == "forward":
                    self._move_fn_out_and_forward(node)
                changed = True
            return changed

        def _move_fn_out_and_backward(self, node):
            fn = node.find("fn")
            fn_copy = deepcopy(fn)
            fn_copy.tail = ""
            node.addprevious(fn_copy)
            node.text = fn.tail
            node.remove(fn)

        def _move_fn_out_and_forward(self, node):
            fn = node.getchildren()[-1]
            fn_copy = deepcopy(fn)
            node.addnext(fn_copy)
            node.remove(fn)

    class AddContentToFnPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            logger.info("AddContentToFnPipe")
            for fn in xml.findall(".//fn"):
                fn.set("move", "true")
            while True:
                fn = xml.find(".//fn[@move]")
                if fn is None:
                    break
                fn.attrib.pop("move")
                self._move_label_into_fn(fn)
                self._move_fn_tail_into_fn(fn)
                # print(etree.tostring(fn))
            return data

        def _move_label_into_fn(self, node):
            logger.info("AddContentToFnPipe._move_label_into_fn")
            text = None
            previous = node.getprevious()
            if previous is not None:
                if previous.tag == "label":
                    node.insert(0, deepcopy(previous))
                    _remove_element_or_comment(previous)
                    return
                else:
                    n = previous
                    text = get_node_inner_text(previous)
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

        def _move_fn_tail_into_fn(self, node):
            logger.info("AddContentToFnPipe._move_fn_tail_into_fn")
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
                    if get_node_inner_text(node):
                        break
                node.append(deepcopy(_next))
                parent = _next.getparent()
                parent.remove(_next)

    class CompleteAssetPipe(plumber.Pipe):
        def _find_xml_text_in_node(self, xml_text, node_text):
            if "." in xml_text:
                xml_text_parts = xml_text.replace(".", "")
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
                    xml_text = asset_node.get("xml_text")
                    text = get_node_inner_text(_next)

                    if xml_text and text:
                        if self._find_xml_text_in_node(xml_text, text):
                            label = _next
                            label.set("content-type", "label")

                i += 1
                children.append(_next)
            return children, label, img, table

        def complete_asset_node(self, asset_node):
            asset_node.set("fix", "asset")
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
            logger.info("CompleteAssetPipe - fig")
            for asset_node in xml.findall(".//fig"):
                print(etree.tostring(asset_node))
                self.complete_asset_node(asset_node)
            logger.info("CompleteAssetPipe - table-wrap")
            for asset_node in xml.findall(".//table-wrap"):
                self.complete_asset_node(asset_node)
            logger.info("CompleteAssetPipe - app")
            for asset_node in xml.findall(".//app"):
                self.complete_asset_node(asset_node)
            return data

    class IdentifyAssetLabelAndCaptionPipe(plumber.Pipe):
        def _identify_label_and_caption_from_bold(self, bold):
            bold.attrib.clear()
            label = etree.Element("label")
            bold.addnext(label)
            label.append(deepcopy(bold))
            p = bold.getparent()
            p.remove(bold)
            caption = etree.Element("caption")
            title = etree.Element("title")
            caption.append(title)
            label.addnext(caption)
            _next = caption
            title.text = caption.tail
            caption.tail = ""
            p = caption.getparent()
            removed = []
            while True:
                _next = _next.getnext()
                if _next is None:
                    break
                title.append(deepcopy(_next))
                removed.append(_next)
            for item in removed:
                p.remove(item)

        def identify_label_and_caption(self, asset_node):
            label_parent = asset_node.find(".//*[@content-type='label']")
            search_expr = asset_node.get("xml_text")
            if not search_expr[0].isalpha():
                search_expr = asset_node.get("xml_tag")
            if label_parent is None:
                for node in asset_node.findall("*"):
                    label_text = get_node_inner_text(node).lower()
                    if label_text.startswith(search_expr):
                        node.set("content-type", "label")
                        label_parent = node
                        break
                for node in asset_node.findall(".//*"):
                    if (node.text or "").lower().startswith(search_expr):
                        if node.tag == "bold":
                            node.set("label-of", asset_node.get("id"))
                        break

            if label_parent is not None:
                bold = label_parent.find(".//bold[@label-of]")
                if bold is not None:
                    self._identify_label_and_caption_from_bold(bold)
                else:
                    self._guess_label_and_caption(asset_node, label_parent)

        def _guess_label_and_caption(self, asset_node, label_parent):
            # FIXME
            found = None
            for node in label_parent.findall(".//*"):
                text = node.text
                if text and text.lower().startswith(asset_node.get("xml_text")):
                    found = node
                    break
            if found is None:
                for node in label_parent.findall(".//*"):
                    text = node.text
                    if text and text.lower().startswith(asset_node.get("xml_text")[:3]):
                        found = node
                        break
            if found is not None:
                xml_text_words = asset_node.get("xml_text").split(" ")
                found_text_words = (found.text or "").split(" ")
                if len(xml_text_words) <= len(found_text_words):
                    label = etree.Element("label")
                    found.addprevious(label)
                    label.text = " ".join(
                        [w for w in found_text_words[:len(xml_text_words)]])
                    caption = etree.Element("caption")
                    title = etree.Element("title")
                    caption.append(title)
                    label.addnext(caption)
                    title.text = found.text.replace(label.text, "")
                    for child in found.getchildren():
                        title.append(deepcopy(child))
                    p = found.getparent()
                    p.remove(found)

        def transform(self, data):
            raw, xml = data
            logger.info("IdentifyAssetLabelAndCaptionPipe")
            for asset_node in xml.findall(".//*[@fix='asset']"):
                print(etree.tostring(asset_node))
                self.identify_label_and_caption(asset_node)
                print(etree.tostring(asset_node))
                print(".-----")
                id = asset_node.get("id")
                asset_node.attrib.clear()
                asset_node.set("id", id)
            return data

    class FixFnContent(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            logger.info("FixFnContent")
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
                        logger.info("FixFnContent: %s" % etree.tostring(children[0]))
            return data

    class FixAssetContent(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            logger.info("FixAssetContent")
            for tag in ["fig", "table-wrap", "app"]:
                for node in xml.findall(".//{}".format(tag)):
                    xref = node.find("xref")
                    if xref is not None:
                        xref.tag = "REMOVEPFIXASSETCONTENT"
                    label_of = node.find(".//*[@label-of]")

                    if label_of is not None:
                        label_of.attrib.clear()
                    for p in node.findall("p"):
                        p.tag = "REMOVEPFIXASSETCONTENT"

            etree.strip_tags(xml, "REMOVEPFIXASSETCONTENT")
            return data

    class IdentifyFnLabelAndPPipe(plumber.Pipe):
        def _create_label(self, new_fn, node):
            if node.find(".//label") is not None:
                return

            children = node.getchildren()
            node_text = (node.text or "").strip()
            if node_text:
                # print("IdentifyFnLabelAndPPipe - _create_label_from_node_text")
                logger.info("IdentifyFnLabelAndPPipe - _create_label_from_node_text")
                label = self._create_label_from_node_text(new_fn, node)
            elif children:
                # print("IdentifyFnLabelAndPPipe - _create_label_from_style_tags")
                logger.info("IdentifyFnLabelAndPPipe - _create_label_from_style_tags")
                self._create_label_from_style_tags(new_fn, node)
                if new_fn.find(".//label") is None:
                    # print("IdentifyFnLabelAndPPipe - _create_label_from_children")
                    logger.info("IdentifyFnLabelAndPPipe - _create_label_from_children")
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
            node_text = get_node_inner_text(node)
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
                node_text = get_node_inner_text(node)
                node_style_text = get_node_inner_text(node_style)
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

        def transform(self, data):
            raw, xml = data
            for fn in xml.findall(".//fn"):
                logger.info("IdentifyFnLabelAndPPipe")
                new_fn = etree.Element("fn")
                for k, v in fn.attrib.items():
                    if k in ["id", "label", "fn-type"]:
                        new_fn.set(k, v)
                self._create_label(new_fn, fn)
                self._create_p(new_fn, fn)
                fn.addprevious(new_fn)
            for fn in xml.findall(".//DELETE"):
                parent = fn.getparent()
                parent.remove(fn)
            return data

    class TargetPipe(plumber.Pipe):
        """
        Os elementos "a" podem ser convertidos a tabelas, figuras, fórmulas,
        anexos, notas de rodapé etc. Por padrão, até este ponto, todos os
        elementos não identificados como tabelas, figuras, fórmulas,
        anexos, são identificados como "fn" (notas de rodapé). No entanto,
        podem ser apenas "target"
        """
        _fn_items = None

        def get_fn_items(self, xml):
            if self._fn_items is None:
                body = xml.getroottree().find(".//body")
                paragraphs = [e.findall(".//fn") for e in body.findall("*")]
                blank = []
                fn_items = []
                for fns in paragraphs[::-1]:
                    if len(fns):
                        fn_items.extend(fns)
                    elif len(blank) and len(fn_items):
                        break
                    else:
                        blank.append("")
                self._fn_items = fn_items

        def transform(self, data):
            raw, xml = data
            self.get_fn_items(xml)
            for node in xml.findall(".//target"):
                id = node.get("id")
                node.attrib.clear()
                node.set("id", id)
            for fn in xml.findall(".//fn"):
                _is_target = self._is_target(fn)
                if _is_target:
                    if self._is_a_top_target(fn):
                        fn.tag = "TARGETREMOVETAG"
                    elif fn.getchildren() or get_node_inner_text(fn):
                        target = etree.Element("target")
                        for k, v in fn.attrib.items():
                            target.set(k, v)
                        fn.addprevious(target)
                        fn.tag = "TARGETREMOVETAG"
                        label = fn.find(".//label")
                        if label is not None:
                            label.tag = "TARGETREMOVETAG"
                    else:
                        fn.tag = "target"
                    logger.info("target resultado:")
                    logger.info(etree.tostring(fn))
            self._remove_target_in_assets(xml)
            etree.strip_tags(xml, "TARGETREMOVETAG")
            return data

        def _is_target(self, node):
            if node.find("label") is not None and node.find("p") is not None:
                return False
            logger.info("Target?")

            if node not in self._fn_items:
                return True

        def _is_a_top_target(self, node):
            root = node.getroottree()
            nodes = root.findall(".//*")
            if node in nodes[:len(nodes)//2]:
                return True

        def _remove_target_in_assets(self, xml):
            for tag in ["table-wrap", "fig", "disp-formula"]:
                for n in xml.findall(".//{}".format(tag)):
                    for target in n.findall(".//target"):
                        target.tag = "TARGETREMOVETAG"
            etree.strip_tags(xml, "TARGETREMOVETAG")


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
    last = node
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
            text = a_href.get("xml_text")
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

    def get_remote_content(self, timeout=1):
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
                logger.info("%s: valor inválido de caminho local para ativo digital" % self.local)
                return
            if not os.path.isdir(dirname):
                os.makedirs(dirname)
            with open(self.local, "wb") as fp:
                if self.ext in [".html", ".htm"]:
                    fp.write(self.ent2char(_content))
                else:
                    fp.write(_content)
            return _content


def fix_relative_paths(body, file_location):
    changes = 0
    if body is not None:
        for tag, attr in [("img", "src"), ("a", "href")]:
            for node in body.findall(".//{}[@{}]".format(tag, attr)):
                location = node.get(attr)
                new_location = None
                if location.startswith("./"):
                    new_location = os.path.join(file_location, location)
                elif (":" not in location and
                      location[0] != "#" and
                      "/" not in location and
                      location.count(".") == 1):
                    new_location = os.path.join(file_location, location)
                if new_location and new_location != location:
                    changes += 1
                    logger.info(
                        "Troca de {} para {}".format(location, new_location))
                    node.set(attr, new_location)
    return changes


def fix_paths(body):
    changes = 0
    if body is not None:
        for tag, attr in [("img", "src"), ("a", "href")]:
            for node in body.findall(".//{}[@{}]".format(tag, attr)):
                location = node.get(attr)
                old_location = location
                if '/img' in location:
                    location = location[location.find("/img"):]
                if " " in location:
                    location = "".join(location.split())
                if location.startswith("img/"):
                    location = "/" + location
                location = location.replace("/img/fbpe", "/img/revistas")
                if old_location != location:
                    changes += 1
                    logger.info(
                        "Troca de {} para {}".format(old_location, location))
                    node.set(attr, location)
    return changes


class HTMLPage:

    def __init__(self, href):
        self.file = FileLocation(href)
        html_tree = self._load()
        self.body = None
        if html_tree is not None:
            self.body = html_tree.find(".//body")
            fix_paths(self.body)
            fix_relative_paths(self.body, href)

    def _load(self):
        # TODO: Tratar excecoes
        html_content = self.file.content
        if html_content:
            return etree.fromstring(html_content, parser=etree.HTMLParser())


class InsertExternalHTMLBodyIntoXMLBody:
    IMG_EXTENSIONS = (".gif", ".jpg", ".jpeg", ".svg", ".png", ".tif", ".bmp")

    def __init__(self, xml):
        self.xml = xml
        self.body = self.xml.find(".//body")
        self.p_nodes = []
        if self.body is not None:
            self.p_nodes = self.body.findall("*")

    def remote_to_local(self):
        self._import_all_href_html_files()
        self._import_external_asset_file()
        self._download_files()

    def _classify_a_href(self):
        for a_href in self.xml.findall(".//a[@href]"):
            if not a_href.get("link-type"):

                href = a_href.get("href")
                if ":" in href:
                    a_href.set("link-type", "external")
                    logger.info("Classificou a[@href]: %s" % etree.tostring(a_href))
                    continue

                if href and href[0] == "#":
                    a_href.set("link-type", "internal")
                    logger.info("Classificou a[@href]: %s" % etree.tostring(a_href))
                    continue

                value = href.split("/")[0]
                if "." in value:
                    if href.startswith("./") or href.startswith("../"):
                        pass
                    else:
                        # pode ser URL
                        a_href.set("link-type", "external")
                        logger.info("Classificou a[@href]: %s" % etree.tostring(a_href))
                        continue

                basename = os.path.basename(href)
                f, ext = os.path.splitext(basename)
                if ".htm" in ext:
                    a_href.set("link-type", "html")
                elif href.startswith("/pdf/"):
                    a_href.set("link-type", "download")
                elif href.startswith("/img/revistas"):
                    a_href.set("link-type", "asset")
                else:
                    logger.info("link-type=???")
                logger.info("Classificou a[@href]: %s" % etree.tostring(a_href))

    def _import_all_href_html_files(self):
        while True:
            changes = fix_paths(self.body)
            self._classify_a_href()
            q = self._import_html_file_content()
            print(changes + q)
            if q + changes == 0:
                break

    def _download_files(self):
        nodes = list(set(self.xml.xpath(".//*[@link-type='download']|.//*[@src]")))
        downloaded = {}
        for node in nodes:
            if node.get("link-type") != "internal":
                attr_name = "src" if node.get("src") else "href"
                location = node.get(attr_name)
                logger.info("Verifica se já foi feito download / faz download: %s" % location)
                basename = downloaded.get(location)
                if basename is None:
                    asset_file = FileLocation(location)
                    if asset_file.content:
                        basename = asset_file.basename
                        downloaded[location] = basename
                if basename:
                    node.set(attr_name, basename)
                    node.set("link-type", "internal")
                    logger.info("Baixado. Altera o caminho: %s" % etree.tostring(node))

    def _import_html_file_content(self):
        new_p_items = []
        _new_p_items = {}
        for p in self.p_nodes:
            for a_href in p.findall(".//a[@link-type='html']"):
                _new_p_items[a_href] = p

        for a_href in self.xml.findall(".//a[@link-type='html']"):
            logger.info("Importar conteúdo de %s" % etree.tostring(a_href))
            href = a_href.get("href")
            f, ext = os.path.splitext(href)
            new_href = os.path.basename(f)
            if ext.startswith(".htm") and "#" in ext:
                href = href.split("#")[0]
                new_href = new_href.split("#")[0]
            if '.htm' in ext:
                # TODO tratar excecoes
                html = HTMLPage(href)
                if html.body is not None:
                    new_p = self._create_new_p_with_imported_html_body(
                        a_href, new_href, deepcopy(html.body))
                    # localiza onde será inserido o new_p
                    new_p_items.append((_new_p_items.get(a_href), new_p))

        for p, new_p in new_p_items[::-1]:
            logger.info(
                "Insere novo p com conteudo do html externo: %s" % etree.tostring(new_p))
            p.addnext(new_p)
        return len(new_p_items)

    def _create_new_p_with_imported_html_body(self, a_href, new_href, body):
        a_href.set("href", "#"+new_href)
        a_href.set("link-type", "internal")
        logger.info("Atualiza a[@href]: %s" % etree.tostring(a_href))

        body.tag = "CREATENEWPREMOVETAG"
        for a in body.findall(".//a"):
            logger.info("Encontrado elem a no body importado: %s" % etree.tostring(a))
            href = a.get("href")
            if href and href[0] == "#":
                a.set("href", "#" + new_href + href[1:].replace("#", "X"))
            elif a.get("name"):
                a.set("name", new_href + "X" + a.get("name"))
            logger.info("Atualiza elem a importado: %s" % etree.tostring(a))

        a_name = a_href.getroottree().find(".//a[@name='{}']".format(new_href))
        if a_name is None:
            a_name = body.find(".//a[@name='{}']".format(new_href))
        if a_name is not None:
            a_name.tag = "CREATENEWPREMOVETAG"
        a_name = etree.Element("a")
        a_name.set("id", new_href)
        a_name.set("name", new_href)
        a_name.append(body)

        new_p = etree.Element("p")
        new_p.set("content-type", "html")
        new_p.append(a_name)
        etree.strip_tags(new_p, "CREATENEWPREMOVETAG")
        logger.info("Cria novo p: %s" % etree.tostring(new_p))

        return new_p

    def _import_external_asset_file(self):
        new_p_items = []
        _new_p_items = {}
        for p in self.p_nodes:
            for a_href in p.findall(".//a[@link-type='asset']"):
                _new_p_items[a_href] = p

        for a_href in self.xml.findall(".//a[@link-type='asset']"):
            logger.info("Importar conteúdo de %s" % etree.tostring(a_href))
            href = a_href.get("href")
            f, ext = os.path.splitext(href)
            new_href = os.path.basename(f)
            if ext:
                new_p = self._create_new_p_with_asset_data(a_href, new_href)
                # localiza onde será inserido o new_p
                new_p_items.append((_new_p_items.get(a_href), new_p))

        for p, new_p in new_p_items[::-1]:
            logger.info(
                "Insere novo p com ativo digital: %s" % etree.tostring(new_p))
            p.addnext(new_p)
        return len(new_p_items)

    def _create_new_p_with_asset_data(self, a_href, new_href):
        location = a_href.get("href")
        a_href.set("href", "#"+new_href)
        a_href.set("link-type", "internal")
        logger.info("Atualiza a[@href]: %s" % etree.tostring(a_href))

        tag = "img"
        ign, ext = os.path.splitext(location)
        if ext.lower() not in self.IMG_EXTENSIONS:
            tag = "media"
        asset = etree.Element(tag)
        asset.set("src", location)

        a_name = a_href.getroottree().find(".//a[@name='{}']".format(new_href))
        if a_name is not None:
            a_name.tag = "CREATENEWPREMOVETAGASSETDATA"
        a_name = etree.Element("a")
        a_name.set("id", new_href)
        a_name.set("name", new_href)
        a_name.append(asset)

        new_p = etree.Element("p")
        new_p.set("content-type", "asset")
        new_p.append(a_name)
        etree.strip_tags(a_href.getroottree(), "CREATENEWPREMOVETAGASSETDATA")
        logger.info("Cria p: %s" % etree.tostring(new_p))
        return new_p
