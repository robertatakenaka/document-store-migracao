import argparse
import os
import shutil
import json
import logging
from datetime import datetime
import time

MISSING_FILES = {}
OTHERS = []

STATUS = {}
STATUS['not_converted'] = {'label': 'Não convertidos', 'items': []}
STATUS['not_packed'] = {'label': 'Não empacotados', 'items': []}
STATUS['pack_incomplete'] = {'label': 'Incompleto', 'items': []}
STATUS['not_imported'] = {'label': 'Não importados', 'items': []}
STATUS['not_linked'] = {'label': 'Não linkados', 'items': []}


def logfileinfo(file_path):
    try:
        created_at = "[%s, criado em: %s]" % (file_path, time.ctime(os.path.getctime(file_path)))
        with open(file_path, "r") as fp:
            content = fp.read().splitlines()
    except FileNotFoundError:
        created_at = "unknown creation date"
        content = [f"Arquivo {file_path} não encontrado"]
    return created_at, content


def trace(year):
    pids_file = f"pids_html/{year}_html.txt"
    year_path = f"html_migracao/{year}"
    logs_path = os.path.join(year_path, "logs")
    reports_path = os.path.join(year_path, "reports")
    source_path = os.path.join(year_path, "source")
    conversion_path = os.path.join(year_path, "conversion")
    packaged_path = os.path.join(year_path, "packaged")
    link2kernel_path = os.path.join(year_path, "link2kernel")

    files_info = []
    for dirname in (link2kernel_path, logs_path):
        for filename in os.listdir(dirname):
            file_path = os.path.join(dirname, filename)
            stat = os.stat(file_path)
            files_info.append(
                (
                    datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    file_path
                )
            )
    files_info = sorted(files_info)

    pids_to_extract = get_pids(pids_file)
    pids_in_conversion = get_pids_in_conversion(conversion_path)
    pids_in_packaged = get_pids_in_packaged(packaged_path)

    result = ["", str(year), "----"]

    result += ['']
    result += [
        "%s\t%s" % (d, filename)
        for d, filename in files_info
    ]
    result += ['']
    result += inform_1(
                len(pids_to_extract), len(pids_in_conversion),
                len(os.listdir(conversion_path)), len(pids_in_packaged))

    created_at, rows = logfileinfo(f"{reports_path}/report.txt")
    result += rows

    # pack
    pack_log = os.path.join(logs_path, "pack.log")
    missing_img, missing_pdf, other, _result = packing_errors(pack_log)
    result += _result

    # import
    created_at, imported = read_import_json(link2kernel_path)

    errors_grouped_by_pid_v2 = group_packing_errors_by_pid_v2(
        missing_img, missing_pdf, other)
    MISSING_FILES.update(errors_grouped_by_pid_v2)

    link_log = os.path.join(logs_path, "link.log")
    pid_v3_not_linked, _result = linking_errors(link_log)
    result += _result

    not_linked = []
    for pid_v2, data in imported.items():
        if data["pid_v3"] in pid_v3_not_linked:
            not_linked.append(pid_v2)

    not_converted = sorted(pids_to_extract - pids_in_conversion)
    not_packed = sorted(pids_to_extract - pids_in_packaged)

    not_imported = sorted(pids_to_extract - set(imported.keys()))

    values = (
        not_converted, not_packed, sorted(errors_grouped_by_pid_v2.keys()),
        not_imported, sorted(not_linked))
    for key, items in zip(STATUS.keys(), values):
        STATUS[key]['items'] += items
        result += lista_pid(STATUS[key]['label'], items)

    OTHERS.extend(other)

    return result


def read_import_json(link2kernel_path):
    """
    {"pid_v3": "MXyDP7F69jfTyZRKHgYKq4g", "eissn": "1678-992X",
     "issn": "1678-992X", "acron": "sa", "pid": "S0103-90161999000100009",
     "year": "1999", "volume": "56", "number": "1", "order": "00009"}
    """
    imported = {}
    _created_at = []
    for filename in sorted(os.listdir(link2kernel_path)):
        name, ext = os.path.splitext(filename)
        if ext == ".json" and name.startswith("imported."):
            file_path = os.path.join(link2kernel_path, filename)
            
            created_at, rows = logfileinfo(file_path)
            _created_at += created_at
            for item in rows:
                data = json.loads(item)
                imported[data["pid"]] = data
    return _created_at, imported


def lista_pid(label, items):
    rows = [
        "\n____",
        "%s: %i" % (label, len(items)),
    ]
    rows.extend(sorted(items))
    return rows


def display_errors(label, items):
    return [
        "\n",
        "----####----",
        "%s: %i" % (label, len(items)),
        "\n".join(items),
        "----####----",
        "",
    ]


def inform_1(q_pids_to_extract, q_pids_in_conversion, q_files_in_conversion, q_pids_in_packaged):
    a = (q_pids_in_conversion, q_files_in_conversion, q_pids_in_packaged)
    return [
        "Quantidade total de documentos a migrar (pids)\t{}".format(q_pids_to_extract),
        "Quantidade total de documentos migrados(*.xml)\t{} (conversion) | {} arquivos | {} (packaged)".format(q_pids_in_conversion, q_files_in_conversion, q_pids_in_packaged),
    ]


def inform_2(values):
    messages = [
        "Quantidade de artigos disponibilizados no site\t{}",
        "Quantidade em bytes de dados\t{}",
        "Quantidade de artigos com citações atualizadas\t{}",
        "Quantidade de artigos que falharam e foram re importados\t{}",
        "Quantidade de artigos não relacionados durante o link com os fascículos\t{}",
        "Tempo total para extrair os HTML\t{}",
        "Tempo total para converter os documentos\t{}",
        "Tempo total para mixedcitations\t{}",
        "Tempo total para empacotar os artigos\t{}",
        "Diretório com todos os dados da migração\t{}",
    ]
    for m, value in zip(messages, values + [''] * (len(messages) - len(values))):
        print(m.format(value))


def get_pids(pids_file):
    with open(pids_file, "r") as fp:
        pids = fp.read().splitlines()
    return set([pid for pid in pids if pid])


def get_pids_in_conversion(conversion_path):
    return set(f.split(".")[0]
               for f in os.listdir(conversion_path) if f.endswith(".xml"))


def get_pids_in_packaged(packaged_path):
    return set(f.split("_")[0]
               for f in os.listdir(packaged_path))


def get_errors_in_logfile(log_file_path):
    with open(log_file_path, "r") as fp:
        lines = fp.read().splitlines()
    return [
        line
        for line in lines
        if "ERROR" in line
    ]


def packing_errors(pack_log):
    """
    {'pid': 'S1517-45222005000200014', 'pkg_name': '1807-0337-soc-14-376-437',
     'old_path': '/img/revistas/soc/n14/a14qdr01.gif',
     'new_fname': '1807-0337-soc-14-376-437-ga14qdr01',
     'msg': 'Not found /img/revistas/soc/n14/a14qdr01.gif'}
    """
    try:
        errors = get_errors_in_logfile(pack_log)
    except FileNotFoundError:
        errors = [f"Arquivo não encontrado {pack_log}"]
    missing_img = [
        msg
        for msg in errors
        if "pid" in msg and "img" in msg
    ]
    missing_img_alt = [
        msg
        for msg in errors
        if "pid" in msg and not "pdf" in msg
    ]
    missing_img.extend(missing_img_alt)
    missing_img = set(missing_img)
    missing_pdf = [
        msg
        for msg in errors
        if "pid" in msg and "pdf" in msg
    ]

    other = set(errors) - set(missing_img) - set(missing_pdf)
    result = []
    for error_type, msg in zip(
            ("missing img", "missing pdf", "others"),
            (missing_img, missing_pdf, other)):
        result += [
            "%s: %i" % (error_type, len(msg))
        ]

    return missing_img, missing_pdf, other, result


def group_packing_errors_by_pid_v2(missing_img, missing_pdf, other):
    """
    {'pid': 'S1517-45222005000200014', 'pkg_name': '1807-0337-soc-14-376-437',
     'old_path': '/img/revistas/soc/n14/a14qdr01.gif',
     'new_fname': '1807-0337-soc-14-376-437-ga14qdr01',
     'msg': 'Not found /img/revistas/soc/n14/a14qdr01.gif'}
    """
    failures = {}
    for items in (missing_img, missing_pdf, other):
        for item in items:
            splitted = item.split("{")
            try:
                msg = "{" + splitted[1]
            except IndexError:
                logging.info("other???: %s", item)
            else:
                try:
                    msg = msg.replace("'", '"')
                    data = json.loads(msg)
                except json.decoder.JSONDecodeError as e:
                    logging.exception(
                        "Unable to execute `json.loads()` with `%s`. %s" % (msg, e))

                else:
                    pid_v2 = data["pid"]
                    failures[pid_v2] = failures.get(pid_v2) or {
                        "id": pid_v2, "items": []}
                    new_data = {"date": splitted[0]}
                    new_data.update(data)
                    failures[pid_v2]["items"].append(
                        new_data
                    )
    return failures


def linking_errors(link_log):
    try:
        errors = get_errors_in_logfile(link_log)
    except FileNotFoundError:
        errors = [f"Arquivo não encontrado {link_log}"]

    result = display_errors("link errors", errors)

    pid_v3_list = [
        item.strip()[-25:][1:-1]
        for item in errors
        if 'No ISSN in document' in item
    ]
    return pid_v3_list, result


def save_items(file_path, items):
    with open(file_path, "w") as fp:
        fp.write("\n".join(items))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("begin", type=int)
    parser.add_argument("end", type=int)
    parser.add_argument("report_path")
    parser.add_argument("--badfiles", action="store_true")

    args = parser.parse_args()

    y_begin, y_end = args.begin, args.end
    if y_begin > y_end:
        y_begin, y_end = y_end, y_begin

    report_path = os.path.join(
        args.report_path, datetime.now().isoformat()[:10])
    if not os.path.isdir(report_path):
        os.makedirs(report_path)

    result = []
    for year in sorted(range(y_begin, y_end+1), reverse=True):
        print(year)
        result += trace(year)

    y_range = f"{y_begin}-{y_end}"
    save_items(os.path.join(report_path, f"{y_range}.txt"), result)

    if args.badfiles:
        with open("pids_html/0000_html.txt", "w") as fp:
            fp.write("\n".join(NOT_CONVERTED))

        for pid in NOT_CONVERTED:
            year = pid[10:14]
            for ext in ("json", "xml"):
                src = f"html_migracao/{year}/source/{pid}.{ext}"
                dst = f"html_migracao/0000/source/{pid}.{ext}"
                shutil.copy(src, dst)

    for i, item in enumerate(STATUS.items()):
        key, data = item
        file_path = os.path.join(report_path, f"{y_range}-{i}-{key}.txt")
        save_items(file_path, sorted(data["items"]))

    save_items(os.path.join(report_path, f"{y_range}-other_errors.txt"), OTHERS)

    with open(os.path.join(report_path, f"{y_range}-missing_files.json"), "w") as fp:
        fp.write(json.dumps(list(MISSING_FILES.values())))

    print(report_path)
    os.system(f"ls -ltr {report_path}")


if __name__ == "__main__":
    main()
