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


def gera_id(_string, index_body):
    if not _string:
        return
    rid = _string

    if not rid[0].isalpha():
        rid = "replace_by_reftype" + rid

    if index_body == 1:
        return rid.lower()

    ref_id = "%s-body%s" % (rid, index_body)
    return ref_id.lower()


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
            self.SetupPipe(super_obj=self),
            self.SaveRawBodyPipe(super_obj=self),
            self.DeprecatedHTMLTagsPipe(),
            self.RemoveImgSetaPipe(),
            self.RemoveDuplicatedIdPipe(),
            self.RemoveOrMoveStyleTagsPipe(),
            self.RemoveEmptyPipe(),
            self.RemoveStyleAttributesPipe(),
            self.RemoveCommentPipe(),
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
            self.ConvertElementsWhichHaveIdPipe(super_obj=self),
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
            self.RemoveRefIdPipe(),
            self.SanitizationPipe(),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(CustomPipe):
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
                os.path.join("/tmp/", "%s.xml" % self.super_obj.pid),
                encoding="utf-8",
                doctype=config.DOC_TYPE_XML,
                xml_declaration=True,
                pretty_print=True,
            )
            return data, xml

    class DeprecatedHTMLTagsPipe(CustomPipe):
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
        STYLE_TAGS = ("b", "i", "em", "strong", "u", "sup", "sub")

        def _wrap_node_content_with_new_tag(self, node, new_tag):
            # envolve o conteúdo de node com new_tag
            if node.tag == new_tag:
                return
            node_copy = etree.Element(node.tag)
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
                    text = get_node_text(node)
                    children = node.getchildren()
                    if not text:
                        node.tag = "STRIPTAG"
                    elif node.find(".//{}".format(style_tag)) is not None:
                        node.tag = "STRIPTAG"
                    elif node.find("p") is not None:
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
                        first.tag = "REMOVE_TAG"
                        change = True
                    if (last.tail or "").strip() == "" and last.tag == "br":
                        last.tag = "REMOVE_TAG"
                        change = True
                if not change:
                    break
            etree.strip_tags(xml, "REMOVE_TAG")
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
                parent = node.getparent()
                new = self._create_new_node(node)
                if node.tag == "p":
                    new.tag = "REMOVE_TAG"
                node.addprevious(new)
                node.set("content-type", "remove")
                parent.remove(node)
            etree.strip_tags(xml, "REMOVE_TAG")
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

    class ConvertElementsWhichHaveIdPipe(CustomPipe):
        def transform(self, data):
            raw, xml = data

            convert = ConvertElementsWhichHaveIdPipeline(html_pipeline=self.super_obj)
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
    def __init__(self, html_pipeline):
        # self.super_obj = html_pipeline
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.RemoveThumbImgPipe(),
            self.AddNameAndIdToElementAPipe(super_obj=html_pipeline),
            self.RemoveInvalidAnchorAndLinksPipe(),
            self.DeduceAndSuggestConversionPipe(super_obj=html_pipeline),
            self.ApplySuggestedConversionPipe(super_obj=html_pipeline),
            self.AddAssetInfoToTablePipe(super_obj=html_pipeline),
            self.CreateAssetElementsFromExternalLinkElementsPipe(
                super_obj=html_pipeline
            ),
            self.CreateAssetElementsFromImgOrTableElementsPipe(super_obj=html_pipeline),
            self.APipe(super_obj=html_pipeline),
            self.ImgPipe(super_obj=html_pipeline),
            self.MoveFnPipe(),
            self.AddContentToFnPipe(),
            self.FnLabelAndPPipe(),
            self.TargetPipe(),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(plumber.Pipe):
        def transform(self, data):
            new_obj = deepcopy(data)
            return data, new_obj

    class AddAssetInfoToTablePipe(CustomPipe):
        def parser_node(self, node):
            _id = node.attrib.get("id")
            if _id:
                new_id = gera_id(_id, self.super_obj.index_body)
                node.set("id", new_id)
                node.set("xml_id", new_id)
                node.set("xml_tag", "table-wrap")
                node.set("xml_label", "Tab")

        def transform(self, data):
            raw, xml = data
            _process(xml, "table[@id]", self.parser_node)
            return data

    class CreateAssetElementsFromExternalLinkElementsPipe(CustomPipe):
        def _create_asset_content_as_graphic(self, node_a):
            href = node_a.attrib.get("href")
            new_graphic = etree.Element("graphic")
            new_graphic.set("{http://www.w3.org/1999/xlink}href", href)
            return new_graphic

        def _create_asset_content_from_html(self, node_a, new_tag, new_id):
            href = node_a.attrib.get("href")
            parts = href.split("/")
            local = "/".join(parts[-4:])
            remote = config.get("STATIC_URL_FILE") + href[1:]
            local = os.path.join(config.get("SITE_SPS_PKG_PATH"), local)
            asset_in_html_page = AssetInHTMLPage(local, remote)
            tree = asset_in_html_page.convert(self.super_obj)
            if tree is not None:
                tree.tag = "REMOVE_TAG"
                return tree
            return

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

                if ext in [".htm", ".html"]:
                    asset_content = self._create_asset_content_from_html(
                        a_href, new_tag, new_id
                    )
                else:
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
            self.super_obj.document.xmltree = xml
            a_href_texts, file_paths = self.super_obj.document.a_href_items
            for path, nodes in file_paths.items():
                if nodes[0].attrib.get("xml_tag"):
                    self._create_asset_group(nodes[0])
                    for node in nodes[1:]:
                        self._create_xref(node)
            return data

    class CreateAssetElementsFromImgOrTableElementsPipe(CustomPipe):
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
            img_or_table_parent = img_or_table.getparent()
            label_and_caption = self._find_label_and_caption_around_node(img_or_table)
            asset = self._get_asset_node(img_or_table, xml_new_tag, xml_id)
            if label_and_caption:
                if label_and_caption[1] is not None:
                    asset.insert(0, label_and_caption[1])
                asset.insert(0, label_and_caption[0])
            new_img_or_table = deepcopy(img_or_table)
            img_or_table_parent.remove(img_or_table)
            for attr in ["xml_id", "xml_reftype", "xml_label", "xml_tag"]:
                if attr in new_img_or_table.attrib.keys():
                    new_img_or_table.attrib.pop(attr)
            asset.append(new_img_or_table)

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

    class AddNameAndIdToElementAPipe(CustomPipe):
        """Garante que todos os elemento a[@name] e a[@id] tenham @name e @id.
        Corrige id e name caso contenha caracteres nao alphanum.
        """

        def _replace_not_alphanum(self, c):
            return c if c.isalnum() else "x"

        def replace_not_alphanum(self, name):
            if name:
                return "".join([self._replace_not_alphanum(c) for c in name])

        def parser_node(self, node):
            _id = self.replace_not_alphanum(node.attrib.get("id"))
            _name = self.replace_not_alphanum(node.attrib.get("name"))
            node.set("id", _name or _id)
            node.set("name", _name or _id)
            href = node.attrib.get("href")
            if href and href[0] == "#":
                a = etree.Element("a")
                a.set("name", node.attrib.get("name"))
                a.set("id", node.attrib.get("id"))
                node.addprevious(a)
                node.set("href", "#" + self.replace_not_alphanum(href[1:]))
                node.attrib.pop("id")
                node.attrib.pop("name")

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@id]", self.parser_node)
            _process(xml, "a[@name]", self.parser_node)
            return data

    class DeduceAndSuggestConversionPipe(CustomPipe):
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
            if new_id.startswith("replace_by_reftype") and ref_type:
                new_id = new_id.replace("replace_by_reftype", ref_type)
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
                    new_id = gera_id(node_id, self.super_obj.index_body)
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
                        new_id = gera_id(node_id or alt_id, self.super_obj.index_body)
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
                        new_id = gera_id(_id, self.super_obj.index_body)
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
                        new_id = gera_id(name, self.super_obj.index_body)
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
            for path, images in images.items():
                text, new_id, tag, reftype = None, None, None, None
                tag_reftype_id = self.inferer.tag_and_reftype_and_id_from_filepath(path)
                if tag_reftype_id:
                    tag, reftype, _id = tag_reftype_id
                    new_id = gera_id(_id, self.super_obj.index_body)
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
            self.super_obj.document.xmltree = xml
            texts, file_paths = self.super_obj.document.a_href_items
            names = self.super_obj.document.a_names
            images = self.super_obj.document.images
            self._add_xml_attribs_to_a_href_from_text(texts)
            self._add_xml_attribs_to_a_name(names)
            self._add_xml_attribs_to_a_href_from_file_paths(file_paths)
            self._add_xml_attribs_to_img(images)
            return data

    class RemoveInvalidAnchorAndLinksPipe(plumber.Pipe):
        """
        No texto há âncoras (a[@name]) e referencias cruzada (a[@href]):
        TEXTO->NOTAS e NOTAS->TEXTO.
        Remove as âncoras e referências cruzadas relacionadas com NOTAS->TEXTO.
        Também remover duplicidade de a[@name]
        """

        def _fix_a_href(self, xml):
            for a in xml.findall(".//a[@name]"):
                name = a.attrib.get("name")
                for a_href in xml.findall(".//a[@href='{}']".format(name)):
                    a_href.set("href", "#" + name)

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

        def _exclude(self, items_by_id):
            for _id, nodes in items_by_id.items():
                repeated = [n for n in nodes if n.attrib.get("name")]
                if len(repeated) > 1:
                    for n in repeated[1:]:
                        nodes.remove(n)
                        parent = n.getparent()
                        parent.remove(n)
                if len(nodes) >= 2 and nodes[0].attrib.get("name"):
                    for n in nodes:
                        _remove_element_or_comment(n)
                if len(nodes) == 1 and nodes[0].attrib.get("href"):
                    _remove_element_or_comment(nodes[0])

        def transform(self, data):
            raw, xml = data
            self._fix_a_href(xml)
            items_by_id = self._identify_order(xml)
            self._exclude(items_by_id)
            return data

    class ApplySuggestedConversionPipe(CustomPipe):
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
            self.super_obj.document.xmltree = xml
            for name, a_name_and_hrefs in self.super_obj.document.a_names.items():
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

    class APipe(CustomPipe):
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
                parent = node.getprevious()
                if parent is None:
                    parent = node.getparent()
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
                rid = gera_id(xref_name, self.super_obj.index_body)
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

    class ImgPipe(CustomPipe):
        def parser_node(self, node):
            node.tag = "graphic"
            src = node.attrib.pop("src")
            node.attrib.clear()
            node.set("{http://www.w3.org/1999/xlink}href", src)

        def transform(self, data):
            raw, xml = data
            _process(xml, "img", self.parser_node)
            return data

    class MoveFnPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            self._fix_fn_position(xml)
            return data

        def _fix_fn_position(self, xml):
            changed = True
            while changed:
                changed = False
                for tag in ["sup", "bold", "italic"]:
                    self._identify_nodes_to_move_out(xml, tag)
                    changed = self._move_fn_out(xml)

        def _identify_nodes_to_move_out(self, xml, style_tag):
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
            _remove_element_or_comment(fn)

        def _move_fn_out_and_forward(self, node):
            fn = node.find("fn")
            fn_copy = deepcopy(fn)
            node.addnext(fn_copy)
            node.remove(fn)

    class AddContentToFnPipe(plumber.Pipe):
        def transform(self, data):
            raw, xml = data
            for fn in xml.findall(".//fn"):
                self._move_fn_tail_into_fn(fn)
            return data

        def _move_fn_tail_into_fn(self, node):
            parent = node.getparent()
            node.text = node.tail
            node.tail = ""
            while True:
                _next = node.getnext()
                if _next is None:
                    break
                if _next.tag in ["fn", "p"]:
                    break
                node.append(deepcopy(_next))
                parent.remove(_next)

    class FnLabelAndPPipe(plumber.Pipe):
        def _create_label(self, node):
            children = node.getchildren()
            node_text = (node.text or "").strip()
            if node_text:
                self._create_label_from_node_text(node)
            elif children:
                self._create_label_from_node_first_child(node)

        def _create_label_from_node_text(self, node):
            node_text = (node.text or "").strip()
            if node_text:
                splitted = node_text.split()
                label_text = None
                if node_text[0].isalpha():
                    if len(splitted[0]) == 1 and node_text[0].lower() == node_text[0]:
                        label_text = splitted[0]
                else:
                    label_text = self._find_label_text(splitted[0])
                if label_text:
                    label = etree.Element("label")
                    label.text = label_text
                    node.insert(0, label)
                    label.tail = node.text.replace(label_text, "").lstrip()
                    node.text = ""

        def _find_label_text(self, text):
            label_text = []
            for c in text:
                if not c.isalpha():
                    label_text.append(c)
                else:
                    break
            return "".join(label_text)

        def _create_label_from_node_first_child(self, node):
            children = node.getchildren()
            if len(children) > 0:
                if children[0].tag == "p":
                    elem = children[0].find("*")
                    if elem is not None and elem.tag in ["sup", "bold"]:
                        children[0].tag = "label"
                elif children[0].tag in ["sup", "bold"]:
                    children_text = get_node_text(children[0])
                    if len(
                        children_text.split()
                    ) <= 3 and children_text != get_node_text(node):
                        label = etree.Element("label")
                        label_content = deepcopy(children[0])
                        label_content.tail = ""
                        label.append(label_content)
                        label.tail = children[0].tail
                        node.insert(0, label)
                        node.remove(children[0])

        def _create_p(self, node):
            children = node.getchildren()
            if len(children) == 0:
                # no label
                new_p = etree.Element("p")
                new_p.text = (node.text or "").strip()
                node.text = ""
                node.insert(0, new_p)
            else:
                first_text = node.text
                node.text = ""
                if children[0].tag == "label":
                    first_text = (children[0].tail or "").lstrip()
                    children[0].tail = ""
                    children = children[1:]
                self._create_p_elements(node, first_text, children)

        def _create_p_elements(self, node, first_text, children):
            elements = []
            remove_items = []
            for child in children:
                if child.tag == "p":
                    self._create_one_p(node, first_text, elements)
                    elements = []
                    first_text = child.tail
                    child.tail = ""
                    node.append(deepcopy(child))
                    remove_items.append(child)
                else:
                    elements.append(child)
            if len(remove_items) > 0:
                parent = remove_items[0].getparent()
                for removed in remove_items:
                    parent.remove(removed)
            if len(elements) > 0 or first_text:
                self._create_one_p(node, first_text, elements)

        def _create_one_p(self, node, first_text, elements):
            if len(elements) > 0 or (first_text or "").strip():
                new_p = etree.Element("p")
                node.append(new_p)
                new_p.text = (first_text or "").lstrip()
                remove_items = []
                for element in elements:
                    new_p.append(deepcopy(element))
                    remove_items.append(element)
                for removed in remove_items:
                    node.remove(removed)

        def transform(self, data):
            raw, xml = data
            for fn in xml.findall(".//fn"):
                self._create_label(fn)
                self._create_p(fn)
            return data

    class TargetPipe(plumber.Pipe):
        """
        Os elementos "a" podem ser convertidos a tabelas, figuras, fórmulas,
        anexos, notas de rodapé etc. Por padrão, até este ponto, todos os
        elementos não identificados como tabelas, figuras, fórmulas,
        anexos, são identificados como "fn" (notas de rodapé). No entanto,
        podem ser apenas "target"
        """

        def transform(self, data):
            raw, xml = data
            for fn in xml.findall(".//fn"):
                _is_target = self._is_target(fn)
                if _is_target:
                    if get_node_text(fn):
                        target = etree.Element("target")
                        for k, v in fn.attrib.items():
                            target.set(k, v)
                        fn.addprevious(target)
                        fn.tag = "REMOVE_TAG"
                        etree.strip_tags(fn, "REMOVE_TAG")
                    else:
                        fn.tag = "target"
            return data

        def _is_target(self, node):
            if not get_node_text(node):
                return True
            previous = fn.getprevious()
            if previous is not None:
                return True
            parent = fn.getparent()
            if (parent.text or "").strip():
                return True
            root = node.getroot()
            found = [e.findall(".//fn") for e in root.findall("*")]
            fn_items = []
            blank = []
            for item in found[::-1]:
                if len(item) == 1:
                    fn_items.append(item)
                elif len(item) > 1:
                    fn_items.extend(item)
                elif len(fn_items) > 0 and len(blank) > 0:
                    break
                else:
                    blank.append(item)
            logger.info(found)
            logger.info(fn_items)
            if fn not in fn_items:
                return True


class AssetInHTMLPage:
    def __init__(self, local=None, remote=None, content=None):
        self.local = local
        self.remote = remote

    def convert(self, _pipeline):
        # xmltree = self.xml_tree
        # if xmltree is None:
        xmltree = self._convert(_pipeline)
        if xmltree is not None:
            with open(self.local + ".xml", "wb") as fp:
                fp.write(self.ent2char(etree.tostring(xmltree)))
        else:
            logger.info(self.local)
        return xmltree

    @property
    def xml_tree(self):
        xml_filepath = self.local + ".xml"
        if os.path.isfile(xml_filepath):
            with open(xml_filepath, "rb") as fp:
                content = fp.read()
                if content:
                    return etree.fromstring(content)

    def _convert(self, _pipeline):
        html_tree = self.html_tree
        if html_tree is not None:
            body_tree = html_tree.find(".//body")

            if body_tree is not None:
                body_tree.set("xmlns:xlink", "http://www.w3.org/1999/xlink")
                __, xml_tree = _pipeline.deploy(body_tree)
                return xml_tree

    @property
    def html_tree(self):
        html_content = self.get_html_content()
        if html_content:
            return etree.fromstring(html_content, parser=etree.HTMLParser())

    def ent2char(self, data):
        return html.unescape(data.decode("utf-8")).encode("utf-8").strip()

    def download(self):
        with request.urlopen(self.remote, timeout=30) as fp:
            return fp.read()

    def get_html_content(self):
        if self.local and os.path.isfile(self.local):
            with open(self.local, "rb") as fp:
                return fp.read()
        try:
            content = self.download()
        except (error.HTTPError, error.URLError) as e:
            logger.exception(e)
        else:
            dirname = os.path.dirname(self.local)
            if not os.path.isdir(dirname):
                os.makedirs(dirname)
            with open(self.local, "wb") as fp:
                fp.write(self.ent2char(content))
            return content


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


def old_gera_id(_string, index_body):
    rid = _string
    number_item = re.search(r"([a-zA-Z]{1,3})(\d+)([a-zA-Z0-9]+)?", _string)
    if number_item:
        name_item, number_item, sufix_item = number_item.groups("")
        rid = name_item + number_item + sufix_item

    if not rid[0].isalpha():
        rid = "replace_by_reftype" + rid

    if index_body == 1:
        return rid.lower()

    ref_id = "%s-body%s" % (rid, index_body)
    return ref_id.lower()


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
