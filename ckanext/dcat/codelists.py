import csv
import os
from pathlib import Path

CODELISTS_DIR = Path(__file__).resolve().parent / "codelists"

def load_codelists():
    codelist_paths = [os.path.join(CODELISTS_DIR, f) for f in os.listdir(CODELISTS_DIR) if f.endswith(".csv")]
    codelists_dfs = {}

    # Iterate over file paths and read in data
    for file_path in codelist_paths:
        with open(file_path, "r") as f:
            reader = csv.reader(f, delimiter=",")
            header = next(reader)
            df = []
            for row in reader:
                df.append(dict(zip(header, row)))
            file_name = os.path.splitext(os.path.basename(file_path))[0].lower()
            codelists_dfs[file_name] = df

    # INSPIRE Codelists
    MD_INSPIRE_REGISTER = []
    for df in codelists_dfs.values():
        MD_INSPIRE_REGISTER += df

    return {
        'MD_INSPIRE_REGISTER': MD_INSPIRE_REGISTER,
        'MD_FORMAT': codelists_dfs.get('file-type'),
        'MD_ES_THEMES': codelists_dfs.get('theme_es'),
        'MD_EU_THEMES': codelists_dfs.get('theme-dcat_ap'),
        'MD_EU_LANGUAGES': codelists_dfs.get('languages'),
        'MD_ES_FORMATS': codelists_dfs.get('format_es')
    }