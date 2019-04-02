import os
from packtools.catalogs import XML_CATALOG

BASE_PATH = os.path.dirname(os.path.dirname(__file__))

"""
DOWNLOADS_PATH:
    arquivos baixados do ArticleMeta, não necessariamente tem que ser a
    origem dos XML
SOURCE_PATH:
    arquivos XML originais como os provenientes do AM
CONVERSION_PATH:
    arquivos XML gerados a partir dos arquivos que estão em SOURCE_PATH,
    cujo body HTML foi convertido para o body do SPS
VALID_XML_PATH:
    arquivos XML válidos de acordo com o SPS
INVALID_XML_PATH:
    arquivos XML inválidos de acordo com o SPS
REMOVE_FROM_SOURCE_PATH_THE_VALID_XML:
    TRUE para remover do SOURCE_PATH os arquivos que foram bem sucedidos
    na geração do XML SPS
REMOVE_FROM_CONVERSION_PATH_THE_VALID_XML:
    TRUE para remover do CONVERSION_PATH os arquivos que foram bem sucedidos
    na geração do XML SPS
"""

_default = dict(
    SCIELO_COLLECTION="spa",
    AM_URL_API="http://articlemeta.scielo.org/api/v1",
    DOWNLOADS_PATH=os.path.join(BASE_PATH, "xml/downloads"),
    SOURCE_PATH=os.path.join(BASE_PATH, "xml/source"),
    SUCCESS_PROCESSING_PATH=os.path.join(BASE_PATH, "xml/success"),
    CONVERSION_PATH=os.path.join(BASE_PATH, "xml/conversion"),
    VALID_XML_PATH=os.path.join(BASE_PATH, "xml/xml_valid"),
    INVALID_XML_PATH=os.path.join(BASE_PATH, "xml/xml_invalid"),
    GENERATOR_PATH=os.path.join(BASE_PATH, "xml/html"),
    LOGGER_PATH=os.path.join(BASE_PATH, ""),
    REMOVE_FROM_SOURCE_PATH_THE_VALID_XML="TRUE",
    REMOVE_FROM_CONVERSION_PATH_THE_VALID_XML="TRUE",
)

INITIAL_PATH = [
    _default["LOGGER_PATH"],
    _default["DOWNLOADS_PATH"],
    _default["SOURCE_PATH"],
    _default["VALID_XML_PATH"],
    _default["INVALID_XML_PATH"],
    _default["SUCCESS_PROCESSING_PATH"],
    _default["CONVERSION_PATH"],
    _default["GENERATOR_PATH"],
]

DOC_TYPE_XML = """<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD v1.1 20151215//EN" "JATS-journalpublishing1.dtd">"""

os.environ["XML_CATALOG_FILES"] = XML_CATALOG


def get(config):
    return os.environ.get(config, _default.get(config, ""))
