import os
import shutil
import logging

from requests.compat import urljoin
from requests.exceptions import HTTPError
from lxml import etree
from documentstore_migracao.utils import (
    files,
    request,
    xml,
)
from documentstore_migracao import (
    config,
)
from documentstore_migracao.export.sps_package import SPS_Package


logger = logging.getLogger(__name__)


def packing_article_xml(file_xml_path):
    original_filename, ign = files.extract_filename_ext_by_path(file_xml_path)

    obj_xml = xml.file2objXML(file_xml_path)

    sps_package = SPS_Package(obj_xml, original_filename)

    pkg_path = os.path.join(config.get("SPS_PKG_PATH"), original_filename)
    files.make_empty_dir(pkg_path)
    bad_pkg_path = os.path.join(
        config.get("INCOMPLETE_SPS_PKG_PATH"), original_filename
    )

    asset_replacements = list(set(sps_package.replace_assets_names()))
    logger.info(
        "%s possui %s ativos digitais", file_xml_path, len(asset_replacements))

    success = packing_assets(
        asset_replacements, pkg_path, bad_pkg_path, sps_package.package_name)
    if not success:
        pkg_path = bad_pkg_path

    xml.objXML2file(
        os.path.join(pkg_path, "%s.xml" % (sps_package.package_name)),
        obj_xml
    )


def packing_article_ALLxml():

    logger.info("Empacotando os documentos XML")
    list_files_xmls = files.xml_files_list(config.get("VALID_XML_PATH"))
    for file_xml in list_files_xmls:

        try:
            packing_article_xml(
                os.path.join(config.get("VALID_XML_PATH"), file_xml)
            )

        except (PermissionError, OSError, etree.Error) as ex:
            logger.error('Falha no empacotamento de %s' % file_xml)
            logger.exception(ex)


def download_asset(old_path, new_fname, dest_path):
    """Returns msg, if error"""
    location = urljoin(config.get("STATIC_URL_FILE"), old_path)
    try:
        request_file = request.get(
            location, timeout=int(config.get("TIMEOUT") or 10))
    except HTTPError as e:
        try:
            msg = str(e)
        except TypeError:
            msg = 'Unknown error'
        logger.error(e)
        return msg
    else:
        filename_m, ext_m = files.extract_filename_ext_by_path(old_path)
        files.write_file_binary(
            os.path.join(dest_path, "%s%s" % (new_fname, ext_m)),
            request_file.content,
        )


def packing_assets(asset_replacements, pkg_path, bad_pkg_path, pkg_name):
    errors = []
    for old_path, new_fname in asset_replacements:
        error = download_asset(old_path, new_fname, pkg_path)
        if error:
            errors.append((old_path, new_fname, error))

    if len(errors) > 0:
        # move pacote incompleto para a pasta de pacotes incompletos
        if bad_pkg_path != pkg_path:
            files.make_empty_dir(bad_pkg_path)
            for item in os.listdir(pkg_path):
                shutil.move(os.path.join(pkg_path, item), bad_pkg_path)
        # gera relatorio de erros
        errors_filename = os.path.join(bad_pkg_path, "%s.err" % pkg_name)
        if len(errors) > 0:
            error_messages = '\n'.join(['%s %s %s' % _err for _err in errors])
            files.write_file(errors_filename, error_messages)
    return len(errors) == 0
