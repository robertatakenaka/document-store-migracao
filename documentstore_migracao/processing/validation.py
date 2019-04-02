import os
import logging
import shutil

from packtools import XMLValidator

from documentstore_migracao.utils import files, dicts
from documentstore_migracao import config

logger = logging.getLogger(__name__)


def validator_article_xml(file_xml_path, print_error=True):

    logger.info(file_xml_path)
    xmlvalidator = XMLValidator.parse(file_xml_path)
    is_valid, errors = xmlvalidator.validate()

    result = {}
    if not is_valid:
        for error in errors:
            if print_error:
                logger.error("%s - %s - %s", error.level, error.line, error.message)

            message = error.message[:80]
            data = {"count": 1, "files": (error.line, file_xml_path)}
            dicts.merge(result, message, data)

    return result


def validator_article_ALLxml(delete_source=False, delete_conversion=False):
    logger.info("Iniciando Validação dos xmls")
    list_files_xmls = files.list_dir(config.get("CONVERSION_PATH"))

    success_path = config.get("VALID_XML_PATH")
    failure_path = config.get("INVALID_XML_PATH")
    for path in [success_path, failure_path]:
        if path and not os.path.isdir(path):
            os.makedirs(path)

    result = {}
    for file_xml in list_files_xmls:
        converted_file = os.path.join(config.get("CONVERSION_PATH"), file_xml)
        try:
            errors = validator_article_xml(converted_file, False)
            for k_error, v_error in errors.items():
                dicts.merge(result, k_error, v_error)
            try:
                func = shutil.move if delete_conversion else shutil.copyfile
                if failure_path and errors:
                    func(converted_file, os.path.join(failure_path, file_xml))
                elif success_path and not errors:
                    func(converted_file, os.path.join(success_path, file_xml))
                if delete_source:
                    source_file = os.path.join(
                        config.get("SOURCE_PATH"), file_xml)
                    os.unlink(source_file)
            except Exception as ex_files:
                logger.exception(ex_files)
        except Exception as ex:
            logger.exception(ex)
            raise

    analase = sorted(result.items(), key=lambda x: x[1]["count"], reverse=True)
    for k_result, v_result in analase:
        logger.error("%s - %s", k_result, v_result["count"])
        # if "boxed-text" in k_result:
        #     for line, file in dicts.group(v_result["files"], 2):
        #         logger.error("\t %s - %s", line, file)
