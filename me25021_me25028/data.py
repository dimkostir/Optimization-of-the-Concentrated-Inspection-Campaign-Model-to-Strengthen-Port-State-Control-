import os
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv
import xml.etree.ElementTree as ET

try:
    import pandas as pd
except ImportError:
    pd = None

BASE_URL = "https://fileserver.parismou.org/api"


# ---------------------------
# ParisMoU Client (download)
# ---------------------------
class ParisMouClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.token: Optional[str] = None
        self.token_time: float = 0.0

    def _token_is_valid(self) -> bool:
        return self.token is not None and (time.time() - self.token_time) < 9 * 60

    def get_token(self) -> str:
        url = f"{BASE_URL}/{self.api_key}/getauthorizationtoken"
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("status", {}).get("code") != "success":
            raise RuntimeError(f"Token request failed: {data.get('status')} | body: {r.text[:400]}")
        token = data.get("access_token")
        if not token:
            raise RuntimeError("No access_token in response.")
        self.token = token
        self.token_time = time.time()
        return token

    def _ensure_token(self) -> str:
        if not self._token_is_valid():
            return self.get_token()
        assert self.token is not None
        return self.token

    def download_file(self, filename: str, out_dir: Path) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / filename
        if out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        token = self._ensure_token()
        url = f"{BASE_URL}/{token}/getfile/{filename}"
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return out_path


# ---------------------------
# ZIP/XML helpers
# ---------------------------
def unzip_to_folder(zip_path: Path, out_folder: Path) -> None:
    out_folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(out_folder)

def find_xml_files(folder: Path) -> List[Path]:
    return [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".xml"]

def norm_tag(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag

def attr_value_by_suffix(elem: ET.Element, suffix: str) -> Optional[str]:
    """
    Find attribute whose local-name (after namespace) endswith suffix.
    Example: {...}Value or Value.
    """
    for k, v in elem.attrib.items():
        lk = k.split("}", 1)[1] if "}" in k else k
        if lk.lower() == suffix.lower():
            vv = str(v).strip()
            if vv:
                return vv
    return None

def find_first_child(elem: ET.Element, tag_name: str) -> Optional[ET.Element]:
    for child in elem:
        if norm_tag(child.tag).lower() == tag_name.lower():
            return child
    return None


# ---------------------------
# Extractors for this schema
# ---------------------------
def extract_ship_type(inspection: ET.Element) -> Optional[str]:
    """
    In your schema, ShipType is an element with attribute Value.
    We locate:
      Inspection -> ShipParticulars -> ShipType (Value=...)
    """
    ship_part = find_first_child(inspection, "ShipParticulars")
    if ship_part is None:
        return None

    ship_type_el = find_first_child(ship_part, "ShipType")
    if ship_type_el is None:
        return None

    # Primary: attribute Value
    v = attr_value_by_suffix(ship_type_el, "Value")
    if v:
        return v

    # Fallback: direct text
    txt = (ship_type_el.text or "").strip()
    return txt or None


def extract_deficiency_code(def_el: ET.Element) -> Optional[str]:
    """
    Try to extract deficiency code robustly:
    1) Look for any attribute whose local name contains 'Code' or equals 'Value'
    2) Look for child tags containing 'Code'
    3) Look for child attribute 'Value'
    """
    # 1) Attributes
    for k, v in def_el.attrib.items():
        lk = k.split("}", 1)[1] if "}" in k else k
        lkl = lk.lower()
        if "code" in lkl or lkl == "value":
            vv = str(v).strip()
            if vv:
                return vv

    # 2) Child tags with code-like names
    for child in def_el.iter():
        t = norm_tag(child.tag).lower()
        if "code" in t:
            # maybe text
            txt = (child.text or "").strip()
            if txt:
                return txt
            # or attribute Value
            vv = attr_value_by_suffix(child, "Value")
            if vv:
                return vv

    # 3) fallback: attribute Value on deficiency itself
    vv = attr_value_by_suffix(def_el, "Value")
    if vv:
        return vv

    return None


def parse_xml_build_counts(xml_path: Path) -> Tuple[Dict[Tuple[str, str], int], Counter, Counter, int, int]:
    """
    Returns:
      pair_counts, shiptype_counter, defcode_counter, inspections_total, inspections_with_def
    """
    pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
    shiptype_counter = Counter()
    defcode_counter = Counter()

    inspections_total = 0
    inspections_with_def = 0

    for event, elem in ET.iterparse(str(xml_path), events=("end",)):
        if norm_tag(elem.tag).lower() != "inspection":
            continue

        inspections_total += 1
        ship_type = extract_ship_type(elem)
        if ship_type:
            shiptype_counter[ship_type] += 1

        # Find Deficiencies container (if any) and Deficiency nodes
        defs_container = find_first_child(elem, "Deficiencies")
        def_nodes = []

        if defs_container is not None:
            for child in defs_container:
                if norm_tag(child.tag).lower() == "deficiency":
                    def_nodes.append(child)
        else:
            # sometimes deficiencies are directly under Inspection
            for child in elem:
                if norm_tag(child.tag).lower() == "deficiency":
                    def_nodes.append(child)

        if def_nodes:
            inspections_with_def += 1

        for d in def_nodes:
            code = extract_deficiency_code(d)
            if not code:
                continue
            defcode_counter[code] += 1
            if ship_type:
                pair_counts[(ship_type, code)] += 1

        elem.clear()

    return pair_counts, shiptype_counter, defcode_counter, inspections_total, inspections_with_def


def save_sparse_csv(pair_counts: Dict[Tuple[str, str], int], out_csv: Path) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8") as f:
        f.write("ship_type,deficiency_code,count\n")
        for (st, dc), c in sorted(pair_counts.items(), key=lambda x: (-x[1], x[0][0], x[0][1])):
            f.write(f"{st.replace(',', ' ')},{dc.replace(',', ' ')},{c}\n")


def save_pivot_csv(pair_counts: Dict[Tuple[str, str], int], out_csv: Path) -> None:
    if pd is None:
        print("\n[pandas not installed] Skipping pivot export. Install with: pip install pandas")
        return
    rows = [{"ship_type": st, "deficiency_code": dc, "count": c} for (st, dc), c in pair_counts.items()]
    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNo data to pivot.")
        return
    pivot = df.pivot_table(index="ship_type", columns="deficiency_code", values="count", aggfunc="sum", fill_value=0)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    pivot.to_csv(out_csv)
    print("Saved pivot matrix CSV to:", out_csv)


def main():
    load_dotenv()
    api_key = os.getenv("PARISMOU_API_KEY")
    if not api_key:
        raise RuntimeError("Missing PARISMOU_API_KEY in .env")

    raw_dir = Path("data_raw")
    extracted_dir = Path("data_extracted")
    outputs_dir = Path("outputs")

    client = ParisMouClient(api_key)

    target_files = [
        "THETIS_PSC_2018_inspections.zip",
        "THETIS_PSC_2019_inspections.zip",
        "THETIS_PSC_2020_inspections.zip",
        "THETIS_PSC_2021_inspections.zip",
    ]

    # 1) Download if missing
    print("\n[STEP 1] Ensure files exist locally...")
    local_zips = []
    for fn in target_files:
        p = client.download_file(fn, raw_dir)
        local_zips.append(p)
        print("OK:", p.name)

    # 2) Unzip and locate XML
    print("\n[STEP 2] Unzip and find XML...")
    xmls: List[Path] = []
    for zp in local_zips:
        out_folder = extracted_dir / zp.stem
        unzip_to_folder(zp, out_folder)
        found = find_xml_files(out_folder)
        print(f"{zp.name} -> XML files: {len(found)}")
        for x in found:
            print("  -", x)
        xmls.extend(found)

    if not xmls:
        raise RuntimeError("No XML files found after unzip.")

    # 3) Parse and aggregate
    print("\n[STEP 3] Parse XML(s) and build counts...")
    global_pairs: Dict[Tuple[str, str], int] = defaultdict(int)
    global_shiptypes = Counter()
    global_defcodes = Counter()
    total_inspections = 0
    total_inspections_with_def = 0

    for x in xmls:
        pairs, ship_cnt, def_cnt, ins_total, ins_with_def = parse_xml_build_counts(x)
        for k, v in pairs.items():
            global_pairs[k] += v
        global_shiptypes.update(ship_cnt)
        global_defcodes.update(def_cnt)
        total_inspections += ins_total
        total_inspections_with_def += ins_with_def

        print(f"Processed {x.name}: inspections={ins_total}, with_def={ins_with_def}, ship_types={len(ship_cnt)}, def_codes={len(def_cnt)}, pairs={len(pairs)}")

    print("\n[RESULTS]")
    print("Total inspections parsed:", total_inspections)
    print("Inspections with deficiencies:", total_inspections_with_def)
    print("Unique ship types detected:", len(global_shiptypes))
    print("Unique deficiency codes detected:", len(global_defcodes))
    print("Unique (ship_type, deficiency_code) pairs:", len(global_pairs))

    print("\nTop 15 ship types:")
    for st, c in global_shiptypes.most_common(15):
        print(f"  {st}: {c}")

    print("\nTop 15 deficiency codes:")
    for dc, c in global_defcodes.most_common(15):
        print(f"  {dc}: {c}")

    sparse_csv = outputs_dir / "shiptype_defcode_counts_2018_2021.csv"
    save_sparse_csv(global_pairs, sparse_csv)
    print("\nSaved sparse CSV:", sparse_csv)

    pivot_csv = outputs_dir / "matrix_shiptype_x_defcode_2018_2021.csv"
    save_pivot_csv(global_pairs, pivot_csv)


if __name__ == "__main__":
    main()
