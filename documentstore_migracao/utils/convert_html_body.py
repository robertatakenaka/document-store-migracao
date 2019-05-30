import logging
import plumber
import html
import re
import os
from lxml import etree
from copy import deepcopy
from documentstore_migracao.utils import files
from documentstore_migracao.utils import xml as utils_xml
from documentstore_migracao import config


logger = logging.getLogger(__name__)


def parse_element_a(a_href_text, _id_name):

    if _id_name[0].isdigit():
        return "fn", "fn"

    for item in ["titulo", "home", "tx", "top"]:
        if _id_name.startswith(item):
            return

    for item in ["not", "_ftnref"]:
        if _id_name.startswith(item) and _id_name[len(item):].isdigit():
            return

    if _id_name.startswith("back"):
        number = _id_name[4:]
        if not number.isdigit():
            return

    if _id_name.startswith("nt"):
        return "fn", "fn"

    if _id_name and not _id_name[0].isalpha() and not _id_name[0].isdigit():
        return "symbol", "fn"

    if _id_name.startswith("fn"):
        return "fn", "fn"

    if a_href_text:
        if a_href_text.startswith("equ") or a_href_text.startswith("ecu"):
            return "disp-formula", "disp-formula"
        if a_href_text.startswith("form"):
            return "disp-formula", "disp-formula"
        if a_href_text[0] == "t":
            return "table-wrap", "table"
        if a_href_text[0] in "gfcq":
            return "fig", "fig"

    if _id_name[0] in "gcq":
        return "fig", "fig"
    if _id_name[0] in "gfcq":
        return "fig", "fig"

    return "fn", "fn"


def parse_asset_path(src_or_href):
    ID_AND_REF = (
        ("for", "disp-formula"),
        ("eq", "disp-formula"),
        ("t", "table-wrap"),
        ("f", "fig"),
        ("q", "fig"),
        ("c", "fig")
    )

    filename, __ = files.extract_filename_ext_by_path(src_or_href)
    for prefix, elem_name in ID_AND_REF:
        if prefix in filename:
            parts = filename.split(prefix)
            ref_type = elem_name
            if ref_type == "table-wrap":
                ref_type = "table"
            return elem_name, ref_type, prefix + parts[-1]


def get_node_text(node):
    node_text = " ".join(node.itertext())
    node_text = " ".join([item for item in node_text.split() if item])
    return node_text


def _remove_element_or_comment(node, remove_inner=False):
    parent = node.getparent()
    if parent is not None:
        removed = node.tag
        try:
            node.tag = "REMOVE_NODE"
        except AttributeError:
            comment = True
            node_text = ""
        else:
            comment = False
            node_text = node.text

        if node.tail:
            text = (node_text or "").strip() + node.tail
            previous = node.getprevious()
            if previous is not None:
                if not previous.tail:
                    previous.tail = ""
                previous.tail += text
            else:
                if not parent.text:
                    parent.text = ""
                parent.text += text

        if comment or remove_inner:
            parent.remove(node)
            return removed

        text = get_node_text(node)
        if text.strip() or len(node.getchildren()) > 0:
            etree.strip_tags(parent, "REMOVE_NODE")
        else:
            parent.remove(node)

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


def wrap_content_node(node, elem_wrap="p"):

    _node = deepcopy(node)
    p = etree.Element(elem_wrap)
    if _node.text:
        p.text = _node.text
    if _node.tail:
        p.tail = _node.tail

    _node.text = None
    _node.tail = None
    _node.append(p)
    node.getparent().replace(node, _node)


def gera_id(_string, index_body):

    number_item = re.search(r"([a-zA-Z]{1,3})(\d+)([a-zA-Z0-9]+)?", _string)
    if number_item:
        name_item, number_item, sufix_item = number_item.groups("")
        rid = name_item + number_item + sufix_item
    else:
        rid = _string

    if index_body == 1:
        return rid.lower()

    ref_id = "%s-body%s" % (rid, index_body)
    return ref_id.lower()


class CustomPipe(plumber.Pipe):
    def __init__(self, super_obj=None, *args, **kwargs):

        self.super_obj = super_obj
        super(CustomPipe, self).__init__(*args, **kwargs)


class HTML2SPSPipeline(object):
    def __init__(self, pid, index_body=1):
        self.pid = pid
        self.index_body = index_body
        self._ppl = plumber.Pipeline(
            self.SetupPipe(super_obj=self),
            self.SaveRawBodyPipe(super_obj=self),
            self.DeprecatedHTMLTagsPipe(),
            self.RemoveImgSetaPipe(),
            self.RemoveDuplicatedIdPipe(),
            self.RemoveExcedingStyleTagsPipe(),
            self.RemoveEmptyPipe(),
            self.RemoveStyleAttributesPipe(),
            self.RemoveCommentPipe(),
            self.HTMLEscapingPipe(),
            self.BRPipe(),
            self.PPipe(),
            self.DivPipe(),
            self.LiPipe(),
            self.OlPipe(),
            self.UlPipe(),
            self.IPipe(),
            self.EmPipe(),
            self.UPipe(),
            self.BPipe(),
            self.StrongPipe(),
            self.AnchorAndInternalLinkPipe(super_obj=self),
            self.AssetsPipe(super_obj=self),
            self.APipe(super_obj=self),
            self.ImgPipe(super_obj=self),
            # self.RemoveTablePipe(),
            self.TdCleanPipe(),
            self.TableCleanPipe(),
            self.BlockquotePipe(),
            self.HrPipe(),
            self.TagsHPipe(),
            self.DispQuotePipe(),
            self.GraphicChildrenPipe(),
            self.RemovePWhichIsParentOfPPipe(),
            self.RemoveRefIdPipe(),
            self.SanitizationPipe(),
        )

    class SetupPipe(CustomPipe):
        def transform(self, data):
            xml = utils_xml.str2objXML(data)
            return data, xml

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

    class RemoveExcedingStyleTagsPipe(plumber.Pipe):
        TAGS = ("b", "i", "em", "strong", "u")

        def transform(self, data):
            raw, xml = data
            for tag in self.TAGS:
                for node in xml.findall(".//" + tag):
                    text = "".join(node.itertext()).strip()
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

        def replace_CHANGE_BR_by_close_p_open_p(self, xml):
            _xml = etree.tostring(xml)
            _xml = _xml.replace(b"<CHANGE_BR/>", b"</p><p>")
            return etree.fromstring(_xml)

        def transform(self, data):
            raw, xml = data
            changed = False
            nodes = xml.findall("*[br]")
            for node in nodes:
                if node.tag in self.ALLOWED_IN:
                    for br in node.findall("br"):
                        br.tag = "break"
                elif node.tag == "p":
                    for br in node.findall("br"):
                        br.tag = "CHANGE_BR"
                        changed = True

            etree.strip_tags(xml, "br")
            if changed:
                return data[0], self.replace_CHANGE_BR_by_close_p_open_p(xml)
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

    class AnchorAndInternalLinkPipe(CustomPipe):
        def _remove_a(self, a_name, a_href):
            _remove_element_or_comment(a_name, True)
            if a_href is not None:
                _remove_element_or_comment(a_href, True)

        def parser_node(self, node):
            _name = node.attrib.get("id", node.attrib.get("name"))
            node.set("name", _name)
            node.set("id", _name)

            _name = node.attrib.get("name")
            if not _name:
                return

            root = node.getroottree()
            a_href_items = root.findall('.//a[@href="#{}"]'.format(_name))
            a_href = None
            if len(a_href_items) > 0:
                a_href = a_href_items[0]

            a_href_text = ""
            if a_href is not None:
                a_href_text = get_node_text(a_href).lower()
            result = parse_element_a(a_href_text, _name)
            if result is None:
                self._remove_a(node, a_href)
                return

            new_tag, reftype = result
            new_id = gera_id(_name, self.super_obj.index_body)
            if new_id[0].isdigit():
                new_id = new_tag + new_id

            node.set("id", new_id)
            node.attrib.pop("name")
            if new_tag == "symbol":
                node.set("symbol", _name)
                new_tag = "fn"

            if new_tag == "fn":
                p = etree.Element("p")
                p.text = (node.text or "") + (node.tail or "")
                node.tail = None
                node.text = None
                node.append(p)

            if new_tag:
                node.tag = new_tag

            for ahref in a_href_items:
                ahref.attrib.clear()
                ahref.set("ref-type", reftype)
                ahref.set("rid", new_id)
                ahref.tag = "xref"

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@id]", self.parser_node)
            _process(xml, "a[@name]", self.parser_node)
            return data

    class RemoveImgSetaPipe(plumber.Pipe):
        def parser_node(self, node):
            if "/seta." in node.find("img").attrib.get("src"):
                _remove_element_or_comment(node.find("img"))

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[img]", self.parser_node)
            return data

    class ImgPipe(CustomPipe):
        def parser_node(self, node):
            node.tag = "graphic"
            _attrib = deepcopy(node.attrib)
            src = _attrib.pop("src")

            node.attrib.clear()
            node.set("{http://www.w3.org/1999/xlink}href", src)

        def transform(self, data):
            raw, xml = data

            _process(xml, "img", self.parser_node)
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

                for child in node.iter():
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

        def transform(self, data):
            raw, xml = data
            self._tag_texts(xml)
            self._identify_extra_p_tags(xml)
            self._tag_text_in_body(xml)
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

    class AssetsPipe(CustomPipe):
        def transform(self, data):
            raw, xml = data

            convert = AssetsPipeline(self.super_obj.pid, self.super_obj.index_body)
            _, obj = convert.deploy(xml)
            return raw, obj

    class RemoveTempIdToAssetElementPipe(plumber.Pipe):
        """Ajuda a identificar table-wrap/@id, fig/@id"""

        def parser_node(self, node):
            if node.attrib.get("asset_new_id"):
                node.attrib.pop("asset_new_id", None)
                node.attrib.pop("asset_reftype", None)

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@href]", self.parser_node)
            _process(xml, "img", self.parser_node)
            return data

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)


class DataSanitizationPipeline(object):
    def __init__(self):
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.GraphicInExtLink(),
            self.TableinBody(),
            self.TableinP(),
            self.AddPinFN(),
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


class AssetsPipeline(object):
    def __init__(self, pid, index_body=1):
        self.pid = pid
        self.index_body = index_body
        self._ppl = plumber.Pipeline(
            self.SetupPipe(),
            self.AddAssetInfoToTablePipe(super_obj=self),
            self.AddAssetInfoToImgAndLinkElementsPipe(super_obj=self),
            self.CreateAssetElementsFromLinkElementsPipe(super_obj=self),
            self.CreateAssetElementsFromImgOrTableElementsPipe(super_obj=self),
        )

    def deploy(self, raw):
        transformed_data = self._ppl.run(raw, rewrap=True)
        return next(transformed_data)

    class SetupPipe(plumber.Pipe):
        def transform(self, data):

            new_obj = deepcopy(data)
            return data, new_obj

    class AddAssetInfoToImgAndLinkElementsPipe(CustomPipe):
        """Ajuda a identificar table-wrap/@id, fig/@id"""

        def set_asset_label(self, node, new_id):
            asset_label = get_node_text(node)
            if asset_label is None:
                a_href = [n.text
                          for n in node.getroottree().findall(
                            ".//xref[@rid='{}']".format(new_id))]
                a_href = [n for n in a_href if n]
                if len(a_href) > 0:
                    asset_label = a_href[0]
            if asset_label:
                node.set("asset_label", asset_label)

        def parser_node(self, node):
            src_or_href = node.attrib.get("href") or node.attrib.get("src")
            if "/img/revistas" in src_or_href:
                elem_name, ref_type, _id = parse_asset_path(src_or_href)
                new_id = gera_id(_id, self.super_obj.index_body)
                node.set("asset_new_id", new_id)
                node.set("asset_elem_name", elem_name)
                node.set("asset_reftype", ref_type)
                self.set_asset_label(node, new_id)

                asset_label = get_node_text(node)
                if asset_label is None:
                    a_href = [n.text
                              for n in node.getroottree().findall(
                                ".//xref[@rid='{}']".format(new_id))]
                    a_href = [n for n in a_href if n]
                    if len(a_href) > 0:
                        asset_label = a_href[0]
                if asset_label:
                    node.set("asset_label", asset_label)

        def transform(self, data):
            raw, xml = data
            _process(xml, "a[@href]", self.parser_node)
            _process(xml, "img[@src]", self.parser_node)
            return data

    class AddAssetInfoToTablePipe(CustomPipe):
        def parser_node(self, node):
            _id = node.attrib.get("id")
            if _id:
                new_id = gera_id(_id, self.super_obj.index_body)
                node.set("id", new_id)
                node.set("asset_new_id", new_id)
                node.set("asset_elem_name", "table-wrap")
                node.set("asset_label", "Tab")

        def transform(self, data):
            raw, xml = data
            _process(xml, "table[@id]", self.parser_node)
            return data

    class CreateAssetElementsFromLinkElementsPipe(CustomPipe):
        def _create_graphic(self, node_a):
            new_graphic = etree.Element("graphic")
            new_graphic.set(
                "{http://www.w3.org/1999/xlink}href", node_a.attrib.get("href")
            )
            return new_graphic

        def _create_asset_element(self, new_id, tag):
            new_elem = etree.Element(tag)
            new_elem.set("id", new_id)
            return new_elem

        def _create_asset_group(self, a_href):
            root = a_href.getroottree()
            asf_id = a_href.attrib.get("asset_new_id")
            elem_name = a_href.attrib.get("asset_elem_name")

            asset = root.find(".//{}[@id='{}']".format(elem_name, asf_id))
            if asset is None:
                asset = self._create_asset_element(asf_id, elem_name)
            if asset is not None:
                new_graphic = self._create_graphic(a_href)
                asset.append(new_graphic)

                label = etree.Element("label")
                label.text = get_node_text(a_href)
                asset.insert(0, label)

                new_p = etree.Element("p")
                new_p.append(asset)

                parent = a_href.getparent()
                parent.addnext(new_p)

                self._create_xref(a_href)

        def _create_xref(self, a_href):
            reftype = a_href.attrib.pop("asset_reftype")
            asf_id = a_href.attrib.pop("asset_new_id")

            a_href.tag = "xref"
            a_href.attrib.clear()
            a_href.set("rid", asf_id)
            a_href.set("ref-type", reftype)

        def _select_asset(self, xml):
            _ids = tuple(
                    set([node.attrib.get("asset_new_id")
                         for node in xml.findall(".//a[@asset_new_id]")]))
            for _id in _ids:
                node = xml.find(".//a[@asset_new_id='{}']".format(_id))
                node.set("selected", "true")

        def transform(self, data):
            raw, xml = data
            self._select_asset(xml)

            _process(xml, "a[@selected]", self._create_asset_group)
            _process(xml, "a[@asset_new_id]", self._create_xref)
            return data

    class CreateAssetElementsFromImgOrTableElementsPipe(CustomPipe):
        def _find_label_and_caption_in_node(self, node, previous_or_next):
            node_text = node.attrib.get("asset_label")
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
                        label.text = " ".join(text[0])
                        title_text = " ".join(text[1])
                        caption = None

                        if title_text:
                            caption = etree.Element("caption")
                            title = etree.Element("title")
                            title.text = " ".join(text[1])
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
                if _node.getparent() is not None:
                    _node.getparent().remove(_node)
                return label, caption

        def _get_asset_node(self, img_or_table, xref_id, xref_reftype):
            root = img_or_table.getroottree()
            asset = root.find('.//*[@xref_id="{}"]'.format(xref_id))
            if asset is None:
                new_asset = self._create_asset_element(xref_id, xref_reftype)
                new_p = etree.Element("p")
                new_p.append(new_asset)
                img_or_table.getparent().addprevious(new_p)
                asset = new_asset
            else:
                asset.attrib.pop("xref_id")
            return asset

        def _create_asset_element(self, xref_id, xref_reftype):
            tag = xref_reftype
            if tag == "table":
                tag = "table-wrap"
            new_elem = etree.Element(tag)
            new_elem.set("id", xref_id)
            return new_elem

        def parser_node(self, img_or_table):
            asset_new_id = img_or_table.attrib.get("asset_new_id")
            asset_reftype = img_or_table.attrib.get("asset_reftype")
            img_or_table_parent = img_or_table.getparent()
            label_and_caption = self._find_label_and_caption_around_node(img_or_table)
            asset = self._get_asset_node(img_or_table, asset_new_id, asset_reftype)
            if label_and_caption:
                if label_and_caption[1] is not None:
                    asset.insert(0, label_and_caption[1])
                asset.insert(0, label_and_caption[0])
            new_elem = deepcopy(img_or_table)
            for attr in ["asset_new_id", "asset_reftype", "asset_label"]:
                if attr in new_elem.attrib.keys():
                    new_elem.attrib.pop(attr)
            asset.append(new_elem)
            img_or_table_parent.remove(img_or_table)

        def transform(self, data):
            raw, xml = data
            _process(xml, "img[@asset_new_id]", self.parser_node)
            _process(xml, "table[@asset_new_id]", self.parser_node)
            return data
