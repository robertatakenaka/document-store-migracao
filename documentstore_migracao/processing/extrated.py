import logging
import os
import shutil

from documentstore_migracao.export import journal, article
from documentstore_migracao.utils import files
from documentstore_migracao import config


logger = logging.getLogger(__name__)


def extrated_journal_data(obj_journal):
    count = 0
    logger.info("\t coletando dados do periodico '%s'", obj_journal.title)

    for path in [config.get("DOWNLOADS_PATH"), config.get("SOURCE_PATH")]:
        if path and not os.path.isdir(path):
            os.makedirs(path)

    articles = article.get_articles(obj_journal.scielo_issn)
    for _article in articles:
        xml_article = article.get_not_xml_article(_article)
        if xml_article:
            count += 1

            file_path = os.path.join(
                config.get("DOWNLOADS_PATH"), "%s.xml" % _article.data["code"]
            )

            logger.info("\t Salvando arquivo '%s'", file_path)
            files.write_file(file_path, xml_article)
            shutil.copy(file_path, config.get("SOURCE_PATH"))

    logger.info("\t Total de %s artigos", count)


def extrated_selected_journal(issn):

    logger.info("Iniciando extração do journal %s" % issn)

    obj_journal = journal.ext_journal(issn)
    extrated_journal_data(obj_journal)


def extrated_all_data():

    logger.info("Iniciando extração")
    list_journais = journal.get_journals()
    for obj_journal in list_journais:

        extrated_journal_data(obj_journal)
